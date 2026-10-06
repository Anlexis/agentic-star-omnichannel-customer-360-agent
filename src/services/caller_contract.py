"""AgentCore Platform v1.0"""

# Caller-request contract for the omnichannel Customer 360 aggregation agent.
#
# One place validates everything a caller can send, so there is exactly one
# answer to "what is accepted?" — the pre_process node calls into here and no
# node downstream re-parses raw request data.
#
# Two request channels reach this module:
#   * the free-text request string (the `input` field), which may also carry the
#     channel records as a JSON envelope for callers that have only one field;
#   * `input_context`, the structured invocation parameter, which is where the
#     channel records belong. The platform rewrites personal-data shapes out of
#     the free-text field at every node boundary, and its name heuristic reads
#     title-case retail proper nouns — store names, product names, brands — as
#     personal names, so store="Shibuya Flagship" arrives masked on that channel
#     and intact on this one. Records sent as structured parameters keep their
#     retail attributes; records embedded in the free-text string do not.
#
# Rules that hold for every field:
#   * numbers go through a finite + bounded parser. NaN and the infinities
#     survive float() and compare False against every bound, so an unchecked
#     non-finite value silently passes the check it was meant to fail;
#   * every string that renders into the released profile is restricted to an
#     inert alphabet — caller free text there is output injection;
#   * identity values (e-mail, phone, card, resident number) are accepted but
#     never rendered: they are converted to non-reversible linkage tokens and
#     the raw value is dropped at this boundary;
#   * a value that fails any check REFUSES the request, naming the field but
#     never repeating the value;
#   * absent data is not an error — the pipeline then reports an empty profile
#     rather than inventing one.

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from framework.security.credential_detector import detect_credentials

# ── Bounds ────────────────────────────────────────────────────────────────────
MAX_REQUEST_CHARS = 8000
MAX_SOURCES = 50
MAX_EVENTS_PER_SOURCE = 200
MAX_ATTRIBUTES_PER_SOURCE = 32
MAX_IDENTITY_VALUE_CHARS = 256
MAX_CONTEXT_DEPTH = 6

# Numeric attributes are aggregated into the profile, so they carry a finite,
# bounded range rather than accepting whatever float() will parse.
NUMERIC_MIN = -1e12
NUMERIC_MAX = 1e12

# ── Channels ──────────────────────────────────────────────────────────────────
# A closed set: an unrecognised channel is refused rather than silently bucketed
# as "unknown", because channel coverage is what the segments are derived from.
KNOWN_CHANNELS: Tuple[str, ...] = ("pos", "ec", "crm", "loyalty")

# ── Inert alphabets ───────────────────────────────────────────────────────────
# Attribute keys become keys of the released profile; attribute values, event
# labels and record identifiers become its values. All of them are restricted
# rather than escaped, so no caller string can open new structure in the
# rendered profile.
_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# Field names are caller-controlled too. One is repeated back in a refusal only
# when it is short and inert.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# Zero-width and bidi controls: invisible in a rendered profile, so they can
# hide a directive from a human reviewer while a model still reads it.
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
# Markup is stripped before the second screening pass, so a directive spliced
# with tags ("ig<b>nore</b> all previous instructions") is caught once the tags
# are gone — and control tokens are caught on the first pass, before the strip
# could remove them.
_MARKUP_RE = re.compile(r"<[^>]{0,64}>")

# ── Identity fields ───────────────────────────────────────────────────────────
# Values on these keys are hashed into linkage tokens and never rendered. They
# are the only place a caller may put free text, and it never survives this
# boundary.
IDENTITY_KEYS: Tuple[str, ...] = (
    "my_number",
    "mynumber",
    "individual_number",
    "resident_id",
    "name",
    "full_name",
    "customer_name",
    "kana",
    "name_kana",
    "address",
    "home_address",
    "postal_code",
    "zip",
    "phone",
    "phone_number",
    "tel",
    "mobile",
    "email",
    "e_mail",
    "mail",
    "loyalty_card",
    "loyalty_card_number",
    "card_number",
    "pan",
    "membership_id",
    "date_of_birth",
    "dob",
    "birthday",
)

# Keys handled structurally rather than as attributes.
_STRUCTURAL_KEYS: Tuple[str, ...] = ("channel", "external_id", "id", "events", "attributes")

# ── Personal-data shapes ──────────────────────────────────────────────────────
# ONE definition, used by both directions of the personal-data guarantee: the
# inbound refusal that keeps these shapes out of rendered fields, and the
# outbound gate that refuses to release anything still matching them. Two lists
# would drift, and the drift would always favour the leak.
#
# The dialling forms are separate patterns on purpose. A single "digits and
# separators" pattern either misses the international form (no leading zero) or
# matches ordinary figures; splitting them keeps each anchored to a real shape.
PERSONAL_DATA_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("email_address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("long_id_number", re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{2,11}\b")),
    ("international_phone_number", re.compile(r"\+\d{1,3}[-\s]\d{1,4}[-\s]\d{2,4}[-\s]\d{3,4}\b")),
    ("phone_number", re.compile(r"\b0\d{1,4}[-\s]?\d{1,4}[-\s]?\d{3,4}\b")),
)
REDACTION_STUB = "[REDACTED]"


def strip_direct_identifiers(text: str) -> str:
    """Replace direct-identifier shapes in free text with a fixed stub."""
    for _name, pattern in PERSONAL_DATA_PATTERNS:
        text = pattern.sub(REDACTION_STUB, text)
    return text


def find_personal_data(text: str) -> Optional[str]:
    """Name the first personal-data shape present in a string, or None."""
    for name, pattern in PERSONAL_DATA_PATTERNS:
        if pattern.search(text):
            return name
    return None


# ── Disallowed-instruction screen ─────────────────────────────────────────────
# Chat-template control tokens are screened as a CLASS, not as a list of the
# ones seen so far. They are how a payload forges a turn boundary, and they
# carry no meaning in a retail record, so matching them cannot block real work.
# The platform's own screen scores <|im_start|> and [INST] but not <<SYS>> or
# <|system|>, so the class is covered here rather than assumed.
_CONTROL_TOKEN_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("chat_template_token", re.compile(r"<\|[^<>|]{0,64}\|>")),
    ("instruction_token", re.compile(r"\[/?INST\]", re.IGNORECASE)),
    ("system_token", re.compile(r"<</?SYS>>", re.IGNORECASE)),
    ("role_tag", re.compile(r"<\s*/?(?:system|assistant|user)\s*>", re.IGNORECASE)),
)

# Instruction-shaped phrases. Every pattern requires a verb AND its object, so
# the surrounding text has to actually be an instruction: a record whose notes
# mention rules, prompts or an assistant does not match.
_INSTRUCTION_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\b[\s\S]{0,40}?"
            r"\b(?:previous|prior|earlier|above|all)\b[\s\S]{0,20}?"
            r"\b(?:instruction|instructions|rule|rules|prompt|prompts|direction|directions)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt_disclosure",
        re.compile(
            r"\b(?:reveal|show|print|repeat|output|dump)\b[\s\S]{0,30}?"
            r"\b(?:your|the)\b[\s\S]{0,20}?\b(?:system\s+prompt|instructions|rules)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role_reassignment",
        re.compile(
            r"\byou\s+are\s+(?:now|from\s+now\s+on)\b[\s\S]{0,40}?" r"\b(?:ai|assistant|model|dan|developer\s+mode)\b",
            re.IGNORECASE,
        ),
    ),
)


def screen_disallowed_instructions(text: str) -> Optional[str]:
    """Name the first disallowed-instruction shape in a string, or None.

    Screened twice: once on the text as received, so control tokens are seen
    before any strip could remove them, and once with invisible characters and
    markup removed, so a directive spliced with tags is seen after the text
    re-assembles. A screen that only ran after the strip would silently convert
    a detectable token attack into undetectable plain text.
    """
    for name, pattern in _CONTROL_TOKEN_PATTERNS:
        if pattern.search(text):
            return name
    stripped = _MARKUP_RE.sub("", _INVISIBLE_RE.sub("", text))
    for candidate in (text, stripped):
        for name, pattern in _INSTRUCTION_PATTERNS:
            if pattern.search(candidate):
                return name
    return None


def _walk_strings(value: Any, depth: int = 0) -> List[str]:
    """Collect every string leaf AND every mapping key, depth-first.

    Keys are collected because a hostile field NAME is caller data on the same
    footing as its value, and a screen that read values only would pass it.
    """
    if depth > MAX_CONTEXT_DEPTH:
        return []
    if isinstance(value, str):
        return [value]
    found: List[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                found.append(key)
            found.extend(_walk_strings(item, depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_walk_strings(item, depth + 1))
    return found


def screen_structure(value: Any) -> Optional[str]:
    """Screen a parsed structure depth-first, keys included.

    Runs after parsing, so a payload that escaped its control tokens as \\u
    sequences is screened in its decoded form.
    """
    for text in _walk_strings(value):
        hit = screen_disallowed_instructions(text)
        if hit:
            return hit
    return None


# ── Validation error ──────────────────────────────────────────────────────────


class ContractError(ValueError):
    """A caller value failed its check. Carries the field, never the value."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def safe_field_reference(name: object, index: int) -> str:
    """Render a caller-supplied field name safe to put in a message."""
    if isinstance(name, str) and _SAFE_NAME_RE.match(name):
        return name
    return f"field #{index}"


# ── Scalar parsers ────────────────────────────────────────────────────────────


def finite_in_range(value: Any, field: str, low: float = NUMERIC_MIN, high: float = NUMERIC_MAX) -> float:
    """Parse a caller number, or refuse.

    Rejects booleans (isinstance(True, int) is True in Python, so an unchecked
    parser reads `true` as 1), non-numeric strings, NaN and both infinities, and
    magnitudes outside the declared range. NaN is the one that matters most:
    it parses through float() and then compares False against every bound, so a
    parser that only range-checked would let it through as "not out of range".
    """
    if isinstance(value, bool):
        raise ContractError(field, "must be a number, not a boolean")
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value.strip())
        except (TypeError, ValueError):
            raise ContractError(field, "must be a number") from None
    else:
        raise ContractError(field, "must be a number")
    if not math.isfinite(parsed):
        raise ContractError(field, "must be a finite number")
    if not (low <= parsed <= high):
        raise ContractError(field, f"must be between {low:g} and {high:g}")
    return parsed


def parses_as_number(value: str) -> bool:
    """True when float() would accept this string.

    Used to route a numeric-looking attribute through the finite parser instead
    of the inert-code check. Without it "NaN", "Infinity" and "1e400" all match
    the inert alphabet and ride into the profile as text — the aggregate then
    compares False against every bound downstream, which is the silent
    fail-open the finite parser exists to prevent.
    """
    try:
        float(value.strip())
    except (TypeError, ValueError):
        return False
    return True


def _inert_code(value: Any, field: str) -> str:
    """Accept a value that will be rendered into the profile, or refuse."""
    if not isinstance(value, str):
        raise ContractError(field, "must be a string")
    text = value.strip()
    if not _CODE_RE.match(text):
        raise ContractError(
            field,
            "must be 1-64 characters of letters, digits, underscore, dot or hyphen",
        )
    hit = find_personal_data(text)
    if hit:
        raise ContractError(field, f"looks like personal data ({hit}) and cannot be rendered")
    return text


def _inert_key(value: Any, index: int) -> str:
    """Accept an attribute key, or refuse."""
    field = f"attributes.{safe_field_reference(value, index)}"
    if not isinstance(value, str):
        raise ContractError(field, "attribute keys must be strings")
    key = value.strip().lower()
    if not _KEY_RE.match(key):
        raise ContractError(field, "attribute keys must be 1-32 characters of lowercase letters, digits or underscore")
    return key


# ── Linkage tokens ────────────────────────────────────────────────────────────
# A stable, non-reversible token stands in for every identity value, so records
# for one physical customer can be matched on equality without the raw
# identifier ever being stored or rendered.
#
# The digest is taken with a named hash, not Python's built-in hash(): the
# built-in is salted per process, so the same customer would receive a
# different unified id after every restart and cross-run linkage would silently
# stop working while every test inside one process still passed.
#
# The token is derived from the identity FIELD and its VALUE, and deliberately
# NOT from the channel the record arrived on. Cross-channel matching is the
# whole point of the agent: a channel-scoped token makes the same e-mail
# address on a store record and a web record hash to two different values, so
# no multi-channel request can ever link and every profile reports itself as
# unlinked. Privacy is carried by the digest being non-reversible, not by the
# channel being mixed into it — mixing it in bought nothing and cost the
# capability.
_TOKEN_DIGEST_CHARS = 16


def linkage_token(key: str, value: Any) -> str:
    """Return the non-reversible linkage token for one identity value."""
    material = f"{key}\x1f{value}".encode("utf-8", "replace")
    return f"tok_{key}_{hashlib.blake2b(material, digest_size=16).hexdigest()[:_TOKEN_DIGEST_CHARS]}"


def unified_customer_id(seed_tokens: Sequence[str]) -> str:
    """Derive the reproducible unified customer id from the linked token set."""
    material = "\x1f".join(sorted(seed_tokens)).encode("utf-8", "replace")
    return f"cust_{hashlib.blake2b(material, digest_size=16).hexdigest()[:16]}"


# ── Request validation ────────────────────────────────────────────────────────


def _validate_record(raw: Any, index: int) -> Dict[str, Any]:
    """Validate one channel record into its normalized, renderable form."""
    where = f"sources[{index}]"
    if not isinstance(raw, Mapping):
        raise ContractError(where, "must be an object")

    channel = raw.get("channel")
    if not isinstance(channel, str) or channel.strip().lower() not in KNOWN_CHANNELS:
        raise ContractError(f"{where}.channel", f"must be one of {', '.join(KNOWN_CHANNELS)}")
    channel = channel.strip().lower()

    external_raw = raw.get("external_id", raw.get("id", ""))
    external_id = _inert_code(external_raw, f"{where}.external_id") if external_raw else ""

    events: List[str] = []
    raw_events = raw.get("events", raw.get("transactions", raw.get("history", [])))
    if raw_events:
        if not isinstance(raw_events, (list, tuple)):
            raise ContractError(f"{where}.events", "must be a list")
        if len(raw_events) > MAX_EVENTS_PER_SOURCE:
            raise ContractError(f"{where}.events", f"must hold at most {MAX_EVENTS_PER_SOURCE} entries")
        for position, event in enumerate(raw_events):
            events.append(_inert_code(event, f"{where}.events[{position}]"))

    identity_tokens: Dict[str, str] = {}
    attributes: Dict[str, Any] = {}

    declared_attributes = raw.get("attributes")
    if declared_attributes is not None and not isinstance(declared_attributes, Mapping):
        raise ContractError(f"{where}.attributes", "must be an object")

    # Flat records (attributes alongside the structural keys) and nested records
    # (attributes under an "attributes" object) are both accepted; they are
    # merged here so the rest of the pipeline sees one shape.
    flat_items = [
        (key, value)
        for key, value in raw.items()
        if isinstance(key, str)
        and key.strip().lower() not in _STRUCTURAL_KEYS
        and key.strip().lower() not in ("transactions", "history")
    ]
    nested_items = list(declared_attributes.items()) if isinstance(declared_attributes, Mapping) else []

    for position, (key, value) in enumerate(flat_items + nested_items, start=1):
        key_l = key.strip().lower() if isinstance(key, str) else key
        if isinstance(key_l, str) and key_l in IDENTITY_KEYS:
            if value in (None, ""):
                continue
            text = str(value)
            if len(text) > MAX_IDENTITY_VALUE_CHARS:
                raise ContractError(f"{where}.{key_l}", f"must be at most {MAX_IDENTITY_VALUE_CHARS} characters")
            identity_tokens[f"{key_l}_token"] = linkage_token(key_l, text)
            continue
        attribute_key = _inert_key(key, position)
        if len(attributes) >= MAX_ATTRIBUTES_PER_SOURCE:
            raise ContractError(f"{where}.attributes", f"must hold at most {MAX_ATTRIBUTES_PER_SOURCE} entries")
        attribute_field = f"{where}.attributes.{attribute_key}"
        if isinstance(value, bool):
            attributes[attribute_key] = value
        elif isinstance(value, (int, float)):
            attributes[attribute_key] = finite_in_range(value, attribute_field)
        elif isinstance(value, str) and parses_as_number(value):
            # A numeric-looking string goes through the finite parser too. "NaN"
            # and "Infinity" match the inert alphabet, so a route that only sent
            # real int/float values through the parser would admit exactly the
            # values it exists to reject.
            attributes[attribute_key] = finite_in_range(value, attribute_field)
        else:
            attributes[attribute_key] = _inert_code(value, attribute_field)

    record: Dict[str, Any] = {
        "channel": channel,
        "external_id": external_id,
        "events": events,
        "attributes": attributes,
    }
    record.update(identity_tokens)
    return record


def _coerce_sources(payload: Any) -> List[Any]:
    """Pull the record list out of either accepted payload shape."""
    if isinstance(payload, Mapping):
        declared = payload.get("sources")
        if isinstance(declared, (list, tuple)):
            return list(declared)
        # Per-channel arrays: {"pos": [...], "ec": [...]}.
        flat: List[Any] = []
        for channel in KNOWN_CHANNELS:
            records = payload.get(channel)
            if isinstance(records, (list, tuple)):
                for record in records:
                    if isinstance(record, Mapping):
                        merged = dict(record)
                        merged.setdefault("channel", channel)
                        flat.append(merged)
                    else:
                        flat.append(record)
        return flat
    if isinstance(payload, (list, tuple)):
        return list(payload)
    return []


def validate_request(user_input: Any, input_context: Any = None) -> Dict[str, Any]:
    """Validate a whole request and return the contract the pipeline runs on.

    Raises ContractError naming the offending field. The caller's raw values
    never appear in the message and never leave this function except as
    validated, inert, or tokenised forms.
    """
    if not isinstance(user_input, str) or not user_input.strip():
        raise ContractError("input", "must be a non-empty string")
    request_text = user_input.strip()
    if len(request_text) > MAX_REQUEST_CHARS:
        raise ContractError("input", f"must be at most {MAX_REQUEST_CHARS} characters")

    hit = screen_disallowed_instructions(request_text)
    if hit:
        raise ContractError("input", f"contains a disallowed instruction pattern ({hit})")

    # A credential-shaped value in the request string cannot succeed either way:
    # the platform's output gate scans every value of every node result, and the
    # screened request is one of them, so the run fails at the request boundary
    # with a traceback and nothing naming the cause. Refusing it here changes
    # nothing about what is accepted and gives the caller something to act on.
    # The SAME detector the platform's gate calls is used, so what this refuses
    # and what that blocks are one set by construction.
    if detect_credentials(request_text):
        raise ContractError("input", "contains a credential-shaped value")

    context: Dict[str, Any] = dict(input_context) if isinstance(input_context, Mapping) else {}
    hit = screen_structure(context)
    if hit:
        raise ContractError("input_context", f"contains a disallowed instruction pattern ({hit})")

    # The structured channel is authoritative. A caller with a single field may
    # still send the records as a JSON envelope in the request string; that path
    # is screened identically, but the platform masks retail proper nouns there,
    # so it degrades rather than failing.
    raw_sources: List[Any] = _coerce_sources(context)
    embedded_request = False
    if not raw_sources:
        try:
            parsed = json.loads(request_text)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if parsed is not None:
            hit = screen_structure(parsed)
            if hit:
                raise ContractError("input", f"contains a disallowed instruction pattern ({hit})")
            raw_sources = _coerce_sources(parsed)
            embedded_request = bool(raw_sources)

    if len(raw_sources) > MAX_SOURCES:
        raise ContractError("sources", f"must hold at most {MAX_SOURCES} records")

    records = [_validate_record(raw, index) for index, raw in enumerate(raw_sources)]

    channel_filter: List[str] = []
    declared_filter = context.get("channel_filter")
    if declared_filter is not None:
        if not isinstance(declared_filter, (list, tuple)):
            raise ContractError("input_context.channel_filter", "must be a list of channel names")
        for position, channel in enumerate(declared_filter):
            if not isinstance(channel, str) or channel.strip().lower() not in KNOWN_CHANNELS:
                raise ContractError(
                    f"input_context.channel_filter[{position}]",
                    f"must be one of {', '.join(KNOWN_CHANNELS)}",
                )
            channel_filter.append(channel.strip().lower())

    if channel_filter:
        records = [record for record in records if record["channel"] in channel_filter]

    return {
        "request_text": strip_direct_identifiers(request_text) if embedded_request is False else "",
        "records": records,
        "channel_filter": channel_filter,
        "record_source": "input_context" if not embedded_request else "input",
    }
