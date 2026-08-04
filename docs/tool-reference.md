# Tool reference

Moneybird MCP defaults to compact Tool Search. The client receives a small core surface and discovers additional tools only when needed.

Use full discovery only for older clients:

```bash
moneybird-mcp --tool-discovery full
```

## Core discovery tools

These stay visible without a search step:

- `get_server_status`
- `list_administrations`
- `search`
- `fetch`
- `sync_search_index`
- `prepare_bookkeeping_correction_batch`
- `execute_approved_action`
- FastMCP `search_tools`
- `call_tool` (read-only proxy)

`search` and `fetch` are the main data-source tools for clients that support deep research or structured retrieval.

## Read tools

### Contacts and reference data

- `list_contacts`
- `get_contact_by_customer_id`
- `get_invoice_defaults_for_contact`
- `list_products`
- `list_tax_rates`
- `list_ledger_accounts`
- `list_financial_accounts`
- `list_projects`
- `list_time_entries`

### Workflow discovery and products

- `list_supported_workflows`
- `audit_products`
- `analyse_product_price_adjustment`

`list_supported_workflows` lists only workflows integrated and tested end to end; pass a workflow id to return its complete definition. Product audit and pricing tools resolve the active administration and validate the selected product records directly instead of relying on a generic readiness claim.

### Sales

- `list_sales_invoices`
- `audit_invoice_delivery_settings`
- `audit_recent_sales_invoice_send_methods`
- `list_estimates`
- `list_recurring_sales_invoices`

### Purchases and documents

- `list_purchase_invoices`
- `get_purchase_invoice_by_reference`
- `list_receipts`
- `list_general_journal_documents`
- `read_document_attachment`
- `review_purchase_invoices`

### Banking and reports

- `list_financial_mutations`
- `get_profit_loss`
- `get_balance_sheet`
- `get_general_ledger`
- `get_financial_report`

### Search and API coverage

- `sync_search_index`
- `search_contacts`
- `moneybird_request`
- `list_administrations`

`moneybird_request` is a read-only JSON GET escape hatch over a finite allowlist generated from the vendored Moneybird OpenAPI routes. It does not permit arbitrary hosts, methods, binary downloads, or write requests.

For endpoint-level status, see [Moneybird API coverage](moneybird_api_coverage.md).

## Guarded writes

Write tools are mechanically denied unless the server is explicitly started with:

```text
MONEYBIRD_CAPABILITY_MODE=write_enabled
```

In compact discovery mode, `call_tool` accepts only tools explicitly annotated read-only, including `prepare_*` previews. Mutating and unannotated targets are refused, and hidden mutating executors cannot be called directly by name. This is deliberate: MCP clients can enforce the destructive annotation only on the tool they call, not on a second tool selected inside a generic proxy.

The normal flow is:

1. a `prepare_*` tool validates the request and stores an exact preview;
2. the user or client reviews the preview;
3. the client calls the directly exposed, destructively annotated `execute_approved_action(approval_id)`, which atomically claims the approval;
4. the server performs the action;
5. action-specific checks record success, failure, partial progress, ambiguity, or verification failure.

The generic executor delegates only to the exact action stored in the approval. Action-specific executors remain available in full discovery mode for compatibility, but compact discovery omits them from search results and never routes them through `call_tool`.

Draft-invoice previews need a verifiable VAT rate to show exact totals. Each line may provide `tax_rate_id`; otherwise the server uses the selected product's tax and ledger defaults, then a previous invoice default for that contact. When none exists, preparation asks for `tax_rate_id` instead of guessing an accounting identifier.

Write families include:

- contacts: create, update, archive, and delivery method changes;
- sales: draft, update, batch create, schedule, send, pause, resume, and credit invoices;
- payments: register payments on supported invoices and documents;
- ledger and journals: create ledger accounts and general journal documents;
- purchase documents: line reclassification and reconciliation;
- banking: link, unlink, and reclassify mutation bookings;
- combined bookkeeping correction batches;
- meter-usage invoice runs.
- product-only bulk price updates.

`prepare_bulk_update_product_prices` supports percentage, fixed-amount, and explicit mappings with filters, exclusions, exact-decimal rounding policies, a maximum of 100 products, source versions, and per-product verification. It derives a semantic fingerprint from the effective day, selectors, strategy, and rounding so repeating the same request cannot silently apply it twice. Execution stops at the first failure; already verified products and a later definitive rejection are returned as a known partial result. Updates are immediate and cannot be backdated or scheduled. Existing invoices, recurring invoices, subscription templates, and subscriptions are never changed by this workflow.

The approval ID is not independent evidence of human consent. Keep client-side destructive-tool confirmation enabled and use writes only in a supervised local or authenticated single-user deployment.

## Prompts and bookkeeping playbook

The server includes progressive-disclosure prompts and a bookkeeping reference resource.

Prompts include:

- `aan_de_slag`
- `koppel_banktransacties`
- `verwerk_achterstand`
- `categoriseer_heel_jaar`
- `leg_cijfers_uit`
- `diagnose_bankmutatie`
- `factureer_meterverbruik`

The resource `moneybird://playbook/bookkeeping` serves the repository bookkeeping playbook on demand.

Bookkeeping guidance can help a model reason consistently, but it is not tax advice and does not replace deterministic enforcement or professional review.

## Workflow catalogue

The typed registry in `moneybird_mcp.workflow_catalogue` is the machine-readable source for workflow ids and versions, intent examples, required tools/scopes, risk, preconditions, verification, failure modes, limitations, prompt links, and test status. Regenerate the checked-in [workflow catalogue](workflow-catalogue.md) with:

```bash
python scripts/render_workflow_catalogue.py
python scripts/render_workflow_catalogue.py --check
```

## Attachment reading

`read_document_attachment` is available only in local or authenticated single-user mode and requires:

```bash
python -m pip install "moneybird-mcp[pdf]"
```

PDF content is untrusted model input. The implementation applies bounded download, page, text, time, and process-memory limits and does not retain the downloaded attachment.

## Local search

Local and single-user modes can synchronise contacts, invoices, documents, and financial mutations into administration-scoped JSON and SQLite FTS state.

Hosted request mode does not read or build durable search indexes. It performs constrained live reads instead.

## Limits

- Moneybird fields and attachment text may contain prompt-injection content.
- Bookings rules are not exposed by the Moneybird API and therefore cannot be read or changed directly.
- Several Moneybird report endpoints restrict the accepted period.
- Multi-step Moneybird operations are not cross-object transactions.
- The project does not guarantee bookkeeping correctness, tax correctness, or data recovery.

See [Deployment and safety](deployment-and-safety.md) and [Security policy](../SECURITY.md).

For opt-in seeded workflow scenarios, see [Developer-administration workflow tests](developer-administration-testing.md).
