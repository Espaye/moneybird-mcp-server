# Roadmap

**Living summary. Last updated 2026-09-04.**

This file answers three questions and nothing else: where the public core is, which product
milestone is next, and what comes after it. Implementation detail belongs in the code, in the
[changelog](https://github.com/Espaye/moneybird-mcp-server/blob/main/CHANGELOG.md), and — for the
commercial layers — in private repositories.

## Where the public core is

`moneybird-mcp` is a reusable, source-available Moneybird MCP server. It is released
independently of anything built on top of it, and it is the layer this repository develops.

- **Current release:** 0.8.1, published on PyPI.
- **Capability baseline:** 61 tools and 26 guarded actions. Every change goes through the same
  prepare → approve → execute flow; nothing writes without an approval the caller asked for.
- **Extension boundary:** stable. Another distribution can register additional tools, write
  contracts and approval executors through the `moneybird_mcp.tools` entry-point group, so
  building on this core does not require forking it.
- **Scope:** advanced commercial accounting workflows are no longer developed in this repository.

Two commercial layers are built privately on top of this core — an advanced-workflow package and
a hosted product. Neither is documented here, and neither changes what the public core is: it
stays reusable, independently versioned, and useful on its own with a personal API token or your
own Moneybird OAuth application.

## NEXT: M2 — staging validation and invite-only read-only alpha

**Objective:** make "Connect Moneybird" safe for real invited users of the hosted product, read
only, before any AI or write layer exists.

The implementation and the pre-provisioning engineering are complete. What remains is validation
against real deployed services, which no amount of offline work substitutes for:

- provision isolated staging services;
- deploy the reviewed build;
- run the real authentication and Moneybird OAuth flow end to end;
- confirm encrypted grant storage;
- confirm explicit administration selection;
- make a real read-only Moneybird request;
- exercise the disconnect and delete lifecycle;
- repeat all of it with at least two isolated users and workspaces;
- pass the tenant-boundary denial tests;
- pass the backup, recovery, security and logging gates;
- confirm staging and production are separated.

M2 is marked done when that release gate passes, not when the code exists. An invite-only,
read-only alpha is acceptable at that point and not before.

## Later milestones

None of these has started.

**M3 — read-only AI agent.** Prove the product loop: a question selects the tools it needs, those
tools run only against that user's authorised administration, and the answer stays traceable to
what produced it. Read-only throughout.

**M4 — usable customer web product.** Turn that loop into something a non-technical customer can
use: connection and administration settings, task history, honest error and reconnect states,
onboarding, and data-retention controls.

**M5 — safe hosted write workflows.** Writes are a separate security milestone, not a feature
increment. Explicit human confirmation, a preview of what will change and in which
administration, idempotency, an audit trail, and revalidation of the identity chain immediately
before execution.

**M6 — broader public and commercial product.** Billing, support workflow, operational
dashboards, capacity planning with Moneybird, public onboarding, and possibly other bookkeeping
providers.

## Open decisions

Genuinely undecided, and deliberately left that way until a milestone needs them:

- which LLM or model to use;
- the exact customer-facing AI and chat experience;
- pricing and billing;
- when hosted writes become customer-facing;
- whether and when to support other accounting providers.

## Rules

1. There is always exactly one milestone marked **NEXT**.
2. A milestone is done when its release gate has passed, never because the code exists.
3. Every hosted Moneybird request resolves this chain server-side, and no part of it may be taken
   from the browser as authorisation on its own:
   **authenticated user → authorised workspace and grant → explicitly authorised Moneybird
   administration.**
4. Keep this file short. A new idea belongs under a later milestone or under open decisions.

## Immediate next action

Execute the private hosted staging provisioning runbook and complete the M2 release gates.
