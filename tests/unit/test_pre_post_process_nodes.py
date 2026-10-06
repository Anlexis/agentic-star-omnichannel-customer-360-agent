# The two backbone slots the template owns: the request boundary and the output
# boundary.
#
# Their declared trust level is pinned here because it is the difference between
# an agent that serves requests and one that returns an error to every caller:
# a node demanding a level no external caller can hold makes the whole run fail
# at its own trust gate, with a green unit suite either way.
#
# Deterministic — no model, no network.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Customer360GraphNode
from src.nodes.normalize_sources_node import NormalizeSourcesNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.resolve_identity_node import ResolveIdentityNode
from src.nodes.unify_profile_node import UnifyProfileNode
from src.schemas.state import from_json

_REQUEST = "aggregate the omnichannel profile"
_RECORDS = {"sources": [{"channel": "pos", "external_id": "pos-a1", "events": ["visit"]}]}

_DOMAIN_NODES = [
    PreProcessNode,
    PostProcessNode,
    NormalizeSourcesNode,
    ResolveIdentityNode,
    UnifyProfileNode,
]


class TestDeclaredTrustLevel:
    @pytest.mark.parametrize("node_class", _DOMAIN_NODES)
    def test_every_node_declares_its_own_level(self, node_class):
        """Silent inheritance of the permissive default is not acceptable."""
        assert "required_trust_level" in node_class.__dict__

    @pytest.mark.parametrize("node_class", _DOMAIN_NODES)
    def test_no_node_demands_more_than_the_entry_contract(self, node_class):
        """The manifest admits verified external callers.

        A node demanding the internal level cannot be reached by any caller the
        entry point admits, so every request would fail at that node's trust
        gate while the unit suite — which supplies the internal level itself —
        stayed green.
        """
        assert node_class.required_trust_level is TrustLevel.VERIFIED_EXTERNAL

    def test_anonymous_callers_are_still_denied(self):
        state = {"user_input": _REQUEST, "input_context": _RECORDS, "caller_trust_level": TrustLevel.ANONYMOUS.value}
        delta = PreProcessNode()(state)
        assert delta["status"] == AgentStatus.ERROR.value


class TestRequestBoundarySlot:
    def test_accepted_request_produces_the_contract(self):
        result = PreProcessNode().execute({"user_input": _REQUEST, "input_context": _RECORDS})
        assert result["status"] == AgentStatus.SUCCESS.value
        contract = from_json(result["caller_contract"], {})
        assert contract["records"][0]["channel"] == "pos"

    def test_structured_parameters_are_recorded_as_the_source(self):
        result = PreProcessNode().execute({"user_input": _REQUEST, "input_context": _RECORDS})
        assert from_json(result["caller_contract"], {})["record_source"] == "input_context"

    def test_channel_filter_is_honoured(self):
        context = {
            "sources": [
                {"channel": "pos", "external_id": "pos-a1"},
                {"channel": "ec", "external_id": "ec-b2"},
            ],
            "channel_filter": ["ec"],
        }
        result = PreProcessNode().execute({"user_input": _REQUEST, "input_context": context})
        contract = from_json(result["caller_contract"], {})
        assert [record["channel"] for record in contract["records"]] == ["ec"]

    def test_absent_structured_parameters_are_not_an_error(self):
        result = PreProcessNode().execute({"user_input": _REQUEST})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert from_json(result["caller_contract"], {})["records"] == []


class TestOutputBoundarySlot:
    def test_clean_profile_is_released(self):
        profile = '{"unified_customer_id": "cust_1", "channels": ["pos"]}'
        result = PostProcessNode().execute({"result": profile})
        assert result["formatted_output"] == profile
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_gate_reads_only_keys_that_exist_at_its_level(self):
        """A layer reading an inner-graph key from outer state compares against
        nothing on every real invocation: dead, healthy-looking, and green.

        The main slot publishes exactly these two keys, so these are the two the
        gate reads.
        """
        published = Customer360GraphNode().merge_output(
            {}, {"customer_360_profile": "P", "status": AgentStatus.SUCCESS.value}
        )
        assert {"result", "customer_360_profile"} <= set(published)
