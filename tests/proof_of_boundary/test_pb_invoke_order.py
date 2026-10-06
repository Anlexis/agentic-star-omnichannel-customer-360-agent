# Invoke-order boundary: a full agent run executes the fixed backbone in order.
#
# The backbone is fixed and is never overridden by a template (edge wiring
# belongs to the framework):
#
#     START -> initialize -> pre_process -> main -> {route} -> post_process
#           -> finalize -> END
#
# Every executed node is recorded in node_history by its CLASS name, appended by
# the framework's node wrapper. For this template the `main` slot is a graph
# node that delegates to the inner aggregation workflow; the inner graph runs
# with its own state and its node history is not merged back, so the outer
# history holds exactly the five backbone slots.
#
# The run is driven at the trust level the manifest declares — the level a real
# caller holds — not at a privileged one. A suite that supplied a higher level
# would pass on an agent that fails every real request at its own trust gate.
#
# A success terminal status is required: on any other status the route sends
# main straight to finalize and the output-gate slot is skipped, which is itself
# an invoke-order violation this file catches.
#
# Deterministic — no model, no network.

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph, runtime_config

# --- TEMPLATE-SPECIFIC ------------------------------------------------------
# The `main`-slot graph-node class for THIS template. A sibling template
# mirroring this file changes ONLY this entry; the other four slot names are
# framework-fixed and identical across every template.
_MAIN_SLOT_NODE = "Customer360GraphNode"

_REQUEST = "aggregate the omnichannel profile"

# A valid request that drives the full aggregation workflow to success. The
# records travel as structured invocation parameters, which is the supported
# channel: the platform rewrites personal-data shapes out of the request string
# at every node boundary, so retail proper nouns embedded there arrive masked.
# Every value is inert — no token matches a direct-identifier shape, so the
# payload survives the request boundary intact.
_VALID_CONTEXT = {
    "sources": [
        {"channel": "pos", "external_id": "pos-a1", "store": "shibuya", "events": ["visit", "purchase"]},
        {"channel": "ec", "external_id": "ec-b2", "site": "web-jp", "events": ["cart", "purchase"]},
        {"channel": "loyalty", "external_id": "ly-c3", "tier": "gold", "membership_id": "M-778"},
    ]
}
# --- END TEMPLATE-SPECIFIC --------------------------------------------------

_EXPECTED_ORDER = [
    "InitializeNode",  # framework default  (initialize slot)
    "PreProcessNode",  # request boundary   (pre_process slot)
    _MAIN_SLOT_NODE,  # TEMPLATE-SPECIFIC  (main slot)
    "PostProcessNode",  # output boundary    (post_process slot)
    "FinalizeNode",  # framework default  (finalize slot)
]


def _run() -> dict:
    """Run a full invocation at the declared entry trust level."""
    ctx = InvocationContext(
        session_id="invoke-order",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        caller_id="test-suite",
    )
    return Graph(config=runtime_config()).invoke(_REQUEST, ctx=ctx, input_context=_VALID_CONTEXT)


class TestInvokeOrderBoundary:
    def test_invoke_reaches_success(self):
        """Any other status means the route short-circuits past the output gate."""
        result = _run()
        assert (
            result.get("status") == AgentStatus.SUCCESS.value
        ), f"Expected success, got {result.get('status')!r}. result={result!r}"

    def test_output_is_non_empty(self):
        assert _run().get("output"), "the run surfaced an empty output"

    def test_node_history_is_populated(self):
        history = _run().get("node_history")
        assert isinstance(history, list) and history
        assert all(isinstance(name, str) for name in history)

    def test_backbone_slot_order(self):
        """The request boundary runs before the domain slot, which runs before
        the output boundary — as a strict ordered subsequence."""
        history = _run().get("node_history", [])
        ordered = ["PreProcessNode", _MAIN_SLOT_NODE, "PostProcessNode"]
        for name in ordered:
            assert name in history, f"Expected backbone slot {name!r} in {history!r}"
        positions = [history.index(name) for name in ordered]
        assert positions == sorted(positions), f"Backbone slots executed out of order: {ordered} at {positions}"

    def test_full_backbone_sequence(self):
        history = _run().get("node_history", [])
        assert history == _EXPECTED_ORDER, (
            "node_history does not match the canonical backbone order.\n"
            f"  expected: {_EXPECTED_ORDER}\n"
            f"  actual:   {history}"
        )
