# Supported workflow catalogue

Generated from `moneybird_mcp.workflow_catalogue`; do not edit by hand.
Only workflows integrated and tested end to end are listed.

| Workflow | Domain | Mode | Risk | Version | Tests |
|---|---|---|---|---:|---|
| `product_inventory_audit` | products | read | read_only_analysis | 1 | unit_contract_and_search |
| `bulk_update_product_prices` | products | analyse_prepare_execute_verify | financial_write | 1 | unit_contract_search_and_guarded_executor |

Use `list_supported_workflows` for the complete preconditions, verification, failure modes, and limitations. Workflow-specific tools perform their own concrete administration and record preflight.

A catalogue entry never grants permission to execute a write.
