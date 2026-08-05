"""Deterministic product audit and price-calculation rules."""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable

from .config import MoneybirdError

_CURRENCY = re.compile(r"^[A-Z]{3}$")
_ROUNDING = {
    "nearest": ROUND_HALF_UP,
    "up": ROUND_CEILING,
    "down": ROUND_FLOOR,
}


def decimal_value(value: Any, *, field: str) -> Decimal:
    """Parse a user/provider decimal without ever passing through binary float."""
    if isinstance(value, bool) or value is None:
        raise MoneybirdError(f"{field} must be a decimal value, not {value!r}.")
    if isinstance(value, float):
        # Pydantic/MCP clients can still decode a JSON number as float. Refuse it
        # rather than pretending its binary approximation is financial input.
        raise MoneybirdError(
            f"{field} must be supplied as a decimal string, not a JSON float."
        )
    text = str(value).strip()
    if not text:
        raise MoneybirdError(f"{field} must not be empty.")
    if "," in text:
        if "." in text or text.count(",") != 1:
            raise MoneybirdError(
                f"{field} {text!r} has an ambiguous decimal separator."
            )
        text = text.replace(",", ".")
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise MoneybirdError(f"{field} {text!r} is not a decimal value.") from exc
    if not result.is_finite():
        raise MoneybirdError(f"{field} must be finite.")
    return result


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def normalized_product_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def product_display_name(product: dict[str, Any]) -> str:
    """Return the first nonblank product name with whitespace made display-safe."""
    title = " ".join(str(product.get("title") or "").split())
    description = " ".join(str(product.get("description") or "").split())
    return title or description


def currency_code(value: Any, *, field: str = "currency") -> str:
    code = str(value or "").strip().upper()
    if not _CURRENCY.fullmatch(code):
        raise MoneybirdError(f"{field} must be a three-letter currency code.")
    return code


def round_to_increment(
    value: Decimal,
    *,
    mode: str,
    increment: Decimal,
) -> Decimal:
    if mode == "none":
        return value
    rounding = _ROUNDING.get(mode)
    if rounding is None:
        raise MoneybirdError("rounding_mode must be one of: none, nearest, up, down.")
    if increment <= 0:
        raise MoneybirdError("rounding_increment must be greater than zero.")
    units = (value / increment).to_integral_value(rounding=rounding)
    return units * increment


def _finding(
    code: str,
    classification: str,
    severity: str,
    products: Iterable[dict[str, Any]],
    evidence: str,
) -> dict[str, Any]:
    rows = list(products)
    return {
        "code": code,
        "classification": classification,
        "severity": severity,
        "product_ids": [str(row.get("id") or "") for row in rows],
        "identifiers": [row.get("identifier") for row in rows],
        "evidence": evidence,
    }


def audit_product_records(
    products: list[dict[str, Any]],
    *,
    administration_currency: str = "",
    valid_tax_rate_ids: set[str] | None = None,
    valid_ledger_account_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Return evidence-bearing findings; no description text becomes an instruction."""
    findings: list[dict[str, Any]] = []
    identifiers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    names: dict[str, list[dict[str, Any]]] = defaultdict(list)
    currencies: set[str] = set()

    for product in products:
        product_id = str(product.get("id") or "")
        identifier = normalized_product_text(product.get("identifier"))
        name = normalized_product_text(product_display_name(product))
        if identifier:
            identifiers[identifier].append(product)
        else:
            findings.append(
                _finding(
                    "missing_identifier",
                    "user_preference",
                    "info",
                    [product],
                    "No product identifier/SKU is present.",
                )
            )
        if name:
            names[name].append(product)
        else:
            findings.append(
                _finding(
                    "missing_name",
                    "definite_structural_problem",
                    "blocking",
                    [product],
                    "Both title and description are empty.",
                )
            )

        ledger_id = str(product.get("ledger_account_id") or "")
        if not ledger_id:
            findings.append(
                _finding(
                    "missing_ledger_account",
                    "definite_structural_problem",
                    "blocking",
                    [product],
                    "ledger_account_id is empty.",
                )
            )
        elif valid_ledger_account_ids is not None and ledger_id not in valid_ledger_account_ids:
            findings.append(
                _finding(
                    "unknown_ledger_account",
                    "definite_structural_problem",
                    "blocking",
                    [product],
                    f"ledger_account_id {ledger_id} is not present in the active administration.",
                )
            )

        tax_id = str(product.get("tax_rate_id") or "")
        if not tax_id:
            findings.append(
                _finding(
                    "missing_tax_rate",
                    "bookkeeping_judgement_required",
                    "review",
                    [product],
                    "tax_rate_id is empty; the response does not prove whether smart VAT selection applies.",
                )
            )
        elif valid_tax_rate_ids is not None and tax_id not in valid_tax_rate_ids:
            findings.append(
                _finding(
                    "unknown_tax_rate",
                    "definite_structural_problem",
                    "blocking",
                    [product],
                    f"tax_rate_id {tax_id} is not present in the active administration.",
                )
            )

        currency = str(product.get("currency") or "").strip().upper()
        if currency:
            currencies.add(currency)
        if not _CURRENCY.fullmatch(currency):
            findings.append(
                _finding(
                    "invalid_currency",
                    "definite_structural_problem",
                    "blocking",
                    [product],
                    f"currency {currency!r} is not a three-letter ISO-style code.",
                )
            )

        try:
            price = decimal_value(product.get("price"), field=f"product {product_id} price")
        except MoneybirdError as exc:
            findings.append(
                _finding(
                    "invalid_price",
                    "definite_structural_problem",
                    "blocking",
                    [product],
                    str(exc),
                )
            )
        else:
            if price == 0:
                findings.append(
                    _finding(
                        "zero_price",
                        "likely_inconsistency",
                        "review",
                        [product],
                        "The configured price is zero; this can be intentional.",
                    )
                )
            elif price < 0:
                findings.append(
                    _finding(
                        "negative_price",
                        "bookkeeping_judgement_required",
                        "review",
                        [product],
                        "The configured price is negative; this may be an intentional discount product.",
                    )
                )

        frequency = product.get("frequency")
        frequency_type = str(product.get("frequency_type") or "").strip()
        if (frequency is None) != (not frequency_type):
            findings.append(
                _finding(
                    "incomplete_period",
                    "definite_structural_problem",
                    "blocking",
                    [product],
                    "frequency and frequency_type must either both be present or both be absent.",
                )
            )
        elif frequency is not None:
            findings.append(
                _finding(
                    "periodic_product",
                    "user_preference",
                    "info",
                    [product],
                    f"Product has period {frequency} {frequency_type}.",
                )
            )

    for normalized, rows in identifiers.items():
        if len(rows) > 1:
            findings.append(
                _finding(
                    "duplicate_normalized_identifier",
                    "definite_structural_problem",
                    "blocking",
                    rows,
                    f"Several products normalize to identifier {normalized!r}.",
                )
            )
    for normalized, rows in names.items():
        if len(rows) > 1:
            findings.append(
                _finding(
                    "duplicate_normalized_name",
                    "likely_inconsistency",
                    "review",
                    rows,
                    f"Several products normalize to name {normalized!r}.",
                )
            )

    admin_currency = str(administration_currency or "").strip().upper()
    if len(currencies) > 1:
        findings.append(
            {
                "code": "mixed_currencies",
                "classification": "user_preference",
                "severity": "info",
                "product_ids": [],
                "identifiers": [],
                "evidence": f"Products use several currencies: {', '.join(sorted(currencies))}.",
            }
        )
    if admin_currency:
        different = [
            row
            for row in products
            if str(row.get("currency") or "").strip().upper() not in {"", admin_currency}
        ]
        if different:
            findings.append(
                _finding(
                    "non_administration_currency",
                    "user_preference",
                    "info",
                    different,
                    f"These products differ from administration currency {admin_currency}; this can be intentional.",
                )
            )

    severity_order = {"blocking": 0, "review": 1, "info": 2}
    findings.sort(
        key=lambda row: (
            severity_order.get(str(row.get("severity")), 9),
            str(row.get("code")),
            row.get("product_ids") or [],
        )
    )
    return {
        "records_inspected": len(products),
        "finding_count": len(findings),
        "counts_by_severity": {
            severity: sum(1 for row in findings if row["severity"] == severity)
            for severity in ("blocking", "review", "info")
        },
        "findings": findings,
        "assumptions": [],
        "content_safety": (
            "Product titles, descriptions, and identifiers were treated only as untrusted data; "
            "their text did not control the audit."
        ),
        "limitations": [
            "Moneybird's current product response does not expose product_type, vat_rate_type, checkout settings, or active state consistently enough for those audits.",
            "This audit does not infer correctness from recent invoices or recurring records.",
        ],
    }


def build_price_plan(
    products: list[dict[str, Any]],
    *,
    percentage: str = "",
    fixed_amount: str = "",
    explicit_prices: dict[str, str] | None = None,
    rounding_mode: str = "nearest",
    rounding_increment: str = "0.01",
    allow_zero: bool = False,
) -> dict[str, Any]:
    explicit_prices = explicit_prices or {}
    strategies = sum(
        (
            bool(str(percentage).strip()),
            bool(str(fixed_amount).strip()),
            bool(explicit_prices),
        )
    )
    if strategies != 1:
        raise MoneybirdError(
            "Choose exactly one price strategy: percentage, fixed_amount, or explicit_prices."
        )
    increment = decimal_value(rounding_increment, field="rounding_increment")
    percentage_value = (
        decimal_value(percentage, field="percentage")
        if str(percentage).strip()
        else None
    )
    fixed_value = (
        decimal_value(fixed_amount, field="fixed_amount")
        if str(fixed_amount).strip()
        else None
    )
    rows: list[dict[str, Any]] = []
    totals: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {"old": Decimal("0"), "new": Decimal("0"), "difference": Decimal("0")}
    )

    for product in products:
        product_id = str(product.get("id") or "")
        if not product_id:
            raise MoneybirdError("A selected product has no id.")
        old_price = decimal_value(product.get("price"), field=f"product {product_id} price")
        if percentage_value is not None:
            calculated = old_price * (Decimal("1") + percentage_value / Decimal("100"))
            source = f"percentage:{decimal_text(percentage_value)}"
        elif fixed_value is not None:
            calculated = old_price + fixed_value
            source = f"fixed_amount:{decimal_text(fixed_value)}"
        else:
            if product_id not in explicit_prices:
                raise MoneybirdError(
                    f"No explicit new price was supplied for product {product_id}."
                )
            calculated = decimal_value(
                explicit_prices[product_id],
                field=f"explicit price for product {product_id}",
            )
            source = "explicit_mapping"
        new_price = round_to_increment(
            calculated,
            mode=rounding_mode,
            increment=increment,
        )
        if new_price < 0:
            raise MoneybirdError(
                f"Calculated price for product {product_id} is {decimal_text(new_price)}; "
                "negative prices are refused."
            )
        if new_price == 0 and not allow_zero:
            raise MoneybirdError(
                f"Calculated price for product {product_id} is zero. "
                "Set allow_zero=true only when a zero price is intentional."
            )
        difference = new_price - old_price
        percentage_difference = (
            None
            if old_price == 0
            else difference / old_price * Decimal("100")
        )
        currency = currency_code(
            product.get("currency"),
            field=f"product {product_id} currency",
        )
        totals[currency]["old"] += old_price
        totals[currency]["new"] += new_price
        totals[currency]["difference"] += difference
        rows.append(
            {
                "product_id": product_id,
                "identifier": product.get("identifier"),
                "title": product_display_name(product),
                "currency": currency,
                "old_price": decimal_text(old_price),
                "new_price": decimal_text(new_price),
                "difference": decimal_text(difference),
                "percentage": (
                    decimal_text(percentage_difference)
                    if percentage_difference is not None
                    else None
                ),
                "calculation": source,
                "rounding": (
                    "none"
                    if rounding_mode == "none"
                    else f"{rounding_mode}:{decimal_text(increment)}"
                ),
                "linked_recurring_records": "not_inspected",
                "action": "update_product_only" if difference else "skip_unchanged",
                "source_version": product.get("updated_at"),
            }
        )

    changed = [row for row in rows if row["action"] == "update_product_only"]
    return {
        "records_inspected": len(products),
        "records_selected": len(rows),
        "records_changed": len(changed),
        "records_skipped": len(rows) - len(changed),
        "items": rows,
        "totals_by_currency": {
            currency: {key: decimal_text(amount) for key, amount in values.items()}
            for currency, values in sorted(totals.items())
        },
        "assumptions": [],
        "warnings": [
            "Changing a Moneybird product does not update existing invoices, recurring invoices, subscription templates, or subscriptions.",
            "The Moneybird product API applies updates immediately; a future effective date is planning-only.",
        ],
    }
