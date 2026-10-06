"""AgentCore Platform v1.0"""

# RET-C2-123 — outer graph.
#
# Retail omnichannel Customer 360 aggregation.
#
# Architecture:
#
#   Outer backbone (fixed — add_edges() is NOT overridden):
#     START -> initialize -> pre_process -> main -> {route} -> post_process
#           -> finalize -> END
#                            | (retry, bounded by max_retry)
#                            -> pre_process
#
#   The `main` slot is a GraphNode subclass (Customer360GraphNode) that
#   delegates the whole aggregation workflow to DomainWorkflowGraph, so the
#   domain topology stays inside the inner graph and the backbone is untouched.
#
# Directory layout:
#   src/graph/graph.py                 <- outer graph (this file)
#   src/graph/domain_workflow_graph.py <- inner graph (aggregation topology)
#   src/graph/context_bridge.py        <- validated caller contract across the boundary
#
# Rules enforced here:
#   - RetC2123Agent inherits AgentBaseGraph (framework base class, directly)
#   - super().register_nodes() is called first (fills initialize + finalize)
#   - Customer360GraphNode occupies self._nodes["main"]
#   - merge_output() returns only changed keys
#   - add_edges() is NOT overridden
#   - No platform SDK imports

from pathlib import Path
from typing import Any, ClassVar, Dict

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.utils.config_loader import load_agent_config
from src.graph.context_bridge import set_caller_contract
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State, from_json

# Repo root: src/graph/graph.py -> parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Fallbacks mirror config/config.yaml so the aggregation tuning is never empty
# even where the config file is unreadable in an exotic deployment layout.
_FALLBACK_AGGREGATION: Dict[str, Any] = {
    "max_records": 50,
    "high_frequency_events": 10,
    "min_link_confidence": 0.5,
}


def runtime_config() -> Dict[str, Any]:
    """Load config/config.yaml — the live runtime parameters.

    The registry loads this file and passes it to the graph constructor; the
    standalone HTTP entry point does the same, so `max_retry` and the
    aggregation tuning are live in both deployments rather than declared and
    ignored.

    Reading the static manifest (config/agent.yaml) here instead would return
    nothing: the manifest carries identity and compile-time requirements only,
    and a reader pointed at it degrades silently to defaults.
    """
    loaded = load_agent_config(_REPO_ROOT)
    return dict(loaded) if isinstance(loaded, dict) else {}


class Customer360GraphNode(GraphNode):
    """The `main` slot: wraps the inner Customer 360 aggregation workflow.

    Contracts:
      get_subgraph()    - instantiate DomainWorkflowGraph with the forwarded
                          runtime config (_parent_config())
      extract_input()   - hand the validated request to the inner graph and
                          stash the validated caller contract on the bridge
      merge_output()    - map sub_result fields into the outer state delta
      error_strategy    - "propagate": re-raise inner errors (fail fast)
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (fail fast).
    # "handle": call on_subgraph_error() instead — for graceful degradation.
    error_strategy: ClassVar[str] = "propagate"

    # False: human-review interrupts are handled inside the inner graph only.
    propagate_hitl: ClassVar[bool] = False

    def _parent_config(self) -> Dict[str, Any]:
        """Forward the live aggregation tuning to the inner graph.

        Returns the tuning block under config["configurable"] — never an empty
        dict. The inner graph republishes it into inner state
        (DomainWorkflowGraph._extra_initial_state()) so the normalize, identity
        and unify steps read live values: node execute() methods take no config
        parameter, so state seeding is the only route config can travel.
        """
        aggregation = runtime_config().get("aggregation")
        if not isinstance(aggregation, dict) or not aggregation:
            aggregation = dict(_FALLBACK_AGGREGATION)
        return {"configurable": {"aggregation": aggregation}}

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner aggregation workflow graph.

        Imported inside the method to avoid circular-import risk at module load
        time. The inner graph receives the runtime-derived config through its
        constructor; its domain nodes still take no constructor arguments and
        read their tuning per call from seeded state.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        """Return the request string, and bridge the validated caller contract.

        The framework hands only a string to the inner graph, so the structured
        part of the request travels on the bridge instead — set here, one step
        before the inner invoke, and read by the inner graph's initial-state
        hook. Only the contract the pre_process node already validated crosses.
        """
        set_caller_contract(from_json(state.get("caller_contract"), {}) or {})
        return str(state.get("validated_input") or state.get("user_input", ""))

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map the inner result into the outer state delta (changed keys only).

        Key coupling, designed together with DomainWorkflowGraph.get_output():
          inner get_output() emits  -> "customer_360_profile", "status"
          this merge_output() reads -> sub_result.get("customer_360_profile"),
                                       sub_result.get("status")

        The profile is mapped to "result" as well, because the post_process
        output gate reads state["result"] — without that mapping the surfaced
        output would always be empty.

        A non-success inner status never reaches here (error_strategy is
        "propagate", so the inner error is re-raised first), but the guard is
        kept so a future strategy change cannot start publishing an un-gated
        profile through this path.
        """
        status = sub_result.get("status")
        profile = sub_result.get("customer_360_profile")
        if status != AgentStatus.SUCCESS.value:
            return {
                "customer_360_profile": None,
                "result": None,
                "status": status,
            }
        return {
            "customer_360_profile": profile,
            "result": profile,
            "status": status,
        }


class RetC2123Agent(AgentBaseGraph):
    """Outer graph for RET-C2-123.

    Inherits AgentBaseGraph directly (framework base class). The domain logic is
    fully encapsulated in Customer360GraphNode (the `main` slot), which delegates
    to DomainWorkflowGraph.

    Backbone (fixed):
        START -> initialize -> pre_process -> main -> post_process -> finalize -> END

    register_nodes() is the only override:
      - super().register_nodes() fills initialize and finalize
      - pre_process:  PreProcessNode  (caller contract validation + personal-data screen)
      - main:         Customer360GraphNode
      - post_process: PostProcessNode (output gate)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the agent registry."""
        return "RetailOmnichannelCustomer360AggregationAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all five backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default initialize node (schema version, session id, trust
        level) and finalize node (response metadata, total time).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = Customer360GraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the caller-facing envelope.

        The framework's envelope resolves the output as
        ``formatted_output or result`` with NO status check, so an error path
        that left ``result`` in place ships the un-gated profile inside the
        error envelope. The output gate cannot close that on its own: when the
        framework's own credential scan raises on the gate node's return value,
        the wrapper discards that node's whole delta — the clearing included —
        and ``result`` survives untouched in state.

        So on any non-success status the output resolves to the gate's own
        notice, or to None. It never falls back to ``result``.

        No third layer re-scans the success path here. One would contain a leak
        by itself and thereby make the gate node's own scan unfalsifiable; the
        gate is the single place the success path is checked, and the mutant
        that removes it must be able to fail.
        """
        output: Dict[str, Any] = dict(super().get_output(state))
        if state.get("status") != AgentStatus.SUCCESS.value:
            output["output"] = state.get("formatted_output") or None
        return output


# Back-compat alias — config/agent.yaml resolves the entry point by this name.
Graph = RetC2123Agent
