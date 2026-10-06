"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never a Pydantic model. Checkpoints are
# serialized with msgpack, and model objects corrupt silently there. Extend the
# framework state with agent-specific fields only; never add credentials,
# secrets, or model objects.
#
# Structured fields (mappings, lists of mappings) are stored as JSON STRINGS,
# not bare containers, for the same reason. Producers serialize with to_json()
# on write; consumers deserialize with from_json() on read.
#
# RET-C2-123 — retail omnichannel Customer 360 aggregation. Two layers: the
# outer backbone and the inner aggregation workflow. The fields below cover
# both.
#
# Personal-data note: this agent ingests POS, EC, CRM and loyalty-card records.
# Direct resident identifiers (names, addresses, phone numbers, e-mail
# addresses, card numbers, resident numbers) never reach state: the request
# boundary replaces every identity value with a non-reversible linkage token
# and refuses any identifier shape in a field that would be rendered. Nothing
# downstream sees a raw identifier, and none is written to the checkpoint.

import json
from typing import Any, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a structured state field to a JSON string.

    None passes through unchanged so an unset field stays distinguishable from
    an empty container.
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON-string state field back to its container.

    None, empty, or malformed input falls back to the supplied ``default`` so a
    missing or corrupt field is non-fatal for the consuming node.
    """
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for RET-C2-123.

    Shared fields (user_input, status, session_id, node_history, error_log, and
    the human-review fields) are inherited from the framework state.
    """

    # ------------------------------------------------------------------
    # Outer layer — written by the request boundary and the main slot
    # ------------------------------------------------------------------

    # The screened request string. Direct-identifier shapes have been rewritten
    # out of it; the raw request is not persisted past the boundary.
    validated_input: Optional[str]

    # JSON STRING — the validated caller contract: bounded, inert channel
    # records with identity values already replaced by linkage tokens, plus the
    # channel filter. This is the only representation of caller data that
    # travels; nothing downstream re-parses the raw request.
    # {"request_text": str, "records": [...], "channel_filter": [str],
    #  "record_source": str}
    caller_contract: Optional[str]

    # ------------------------------------------------------------------
    # Inner layer — the aggregation workflow
    # ------------------------------------------------------------------

    # JSON STRING — the live aggregation tuning, seeded into inner state by the
    # inner graph's initial-state hook. Node execute() methods take no config
    # argument, so this is how a declared runtime value reaches a domain node.
    # {"max_records": int, "high_frequency_events": int,
    #  "min_link_confidence": float}
    aggregation_config: Optional[str]

    # JSON STRING — the unified per-channel records.
    # {"records": [{"channel": str, "external_id": str, "events": [str],
    #               "attributes": {...}, "<identity>_token": str}, ...],
    #  "channels": [str], "record_count": int, "truncated": bool}
    normalized_sources: Optional[str]

    # JSON STRING — the cross-channel linkage result. No raw identifier; the
    # match runs on linkage tokens only.
    # {"unified_customer_id": str, "linked_records": [...],
    #  "match_method": str, "confidence": float, "min_link_confidence": float}
    resolved_identity: Optional[str]

    # JSON STRING — the unified Customer 360 profile.
    # {"unified_customer_id": str, "channels": [str], "segments": [str],
    #  "metrics": {...}, "attributes": {...}, "confidence": float,
    #  "match_method": str, "truncated": bool}
    # Surfaced to the outer graph (mapped to "result") by merge_output().
    customer_360_profile: Optional[str]

    # ------------------------------------------------------------------
    # Tracing — framework-managed; not written by node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
