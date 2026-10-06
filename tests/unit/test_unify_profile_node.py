# UnifyProfileNode — assembling the released profile.
#
# The terminal step. Its success status is what routes the run through the
# output gate rather than short-circuiting past it, so the case where an earlier
# step failed is pinned here too: emitting success there would overwrite the
# failure and hand the caller an empty profile presented as a real answer.
#
# Deterministic — no model, no network.

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.unify_profile_node import UnifyProfileNode
from src.schemas.state import from_json, to_json


def _normalized(records, channels, truncated=False):
    return {
        "records": records,
        "channels": channels,
        "record_count": len(records),
        "truncated": truncated,
    }


def _run(normalized, identity=None, tuning=None, status=None):
    state = {
        "normalized_sources": to_json(normalized),
        "resolved_identity": to_json(identity or {}),
        "aggregation_config": to_json(tuning or {}),
    }
    if status is not None:
        state["status"] = status
    result = UnifyProfileNode().execute(state)
    return from_json(result.get("customer_360_profile"), None), result


_POS = {"channel": "pos", "external_id": "pos-a1", "events": ["visit", "purchase"], "attributes": {"store": "shibuya"}}
_EC = {"channel": "ec", "external_id": "ec-b2", "events": ["cart"], "attributes": {"site": "web-jp"}}
_LOYALTY = {"channel": "loyalty", "external_id": "ly-c3", "events": [], "attributes": {"tier": "gold"}}


class TestProfileAssembly:
    def test_metrics_reflect_the_records(self):
        profile, _ = _run(_normalized([_POS, _EC], ["pos", "ec"]))
        assert profile["metrics"]["total_events"] == 3
        assert profile["metrics"]["channel_count"] == 2
        assert profile["metrics"]["record_count"] == 2

    def test_attributes_are_merged_across_channels(self):
        profile, _ = _run(_normalized([_POS, _EC, _LOYALTY], ["pos", "ec", "loyalty"]))
        assert profile["attributes"] == {"store": "shibuya", "site": "web-jp", "tier": "gold"}

    def test_first_writer_wins_on_a_repeated_attribute(self):
        first = {"channel": "pos", "external_id": "p", "events": [], "attributes": {"tier": "gold"}}
        second = {"channel": "ec", "external_id": "e", "events": [], "attributes": {"tier": "silver"}}
        profile, _ = _run(_normalized([first, second], ["pos", "ec"]))
        assert profile["attributes"]["tier"] == "gold"

    def test_identity_fields_are_carried_through(self):
        profile, _ = _run(
            _normalized([_POS], ["pos"]),
            identity={
                "unified_customer_id": "cust_1",
                "confidence": 0.75,
                "match_method": "token_equality",
                "linked_records": [{}],
            },
        )
        assert profile["unified_customer_id"] == "cust_1"
        assert profile["confidence"] == 0.75
        assert profile["match_method"] == "token_equality"

    def test_truncation_is_surfaced(self):
        profile, _ = _run(_normalized([_POS], ["pos"], truncated=True))
        assert profile["truncated"] is True

    def test_terminal_status_is_success(self):
        """The status the outer backbone routes on. Without it the run
        short-circuits to finalize and never reaches the output gate."""
        _, result = _run(_normalized([_POS], ["pos"]))
        assert result["status"] == AgentStatus.SUCCESS.value


class TestSegments:
    @pytest.mark.parametrize(
        "channels,expected",
        [
            (["pos", "ec"], "omnichannel_shopper"),
            (["ec"], "online_only"),
            (["pos"], "instore_only"),
            (["loyalty"], "loyalty_member"),
        ],
    )
    def test_channel_coverage_drives_the_segment(self, channels, expected):
        records = [{"channel": channel, "external_id": channel, "events": [], "attributes": {}} for channel in channels]
        profile, _ = _run(_normalized(records, channels))
        assert expected in profile["segments"]

    def test_no_activity_is_dormant(self):
        profile, _ = _run(_normalized([_LOYALTY], ["loyalty"]))
        assert "dormant" in profile["segments"]

    def test_no_records_is_dormant_not_unclassified(self):
        """No channel coverage and no activity: the activity rule still fires,
        so the profile is dormant rather than falling through to unclassified."""
        profile, _ = _run(_normalized([], []))
        assert profile["segments"] == ["dormant"]

    def test_unclassified_is_the_fallback_when_no_rule_fires(self):
        crm = {"channel": "crm", "external_id": "c1", "events": ["update"], "attributes": {}}
        profile, _ = _run(_normalized([crm], ["crm"]))
        assert profile["segments"] == ["unclassified"]


class TestDeclaredFrequencyThreshold:
    def _profile_with_threshold(self, threshold):
        busy = {"channel": "pos", "external_id": "p", "events": ["e"] * 6, "attributes": {}}
        return _run(_normalized([busy], ["pos"]), tuning={"high_frequency_events": threshold})[0]

    def test_at_or_above_the_threshold_the_segment_appears(self):
        assert "high_frequency" in self._profile_with_threshold(6)["segments"]

    def test_below_the_threshold_it_does_not(self):
        assert "high_frequency" not in self._profile_with_threshold(7)["segments"]

    @pytest.mark.parametrize("bad", [None, "ten", -1, True])
    def test_unusable_threshold_falls_back_to_the_declared_default(self, bad):
        assert "high_frequency" not in self._profile_with_threshold(bad)["segments"]


class TestFailureIsCarriedForward:
    def test_an_earlier_error_is_not_overwritten(self):
        """A success emitted here would replace the failure and present an empty
        profile as a real answer."""
        profile, result = _run(_normalized([], []), status=AgentStatus.ERROR.value)
        assert result == {}
        assert profile is None
