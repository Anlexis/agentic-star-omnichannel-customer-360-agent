# Omnichannel Customer 360 Agent

AI agent for aggregating omnichannel customer data into a single customer view, built with Agentic Star.

> **Category**: Cat 2 (a domain-specific pipeline for one job-to-be-done)
> **Industry**: Retail
> **Template ID**: RET-C2-123

## Overview

A retailer's view of one customer is split across systems that were never
designed to agree: the point-of-sale record from a store visit, the e-commerce
order, the CRM contact, the loyalty-card account. Each holds part of the
behaviour and its own identifier.

This agent takes those records and returns a single, identity-resolved
**Customer 360** profile — channel coverage, behavioural metrics, coarse
segments, and a confidence figure describing how firmly the records were tied
together.

The joining runs entirely on non-reversible linkage tokens. Every identity value
a caller sends — e-mail address, phone number, card number, resident number,
name, address — is converted to a keyed digest at the request boundary and the
raw value is dropped there. Records for the same customer still match on token
equality; nothing downstream ever holds the identifier that made the match, and
none reaches the released profile.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Sending a request

Channel records belong in `input_context`, not in the request string. The
platform rewrites personal-data shapes out of the request string at every node
boundary, and its name heuristic reads title-case retail proper nouns — store
names, product names, brands — as personal names, so records embedded there
arrive with those attributes masked.

```json
{
  "input": "aggregate the omnichannel profile",
  "input_context": {
    "sources": [
      {"channel": "pos", "external_id": "pos-a1", "store": "shibuya",
       "email": "customer@example.com", "events": ["visit", "purchase"]},
      {"channel": "ec", "external_id": "ec-b2", "site": "web-jp",
       "email": "customer@example.com", "events": ["cart", "purchase"]},
      {"channel": "loyalty", "external_id": "ly-c3", "tier": "gold",
       "membership_id": "M-778"}
    ]
  }
}
```

Channels are a closed set (`pos`, `ec`, `crm`, `loyalty`). Values that render
into the profile are restricted to inert codes; numbers must be finite and in
range; identity fields are tokenised. A refused request names the field that
failed and never repeats the value. `docs/02_design.md` carries the full
contract.

## Project Structure

```
src/          agent implementation (nodes, graphs, services, schemas)
tests/        unit, boundary and integration tests
config/       agent manifest and runtime parameters
deploy/       local deployment recipe and a smoke payload
docs/         design and operational documentation
```

`docs/` holds the design (`02_design.md`) and the test specification
(`03_test_spec.md`).

## Customising

1. Adjust `config/config.yaml` for your own thresholds — the record cap, the
   high-frequency event threshold and the linkage confidence floor all change
   the released profile.
2. Extend the channel set and the identity fields in
   `src/services/caller_contract.py` if your systems carry others.
3. Review the aggregation steps under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
