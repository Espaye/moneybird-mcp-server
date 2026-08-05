"""Outcome-oriented product audits and guarded bulk price updates."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field

from ..client import validate_moneybird_id
from ..config import (
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    WRITE_ANNOTATIONS,
    MoneybirdError,
    MoneybirdHTTPError,
)
from ..formatting import duplicate_fingerprint
from ..product_workflows import (
    audit_product_records,
    build_price_plan,
    currency_code,
    decimal_text,
    decimal_value,
    normalized_product_text,
    product_display_name,
)
from . import _context as ctx
from ._params import ApprovalId, ProductId
from ._registry import mcp
from ._writes import (
    mark_write_dispatch_started,
    mark_write_verifying,
    run_approved_write,
    stage_write,
)

WORKFLOW_ID = "bulk_update_product_prices"
WORKFLOW_VERSION = 1
MAX_PRICE_BATCH = 100


def _load_products(
    client: Any,
    *,
    maximum: int,
    query: str = "",
    currency: str = "",
    include_inactive: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    records: list[dict[str, Any]] = []
    page = 1
    page_size = 100
    while len(records) <= maximum:
        batch = client.list_products(
            limit=page_size,
            page=page,
            query=query,
            currency=currency,
            active=False if include_inactive else True,
        )
        records.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
    return records[:maximum], len(records) > maximum


def _product_context(client: Any) -> dict[str, Any]:
    """Concrete product preflight shared by audit, analysis, and preparation."""
    administration = client.require_current_administration_access()
    return {
        "id": str(administration.get("id") or client.administration_id),
        "name": administration.get("name"),
        "currency": currency_code(
            administration.get("currency"),
            field="administration currency",
        ),
    }


@mcp.tool(
    annotations=READ_ONLY_ANNOTATIONS,
    tags={"domain:products", "capability:analyse", "workflow:product_inventory_audit"},
)
def audit_products(
    max_products: Annotated[
        int,
        Field(ge=1, le=1000, description="Maximum products to inspect. The result reports when the collection was truncated."),
    ] = 500,
    include_inactive: Annotated[
        bool,
        Field(description="Ask Moneybird to include deactivated products as well as active products."),
    ] = False,
    query: Annotated[
        str,
        Field(description="Optional Moneybird product-name query. Empty audits the full selected active/inactive collection."),
    ] = "",
    currency: Annotated[
        str,
        Field(description="Optional exact three-letter currency filter such as EUR."),
    ] = "",
    validate_accounting_ids: Annotated[
        bool,
        Field(description="Also load current ledger accounts and tax rates to prove referenced ids still exist."),
    ] = True,
) -> dict[str, Any]:
    """Find duplicate product SKUs and names, missing VAT or ledger defaults, strange zero/negative prices, currencies, and periods. Dutch: zoek dubbele product-SKU's, ontbrekende btw, nulprijzen en afwijkende producten."""
    client = ctx.get_client()
    administration = _product_context(client)
    currency_filter = currency_code(currency) if str(currency or "").strip() else ""
    products, truncated = _load_products(
        client,
        maximum=max_products,
        query=query,
        currency=currency_filter,
        include_inactive=include_inactive,
    )
    tax_ids: set[str] | None = None
    ledger_ids: set[str] | None = None
    if validate_accounting_ids:
        tax_ids = {str(item.get("id") or "") for item in client.list_tax_rates()}
        ledger_ids = {
            str(item.get("id") or "") for item in client.list_ledger_accounts()
        }
    result = audit_product_records(
        products,
        administration_currency=administration["currency"],
        valid_tax_rate_ids=tax_ids,
        valid_ledger_account_ids=ledger_ids,
    )
    result.update(
        {
            "workflow_id": "product_inventory_audit",
            "workflow_version": 1,
            "administration_id": str(client.administration_id),
            "filters": {
                "query": query or None,
                "currency": currency_filter or None,
                "include_inactive": include_inactive,
            },
            "truncated": truncated,
        }
    )
    if truncated:
        result["limitations"].insert(
            0,
            f"The audit stopped after {max_products} products; narrow the filters or raise max_products.",
        )
    return result


def _resolve_explicit_prices(
    client: Any,
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    products: list[dict[str, Any]] = []
    prices: dict[str, str] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise MoneybirdError(f"explicit_prices[{index}] must be an object.")
        unknown = sorted(set(entry) - {"product_id", "identifier", "new_price"})
        if unknown:
            raise MoneybirdError(
                f"explicit_prices[{index}] has unsupported field(s): {', '.join(unknown)}."
            )
        product_id = str(entry.get("product_id") or "").strip()
        identifier = str(entry.get("identifier") or "").strip()
        if bool(product_id) == bool(identifier):
            raise MoneybirdError(
                f"explicit_prices[{index}] needs exactly one of product_id or identifier."
            )
        if "new_price" not in entry:
            raise MoneybirdError(f"explicit_prices[{index}] needs new_price.")
        product = (
            client.get_product(product_id)
            if product_id
            else client.get_product_by_identifier(identifier)
        )
        resolved_id = validate_moneybird_id(product.get("id"), "product_id")
        if resolved_id in prices:
            raise MoneybirdError(f"Product {resolved_id} is selected more than once.")
        # Validate now; the canonical decimal string is stored in the approval.
        prices[resolved_id] = decimal_text(
            decimal_value(entry["new_price"], field=f"explicit_prices[{index}].new_price")
        )
        products.append(product)
    return products, prices


def _resolve_selected_products(
    client: Any,
    *,
    product_ids: list[str] | None,
    identifiers: list[str] | None,
    explicit_prices: list[dict[str, Any]] | None,
    all_matching: bool,
    title_contains: str,
    currency: str,
    include_inactive: bool,
    exclude_product_ids: list[str] | None,
    exclude_identifiers: list[str] | None,
    max_products: int,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    product_ids = product_ids or []
    identifiers = identifiers or []
    explicit_prices = explicit_prices or []
    if explicit_prices and (product_ids or identifiers or all_matching):
        raise MoneybirdError(
            "explicit_prices defines its own selection; do not combine it with product_ids, identifiers, or all_matching."
        )
    if explicit_prices:
        products, price_map = _resolve_explicit_prices(client, explicit_prices)
    else:
        price_map = {}
        if all_matching:
            products, truncated = _load_products(
                client,
                maximum=max_products,
                query=title_contains,
                currency=currency,
                include_inactive=include_inactive,
            )
            if truncated:
                raise MoneybirdError(
                    f"More than {max_products} products match. Narrow the filters; a price batch cannot exceed {MAX_PRICE_BATCH}."
                )
        else:
            if not product_ids and not identifiers:
                raise MoneybirdError(
                    "Select product_ids/identifiers, supply explicit_prices, or set all_matching=true with reviewable filters."
                )
            products = [client.get_product(item) for item in product_ids]
            products.extend(client.get_product_by_identifier(item) for item in identifiers)

    excluded_ids = {str(item).strip() for item in (exclude_product_ids or [])}
    excluded_identifiers = {
        normalized_product_text(item) for item in (exclude_identifiers or [])
    }
    currency_filter = currency_code(currency) if str(currency or "").strip() else ""
    title_filter = normalized_product_text(title_contains)
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for product in products:
        product_id = validate_moneybird_id(product.get("id"), "product_id")
        if product_id in seen:
            raise MoneybirdError(f"Product {product_id} is selected more than once.")
        seen.add(product_id)
        identifier = normalized_product_text(product.get("identifier"))
        title = normalized_product_text(product_display_name(product))
        excluded_reason = ""
        if product_id in excluded_ids or (identifier and identifier in excluded_identifiers):
            excluded_reason = "exclusion_list"
        elif currency_filter and str(product.get("currency") or "").upper() != currency_filter:
            excluded_reason = "currency_filter"
        elif title_filter and title_filter not in title:
            excluded_reason = "title_filter"
        if excluded_reason:
            excluded.append(
                {
                    "product_id": product_id,
                    "identifier": product.get("identifier"),
                    "reason": excluded_reason,
                }
            )
            price_map.pop(product_id, None)
        else:
            selected.append(product)
    if not selected:
        raise MoneybirdError("No products remain after applying the selection and exclusions.")
    if len(selected) > max_products or len(selected) > MAX_PRICE_BATCH:
        raise MoneybirdError(
            f"The batch selects {len(selected)} products; the maximum is {min(max_products, MAX_PRICE_BATCH)}."
        )
    return selected, price_map, {
        "resolved": len(products),
        "selected": len(selected),
        "excluded": excluded,
    }


def _effective_date_status(effective_date: str) -> dict[str, Any]:
    value = str(effective_date or "").strip()
    if not value:
        return {"effective_date": None, "planning_only": False}
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise MoneybirdError("effective_date must use YYYY-MM-DD.") from exc
    today = date.today()
    return {
        "effective_date": value,
        "planning_only": parsed != today,
        "date_relation": "past" if parsed < today else "future" if parsed > today else "today",
        "today": today.isoformat(),
    }


def _analyse_price_adjustment(
    client: Any,
    *,
    percentage: str,
    fixed_amount: str,
    explicit_prices: list[dict[str, Any]] | None,
    product_ids: list[str] | None,
    identifiers: list[str] | None,
    all_matching: bool,
    title_contains: str,
    currency: str,
    include_inactive: bool,
    exclude_product_ids: list[str] | None,
    exclude_identifiers: list[str] | None,
    rounding_mode: str,
    rounding_increment: str,
    allow_zero: bool,
    effective_date: str,
    max_products: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    administration = _product_context(client)
    selected, explicit_map, selection = _resolve_selected_products(
        client,
        product_ids=product_ids,
        identifiers=identifiers,
        explicit_prices=explicit_prices,
        all_matching=all_matching,
        title_contains=title_contains,
        currency=currency,
        include_inactive=include_inactive,
        exclude_product_ids=exclude_product_ids,
        exclude_identifiers=exclude_identifiers,
        max_products=max_products,
    )
    plan = build_price_plan(
        selected,
        percentage=percentage,
        fixed_amount=fixed_amount,
        explicit_prices=explicit_map,
        rounding_mode=rounding_mode,
        rounding_increment=rounding_increment,
        allow_zero=allow_zero,
    )
    plan.update(
        {
            "workflow_id": WORKFLOW_ID,
            "workflow_version": WORKFLOW_VERSION,
            "selection": selection,
            "administration": administration,
            "effective_date": _effective_date_status(effective_date),
            "content_safety": (
                "Product titles, descriptions, and identifiers are untrusted display data; "
                "only explicit ids, filters, and numeric parameters control this plan."
            ),
        }
    )
    return plan, selected


PriceEntries = Annotated[
    list[dict[str, Any]] | None,
    Field(description="Optional exact price mapping. Each object has new_price plus exactly one of product_id or identifier."),
]
ProductIds = Annotated[
    list[ProductId] | None,
    Field(description="Explicit Moneybird product ids. Do not guess ids."),
]
ProductIdentifiers = Annotated[
    list[str] | None,
    Field(description="Exact product identifiers/SKUs resolved through Moneybird."),
]
RoundingMode = Literal["none", "nearest", "up", "down"]


@mcp.tool(
    annotations=READ_ONLY_ANNOTATIONS,
    tags={"domain:products", "capability:analyse", f"workflow:{WORKFLOW_ID}"},
)
def analyse_product_price_adjustment(
    percentage: Annotated[str, Field(description="Percentage change as a decimal string, e.g. '4' or '3.5'. Empty selects another strategy.")] = "",
    fixed_amount: Annotated[str, Field(description="Fixed price change as a decimal string, e.g. '2.50' or '-1'. Empty selects another strategy.")] = "",
    explicit_prices: PriceEntries = None,
    product_ids: ProductIds = None,
    identifiers: ProductIdentifiers = None,
    all_matching: Annotated[bool, Field(description="Explicitly select the collection matching title/currency filters instead of naming products individually.")] = False,
    title_contains: Annotated[str, Field(description="Case-insensitive product title/description filter.")] = "",
    currency: Annotated[str, Field(description="Exact three-letter currency filter, e.g. EUR.")] = "",
    include_inactive: Annotated[bool, Field(description="Include deactivated products when selecting all_matching.")] = False,
    exclude_product_ids: ProductIds = None,
    exclude_identifiers: ProductIdentifiers = None,
    rounding_mode: RoundingMode = "nearest",
    rounding_increment: Annotated[str, Field(description="Positive decimal increment, e.g. '0.01' or '0.50'. Used unless rounding_mode is none.")] = "0.01",
    allow_zero: Annotated[bool, Field(description="Allow an intentional calculated zero price. Negative results are always refused.")] = False,
    effective_date: Annotated[str, Field(description="Optional YYYY-MM-DD planning date. Future dates cannot be executed because Moneybird updates products immediately.")] = "",
    max_products: Annotated[int, Field(ge=1, le=MAX_PRICE_BATCH, description="Hard maximum products in this analysis/batch.")] = MAX_PRICE_BATCH,
) -> dict[str, Any]:
    """Analyse or dry-run a bulk product price increase/decrease without changing Moneybird. Dutch: analyseer of bereken een voorbeeld van productprijzen verhogen/verlagen, prijswijziging of nieuwe tarieven."""
    plan, _ = _analyse_price_adjustment(
        ctx.get_client(),
        percentage=percentage,
        fixed_amount=fixed_amount,
        explicit_prices=explicit_prices,
        product_ids=product_ids,
        identifiers=identifiers,
        all_matching=all_matching,
        title_contains=title_contains,
        currency=currency,
        include_inactive=include_inactive,
        exclude_product_ids=exclude_product_ids,
        exclude_identifiers=exclude_identifiers,
        rounding_mode=rounding_mode,
        rounding_increment=rounding_increment,
        allow_zero=allow_zero,
        effective_date=effective_date,
        max_products=max_products,
    )
    return plan


def _product_precondition(product: dict[str, Any]) -> dict[str, Any]:
    product_id = validate_moneybird_id(product.get("id"), "product_id")
    updated_at = str(product.get("updated_at") or "").strip()
    if not updated_at:
        raise MoneybirdError(
            f"Product {product_id} has no updated_at version; a guarded update cannot be prepared."
        )
    return {
        "id": product_id,
        "updated_at": updated_at,
        "price": str(product.get("price") or ""),
        "currency": currency_code(
            product.get("currency"),
            field=f"product {product_id} currency",
        ),
        "identifier": product.get("identifier"),
        "title": product.get("title"),
        "description": product.get("description"),
        "tax_rate_id": product.get("tax_rate_id"),
        "ledger_account_id": product.get("ledger_account_id"),
        "frequency": product.get("frequency"),
        "frequency_type": product.get("frequency_type"),
    }


def _same_snapshot(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    fields_match = all(
        current.get(field) == expected.get(field)
        for field in (
            "identifier",
            "title",
            "description",
            "tax_rate_id",
            "ledger_account_id",
            "frequency",
            "frequency_type",
        )
    )
    currency_matches = (
        str(current.get("currency") or "").strip().upper() == expected["currency"]
    )
    return (
        fields_match
        and currency_matches
        and str(current.get("id") or "") == expected["id"]
    )


def _assert_precondition(current: dict[str, Any], expected: dict[str, Any]) -> None:
    if (
        str(current.get("updated_at") or "") != expected["updated_at"]
        or str(current.get("price") or "") != expected["price"]
        or not _same_snapshot(current, expected)
    ):
        raise MoneybirdError(
            f"Product {expected['id']} changed after preparation. No product was updated; prepare the batch again."
        )


def _verify_product(
    current: dict[str, Any],
    *,
    expected: dict[str, Any],
    new_price: str,
) -> dict[str, Any]:
    try:
        price_matches = decimal_value(
            current.get("price"), field=f"product {expected['id']} verified price"
        ) == decimal_value(new_price, field="expected new price")
    except MoneybirdError:
        price_matches = False
    identity_unchanged = _same_snapshot(current, expected)
    id_matches = str(current.get("id") or "") == expected["id"]
    return {
        "product_id": expected["id"],
        "expected_price": new_price,
        "actual_price": current.get("price"),
        "checks": {
            "id_matches": id_matches,
            "price_matches": price_matches,
            "identity_and_accounting_defaults_unchanged": identity_unchanged,
        },
        "verified": id_matches and price_matches and identity_unchanged,
    }


def _preview_table(items: list[dict[str, Any]]) -> str:
    def cell(value: Any, limit: int = 80) -> str:
        return " ".join(str(value or "").replace("|", "\\|").split())[:limit]

    lines = [
        "| Product | Old price | New price | Difference | Percentage | Linked recurring records | Action |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in items:
        label = item.get("identifier") or item.get("title") or item.get("product_id")
        lines.append(
            f"| {cell(label)} | {cell(item['old_price'])} | {cell(item['new_price'])} | "
            f"{cell(item['difference'])} | {cell(item.get('percentage'))} | not inspected | "
            f"{cell(item['action'])} |"
        )
    return "\n".join(lines)


def _price_operation_fingerprint(
    plan: dict[str, Any],
    *,
    all_matching: bool,
    title_contains: str,
    currency: str,
    include_inactive: bool,
    exclude_product_ids: list[str] | None,
    exclude_identifiers: list[str] | None,
) -> str:
    """Identify the requested business change without depending on old prices."""
    rows = plan["items"]
    calculations = sorted({str(row["calculation"]) for row in rows})
    if all_matching:
        selection: dict[str, Any] = {
            "all_matching": True,
            "title_contains": normalized_product_text(title_contains),
            "currency": str(currency or "").strip().upper(),
            "include_inactive": include_inactive,
            "exclude_product_ids": sorted(str(value) for value in (exclude_product_ids or [])),
            "exclude_identifiers": sorted(
                normalized_product_text(value) for value in (exclude_identifiers or [])
            ),
        }
    else:
        selection = {
            "product_ids": sorted(str(row["product_id"]) for row in rows),
        }
    strategy: dict[str, Any] = {"calculations": calculations}
    if calculations == ["explicit_mapping"]:
        strategy["targets"] = sorted(
            (str(row["product_id"]), str(row["new_price"])) for row in rows
        )
    return duplicate_fingerprint(
        "bulk_update_product_prices_operation",
        {
            "workflow_id": WORKFLOW_ID,
            "workflow_version": WORKFLOW_VERSION,
            "effective_date": plan["effective_date"].get("effective_date")
            or date.today().isoformat(),
            "selection": selection,
            "strategy": strategy,
            "rounding": sorted({str(row["rounding"]) for row in rows}),
        },
    )


@mcp.tool(
    annotations=PREPARE_ANNOTATIONS,
    tags={"domain:products", "capability:prepare", f"workflow:{WORKFLOW_ID}"},
)
def prepare_bulk_update_product_prices(
    percentage: Annotated[str, Field(description="Percentage change as a decimal string, e.g. '4' or '3.5'. Empty selects another strategy.")] = "",
    fixed_amount: Annotated[str, Field(description="Fixed price change as a decimal string, e.g. '2.50' or '-1'. Empty selects another strategy.")] = "",
    explicit_prices: PriceEntries = None,
    product_ids: ProductIds = None,
    identifiers: ProductIdentifiers = None,
    all_matching: Annotated[bool, Field(description="Explicitly select the collection matching title/currency filters instead of naming products individually.")] = False,
    title_contains: Annotated[str, Field(description="Case-insensitive product title/description filter.")] = "",
    currency: Annotated[str, Field(description="Exact three-letter currency filter, e.g. EUR.")] = "",
    include_inactive: Annotated[bool, Field(description="Include deactivated products when selecting all_matching.")] = False,
    exclude_product_ids: ProductIds = None,
    exclude_identifiers: ProductIdentifiers = None,
    rounding_mode: RoundingMode = "nearest",
    rounding_increment: Annotated[str, Field(description="Positive decimal increment, e.g. '0.01' or '0.50'. Used unless rounding_mode is none.")] = "0.01",
    allow_zero: Annotated[bool, Field(description="Allow an intentional calculated zero price. Negative results are always refused.")] = False,
    effective_date: Annotated[str, Field(description="Empty or today's YYYY-MM-DD date only. Future dates are planning-only and cannot be staged.")] = "",
    max_products: Annotated[int, Field(ge=1, le=MAX_PRICE_BATCH, description="Hard maximum products in this batch.")] = MAX_PRICE_BATCH,
) -> dict[str, Any]:
    """Increase, decrease, or set product prices in bulk after a guarded preview. Dutch: verhoog of verlaag productprijzen, pas prijzen/procenten aan, stel nieuwe tarieven in; supports exact decimals, rounding and exclusions."""
    client = ctx.get_client()
    plan, selected = _analyse_price_adjustment(
        client,
        percentage=percentage,
        fixed_amount=fixed_amount,
        explicit_prices=explicit_prices,
        product_ids=product_ids,
        identifiers=identifiers,
        all_matching=all_matching,
        title_contains=title_contains,
        currency=currency,
        include_inactive=include_inactive,
        exclude_product_ids=exclude_product_ids,
        exclude_identifiers=exclude_identifiers,
        rounding_mode=rounding_mode,
        rounding_increment=rounding_increment,
        allow_zero=allow_zero,
        effective_date=effective_date,
        max_products=max_products,
    )
    if plan["effective_date"]["planning_only"]:
        raise MoneybirdError(
            "Moneybird product updates apply immediately and approvals expire quickly. "
            "A non-today effective date cannot be backdated or scheduled; keep this "
            "analysis as a plan and prepare it on the actual effective date."
        )
    products_by_id = {str(product["id"]): product for product in selected}
    items = []
    for row in plan["items"]:
        if row["action"] != "update_product_only":
            continue
        product = products_by_id[row["product_id"]]
        items.append(
            {
                "product_id": row["product_id"],
                "identifier": row.get("identifier"),
                "old_price": row["old_price"],
                "new_price": row["new_price"],
                "precondition": _product_precondition(product),
            }
        )
    if not items:
        raise MoneybirdError("The plan contains no changed prices, so no approval was created.")
    payload = {
        "workflow_id": WORKFLOW_ID,
        "workflow_version": WORKFLOW_VERSION,
        "items": items,
    }
    plan["preview_table"] = _preview_table(plan["items"])
    plan["idempotency_note"] = (
        "The effective day, selection, strategy, and rounding identify this operation. "
        "Repeating them is suppressed; select newly added products explicitly."
    )
    operation_fingerprint = _price_operation_fingerprint(
        plan,
        all_matching=all_matching,
        title_contains=title_contains,
        currency=currency,
        include_inactive=include_inactive,
        exclude_product_ids=exclude_product_ids,
        exclude_identifiers=exclude_identifiers,
    )
    return stage_write(
        "bulk_update_product_prices",
        summary=f"Update {len(items)} Moneybird product price(s) immediately",
        payload=payload,
        preview=plan,
        fingerprint=operation_fingerprint,
    )


def _execute_bulk_price_update(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("workflow_id") != WORKFLOW_ID or payload.get("workflow_version") != WORKFLOW_VERSION:
        raise MoneybirdError(
            "This approval uses an unsupported product-price workflow version; prepare it again."
        )
    items = payload.get("items") or []
    if not items or len(items) > MAX_PRICE_BATCH:
        raise MoneybirdError("The approved product-price batch is empty or exceeds its maximum size.")

    # Fail the complete batch before the first mutation if any source version drifted.
    for item in items:
        current = client.get_product(item["product_id"])
        _assert_precondition(current, item["precondition"])

    updated: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    verification: list[dict[str, Any]] = []
    for item in items:
        mark_write_dispatch_started()
        try:
            client.update_product(item["product_id"], {"price": item["new_price"]})
        except Exception as exc:
            mark_write_verifying()
            try:
                current = client.get_product(item["product_id"])
                checked = _verify_product(
                    current,
                    expected=item["precondition"],
                    new_price=item["new_price"],
                )
            except Exception as verify_exc:
                checked = {
                    "product_id": item["product_id"],
                    "verified": False,
                    "verification_error": str(verify_exc),
                }
            verification.append(checked)
            if checked.get("verified"):
                updated.append(
                    {
                        "product_id": item["product_id"],
                        "new_price": item["new_price"],
                        "recovered_after_uncertain_response": True,
                    }
                )
                continue
            if isinstance(exc, MoneybirdHTTPError) and exc.is_definitive_rejection:
                if updated:
                    failed.append(
                        {
                            "product_id": item["product_id"],
                            "error": str(exc),
                            "outcome": "definitively_rejected",
                        }
                    )
                    return _completed_price_result(
                        updated=updated,
                        failed=failed,
                        verification=verification,
                        audit_result="partial_failure",
                        status="completed_with_errors",
                    )
                raise
            return {
                "_audit_result": "ambiguous",
                "_status": "ambiguous",
                "_audit": {
                    "workflow_id": WORKFLOW_ID,
                    "workflow_version": WORKFLOW_VERSION,
                    "updated": updated,
                    "failed": failed,
                    "verification": verification,
                },
                "updated": updated,
                "failed": failed,
                "verification": verification,
                "ambiguous_product_id": item["product_id"],
                "error": str(exc),
                "retry_guidance": "Do not prepare or retry this product until its current Moneybird price is reconciled.",
            }

        mark_write_verifying()
        try:
            current = client.get_product(item["product_id"])
            checked = _verify_product(
                current,
                expected=item["precondition"],
                new_price=item["new_price"],
            )
        except Exception as exc:
            return {
                "_audit_result": "ambiguous",
                "_status": "ambiguous",
                "_audit": {
                    "workflow_id": WORKFLOW_ID,
                    "workflow_version": WORKFLOW_VERSION,
                    "updated": updated,
                    "failed": failed,
                    "verification": verification,
                },
                "updated": updated,
                "failed": failed,
                "verification": verification,
                "ambiguous_product_id": item["product_id"],
                "error": str(exc),
                "retry_guidance": "The update returned but independent verification failed; reconcile before retrying.",
            }
        verification.append(checked)
        updated.append(
            {
                "product_id": item["product_id"],
                "new_price": item["new_price"],
                "verified": checked["verified"],
            }
        )
        if not checked["verified"]:
            return {
                "_audit_result": "verification_failed",
                "_status": "completed_with_verification_errors",
                "_audit": {
                    "workflow_id": WORKFLOW_ID,
                    "workflow_version": WORKFLOW_VERSION,
                    "updated": updated,
                    "failed": failed,
                    "verification": verification,
                },
                "updated": updated,
                "failed": failed,
                "verification": verification,
                "all_verified": False,
            }

    return _completed_price_result(
        updated=updated,
        failed=failed,
        verification=verification,
        audit_result="success",
        status="completed",
    )


def _completed_price_result(
    *,
    updated: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    verification: list[dict[str, Any]],
    audit_result: str,
    status: str,
) -> dict[str, Any]:
    return {
        "_audit_result": audit_result,
        "_status": status,
        "_audit": {
            "workflow_id": WORKFLOW_ID,
            "workflow_version": WORKFLOW_VERSION,
            "updated": updated,
            "failed": failed,
            "verification": verification,
        },
        "workflow_id": WORKFLOW_ID,
        "workflow_version": WORKFLOW_VERSION,
        "updated": updated,
        "failed": failed,
        "verification": verification,
        "all_verified": not failed and all(row.get("verified") for row in verification),
        "limitations": [
            "Only Moneybird product records were changed; recurring invoices, subscription templates, subscriptions, and existing invoices were not changed."
        ],
    }


@mcp.tool(
    annotations=WRITE_ANNOTATIONS,
    tags={"domain:products", "capability:execute", f"workflow:{WORKFLOW_ID}"},
)
def bulk_update_product_prices_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Execute and independently verify the exact product price batch only after explicit preview approval."""
    client = ctx.get_client()
    return run_approved_write(
        client,
        approval_id,
        "bulk_update_product_prices",
        _execute_bulk_price_update,
    )
