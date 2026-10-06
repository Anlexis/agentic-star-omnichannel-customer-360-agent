# End-to-end through the real HTTP entry point.
#
# The suite that only drove nodes in isolation is what let a deployed agent
# fail every request while staying green: it ran at a trust level no external
# caller can hold, so the trust gate never fired in a test. Everything here goes
# through the ASGI application with Bearer auth, at the trust level the manifest
# declares.
#
# Covered:
#   * an authenticated request produces a real profile computed from the caller's
#     records — not a fixed baseline;
#   * a request without the caller credential is refused;
#   * a declared runtime value visibly changes the released profile;
#   * a credential-shaped structured parameter is refused with the field named,
#     rather than failing opaquely inside the first node;
#   * the error envelope carries no released text, no traceback, no source path.
#
# Deterministic — no model, no network.

import json
import os

import pytest

from tests.integration.asgi import Client

_TOKEN = "test-caller-token"
os.environ["INVOKE_AUTH_TOKEN"] = _TOKEN

from src.api.server import app  # noqa: E402

client = Client(app)
AUTH = {"Authorization": f"Bearer {_TOKEN}"}

_REQUEST = "aggregate the omnichannel profile"
_RECORDS = {
    "sources": [
        {
            "channel": "pos",
            "external_id": "pos-a1",
            "store": "shibuya_flagship",
            "email": "hanako@example.com",
            "events": ["visit", "purchase"],
        },
        {
            "channel": "ec",
            "external_id": "ec-b2",
            "site": "web-jp",
            "email": "hanako@example.com",
            "events": ["cart", "purchase", "review"],
        },
        {"channel": "loyalty", "external_id": "ly-c3", "tier": "gold", "membership_id": "M-778", "events": ["earn"]},
    ]
}


def _invoke(body, headers=AUTH):
    return client.post("/invoke", json=body, headers=headers)


def _profile(response):
    return json.loads(response.json()["output"])


class TestAuthenticatedRequestDoesRealWork:
    def test_health(self):
        assert client.get("/health").status_code == 200

    def test_request_succeeds_at_the_declared_trust_level(self):
        response = _invoke({"input": _REQUEST, "input_context": _RECORDS})
        assert response.status_code == 200
        assert response.json()["status"] == "success", response.json()

    def test_output_is_non_empty(self):
        assert _invoke({"input": _REQUEST, "input_context": _RECORDS}).json()["output"]

    def test_profile_is_computed_from_the_caller_records(self):
        profile = _profile(_invoke({"input": _REQUEST, "input_context": _RECORDS}))
        assert set(profile["channels"]) == {"pos", "ec", "loyalty"}
        assert profile["metrics"]["total_events"] == 6
        assert profile["metrics"]["record_count"] == 3
        assert "omnichannel_shopper" in profile["segments"]
        assert "loyalty_member" in profile["segments"]

    def test_a_different_request_produces_a_different_profile(self):
        """The control against a stub path that emits one baseline whatever it is sent."""
        other = {"sources": [{"channel": "ec", "external_id": "ec-1", "events": ["view"]}]}
        first = _profile(_invoke({"input": _REQUEST, "input_context": _RECORDS}))
        second = _profile(_invoke({"input": _REQUEST, "input_context": other}))
        assert first != second
        assert second["channels"] == ["ec"]
        assert second["segments"] == ["online_only"]

    def test_cross_channel_linkage_actually_links(self):
        """Two channels carrying the same address resolve to one customer.

        This is the capability the agent exists for. A linkage token derived
        from the channel as well as the value would hash the same address to two
        values and report every multi-channel profile as unlinked.
        """
        profile = _profile(_invoke({"input": _REQUEST, "input_context": _RECORDS}))
        assert profile["match_method"] == "token_equality"
        assert profile["metrics"]["linked_record_count"] >= 2
        assert profile["unified_customer_id"].startswith("cust_")

    def test_no_direct_identifier_reaches_the_released_profile(self):
        body = _invoke({"input": _REQUEST, "input_context": _RECORDS}).text
        assert "hanako@example.com" not in body

    def test_backbone_reaches_the_output_gate(self):
        history = _invoke({"input": _REQUEST, "input_context": _RECORDS}).json()["node_history"]
        assert history == [
            "InitializeNode",
            "PreProcessNode",
            "Customer360GraphNode",
            "PostProcessNode",
            "FinalizeNode",
        ]


class TestCallerAuthentication:
    def test_missing_credential_is_refused(self):
        assert _invoke({"input": _REQUEST, "input_context": _RECORDS}, headers={}).status_code == 401

    def test_wrong_credential_is_refused(self):
        response = _invoke({"input": _REQUEST, "input_context": _RECORDS}, headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401

    def test_refusal_does_not_say_which_way_it_failed(self):
        absent = _invoke({"input": _REQUEST}, headers={}).json()["detail"]
        wrong = _invoke({"input": _REQUEST}, headers={"Authorization": "Bearer wrong"}).json()["detail"]
        assert absent == wrong


class TestRuntimeConfigurationIsLive:
    """A declared value must change the released profile, or it is decoration."""

    def _with_high_frequency_events(self, threshold):
        import src.graph.graph as graph_module

        original = graph_module.runtime_config
        graph_module.runtime_config = lambda: {
            "max_retry": 3,
            "timeout_s": 30,
            "aggregation": {
                "max_records": 50,
                "high_frequency_events": threshold,
                "min_link_confidence": 0.5,
            },
        }
        try:
            return _profile(_invoke({"input": _REQUEST, "input_context": _RECORDS}))
        finally:
            graph_module.runtime_config = original

    def test_declared_threshold_changes_the_segments(self):
        below = self._with_high_frequency_events(3)  # 6 events >= 3
        above = self._with_high_frequency_events(99)  # 6 events < 99
        assert "high_frequency" in below["segments"]
        assert "high_frequency" not in above["segments"]

    def test_declared_record_cap_is_enforced(self):
        import src.graph.graph as graph_module

        original = graph_module.runtime_config
        graph_module.runtime_config = lambda: {
            "aggregation": {"max_records": 1, "high_frequency_events": 10, "min_link_confidence": 0.5}
        }
        try:
            profile = _profile(_invoke({"input": _REQUEST, "input_context": _RECORDS}))
        finally:
            graph_module.runtime_config = original
        assert profile["metrics"]["record_count"] == 1
        assert profile["truncated"] is True

    def test_declared_link_floor_downgrades_a_weak_match(self):
        import src.graph.graph as graph_module

        original = graph_module.runtime_config
        graph_module.runtime_config = lambda: {
            "aggregation": {"max_records": 50, "high_frequency_events": 10, "min_link_confidence": 0.99}
        }
        try:
            profile = _profile(_invoke({"input": _REQUEST, "input_context": _RECORDS}))
        finally:
            graph_module.runtime_config = original
        assert profile["match_method"] == "unlinked"


class TestStructuredParameterScreen:
    """A credential-shaped structured parameter cannot succeed either way.

    The platform's output gate scans every value of every node result, and the
    backbone's first node copies the structured parameters verbatim into its
    own result — so such a value fails the FIRST node with nothing naming the
    cause. Refusing it at the adapter turns that into something a caller can act
    on.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "AKIAIOSFODNN7EXAMPLE",
            "sk-TESTKEY1234567890abcdefghij",
            "postgresql://db.internal.example:5432/retail_analytics",
        ],
    )
    def test_credential_shaped_parameter_is_refused(self, value):
        response = _invoke(
            {
                "input": _REQUEST,
                "input_context": {"sources": [{"channel": "pos", "external_id": "pos-a1", "note": value}]},
            }
        )
        assert response.status_code == 400
        assert "input_context" in response.json()["detail"]

    def test_refusal_never_echoes_the_value(self):
        value = "AKIAIOSFODNN7EXAMPLE"
        response = _invoke(
            {
                "input": _REQUEST,
                "input_context": {"sources": [{"channel": "pos", "external_id": "pos-a1", "note": value}]},
            }
        )
        assert value not in response.text

    def test_ordinary_retail_text_on_the_same_field_still_passes(self):
        """The other direction: the screen must not block real work."""
        response = _invoke(
            {
                "input": _REQUEST,
                "input_context": {"sources": [{"channel": "pos", "external_id": "pos-a1", "note": "gold_tier"}]},
            }
        )
        assert response.status_code == 200
        assert response.json()["status"] == "success"

    def test_oversized_parameters_are_refused(self):
        response = _invoke(
            {
                "input": _REQUEST,
                "input_context": {"sources": [{"channel": "pos", "external_id": "pos-a1", "note": "x" * 300_000}]},
            }
        )
        assert response.status_code == 413


class TestErrorEnvelopeContainment:
    """The error envelope must carry no released text, traceback, or path."""

    def _refused(self):
        return _invoke(
            {
                "input": _REQUEST,
                "input_context": {
                    "sources": [{"channel": "pos", "external_id": "pos-a1", "note": "hanako@example.com"}]
                },
            }
        )

    def test_refused_request_returns_an_error_status(self):
        assert self._refused().json()["status"] == "error"

    def test_refused_request_carries_no_profile(self):
        payload = self._refused().json()
        assert "unified_customer_id" not in str(payload.get("output"))

    def test_refused_request_never_echoes_the_value(self):
        assert "hanako@example.com" not in self._refused().text

    def test_refused_request_carries_no_traceback_or_path(self):
        body = self._refused().text
        assert "Traceback" not in body
        assert "/src/" not in body
        assert ".py" not in body

    def test_refusal_names_the_field_so_the_caller_can_act(self):
        assert "attributes.note" in str(self._refused().json()["output"])
