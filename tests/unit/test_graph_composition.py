# Graph composition — the two-layer wiring, and the contracts that hold it
# together.
#
# Outer backbone:  initialize -> pre_process -> main -> {route} -> post_process -> finalize
# Main slot:       Customer360GraphNode -> DomainWorkflowGraph
# Inner pipeline:  normalize_sources -> resolve_identity -> unify_profile
#
# The pieces that are easy to get wrong and impossible to see from a passing
# run are pinned here: the structured request only crosses the boundary because
# the bridge carries it, the runtime tuning only reaches a domain node because
# the initial-state hook seeds it, and the envelope only withholds a failed
# answer because the output resolution is overridden.
#
# Deterministic — no model, no network.

import pytest

from framework.errors import ConfigError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext

from src.graph.context_bridge import get_caller_contract, set_caller_contract
from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.graph.graph import Customer360GraphNode, Graph, RetC2123Agent, runtime_config
from src.schemas.state import State, from_json, to_json

_REQUEST = "aggregate the omnichannel profile"
_RECORDS = {
    "sources": [
        {"channel": "pos", "external_id": "pos-a1", "email": "a@example.com", "events": ["visit", "purchase"]},
        {"channel": "ec", "external_id": "ec-b2", "email": "a@example.com", "events": ["cart"]},
        {"channel": "loyalty", "external_id": "ly-c3", "tier": "gold", "events": []},
    ]
}


class TestOuterGraph:
    def test_state_schema(self):
        assert RetC2123Agent().state_schema is State

    def test_agent_name(self):
        assert RetC2123Agent().name == "RetailOmnichannelCustomer360AggregationAgent"

    def test_compile_fills_every_backbone_slot(self):
        agent = RetC2123Agent()
        agent.compile()
        for slot in ("initialize", "pre_process", "main", "post_process", "finalize"):
            assert agent._nodes.get(slot) is not None, f"backbone slot not filled: {slot}"

    def test_main_slot_is_the_domain_graph_node(self):
        agent = RetC2123Agent()
        agent.compile()
        assert isinstance(agent._nodes["main"], Customer360GraphNode)

    def test_alias_resolves_to_the_agent(self):
        assert Graph is RetC2123Agent


class TestRuntimeConfiguration:
    def test_config_file_is_loaded(self):
        loaded = runtime_config()
        assert loaded["max_retry"] == 3
        assert loaded["aggregation"]["high_frequency_events"] == 10

    def test_declared_retry_ceiling_reaches_the_backbone(self):
        """max_retry is validated at compile time, so a bad value must fail."""
        with pytest.raises(ConfigError):
            RetC2123Agent(config={"max_retry": 99}).compile()

    def test_tuning_is_forwarded_to_the_inner_graph(self):
        forwarded = Customer360GraphNode()._parent_config()
        assert forwarded["configurable"]["aggregation"]["high_frequency_events"] == 10

    def test_inner_graph_seeds_the_tuning_into_state(self):
        """Node execute() methods take no config argument, so seeding is the
        only route a declared value can travel into a domain node."""
        inner = DomainWorkflowGraph(config={"configurable": {"aggregation": {"max_records": 7}}})
        seeded = from_json(inner._extra_initial_state()["aggregation_config"], {})
        assert seeded["max_records"] == 7

    @pytest.mark.parametrize(
        "aggregation",
        [
            {"max_records": -1},
            {"max_records": "many"},
            {"high_frequency_events": True},
            {"min_link_confidence": 5},
            {"min_link_confidence": "half"},
        ],
    )
    def test_malformed_tuning_is_refused_at_compile_time(self, aggregation):
        with pytest.raises(ConfigError):
            DomainWorkflowGraph(config={"configurable": {"aggregation": aggregation}}).compile()

    def test_absent_tuning_is_not_an_error(self):
        DomainWorkflowGraph(config={}).compile()


class TestContextBridge:
    def test_contract_crosses_the_boundary(self):
        """The framework hands the inner graph only a string, so the structured
        request travels here or not at all."""
        contract = {"records": [{"channel": "pos"}], "channel_filter": []}
        Customer360GraphNode().extract_input({"validated_input": _REQUEST, "caller_contract": to_json(contract)})
        assert get_caller_contract()["records"] == [{"channel": "pos"}]

    def test_inner_initial_state_reads_the_bridge(self):
        set_caller_contract({"records": [{"channel": "ec"}]})
        seeded = from_json(DomainWorkflowGraph()._extra_initial_state()["caller_contract"], {})
        assert seeded["records"] == [{"channel": "ec"}]

    def test_extract_input_prefers_the_validated_request(self):
        node = Customer360GraphNode()
        assert node.extract_input({"validated_input": "V", "user_input": "U"}) == "V"

    def test_extract_input_falls_back_to_the_raw_request(self):
        assert Customer360GraphNode().extract_input({"user_input": "U"}) == "U"


class TestMergeOutput:
    def test_success_maps_the_profile_to_both_fields(self):
        delta = Customer360GraphNode().merge_output(
            {}, {"customer_360_profile": "PROFILE", "status": AgentStatus.SUCCESS.value}
        )
        assert delta["customer_360_profile"] == "PROFILE"
        assert delta["result"] == "PROFILE"

    def test_only_changed_keys_are_returned(self):
        delta = Customer360GraphNode().merge_output(
            {}, {"customer_360_profile": "P", "status": AgentStatus.SUCCESS.value}
        )
        assert set(delta) == {"customer_360_profile", "result", "status"}

    def test_non_success_publishes_no_profile(self):
        delta = Customer360GraphNode().merge_output(
            {}, {"customer_360_profile": "P", "status": AgentStatus.ERROR.value}
        )
        assert delta["result"] is None
        assert delta["customer_360_profile"] is None


class TestEnvelopeResolution:
    """The framework resolves the output as `formatted_output or result` with no
    status check, so an error path that left result in place would ship the
    un-gated profile inside the error envelope."""

    def test_error_envelope_never_falls_back_to_result(self):
        envelope = RetC2123Agent().get_output(
            {
                "status": AgentStatus.ERROR.value,
                "result": "UNGATED PROFILE",
            }
        )
        assert envelope["output"] is None

    def test_error_envelope_surfaces_the_gate_notice(self):
        envelope = RetC2123Agent().get_output(
            {
                "status": AgentStatus.ERROR.value,
                "formatted_output": "WITHHELD",
                "result": "UNGATED PROFILE",
            }
        )
        assert envelope["output"] == "WITHHELD"

    def test_success_envelope_surfaces_the_gated_output(self):
        envelope = RetC2123Agent().get_output(
            {
                "status": AgentStatus.SUCCESS.value,
                "formatted_output": "PROFILE",
            }
        )
        assert envelope["output"] == "PROFILE"


class TestInnerGraph:
    def test_registers_the_three_domain_steps(self):
        inner = DomainWorkflowGraph()
        inner.register_nodes()
        assert set(inner._nodes) == {"normalize_sources", "resolve_identity", "unify_profile"}

    def test_name_and_schema(self):
        inner = DomainWorkflowGraph()
        assert inner.name == "ret_c2_123_customer_360_workflow"
        assert inner.state_schema is State

    def test_output_shape_on_success(self):
        out = DomainWorkflowGraph().get_output({"customer_360_profile": "P", "status": AgentStatus.SUCCESS.value})
        assert out["customer_360_profile"] == "P"
        assert out["status"] == AgentStatus.SUCCESS.value

    def test_output_withholds_a_profile_on_failure(self):
        out = DomainWorkflowGraph().get_output({"customer_360_profile": "P", "status": AgentStatus.ERROR.value})
        assert out["customer_360_profile"] is None

    def test_route_is_annotated_with_this_graphs_own_state(self):
        """LangGraph reads a path callable's annotation as its input schema and
        projects away every field the annotation does not declare."""
        annotation = DomainWorkflowGraph.route.__annotations__["state"]
        assert annotation.__name__ in ("State", "AgentState")


class TestEndToEndComposition:
    """The full agent, at the trust level the manifest declares."""

    def _run(self):
        ctx = InvocationContext(session_id="s", caller_trust_level=_verified_external())
        return RetC2123Agent(config=runtime_config()).invoke(_REQUEST, ctx=ctx, input_context=_RECORDS)

    def test_run_succeeds(self):
        assert self._run()["status"] == AgentStatus.SUCCESS.value

    def test_output_is_the_assembled_profile(self):
        profile = from_json(self._run()["output"])
        assert set(profile["channels"]) == {"pos", "ec", "loyalty"}
        assert profile["metrics"]["total_events"] == 3

    def test_backbone_traverses_the_output_gate(self):
        assert "PostProcessNode" in self._run()["node_history"]


def _verified_external():
    from framework.schemas.trust_level import TrustLevel

    return TrustLevel.VERIFIED_EXTERNAL
