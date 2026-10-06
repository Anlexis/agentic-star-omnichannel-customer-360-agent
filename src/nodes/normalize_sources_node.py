"""AgentCore Platform v1.0"""

# RET-C2-123 — NormalizeSourcesNode.
#
# Aggregation step 1: take the validated channel records the request boundary
# produced and publish them in the unified schema the rest of the pipeline
# reads.
#
# It does not re-parse raw request data. Every record arriving here has already
# passed its bounded, inert shape check and every identity value has already
# been replaced by a non-reversible linkage token, so there is exactly one
# place in the repo that decides what a caller may send.
#
# Wired by the inner aggregation graph. Returns only changed state keys.

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# Used when the runtime tuning is unreadable; mirrors config/config.yaml.
_DEFAULT_MAX_RECORDS = 50


class NormalizeSourcesNode(FunctionNode):
    """Publish the validated per-channel records in the unified schema.

    Input state keys:
        caller_contract:    JSON string — validated records (request boundary)
        aggregation_config: JSON string — live tuning (max_records)

    Output state keys (partial dict):
        normalized_sources: JSON string — {"records": [...], "channels": [...],
                            "record_count": int, "truncated": bool}
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        if state.get("status") == AgentStatus.ERROR.value:
            # An earlier step failed. Publishing an empty record set here would
            # let the run continue and report a profile for data that never
            # arrived, so the failure is carried forward instead.
            return {}

        contract: Dict[str, Any] = from_json(state.get("caller_contract"), {}) or {}
        tuning: Dict[str, Any] = from_json(state.get("aggregation_config"), {}) or {}

        max_records = tuning.get("max_records")
        if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 0:
            max_records = _DEFAULT_MAX_RECORDS

        validated: List[Dict[str, Any]] = list(contract.get("records") or [])
        truncated = len(validated) > max_records
        records = validated[:max_records]

        channels: List[str] = []
        for record in records:
            channel = record.get("channel", "")
            if channel and channel not in channels:
                channels.append(channel)

        normalized_sources: Dict[str, Any] = {
            "records": records,
            "channels": channels,
            "record_count": len(records),
            "truncated": truncated,
        }

        emit_trace_event(
            "sources_normalized",
            {
                "record_count": len(records),
                "channels": channels,
                "truncated": truncated,
            },
            state,
        )

        logger.info(
            "NormalizeSourcesNode: %d records across %d channels (truncated=%s)",
            len(records),
            len(channels),
            truncated,
        )

        return {"normalized_sources": to_json(normalized_sources)}
