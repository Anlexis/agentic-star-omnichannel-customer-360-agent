"""AgentCore Platform v1.0"""

# RET-C2-123 — UnifyProfileNode.
#
# Aggregation step 3: assemble the unified Customer 360 profile from the
# normalized channel records and the resolved cross-channel identity. Produces
# channel coverage, behavioural metrics and coarse segments.
#
# Terminal step of the inner graph. Its output is shaped into the inner
# get_output() and mapped to the outer "result" by the main-slot node. It emits
# a terminal success status so the outer backbone routes main -> post_process
# (the output gate) rather than short-circuiting to finalize.
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
_DEFAULT_HIGH_FREQUENCY_EVENTS = 10


def _derive_segments(channels: List[str], total_events: int, high_frequency_events: int) -> List[str]:
    """Derive coarse segments from channel coverage and activity."""
    segments: List[str] = []
    covered = set(channels)
    if {"pos", "ec"} <= covered:
        segments.append("omnichannel_shopper")
    elif "ec" in covered:
        segments.append("online_only")
    elif "pos" in covered:
        segments.append("instore_only")
    if "loyalty" in covered:
        segments.append("loyalty_member")
    if total_events >= high_frequency_events:
        segments.append("high_frequency")
    elif total_events == 0:
        segments.append("dormant")
    return segments or ["unclassified"]


class UnifyProfileNode(FunctionNode):
    """Assemble the unified Customer 360 profile.

    Input state keys:
        normalized_sources: JSON string — unified per-channel records
        resolved_identity:  JSON string — cross-channel linkage result
        aggregation_config: JSON string — live tuning (high_frequency_events)

    Output state keys (partial dict):
        customer_360_profile: JSON string — {"unified_customer_id": str,
                              "channels": [...], "segments": [...],
                              "metrics": {...}, "attributes": {...},
                              "confidence": float, "match_method": str}
        status:               AgentStatus.SUCCESS — the terminal signal the
                              outer backbone routes on. Without it the run
                              short-circuits to finalize and skips the gate.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        if state.get("status") == AgentStatus.ERROR.value:
            # An earlier step failed, so the inputs below are empty. Emitting a
            # success status here would overwrite the failure and hand the
            # caller an empty profile presented as a real answer.
            return {}

        normalized: Dict[str, Any] = from_json(state.get("normalized_sources"), {}) or {}
        identity: Dict[str, Any] = from_json(state.get("resolved_identity"), {}) or {}
        tuning: Dict[str, Any] = from_json(state.get("aggregation_config"), {}) or {}

        high_frequency_events = tuning.get("high_frequency_events")
        if (
            not isinstance(high_frequency_events, int)
            or isinstance(high_frequency_events, bool)
            or high_frequency_events < 0
        ):
            high_frequency_events = _DEFAULT_HIGH_FREQUENCY_EVENTS

        records: List[Dict[str, Any]] = normalized.get("records") or []
        channels: List[str] = normalized.get("channels") or []

        total_events = 0
        merged_attributes: Dict[str, Any] = {}
        for record in records:
            events = record.get("events") or []
            total_events += len(events)
            for key, value in (record.get("attributes") or {}).items():
                # First writer wins across channels for scalar attributes, so a
                # later channel cannot silently overwrite an earlier value.
                merged_attributes.setdefault(key, value)

        metrics: Dict[str, Any] = {
            "total_events": total_events,
            "channel_count": len(channels),
            "record_count": len(records),
            "linked_record_count": len(identity.get("linked_records") or []),
        }

        segments = _derive_segments(channels, total_events, high_frequency_events)

        profile: Dict[str, Any] = {
            "unified_customer_id": identity.get("unified_customer_id", ""),
            "channels": channels,
            "segments": segments,
            "metrics": metrics,
            "attributes": merged_attributes,
            "confidence": identity.get("confidence", 0.0),
            "match_method": identity.get("match_method", "unlinked"),
            "truncated": bool(normalized.get("truncated", False)),
        }

        emit_trace_event(
            "customer_360_unified",
            {
                "channel_count": len(channels),
                "segments": segments,
                "total_events": total_events,
            },
            state,
        )

        logger.info(
            "UnifyProfileNode: %d channels, %d events, segments=%s",
            len(channels),
            total_events,
            segments,
        )

        return {
            "customer_360_profile": to_json(profile),
            "status": AgentStatus.SUCCESS.value,
        }
