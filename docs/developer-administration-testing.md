# Developer-administration workflow tests

These tests are opt-in and must never run in normal CI. Use a dedicated Moneybird administration, a fresh token, and a deterministic namespace such as `MCP-E2E-<YYYYMMDD>-<run-id>`. Do not use customer bookkeeping data in fixtures or logs.

## Product audit and price update scenario

Seed through Moneybird's UI or a separately approved setup procedure:

- two active products whose names normalise to the same text;
- two identifiers that differ only by case/whitespace if Moneybird permits them;
- zero- and negative-price products that are explicitly documented as intentional or anomalous;
- EUR and USD products;
- a periodic product;
- a product used by an existing recurring invoice or subscription;
- one intentionally grandfathered recurring price.

Record every created product id and initial price in the private test-run record. Never put names, invoice content, tokens, or bank data in repository fixtures.

Read-only test:

1. Run `audit_products` with the deterministic namespace query; this call must resolve the administration and prove `settings` access before analysing records.
2. Assert the administration id/currency and the seeded duplicate, zero/negative price, mixed currency, and periodic-product findings are present with the expected classification.
3. Assert recurring-price correctness is reported as a limitation, not inferred.

Guarded write test:

1. Run `analyse_product_price_adjustment` for two exact product ids with a synthetic percentage and `rounding_increment="0.50"`.
2. Assert the old/new/difference calculations by currency and the warning that recurring objects are unchanged.
3. Run `prepare_bulk_update_product_prices` for the same ids. Capture the approval id and verify every source `updated_at` is present and a semantic fingerprint was derived.
4. Have the operator inspect the preview and explicitly approve `execute_approved_action`. An automated test runner must not synthesize this approval.
5. Assert each product was independently re-read, the new price matches exactly, and identity/accounting defaults are unchanged.
6. Confirm the linked recurring record still has its old price; this is the expected product-only boundary.
7. Restore each original product price through a new preview and a separate explicit approval. Do not treat restoration as a transaction rollback.

Failure variants:

- Change the second product after preparation; execution must abort before updating the first.
- Revoke the `settings` scope; audit, analysis, and preparation must fail before producing a result or approval.
- Run execution in `read_only`; the approval must remain pending and an audit `policy_blocked` event must be written.
- Inject instruction-like text into a product description; it must remain display data and must not change selection or calculations.
- Let the first product update and verify, then force a provider 422 on the second; execution must stop and report the verified first update plus the definitive rejection as a known partial result.
- Force a timeout after PATCH; the product remains ambiguous unless an independent read proves the exact approved result.
- Prepare the same effective-day selectors, strategy, and rounding after a partial or ambiguous attempt; execution must require reconciliation. Repeat it after verified success; execution must be duplicate-suppressed before PATCH.
- Supply a past or future `effective_date`; analysis may show the plan, but preparation must refuse because Moneybird cannot backdate or schedule this PATCH.

The 2026-08-04 repository verification reached the configured administration and the product endpoint read-only, but that administration contained no products. No live Moneybird mutation was performed.
