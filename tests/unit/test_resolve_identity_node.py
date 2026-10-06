# ResolveIdentityNode — cross-channel record linkage over tokens.
#
# The capability under test is the agent's reason to exist: two channels that
# carry the same identity value must resolve to one customer. The linkage runs
# on non-reversible tokens; no raw identifier ever reaches this node.
#
# Deterministic — no model, no network.

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.resolve_identity_node import ResolveIdentityNode
from src.schemas.state import from_json, to_json
from src.services.caller_contract import validate_request

_REQUEST = "aggregate the omnichannel profile"


def _normalized(*records):
    contract = validate_request(_REQUEST, {"sources": list(records)})
    validated = contract["records"]
    channels: list[str] = []
    for record in validated:
        if record["channel"] not in channels:
            channels.append(record["channel"])
    return {"records": validated, "channels": channels, "record_count": len(validated), "truncated": False}


def _run(normalized, tuning=None, status=None):
    state = {
        "normalized_sources": to_json(normalized),
        "aggregation_config": to_json(tuning or {}),
    }
    if status is not None:
        state["status"] = status
    result = ResolveIdentityNode().execute(state)
    return from_json(result.get("resolved_identity"), None), result


class TestCrossChannelLinkage:
    def test_shared_identity_value_links_two_channels(self):
        identity, _ = _run(
            _normalized(
                {"channel": "pos", "external_id": "pos-a1", "email": "a@example.com"},
                {"channel": "ec", "external_id": "ec-b2", "email": "a@example.com"},
            )
        )
        assert identity["match_method"] == "token_equality"
        assert len(identity["linked_records"]) == 2
        assert identity["confidence"] == 1.0

    def test_unrelated_records_do_not_link(self):
        identity, _ = _run(
            _normalized(
                {"channel": "pos", "external_id": "pos-a1", "email": "a@example.com"},
                {"channel": "ec", "external_id": "ec-b2", "email": "b@example.com"},
            )
        )
        assert len(identity["linked_records"]) == 1

    def test_unified_id_is_derived_from_the_linked_tokens(self):
        first, _ = _run(_normalized({"channel": "pos", "external_id": "pos-a1", "email": "a@example.com"}))
        second, _ = _run(_normalized({"channel": "ec", "external_id": "ec-b2", "email": "a@example.com"}))
        assert first["unified_customer_id"] == second["unified_customer_id"]
        assert first["unified_customer_id"].startswith("cust_")

    def test_no_identity_values_yields_no_unified_id(self):
        identity, _ = _run(_normalized({"channel": "pos", "external_id": "pos-a1"}))
        assert identity["unified_customer_id"] == ""
        assert identity["match_method"] == "unlinked"

    def test_no_records_is_not_an_error(self):
        identity, _ = _run({"records": [], "channels": [], "record_count": 0})
        assert identity["confidence"] == 0.0
        assert identity["linked_records"] == []

    def test_no_raw_identifier_appears_in_the_result(self):
        identity, _ = _run(_normalized({"channel": "pos", "external_id": "pos-a1", "email": "hanako@example.com"}))
        assert "hanako@example.com" not in str(identity)


class TestDeclaredLinkFloor:
    def _weak_match(self, tuning):
        return _run(
            _normalized(
                {"channel": "pos", "external_id": "pos-a1", "email": "a@example.com"},
                {"channel": "ec", "external_id": "ec-b2", "email": "a@example.com"},
                {"channel": "loyalty", "external_id": "ly-c3", "membership_id": "M-778"},
            ),
            tuning=tuning,
        )[0]

    def test_above_the_floor_the_match_stands(self):
        assert self._weak_match({"min_link_confidence": 0.5})["match_method"] == "token_equality"

    def test_below_the_floor_the_match_is_reported_as_unlinked(self):
        """A caller acting on a Customer 360 needs to know the records were not
        actually tied together."""
        assert self._weak_match({"min_link_confidence": 0.99})["match_method"] == "unlinked"

    def test_the_floor_is_reported_back(self):
        assert self._weak_match({"min_link_confidence": 0.75})["min_link_confidence"] == 0.75

    @pytest.mark.parametrize("bad", [None, "half", True])
    def test_unusable_floor_falls_back_to_the_declared_default(self, bad):
        assert self._weak_match({"min_link_confidence": bad})["min_link_confidence"] == 0.5


class TestFailureIsCarriedForward:
    def test_an_earlier_error_is_not_overwritten(self):
        identity, result = _run(
            _normalized({"channel": "pos", "external_id": "pos-a1"}), status=AgentStatus.ERROR.value
        )
        assert result == {}
        assert identity is None
