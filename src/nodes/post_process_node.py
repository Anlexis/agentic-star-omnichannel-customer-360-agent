"""AgentCore Platform v1.0"""

# RET-C2-123 — PostProcessNode: the output boundary of the agent.
#
# The stated output invariant of this template is: no direct resident
# identifier and nothing credential-shaped leaves the agent. This node enforces
# it on the whole released surface — the rendered profile AND the structured
# fields behind it — with the module-level scan below, called from execute().
#
# The scan is RECURSIVE. Caller-derived text rides inside nested mappings, and
# a gate that looked only at top-level strings would report zero findings on a
# payload whose leak sits one level down.
#
# The credential half DELEGATES to the platform's own detector rather than
# keeping a private pattern list. That is not a style choice. The platform
# scans every value of every node result for credentials and RAISES when it
# finds one; the wrapper then discards this node's whole return value, the
# clearing included, and the envelope falls back to the un-gated profile still
# sitting in state. So a local list narrower than the platform's is not a
# weaker gate — it is a containment bypass. Measured on the shipped code: an
# AWS access-key id passed the old local list, the platform raised, and the
# envelope surfaced the profile with the key in it.
#
# Two independent layers, each with its own audit event:
#   * inbound — the request boundary refuses personal-data shapes in every
#     rendered field and tokenises identity values (src/services/caller_contract.py);
#   * outbound — this gate refuses to release anything still matching. Both
#     directions read ONE pattern definition, so they cannot drift apart.
#
# Containment on violation: returning an error is not enough on its own. The
# framework's envelope resolves the output as `formatted_output or result` with
# no status check, so a gate that raised — or that set an error status without
# clearing — still ships the un-gated profile inside the error envelope. This
# node therefore CLEARS every output-bearing field as it blocks, and replaces
# formatted_output with a TRUTHY notice: a falsy replacement re-opens the very
# fallback the clearing exists to close.
#
# No _extra_security_gate_input/_output instance methods are defined here: the
# framework auto-wraps such hooks, and defining them would change the node's
# call pipeline.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json
from src.services.caller_contract import PERSONAL_DATA_PATTERNS

logger = logging.getLogger(__name__)

# Credential shapes the template refuses on its own account, on top of whatever
# the platform detector finds. These cover assignment forms the platform's
# value-shape patterns do not, so the union is a superset of the platform's set
# in both directions: nothing the platform catches gets past this node, and the
# template still refuses in a deployment where that scan is absent.
_EXTRA_DISALLOWED_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    (
        "credential_assignment",
        re.compile(
            r"\b(?:password|passwd|secret|api_key|token|access_key|private_key)\s*[:=]\s*\S{8,}",
            re.IGNORECASE,
        ),
    ),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----")),
]

# The replacement is TRUTHY on purpose. The framework's envelope falls back to
# state["result"] whenever formatted_output is falsy, so an empty string here
# would re-open the exact channel the clearing closes.
BLOCKED_NOTICE = (
    "[OUTPUT WITHHELD — the assembled profile did not pass the output boundary. "
    "Resend the records without credential-like or direct-identifier strings.]"
)

# Reasons are drawn from a closed set: the label of the pattern that matched.
# The matched text is never part of the message, in the response or in the
# audit record — a violation report that quoted the value would be the leak.

# Every state field AT THIS LEVEL that can carry released text. On a violation
# each one is overwritten, so no path out of the graph — including the
# framework's own fallback to state["result"] — can reach the un-gated profile,
# and a checkpoint or a downstream reader cannot pick it up either.
#
# The list is deliberately confined to the keys the outer state actually holds.
# `normalized_sources` and `resolved_identity` are inner-graph keys: the main
# slot's merge_output() publishes only the profile, so reading or clearing them
# here would compare against nothing on every real invocation — a layer that
# looks healthy, passes a hand-built fixture, and can never fire.
OUTPUT_BEARING_FIELDS: Tuple[str, ...] = (
    "result",
    "formatted_output",
    "customer_360_profile",
)


def security_gate_output(content: Any) -> Optional[str]:
    """Scan released content and name the first violation, or None if clean.

    Walks nested mappings and sequences, scanning every leaf. Returns the NAME
    of the matched pattern — never the matched text, which would put the leak
    into the record that reports it.
    """
    if content is None:
        return None
    if isinstance(content, dict):
        for value in content.values():
            hit = security_gate_output(value)
            if hit:
                return hit
        return None
    if isinstance(content, (list, tuple)):
        for item in content:
            hit = security_gate_output(item)
            if hit:
                return hit
        return None
    text = str(content)
    findings = detect_credentials(text)
    if findings:
        return str(findings[0]["type"])
    for name, pattern in _EXTRA_DISALLOWED_PATTERNS:
        if pattern.search(text):
            return name
    for name, pattern in PERSONAL_DATA_PATTERNS:
        if pattern.search(text):
            return name
    return None


class PostProcessNode(FunctionNode):
    """Output gate: refuse to release credential-like or identifier-like content.

    Input state keys (both written into outer state by the main slot's
    merge_output(), so both are present here on every real invocation):
        result:               the assembled profile the envelope surfaces
        customer_360_profile: the same profile as the inner graph wrote it

    Output state keys (partial dict):
        formatted_output: the profile when clean; a truthy withheld notice on a
                          violation — never an empty value, which would re-open
                          the envelope's fallback
        result:           gated alongside formatted_output
        status:           AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:        (on a violation) one closed-set reason label
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        result = state.get("result") or ""

        if not str(result).strip():
            # Nothing was assembled. The notice is still truthy: an empty
            # formatted_output would let the envelope fall back to whatever
            # remains in result.
            return {
                "formatted_output": "[NO PROFILE — the request produced no records to aggregate.]",
                "result": None,
                "status": AgentStatus.SUCCESS.value,
            }

        # The structured fields are released alongside the rendered profile, so
        # they are gated with it. Blocking here rather than dropping them later
        # means the caller is told the profile was withheld instead of receiving
        # a success envelope quietly missing half its content.
        released = {
            "result": str(result),
            "customer_360_profile": from_json(state.get("customer_360_profile"), {}),
        }
        violation = security_gate_output(released)
        if violation:
            logger.error("PostProcessNode: output withheld — violation type: %s", violation)
            emit_trace_event("output_withheld", {"violation": violation}, state)
            blocked: Dict[str, Any] = {field: None for field in OUTPUT_BEARING_FIELDS}
            blocked.update(
                {
                    "formatted_output": BLOCKED_NOTICE,
                    "result": BLOCKED_NOTICE,
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"Output withheld at the boundary — {violation}"],
                }
            )
            return blocked

        emit_trace_event(
            "customer_360_profile_released",
            {"profile_chars": len(str(result))},
            state,
        )

        return {
            "formatted_output": result,
            "status": AgentStatus.SUCCESS.value,
        }
