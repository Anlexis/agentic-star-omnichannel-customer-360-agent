# Test Specification — RET-C2-123

**Template:** RET-C2-123 — RetailOmnichannelCustomer360AggregationAgent
**Category:** Cat 2 (two-layer nested) · **Inheritance:** `AgentBaseGraph`

The tables below describe the tests that ship in this repository. Every case is
deterministic: no model call, no network.

## 1. Test files

| Layer | Target | File |
|---|---|---|
| Unit | the caller contract (all accepted and refused shapes) | `tests/unit/test_caller_contract.py` |
| Unit | graph composition, config plumbing, envelope resolution | `tests/unit/test_graph_composition.py` |
| Unit | the two backbone slots the template owns | `tests/unit/test_pre_post_process_nodes.py` |
| Unit | `NormalizeSourcesNode` | `tests/unit/test_normalize_sources_node.py` |
| Unit | `ResolveIdentityNode` | `tests/unit/test_resolve_identity_node.py` |
| Unit | `UnifyProfileNode` | `tests/unit/test_unify_profile_node.py` |
| Unit | framework compliance | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| Boundary | the request boundary refuses on its own account | `tests/proof_of_boundary/test_request_boundary.py` |
| Boundary | the output boundary and its clearing | `tests/proof_of_boundary/test_output_boundary.py` |
| Boundary | backbone invoke order | `tests/proof_of_boundary/test_pb_invoke_order.py` |
| Boundary | import isolation | `tests/proof_of_boundary/test_import_isolation.py` |
| Boundary | state safety | `tests/proof_of_boundary/test_state_safety.py` |
| Boundary | human-review interrupt propagation | `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` |
| Integration | the real HTTP entry point, end to end | `tests/integration/test_invoke_end_to_end.py` |
| Integration | error-envelope containment | `tests/integration/test_error_envelope_containment.py` |

`tests/integration/asgi.py` is a small synchronous driver for the application
under test, not a test module.

## 2. The caller contract

| Case | Input | Expected |
|---|---|---|
| CC-01 | records as structured invocation parameters | accepted; every record normalized and tokenised |
| CC-02 | records as a JSON envelope in the request string | accepted; `record_source` reports `input` |
| CC-03 | both channels populated | the structured parameters win |
| CC-04 | empty, whitespace-only or non-string request | refused, field named |
| CC-05 | request over the character cap | refused |
| CC-06 | more records than the cap | refused |
| CC-07 | more events or attributes than the per-record cap | refused |
| CC-08 | unknown channel name | refused (the channel set is closed) |
| CC-09 | `NaN`, `Infinity`, `-Infinity`, an overflowing literal, a boolean, a non-numeric string, as a real number AND as a string | refused, field named |
| CC-10 | a finite in-range number | accepted |
| CC-11 | chat-template control tokens: `<\|im_start\|>`, `<\|system\|>`, `[INST]`, `<<SYS>>`, `<system>` | refused |
| CC-12 | instruction-shaped phrases | refused |
| CC-13 | a directive spliced with markup, or hidden with zero-width characters | refused |
| CC-14 | a hostile field NAME | refused |
| CC-15 | ordinary retail prose containing "rules", "instructions", "assistant" | accepted |
| CC-16 | a direct-identifier shape in a field that would be rendered | refused |
| CC-17 | an identity value on an identity field | accepted; replaced by a linkage token, raw value dropped |
| CC-18 | a credential-shaped value in the request string | refused, field named |
| CC-19 | any refusal | names the field, never repeats the value |

## 3. Linkage tokens

| Case | Expected |
|---|---|
| LT-01 | the same identity value yields the same token, pinned to a literal (a per-process salt cannot reproduce it) |
| LT-02 | the same value on two channels yields ONE token, so multi-channel records link |
| LT-03 | different values yield different tokens |
| LT-04 | the token contains no part of the raw value |
| LT-05 | the unified customer id is reproducible and independent of token order |

## 4. Aggregation steps

| Case | Target | Expected |
|---|---|---|
| AG-01 | normalize | records published in the unified schema; channels in first-seen order |
| AG-02 | normalize | the declared record cap truncates and reports `truncated` |
| AG-03 | normalize | an unusable cap falls back to the declared default |
| AG-04 | resolve | records sharing a token link; `match_method` is `token_equality` |
| AG-05 | resolve | records sharing nothing do not link |
| AG-06 | resolve | below the declared confidence floor the match is reported as `unlinked` |
| AG-07 | unify | metrics, merged attributes and segments reflect the records |
| AG-08 | unify | the declared frequency threshold changes the segments |
| AG-09 | every step | an earlier failure is carried forward, never overwritten with success |

## 5. The output boundary

| Case | Released content | Expected |
|---|---|---|
| OB-01 | every credential shape the platform detector recognises | refused by this gate first, with a reason label |
| OB-02 | a credential-assignment line or a private-key header | refused |
| OB-03 | a leak nested inside a mapping or a list | refused |
| OB-04 | a clean nested structure | released (the control that keeps OB-03 honest) |
| OB-05 | a direct-identifier shape | refused |
| OB-06 | a linkage token (long hex run) | released — not mistaken for an identifier |
| OB-07 | a violation | every output-bearing field is PRESENT in the returned update and cleared |
| OB-08 | a violation | the replacement notice is truthy |
| OB-09 | nothing assembled | a truthy notice, never an empty value |
| OB-10 | a clean profile | released unchanged |

## 6. End to end, through the HTTP entry point

| Case | Expected |
|---|---|
| E2E-01 | an authenticated request at the declared trust level succeeds with a non-empty profile |
| E2E-02 | the profile is computed from the caller's records; a different request gives a different profile |
| E2E-03 | two channels carrying the same identity value resolve to one customer |
| E2E-04 | no direct identifier appears anywhere in the response |
| E2E-05 | the backbone reaches the output gate (`node_history` pinned) |
| E2E-06 | a missing or wrong caller credential is refused, indistinguishably |
| E2E-07 | a declared runtime value visibly changes the released profile (three separate parameters) |
| E2E-08 | a credential-shaped structured parameter is refused with the field named, and never echoed |
| E2E-09 | ordinary retail text on the same field still succeeds |
| E2E-10 | oversized structured parameters are refused |
| E2E-11 | a refused request carries no profile, no traceback and no source path |

## 7. Error-envelope containment

The envelope resolves the released output as `formatted_output or result`, with
no status check, so a gate that raised — or that set an error status without
clearing — would still ship the un-gated profile inside the error envelope.

| Case | Expected |
|---|---|
| EC-01 | with a drifted profile on the DATA path, the envelope carries no leaked value |
| EC-02 | the status is an error and the output is the withheld notice |
| EC-03 | the gate node appears in `node_history`, proving the block happened there |
| EC-04 | no traceback and no source path reach the surface |
| EC-05 | without the drift, the same request still returns its real answer |

The fault is injected on the data path, never on the gate: patching the gate
would test the patch rather than the agent.

## 8. Release thresholds

- Every case above passes under the framework wheel the build pipeline installs.
- The output invariant holds for every representation probed, in both
  directions: leak forms refused, ordinary retail values released unchanged.
- No direct resident identifier is present in any state field at any stage.
