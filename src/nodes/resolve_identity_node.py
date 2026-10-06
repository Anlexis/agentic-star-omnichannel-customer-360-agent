"""AgentCore Platform v1.0"""

# RET-C2-123 — ResolveIdentityNode.
#
# Aggregation step 2: link the normalized per-channel records that belong to
# the same physical customer, matching on the non-reversible linkage tokens the
# request boundary produced — never on a raw resident identifier, which the
# pipeline never receives.
#
# Matching is deterministic token equality. The linkage tokens are derived with
# a named digest, so the same identity value produces the same token in every
# process and the unified customer id a caller stored yesterday still resolves
# to the same customer today.
#
# Wired by the inner aggregation graph. Returns only changed state keys.

import logging
from typing import Any, ClassVar, Dict, List, Set

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.schemas.state import from_json, to_json
from src.services.caller_contract import REDACTION_STUB, unified_customer_id

logger = logging.getLogger(__name__)

# Used when the runtime tuning is unreadable; mirrors config/config.yaml.
_DEFAULT_MIN_LINK_CONFIDENCE = 0.5

# Token fields that participate in cross-channel matching.
_MATCH_TOKEN_KEYS = (
    "email_token",
    "phone_token",
    "phone_number_token",
    "mobile_token",
    "tel_token",
    "loyalty_card_token",
    "loyalty_card_number_token",
    "card_number_token",
    "membership_id_token",
    "pan_token",
    "my_number_token",
    "individual_number_token",
    "resident_id_token",
    "name_token",
    "full_name_token",
    "customer_name_token",
)


def _match_tokens(record: Dict[str, Any]) -> Set[str]:
    """Collect the non-empty linkage tokens present on one record."""
    tokens: Set[str] = set()
    for key in _MATCH_TOKEN_KEYS:
        value = record.get(key)
        if value and value != REDACTION_STUB:
            tokens.add(str(value))
    return tokens


class ResolveIdentityNode(FunctionNode):
    """Resolve one customer identity across the normalized channel records.

    Input state keys:
        normalized_sources: JSON string — unified per-channel records
        aggregation_config: JSON string — live tuning (min_link_confidence)

    Output state keys (partial dict):
        resolved_identity: JSON string — {"unified_customer_id": str,
                           "linked_records": [...], "match_method": str,
                           "confidence": float}
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        normalized: Dict[str, Any] = from_json(state.get("normalized_sources"), {}) or {}
        tuning: Dict[str, Any] = from_json(state.get("aggregation_config"), {}) or {}
        records: List[Dict[str, Any]] = normalized.get("records") or []

        min_confidence = tuning.get("min_link_confidence")
        if isinstance(min_confidence, bool) or not isinstance(min_confidence, (int, float)):
            min_confidence = _DEFAULT_MIN_LINK_CONFIDENCE
        min_confidence = float(min_confidence)

        # Greedy single-cluster linkage: a record joins the customer cluster
        # when it shares at least one linkage token with a record already in it.
        # The first record seeds the cluster. Disambiguating several customers
        # in one request is out of scope — this agent resolves one Customer 360
        # per request, which is what its caller contract accepts.
        linked_records: List[Dict[str, Any]] = []
        cluster_tokens: Set[str] = set()
        token_matches = 0

        for index, record in enumerate(records):
            record_tokens = _match_tokens(record)
            shares = bool(record_tokens & cluster_tokens)
            if index == 0 or shares or not cluster_tokens:
                if shares:
                    token_matches += 1
                cluster_tokens |= record_tokens
                linked_records.append(
                    {
                        "channel": record.get("channel", ""),
                        "external_id": record.get("external_id", ""),
                        "matched_on_token": bool(shares),
                    }
                )

        record_count = len(records)
        if record_count == 0:
            confidence = 0.0
        elif record_count == 1:
            confidence = 1.0
        else:
            confidence = round((token_matches + 1) / record_count, 4)

        # Below the declared floor the linkage is reported as unlinked rather
        # than presented as a resolved customer: a caller acting on a Customer
        # 360 needs to know the records were not actually tied together.
        linked = confidence >= min_confidence and bool(cluster_tokens)
        match_method = "token_equality" if linked else "unlinked"

        identity: Dict[str, Any] = {
            "unified_customer_id": unified_customer_id(sorted(cluster_tokens)) if cluster_tokens else "",
            "linked_records": linked_records,
            "match_method": match_method,
            "confidence": confidence,
            "min_link_confidence": min_confidence,
        }

        emit_trace_event(
            "identity_resolved",
            {
                "linked_records": len(linked_records),
                "confidence": confidence,
                "match_method": match_method,
            },
            state,
        )

        logger.info(
            "ResolveIdentityNode: linked %d/%d records, confidence=%.4f, method=%s",
            len(linked_records),
            record_count,
            confidence,
            match_method,
        )

        return {"resolved_identity": to_json(identity)}
