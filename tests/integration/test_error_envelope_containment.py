# Containment of the error envelope, driven end to end.
#
# The envelope resolves the released output as `formatted_output or result`,
# with no status check. Three properties follow, and all three are live here:
#
#   1. a falsy formatted_output ACTIVATES the fallback, so "" as a "withheld"
#      marker produces the exact disclosure it was written to prevent;
#   2. an output gate that RAISES leaks, because the wrapper turns an exception
#      into a bare error update that clears nothing;
#   3. the platform's own credential scan raises on the gate node's return
#      value and the wrapper then discards that node's whole update — the
#      clearing included — so a gate whose pattern set is narrower than the
#      platform's is a containment bypass rather than a weaker filter.
#
# The fault is injected on the DATA path, never on the gate: the main slot's
# merge_output is made to publish a drifted profile, exactly as a transport
# returning a credential-shaped value would. Patching the gate would test the
# patch, not the agent.
#
# Deterministic — no model, no network.

import json
import os

import pytest

from tests.integration.asgi import Client

_TOKEN = "test-caller-token"
os.environ["INVOKE_AUTH_TOKEN"] = _TOKEN

from src.api.server import app  # noqa: E402
from src.graph.graph import Customer360GraphNode  # noqa: E402
from src.nodes.post_process_node import BLOCKED_NOTICE  # noqa: E402

client = Client(app)
AUTH = {"Authorization": f"Bearer {_TOKEN}"}

_REQUEST = "aggregate the omnichannel profile"
_RECORDS = {
    "sources": [
        {"channel": "pos", "external_id": "pos-a1", "store": "shibuya_flagship", "events": ["visit", "purchase"]},
        {"channel": "ec", "external_id": "ec-b2", "site": "web-jp", "events": ["cart"]},
    ]
}

# A shape the platform's own detector recognises. Probing with one it does not
# (a GitHub token, say) would report quoting as safe when it is not.
_LEAKED = "AKIAIOSFODNN7EXAMPLE"

_DRIFTED_PROFILE = json.dumps(
    {
        "unified_customer_id": "cust_10489dbd991c3d57",
        "channels": ["pos", "ec"],
        "segments": ["omnichannel_shopper"],
        "metrics": {"total_events": 3, "channel_count": 2, "record_count": 2, "linked_record_count": 1},
        "attributes": {"upstream_note": _LEAKED},
        "confidence": 1.0,
        "match_method": "token_equality",
    }
)


@pytest.fixture
def drifted_main_slot(monkeypatch):
    """Make the DATA path publish a profile the gate must refuse.

    merge_output is where the inner result becomes the released answer, so a
    drift here is the same shape as an upstream system returning a
    credential-bearing value — a real data-path fault, not a doctored gate.
    """

    def _drifted(self, state, sub_result):  # noqa: ANN001
        return {
            "customer_360_profile": _DRIFTED_PROFILE,
            "result": _DRIFTED_PROFILE,
            "status": sub_result.get("status"),
        }

    monkeypatch.setattr(Customer360GraphNode, "merge_output", _drifted)
    return _drifted


def _invoke():
    return client.post("/invoke", json={"input": _REQUEST, "input_context": _RECORDS}, headers=AUTH)


class TestDriftedProfileIsContained:
    def test_envelope_carries_no_leaked_value(self, drifted_main_slot):
        assert _LEAKED not in _invoke().text

    def test_status_is_error(self, drifted_main_slot):
        assert _invoke().json()["status"] == "error"

    def test_output_is_the_withheld_notice(self, drifted_main_slot):
        """The notice is TRUTHY, so the fallback to result stays closed.

        It also proves the block came from the gate rather than from the
        platform raising: when the platform raises, this node's update is
        discarded and there is no notice to observe.
        """
        assert _invoke().json()["output"] == BLOCKED_NOTICE

    def test_block_happened_at_the_gate_not_upstream(self, drifted_main_slot):
        history = _invoke().json()["node_history"]
        assert "PostProcessNode" in history

    def test_envelope_carries_no_traceback_or_source_path(self, drifted_main_slot):
        body = _invoke().text
        assert "Traceback" not in body
        assert "/src/" not in body

    def test_envelope_carries_no_profile_content(self, drifted_main_slot):
        assert "unified_customer_id" not in str(_invoke().json()["output"])


class TestCleanPathControl:
    """Without the drift the same request still produces its real answer.

    A gate that refused everything would pass every assertion above; this is
    what stops that from counting as containment.
    """

    def test_same_request_succeeds_when_the_data_path_is_clean(self):
        response = _invoke()
        assert response.status_code == 200
        assert response.json()["status"] == "success"
        profile = json.loads(response.json()["output"])
        assert profile["metrics"]["record_count"] == 2

    def test_gate_node_runs_on_the_clean_path_too(self):
        assert "PostProcessNode" in _invoke().json()["node_history"]
