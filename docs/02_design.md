# Design Document — RET-C2-123

**Template ID:** RET-C2-123
**Template Name:** RetailOmnichannelCustomer360AggregationAgent
**Category:** Cat 2 (domain-specific pipeline)
**Industry:** RET (Retail)
**Inheritance:** `AgentBaseGraph` (framework base class — direct inheritance)
**Pattern:** two-layer nested (outer backbone + inner aggregation workflow)

| Role | Class |
|---|---|
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |

## 1. Overview

RetailOmnichannelCustomer360AggregationAgent takes retail customer records from
point-of-sale, e-commerce, CRM and loyalty-card systems and returns a single,
identity-resolved **Customer 360** profile. It gives downstream retail agents a
normalized customer backbone: per-channel records are mapped to one schema, the
same physical customer is linked across channels, and a profile — channel
coverage, behavioural metrics, coarse segments — is emitted.

Direct resident identifiers (names, addresses, phone numbers, e-mail addresses,
card numbers, resident numbers) never reach state. The request boundary replaces
every identity value with a non-reversible linkage token and refuses any
identifier shape in a field that would be rendered.

## 2. Architecture

```
OUTER backbone (AgentBaseGraph — fixed; add_edges() NOT overridden)
  START -> initialize -> pre_process -> main -> post_process -> finalize -> END
                                         |
                                         v  (Customer360GraphNode.get_subgraph)
INNER aggregation workflow (DomainWorkflowGraph : BaseGraph)
  START -> normalize_sources -> resolve_identity -> unify_profile -> END
```

- The `main` slot is **`Customer360GraphNode`**, a `GraphNode` subclass. It
  delegates the whole workflow to `DomainWorkflowGraph`.
- `merge_output()` maps the inner profile into outer state as both
  `customer_360_profile` and **`result`** — the field the output gate reads and
  the envelope surfaces.
- The inner graph is constructed with the live runtime tuning; its domain nodes
  take no constructor arguments and read their tuning from seeded state.

### Directory layout

| Path | Role |
|---|---|
| `src/graph/graph.py` | outer graph, main-slot node, runtime-config loader |
| `src/graph/domain_workflow_graph.py` | inner graph: three steps, linear edges |
| `src/graph/context_bridge.py` | carries the validated request across the boundary |
| `src/services/caller_contract.py` | the single definition of what a caller may send |
| `src/nodes/pre_process_node.py` | request boundary |
| `src/nodes/post_process_node.py` | output boundary |
| `src/nodes/normalize_sources_node.py` | aggregation step 1 |
| `src/nodes/resolve_identity_node.py` | aggregation step 2 |
| `src/nodes/unify_profile_node.py` | aggregation step 3 |
| `src/schemas/state.py` | flat `State(AgentState)` + JSON helpers |
| `config/agent.yaml` | static manifest (identity, entry point, requirements) |
| `config/config.yaml` | runtime parameters |
| `src/api/server.py` | HTTP adapter |

## 3. Request contract

Two channels reach the agent, and one of them is the supported route for
records.

| Field | Content |
|---|---|
| `input` | the request string. May also carry the records as a JSON envelope. |
| `input_context` | structured invocation parameters: `sources`, `channel_filter`. **The records belong here.** |

The platform rewrites personal-data shapes out of the request string at every
node boundary, and its name heuristic reads title-case retail proper nouns —
store names, product names, brands — as personal names. Records embedded in the
request string therefore arrive with those attributes replaced by a mask token;
records sent as structured parameters arrive intact. Both routes are screened
identically; only the structured route is lossless. The contract reports which
route was used (`record_source`).

Rules that hold for every field:

| Rule | Detail |
|---|---|
| Closed channel set | `pos`, `ec`, `crm`, `loyalty`. An unknown channel is refused, not bucketed as "unknown" — channel coverage is what the segments are derived from. |
| Inert rendered fields | record identifiers, event labels and attribute values are restricted to `[A-Za-z0-9_.-]{1,64}`; attribute keys to `[a-z0-9_]{1,32}`. Caller free text in a rendered field is output injection. |
| Finite numbers | every caller number — real or string-shaped — goes through a finite + bounded parser. `NaN` and the infinities parse through `float()` and compare False against every bound, so an unchecked value passes exactly the check it should fail. |
| Structural caps | 50 records, 200 events and 32 attributes per record, 256 characters per identity value, 8000 characters of request text, 256 KB of structured parameters. |
| Identity values | accepted on the identity fields, replaced by a linkage token, never rendered. |
| Disallowed instructions | chat-template control tokens are screened as a class, raw and after markup and invisible characters are stripped; instruction-shaped phrases require a verb and its object so ordinary retail prose is unaffected. |
| Credential shapes | refused at the adapter (structured parameters) and at the request boundary (request string), using the platform's own detector. |
| Refusals | name the field, never repeat the value. |

## 4. Linkage tokens

An identity value becomes `tok_<field>_<digest>`, where the digest is a keyed
hash of the FIELD and the VALUE. Two properties matter:

- **A named digest, not the interpreter's built-in hash.** The built-in is
  salted per process, so a token derived from it changes on every restart: a
  unified customer id stored yesterday would resolve to nothing today, while
  every test inside one process still passed.
- **The channel is deliberately NOT part of the material.** Cross-channel
  matching is what the agent exists for. A channel-scoped token hashes the same
  e-mail address on a store record and a web record to two different values, so
  no multi-channel request can ever link and every profile reports itself as
  unlinked. Privacy is carried by the digest being non-reversible, not by mixing
  the channel in.

## 5. Node design

| Node | Layer | Responsibility | Writes |
|---|---|---|---|
| `PreProcessNode` | outer pre_process | validate the whole request against the caller contract; refuse with the field named | `validated_input`, `caller_contract`, `status` |
| `Customer360GraphNode` | outer main | bridge the validated contract, run the inner workflow, map its profile to `result` | `customer_360_profile`, `result`, `status` |
| `NormalizeSourcesNode` | inner 1 | publish the validated records in the unified schema; honour the record cap | `normalized_sources` |
| `ResolveIdentityNode` | inner 2 | link records by token equality; report `unlinked` below the declared confidence floor | `resolved_identity` |
| `UnifyProfileNode` | inner 3 | assemble the profile; emit the terminal success status that routes the run through the output gate | `customer_360_profile`, `status` |
| `PostProcessNode` | outer post_process | scan the released surface; on a violation clear every output-bearing field and return a truthy notice | `formatted_output`, `result`, `customer_360_profile`, `status` |

Every node is a `FunctionNode` subclass returning a partial update, and every
node declares `TrustLevel.VERIFIED_EXTERNAL` — the level the manifest admits.
A node demanding the internal level could not be reached by any caller the
entry point admits, so every request would fail at that node's trust gate.

Each aggregation step declines to overwrite an error status. A step that failed
leaves the state it should have written empty, so anything assembled downstream
describes data that never arrived; emitting success there would present an empty
profile as a real answer.

## 6. State

Flat `State(AgentState)` (`src/schemas/state.py`). Structured fields are stored
as JSON strings — checkpoints are serialized with msgpack, and a bare container
there corrupts silently.

| Field | Producer | Notes |
|---|---|---|
| `validated_input` | PreProcessNode | screened request string |
| `caller_contract` | PreProcessNode | JSON — validated records, filters, source |
| `aggregation_config` | inner initial-state hook | JSON — the live tuning |
| `normalized_sources` | NormalizeSourcesNode | JSON — unified records |
| `resolved_identity` | ResolveIdentityNode | JSON — linkage result |
| `customer_360_profile` | UnifyProfileNode | JSON — the profile |
| `trace_id` / `correlation_id` | framework | tracing only |

No credential, secret or model object is present in state.

## 7. Runtime configuration

`config/config.yaml` carries the live parameters. Every key has a reader.

| Key | Reader | Effect |
|---|---|---|
| `max_retry` | the backbone | retry ceiling; validated at compile time |
| `timeout_s` | the runtime | request timeout |
| `aggregation.max_records` | normalize step | records beyond the cap are not aggregated; `truncated` is reported |
| `aggregation.high_frequency_events` | unify step | the event count at which a profile is segmented as high frequency |
| `aggregation.min_link_confidence` | identity step | below it, the match is reported as `unlinked` |

The path a value travels: the registry (or the HTTP adapter, which mirrors it)
loads the file and passes it to the graph constructor; the main slot forwards
the aggregation block to the inner graph; the inner graph's initial-state hook
seeds it into inner state. Node `execute()` methods take no config argument, so
state seeding is the only route a declared value can reach a domain node. The
integration suite proves three separate parameters visibly change the released
profile.

## 8. The output boundary

The stated invariant: **no direct resident identifier and nothing
credential-shaped leaves the agent.**

The agent renders no monetary aggregates — the profile carries channel names,
event counts, coarse segments, a confidence figure and caller-supplied inert
attribute codes — so a monetary rounding grid does not apply. The identifier
invariant above is what is enforced instead, and it is enforced on every
representation: the rendered profile and the structured payload behind it,
nested mappings and lists included.

The credential half **delegates to the platform's own detector** rather than
keeping a private pattern list. The platform scans every value of every node
result and RAISES when it finds a credential; the wrapper then discards the
gate node's whole return value — the clearing included — and the envelope falls
back to the un-gated profile still in state. A local list narrower than the
platform's is therefore not a weaker filter but a containment bypass.

Two independent layers, each with its own audit event:

- **inbound** — the request boundary refuses identifier shapes in rendered
  fields and tokenises identity values;
- **outbound** — this gate refuses to release anything still matching.

Both read ONE pattern definition, so they cannot drift apart.

### Containment

`AgentBaseGraph.get_output` resolves the released output as
`formatted_output or result`, with no status check. Three consequences, all
handled:

1. A falsy `formatted_output` re-opens the fallback, so the withheld notice is
   truthy.
2. A gate that raises leaks, because the wrapper turns an exception into a bare
   error update that clears nothing — which is why the gate's detector matches
   the platform's rather than being narrower than it.
3. `get_output` is overridden so that on any non-success status the output
   resolves to the gate's notice or to None, never to `result`.

No third layer re-scans the success path. One would contain a leak by itself and
thereby make the gate's own scan unfalsifiable.

The gate reads `result` and `customer_360_profile` — the two keys the main slot
publishes into outer state. It deliberately does not read the inner-graph keys:
a layer reading a key that does not exist at its level compares against nothing
on every real invocation, and passes a hand-built fixture while being dead in
production.
