"""AgentCore Platform v1.0"""

# RET-C2-123 — PreProcessNode (outer pre_process slot).
#
# The request boundary. Everything a caller can send is validated here, once,
# against the contract in src/services/caller_contract.py:
#   * the request string is length-capped and screened for disallowed
#     instructions, including chat-template control tokens;
#   * the structured invocation parameters are screened depth-first, keys
#     included, after parsing — so a payload that escaped its control tokens
#     as \u sequences is screened in its decoded form;
#   * every channel record is bounded, its rendered fields restricted to inert
#     alphabets, its numbers parsed by a finite + bounded parser, and its
#     identity values replaced by non-reversible linkage tokens;
#   * a refusal names the field and never repeats the value.
#
# The refusal is enforced HERE, in the node that owns the caller contract,
# rather than being left to the platform's own input screen. That screen scores
# some control-token forms and not others, and where it is absent or configured
# off the payload would reach the aggregation path and return success. This
# node refuses on its own, which is why the boundary tests call execute()
# directly with no wrapper in front of it.
#
# Node contract:
#  - extend FunctionNode; implement execute(state) -> dict
#  - return ONLY the fields this node changes (never the full state)
#  - return AgentStatus enum values — never plain strings
#  - read input_context via state.get("input_context", {}) — read-only

from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import to_json
from src.services.caller_contract import (
    ContractError,
    strip_direct_identifiers,
    validate_request,
)


class PreProcessNode(FunctionNode):
    """Validate the caller request and produce the contract the pipeline runs on.

    Input state keys:
        user_input:    the request string
        input_context: structured invocation parameters (read-only)

    Output state keys (partial dict):
        validated_input:  the screened request string
        caller_contract:  JSON string — the validated records and filters
        status:           AgentStatus.SUCCESS, or ERROR on a refusal
        error_log:        (on refusal) one message naming the offending field
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only

        try:
            contract = validate_request(user_input, input_context)
        except ContractError as refusal:
            # The field is named so the caller can act; the value never appears.
            #
            # The refusal is also written to formatted_output, because the
            # envelope surfaces that field and nothing else: the error log is
            # not part of it, so a refusal that lived only there would reach the
            # caller as an error with no reason at all. What travels is the
            # field path and a fixed reason phrase — both are the contract's own
            # vocabulary, never a caller value.
            emit_trace_event(
                "request_refused",
                {"field": refusal.field, "reason": refusal.reason},
                state,
            )
            message = f"Request refused — {refusal.field}: {refusal.reason}"
            return {
                "status": AgentStatus.ERROR.value,
                "formatted_output": message,
                "result": None,
                "error_log": [message],
            }

        record_count = len(contract["records"])
        channels = sorted({record["channel"] for record in contract["records"]})

        emit_trace_event(
            "aggregation_request_accepted",
            {
                "record_count": record_count,
                "channels": channels,
                "record_source": contract["record_source"],
            },
            state,
        )

        return {
            # Structured values are stored as JSON strings: checkpoint
            # serialization does not carry bare containers safely.
            "validated_input": strip_direct_identifiers(str(user_input).strip()),
            "caller_contract": to_json(contract),
            "status": AgentStatus.SUCCESS.value,
        }
