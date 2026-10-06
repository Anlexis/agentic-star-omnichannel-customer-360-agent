"""AgentCore Platform v1.0"""

# RET-C2-123 — inner aggregation workflow graph.
#
# Encapsulates the whole omnichannel Customer 360 workflow:
#
#   START -> normalize_sources -> resolve_identity -> unify_profile -> END
#
# Called by Customer360GraphNode.get_subgraph() (graph.py). get_output() shapes
# the sub_result dict that merge_output() there consumes.
#
# Rules enforced here:
#   - inherits BaseGraph (fully custom topology, no forced backbone)
#   - implements every BaseGraph abstract method
#   - register_nodes() does NOT call super() (it is abstract on BaseGraph)
#   - register_nodes() instantiates every domain node with NO constructor args
#   - does NOT register initialize / finalize (outer backbone concerns)
#   - get_output() is designed together with Customer360GraphNode.merge_output()
#   - No platform SDK imports

from typing import Any, Dict

from langgraph.graph import END, START

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.graph.context_bridge import get_caller_contract
from src.nodes.normalize_sources_node import NormalizeSourcesNode
from src.nodes.resolve_identity_node import ResolveIdentityNode
from src.nodes.unify_profile_node import UnifyProfileNode
from src.schemas.state import State, to_json


class DomainWorkflowGraph(BaseGraph):
    """Inner aggregation workflow for RET-C2-123.

    Pipeline (linear):
        START
          -> normalize_sources (per-channel records to the unified schema)
          -> resolve_identity  (cross-channel record linkage over tokens)
          -> unify_profile     (assemble the Customer 360 profile)
          -> END

    Every node is a FunctionNode subclass returning a partial state update.
    initialize / finalize are outer backbone concerns and are not registered.
    """

    # -- Identity --------------------------------------------------------------

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "ret_c2_123_customer_360_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across the inner and outer graph."""
        return State

    # -- Config validation -----------------------------------------------------

    def _validate_config(self) -> None:
        """Reject an aggregation block that would silently change the answer.

        The tuning arrives from config/config.yaml through the parent graph.
        Every value below is read by a domain node and shapes the released
        profile, so a malformed one is refused at compile time rather than
        being coerced to a default that the operator never declared.
        """
        aggregation = (self.config.get("configurable") or {}).get("aggregation")
        if aggregation is None:
            return
        if not isinstance(aggregation, dict):
            raise ConfigError(f"[{self.__class__.__name__}] 'aggregation' must be a mapping")

        for key in ("max_records", "high_frequency_events"):
            value = aggregation.get(key)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError(f"[{self.__class__.__name__}] 'aggregation.{key}' must be a non-negative integer")

        confidence = aggregation.get("min_link_confidence")
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise ConfigError(f"[{self.__class__.__name__}] 'aggregation.min_link_confidence' must be a number")
            if not (0.0 <= float(confidence) <= 1.0):
                raise ConfigError(
                    f"[{self.__class__.__name__}] 'aggregation.min_link_confidence' must be between 0 and 1"
                )

    # -- Initial state ---------------------------------------------------------

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the bridged caller contract and the live tuning into inner state.

        Both have to arrive this way. The framework hands the inner graph only a
        string, so the validated records travel on the context bridge; and node
        execute() methods take no config argument, so the tuning travels as
        state. Structured values are stored as JSON strings, because checkpoint
        serialization does not carry bare containers safely.
        """
        aggregation = (self.config.get("configurable") or {}).get("aggregation") or {}
        return {
            "caller_contract": to_json(get_caller_contract()),
            "aggregation_config": to_json(dict(aggregation)),
        }

    # -- Node registration -----------------------------------------------------

    def register_nodes(self) -> None:
        """Register the three domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract. initialize and
        finalize are outer backbone concerns handled in graph.py.

        Every node is instantiated with NO constructor arguments: FunctionNode
        subclasses take no __init__, and their tuning arrives through seeded
        state. Every key registered here is referenced in add_edges().
        """
        self._nodes["normalize_sources"] = NormalizeSourcesNode()
        self._nodes["resolve_identity"] = ResolveIdentityNode()
        self._nodes["unify_profile"] = UnifyProfileNode()

    # -- Edge wiring -----------------------------------------------------------

    def add_edges(self) -> None:
        """Wire the linear aggregation topology.

        Each step passes its partial update into the shared state. The topology
        is intentionally linear — there is no branch between the domain steps —
        so add_conditional_edges() is not used. A step that fails does not stop
        the run here; instead each downstream step refuses to overwrite an error
        status, so the inner graph reports the failure rather than a profile
        assembled from data that never arrived.
        """
        self._sg.add_edge(START, "normalize_sources")
        self._sg.add_edge("normalize_sources", "resolve_identity")
        self._sg.add_edge("resolve_identity", "unify_profile")
        self._sg.add_edge("unify_profile", END)

    # -- Routing ---------------------------------------------------------------

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by the BaseGraph contract.

        add_conditional_edges() is not used for this linear topology, so this is
        never called at runtime. It is annotated with this graph's own State
        because LangGraph reads a path callable's annotation as its input schema
        and projects away every field the annotation does not declare — an
        annotation naming the base state would make the routing fields
        permanently absent if this method were ever wired.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "unify_profile"

    # -- Output shape ----------------------------------------------------------

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the sub_result dict returned to the outer graph.

        Received by Customer360GraphNode.merge_output() in graph.py. Both are
        designed together to guarantee field-name consistency:

            inner get_output()  emits: "customer_360_profile", "status", ...
            outer merge_output() reads: sub_result.get("customer_360_profile"),
                                        sub_result.get("status")

        On a non-success status the profile is withheld here as well as at the
        outer boundary. A step that failed leaves the state it should have
        written empty, so anything assembled downstream describes data that
        never arrived — releasing it as a partial answer would report a
        customer with no channels as a real result.
        """
        status = state.get("status")
        if status != AgentStatus.SUCCESS.value:
            return {
                "customer_360_profile": None,
                "status": status,
                "normalized_sources": None,
                "resolved_identity": None,
                "trace_id": state.get("trace_id"),
                "correlation_id": state.get("correlation_id"),
                "node_history": state.get("node_history", []),
            }
        return {
            "customer_360_profile": state.get("customer_360_profile"),
            "status": status,
            "normalized_sources": state.get("normalized_sources"),
            "resolved_identity": state.get("resolved_identity"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
