# NormalizeSourcesNode — publishing the validated records in the unified schema.
#
# The node does not re-parse raw request data: everything it reads has already
# passed the caller contract. What is pinned here is the tuning it honours and
# the failure it declines to paper over.
#
# Deterministic — no model, no network.

import json

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.normalize_sources_node import NormalizeSourcesNode
from src.schemas.state import from_json, to_json
from src.services.caller_contract import validate_request

_REQUEST = "aggregate the omnichannel profile"


def _contract(*records):
    return validate_request(_REQUEST, {"sources": list(records)})


def _record(**overrides):
    base = {"channel": "pos", "external_id": "pos-a1", "events": ["visit"]}
    base.update(overrides)
    return base


def _run(contract, tuning=None, status=None):
    state = {"caller_contract": to_json(contract), "aggregation_config": to_json(tuning or {})}
    if status is not None:
        state["status"] = status
    result = NormalizeSourcesNode().execute(state)
    return from_json(result.get("normalized_sources"), None), result


class TestUnifiedSchema:
    def test_records_are_published(self):
        normalized, _ = _run(_contract(_record(), _record(channel="ec", external_id="ec-b2")))
        assert normalized["record_count"] == 2
        assert normalized["channels"] == ["pos", "ec"]

    def test_channels_are_listed_in_first_seen_order(self):
        normalized, _ = _run(_contract(_record(channel="ec", external_id="ec-1"), _record(channel="pos")))
        assert normalized["channels"] == ["ec", "pos"]

    def test_output_is_a_json_string(self):
        _, result = _run(_contract(_record()))
        assert isinstance(result["normalized_sources"], str)
        json.loads(result["normalized_sources"])

    def test_no_records_is_not_an_error(self):
        normalized, _ = _run({"records": []})
        assert normalized["record_count"] == 0
        assert normalized["truncated"] is False

    def test_identity_values_are_present_only_as_tokens(self):
        normalized, _ = _run(_contract(_record(email="hanako@example.com")))
        assert "hanako@example.com" not in json.dumps(normalized)
        assert normalized["records"][0]["email_token"].startswith("tok_email_")


class TestDeclaredRecordCap:
    def test_cap_truncates_and_says_so(self):
        contract = _contract(*[_record(external_id=f"pos-{index}") for index in range(5)])
        normalized, _ = _run(contract, tuning={"max_records": 2})
        assert normalized["record_count"] == 2
        assert normalized["truncated"] is True

    def test_below_the_cap_nothing_is_truncated(self):
        normalized, _ = _run(_contract(_record()), tuning={"max_records": 2})
        assert normalized["truncated"] is False

    @pytest.mark.parametrize("bad", [None, "many", -1, True, 2.5])
    def test_unusable_cap_falls_back_to_the_declared_default(self, bad):
        normalized, _ = _run(_contract(_record()), tuning={"max_records": bad})
        assert normalized["record_count"] == 1


class TestFailureIsCarriedForward:
    def test_an_earlier_error_is_not_overwritten(self):
        """Publishing an empty record set here would let the run continue and
        report a profile for data that never arrived."""
        normalized, result = _run(_contract(_record()), status=AgentStatus.ERROR.value)
        assert result == {}
        assert normalized is None
