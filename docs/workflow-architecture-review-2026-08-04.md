# Workflow expansion: current-state review and implementation plan

Initial review baseline: `origin/main` at `513f65a` (2026-08-04), where the full suite passed 526 tests and 221 subtests with one skip. During the review, [PR #22](https://github.com/Espaye/moneybird-mcp-server/pull/22) merged the pre-existing safety commit (`0a52267`); the implementation worktree was then moved without loss onto current `main` at `ba1818c`.

## Current architecture

The server is a FastMCP package with domain-split tool modules in `moneybird_mcp/tools`. `MoneybirdClient` owns typed HTTP calls and a confined generic GET escape hatch. Compact discovery exposes seven core tools plus FastMCP Tool Search; the generic proxy is mechanically read-only and all mutations converge on the directly exposed `execute_approved_action`.

Writes use an administration-bound SQLite approval, a 15-minute expiry, an atomic claim/lease, a canonical fingerprint, audit events, durable dispatch/verification phases, capability-mode enforcement, and an action-specific `WriteSpec`. Most newer writes use `stage_write` and `run_approved_write`; several older batch tools still hand-roll the same lifecycle.

Procedural knowledge was primarily stored in three prose layers: server instructions, seven Dutch MCP prompts, and the bookkeeping playbook. Tests pin important guidance and Tool Search rankings, but there was no shared outcome registry from which prompts, search metadata, preflight, risk, verification expectations, and documentation could be derived.

The API coverage document is endpoint-oriented and was manually annotated. Its path snapshot is enforced against client calls, which is a strength, but the regeneration recipe was not executable and the recorded OpenAPI version lagged the current official source. The official 2026-08-04 spec still has 296 operations and now documents the product PATCH fields and `settings` scope used by this slice.

## Baseline domain matrix

“Guidance” means an agent can discover and safely assemble an outcome, not merely that prose or an API method exists.

| Domain | Read support | Write support | Workflow guidance | Verification | Tests | Main gaps on baseline main |
|---|---:|---:|---:|---:|---:|---|
| Contacts | Strong | Guarded create/update/archive/delivery | Partial | Action-specific | Strong mocked | Duplicate analysis, merge/contact-person lifecycle, archive audit |
| Products | List/get only | None | None | None | Shallow client/tool | Inventory audit, bulk create, bulk prices, recurring/invoice comparisons |
| Projects | List only | None | None | None | Shallow | Create/update/archive, contact link, budget/utilisation audits |
| Time entries | List/filter only | None | Minimal description | None | Shallow | Audit, import, overlap/rounding/timer constraints, unbilled-time invoicing |
| Estimates | List/get only | None | None | None | Shallow | Draft/update/send/state/bill and estimate-to-invoice verification |
| Sales invoices | Strong | Strong guarded draft/batch/update/send/pause/resume/full credit | Moderate; meter flow is first-class | Strong, versioned WriteSpecs | Strong mocked | Consolidated health audit, partial credit, reminders, replacement workflow |
| Recurring invoices/subscriptions | List/get and delivery audit support | None | Weak | None for mutations | Limited mocked | Create/update/pause/resume, subscription restrictions, price comparison/migration |
| Purchase documents | Strong, including bounded PDF reads and supplier review | Guarded reclass/reconcile/payment | Strong playbook/prompt coverage | Strong line/total/version checks | Strong mocked | Creation/intake, duplicate detection, attachment upload/link verification |
| Banking | Strong | Guarded link/unlink/reclassify | Strong prompts/playbook | Strong booking delta/open-amount checks | Strong mocked | Deterministic matching analyser, confidence levels, payout/fee reconciliation |
| Assets/depreciation | Generic GET and assets report | None | None | None | Endpoint confinement only | Dedicated reads, purchase analysis, source links, migration, value changes, disposal |
| VAT and journals | Reports, ledger and journal reads | Guarded journal creation and VAT settlement | Strong playbook | Strong gross/net/lock/read-after-write checks | Strong mocked | Return comparison, closing audit, broader correction explanations |
| Reporting and audits | Strong report surface plus several focused audits | Not applicable | `leg_cijfers_uit` prompt | Source period remains visible | Moderate mocked | Period-closing checklist, migration validation, cross-domain anomalies, live contracts |

## Structural strengths

- Read-only is the mechanical default; hosted-request mode refuses every write.
- The generic Tool Search proxy cannot hide a destructive action behind a read-only annotation.
- Approval records are durable, administration-bound, expiring, single-use, and atomically claimed.
- Dispatch and verification phases preserve the “unknown after timeout” boundary.
- Every generic approval action must have a versioned `WriteSpec`; registry drift fails import/tests.
- Financial calculations already use `Decimal` in the mature flows.
- The OpenAPI path snapshot prevents typoed client endpoints; the generic GET allowlist is finite.
- Audit/telemetry design excludes tokens, request bodies, customer names, and raw attachments.
- The full suite is mocked and runs without credentials; recent safety and purchase/VAT coverage is substantial.

## Structural risks and duplicated patterns

- Prompts, playbook prose, tool descriptions, API coverage, and tests can drift because they were independent sources.
- Some batch executors predate `_writes.py` and duplicate claim, phase, audit, partial-failure, and verification code.
- “Workflow” previously meant both Moneybird invoice workflows and MCP task orchestration, which can confuse discovery.
- The API coverage count is endpoint coverage, not evidence that an end-to-end outcome is dependable.
- Provider scopes cannot be introspected. A successful representative read is evidence of access, not a complete grant listing.
- There is no reusable batch result/checkpoint model. Existing batches correctly admit partial completion but resumability varies.
- Captured/redacted provider fixtures and developer-administration scenarios are not a systematic contract-test layer.
- No agent benchmark runner measures workflow selection, clarification quality, approval compliance, or duplicate writes.

## Prioritised gap matrix

| Gap | Frequency/value | Risk | Effort | Priority | Dependency |
|---|---|---:|---:|---:|---|
| Workflow registry, discovery, preflight | High across every task | Low | Medium | P0 | None |
| Product inventory audit | High for onboarding/cleanup | Read-only | Low | P0 | Registry |
| Guarded product price update | High annual/contractual value | Medium financial | Medium | P0 | Product audit, shared write kernel |
| Recurring-price comparison | High; prevents false migration claims | Read-only | Medium | P0 next | Product identifiers and recurring detail contract |
| Time-entry audit/import | High weekly use | Medium | Medium | P1 | Time contract fixtures, timezone rules |
| Invoice unbilled time | High value, multi-object | High | High | P1 | Rate-resolution policy, linkage contract |
| Project maintenance/utilisation | Medium | Low/medium | Medium | P1 | Project schema fixtures |
| Period-closing checklist | High monthly/quarterly | Read-only judgement | Medium | P1 | Cross-domain audit model |
| Estimate lifecycle | Medium | Medium external side effect | Medium | P2 | Sales line verifier reuse |
| Recurring billing mutation/migration | High value | High | High | P2 | Subscription restrictions verified live |
| Asset lifecycle | Medium frequency, high accounting value | Very high | High | P3 | Accountant-reviewed design and developer admin |
| Provider payout reconciliation | Medium/high | High multi-step | High | P3 | Deterministic matching and batch checkpoints |

## Architecture proposal

The new `moneybird_mcp.workflow_catalogue` is deliberately small: it records only the two product workflows integrated and tested end to end. `list_supported_workflows` lists or explains those definitions without pretending that prose preconditions prove readiness. Product audit, analysis, and preparation perform their concrete administration and record validation directly. The checked-in catalogue is rendered from the registry and CI should run the renderer in `--check` mode.

Incrementally move deterministic workflow data out of prose. Prompts should eventually render their tool sequence and non-negotiable preconditions from the registry while retaining prose only for explanation and judgement. Tool Search descriptions and benchmark cases should reference the workflow id. Do not rewrite all existing prompts in one PR.

For writes, store `workflow_id` and `workflow_version` in every new approval. Existing approvals remain compatible with their action `WriteSpec`; migrate old actions one at a time. A semantic change either gets a new version with an executor retained for still-valid approvals or explicitly invalidates those approvals. Never reinterpret an old payload under new rules.

Generalise batch execution only after two more vertical slices expose common requirements. The future model may need ordered child ids, exact source occurrences, all-or-nothing preflight, optional best-effort execution, per-child durable status, semantic occurrence identity, maximum size/concurrency, rate-limit handling, and reconciliation instructions. It must never claim cross-object transactionality.

## First vertical slice in this branch

The product slice adds:

- deterministic `audit_products` findings classified as structural problem, likely inconsistency, user preference, or bookkeeping judgement;
- exact-decimal `analyse_product_price_adjustment` with percentage, fixed, or explicit mapping strategies, exclusions, currency/title filters, rounding policies, maximum batch size, and non-today date planning;
- `prepare_bulk_update_product_prices` with explicit selection, a derived semantic fingerprint, a per-product old/new/difference preview, immutable source snapshots, workflow version, and recurring-object warnings;
- one stop-on-error guarded executor with complete-batch stale preflight, immediate independent GET verification, known-partial reporting after a later definitive rejection, and ambiguous handling only for uncertain writes;
- official current product client methods and coverage metadata.

Deliberate limits: product updates are immediate and product-only. The workflow does not update or claim to inspect existing invoices, recurring invoices, subscription templates, or subscriptions. It cannot backdate or schedule changes. Those are separate read-first workflows.

## Reviewable PR plan

1. Small registry/discovery tool and generated catalogue; no generic readiness claims.
2. Product audit, concrete product preflight, official response fixture, and executable Dutch/English Tool Search cases.
3. Product PATCH client plus guarded bulk price update and failure contracts.
4. Read-only product-to-recurring/invoice comparison; verify product linkage fields and active-subscription constraints.
5. Reusable batch checkpoint/result model, migrated first by product prices and one existing sales batch.
6. Project/time read-only audits and developer-administration seed namespace.
7. Guarded time import, then unbilled-time invoicing only after rate/linkage behaviour is verified.
8. Estimates and recurring billing; keep sending/activation as separate approvals.
9. Asset reads and purchase analysis; mutation design requires accountant review before implementation.

Compatibility notes: tool discovery remains compact; the new tools are searchable rather than always visible. Existing action-specific executors and prompts remain available. The generic executor gains one new versioned action. No existing approval schema is rewritten.

## Test and developer-administration plan

Unit/contract tests cover decimal strings, comma normalisation, float refusal, rounding, duplicate selection, hostile product text as data, API path/body, stale versions, capability mode, definitive rejection, partial completion, ambiguous timeout, and verification mismatch. Executable Dutch and English Tool Search cases must rank the correct product analyser, prepare tool, or audit tool; the small registry binds those tools to the product workflow ids.

The developer administration should use a deterministic prefix such as `MCP-E2E-<run-id>` and seed: two normalised duplicate names, a duplicate-like SKU case variant, zero/negative prices, EUR/USD products, periodic products, one recurring record on an old price, and one intentionally grandfathered customer. Write tests must record created ids and restore prices explicitly; no test may assume a cross-object rollback or delete products with dependencies.

Live write tests were not run as part of this repository task because no explicit approval to mutate a Moneybird administration was given. The contract and end-to-end scenarios are therefore marked mocked/documented, not live-verified.
