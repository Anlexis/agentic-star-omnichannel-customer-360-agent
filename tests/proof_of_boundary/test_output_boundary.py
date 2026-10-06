# The output boundary — what the agent refuses to release, and what it clears
# when it refuses.
#
# The stated invariant: no direct resident identifier and nothing
# credential-shaped leaves the agent. Enforced on the whole released surface,
# nested structures included.
#
# Two properties are pinned here that a gate can appear to have without having:
#
#   * DETECTOR PARITY. The platform scans every value of every node result for
#     credentials and RAISES when it finds one; the wrapper then discards this
#     node's whole return value, the clearing included, and the envelope falls
#     back to the un-gated profile still in state. So a local pattern list
#     narrower than the platform's is not a weaker gate — it is a bypass. Every
#     shape the platform recognises is probed here.
#
#   * CLEARING, ASSERTED AS PRESENCE AND EMPTINESS. Partial state updates are
#     MERGED, so omitting a key leaves the old value in state — and
#     `assert not result.get(field)` then passes on a gate that cleared
#     nothing. Each assertion below requires the key to be in the returned
#     update AND to carry the cleared value.
#
# The replacement notice is TRUTHY on purpose: the envelope resolves the output
# as `formatted_output or result`, so a falsy replacement re-opens the exact
# fallback the clearing exists to close.
#
# Deterministic — no model, no network.

import json

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.security.credential_detector import detect_credentials

from src.nodes.post_process_node import (
    BLOCKED_NOTICE,
    OUTPUT_BEARING_FIELDS,
    PostProcessNode,
    security_gate_output,
)

# Simulated shapes — not real credentials.
_SHAPES = {
    "openai_key": "sk-TESTKEY1234567890abcdefghij",
    "stripe_key": "sk_live_TESTKEY1234567890abcd",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c",
    "aws_key": "AKIAIOSFODNN7EXAMPLE",
    "bearer_token": "Bearer abcdefghijklmnopqrstuvwxyz",
    "conn_string": "postgresql://db.internal.example:5432/retail_analytics",
}

_CLEAN_PROFILE = json.dumps(
    {
        "unified_customer_id": "cust_10489dbd991c3d57",
        "channels": ["pos", "ec", "loyalty"],
        "segments": ["omnichannel_shopper", "loyalty_member"],
        "metrics": {"total_events": 12, "channel_count": 3, "record_count": 3, "linked_record_count": 2},
        "attributes": {"store": "shibuya_flagship", "tier": "gold"},
        "confidence": 0.83,
        "match_method": "token_equality",
    }
)


def _profile_with(value: str) -> str:
    return json.dumps({"unified_customer_id": "cust_1", "attributes": {"note": value}})


def _block(value: str) -> dict:
    state = {"result": _profile_with(value), "customer_360_profile": _profile_with(value)}
    return PostProcessNode().execute(state)


class TestDetectorParity:
    """Every shape the platform recognises must be refused HERE first."""

    @pytest.mark.parametrize("name,value", sorted(_SHAPES.items()))
    def test_platform_recognises_the_probe(self, name, value):
        """Guard on the probes themselves: a shape the platform ignores would
        make the parity assertion below vacuously true."""
        assert detect_credentials(value), f"probe for {name} no longer matches"

    @pytest.mark.parametrize("name,value", sorted(_SHAPES.items()))
    def test_gate_refuses_every_platform_shape(self, name, value):
        assert security_gate_output(_profile_with(value)) is not None

    @pytest.mark.parametrize("name,value", sorted(_SHAPES.items()))
    def test_block_is_attributed_to_this_gate(self, name, value):
        """The block must come from the node, not from the platform raising.

        When the platform raises instead, this node's whole return value is
        discarded — so there is no error status to observe here at all.
        """
        result = _block(value)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"], "a block must name its reason"

    def test_gate_also_covers_shapes_the_platform_does_not(self):
        assert security_gate_output("password = hunter2hunter2") is not None
        assert security_gate_output("-----BEGIN RSA PRIVATE KEY-----") is not None

    def test_reason_is_a_label_never_the_matched_text(self):
        result = _block(_SHAPES["aws_key"])
        assert _SHAPES["aws_key"] not in str(result)
        assert "aws_key" in result["error_log"][0]


class TestNestedSurface:
    def test_nested_leak_is_found(self):
        nested = {"payload": {"args": {"note": _SHAPES["aws_key"]}}}
        assert security_gate_output(nested) is not None

    def test_leak_inside_a_list_is_found(self):
        assert security_gate_output([{"note": _SHAPES["jwt"]}]) is not None

    def test_clean_nested_structure_passes(self):
        """The control that proves the scan is not simply refusing everything."""
        assert security_gate_output({"payload": {"args": {"note": "shibuya_flagship"}}}) is None


class TestPersonalDataIsRefusedOutbound:
    @pytest.mark.parametrize("value", ["hanako@example.com", "1234-5678-9012", "03-1234-5678"])
    def test_identifier_shapes_are_refused(self, value):
        assert security_gate_output(_profile_with(value)) is not None

    def test_linkage_tokens_are_not_mistaken_for_identifiers(self):
        """Tokens carry long hex runs; they must not trip the identifier scan."""
        tokens = json.dumps({"records": [{"email_token": "tok_email_7c68626aef7e323b"}]})
        assert security_gate_output(tokens) is None

    def test_a_real_profile_passes_the_gate(self):
        assert security_gate_output(_CLEAN_PROFILE) is None


class TestClearingOnViolation:
    """Presence AND emptiness — merged updates make omission look like clearing."""

    @pytest.mark.parametrize("field", OUTPUT_BEARING_FIELDS)
    def test_every_output_bearing_field_is_present_in_the_update(self, field):
        result = _block(_SHAPES["aws_key"])
        assert field in result, (
            f"{field} missing from the returned update — a merged update leaves "
            "the previous value in state, so omission is not clearing"
        )

    def test_profile_field_is_emptied(self):
        result = _block(_SHAPES["aws_key"])
        assert result["customer_360_profile"] is None

    def test_released_text_is_replaced_not_merely_flagged(self):
        result = _block(_SHAPES["aws_key"])
        assert _SHAPES["aws_key"] not in str(result["result"])
        assert _SHAPES["aws_key"] not in str(result["formatted_output"])

    def test_replacement_is_truthy(self):
        """A falsy replacement re-opens the envelope's fallback to result."""
        result = _block(_SHAPES["aws_key"])
        assert result["formatted_output"] == BLOCKED_NOTICE
        assert bool(result["formatted_output"])

    def test_status_is_error(self):
        assert _block(_SHAPES["aws_key"])["status"] == AgentStatus.ERROR.value


class TestCleanAndEmptyPaths:
    def test_clean_profile_passes_unchanged(self):
        result = PostProcessNode().execute({"result": _CLEAN_PROFILE})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"] == _CLEAN_PROFILE

    def test_empty_result_still_yields_a_truthy_notice(self):
        """An empty formatted_output would let the envelope fall back to result."""
        result = PostProcessNode().execute({"result": ""})
        assert bool(result["formatted_output"])
        assert result["result"] is None
        assert result["status"] == AgentStatus.SUCCESS.value
