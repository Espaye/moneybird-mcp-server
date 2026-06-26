from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from moneybird_mcp_server import get_client

PROPOSAL_PATH = ROOT / "outputs" / "moneybird_2026_category_proposal_v3.json"
OUTPUT_DIR = ROOT / "outputs"

# Keep the user-approved mapping centralized and explicit.
CATEGORY_SPECS: dict[str, dict[str, Any]] = {
    "Gereedschap, kleine inventaris en werkkleding": {
        "account_name": "Gereedschap, kleine inventaris en werkkleding",
        "account_type": "expenses",
        "account_id": "46102",
        "rgs_code": "WBedAlkOal",
    },
    "Huur machines en gereedschap": {
        "account_name": "Huur machines en gereedschap",
        "account_type": "expenses",
        "account_id": "46103",
        "rgs_code": "WBedAlkOal",
    },
    "Onderhoud en reparatie machines/voertuigen": {
        "account_name": "Onderhoud en reparatie machines/voertuigen",
        "account_type": "expenses",
        "account_id": "46104",
        "rgs_code": "WBedAlkOal",
    },
    "Afvalverwerking": {
        "account_name": "Afvalverwerking",
        "account_type": "expenses",
        "account_id": "46105",
        "rgs_code": "WBedAlkOal",
    },
    "Opleiding / studiereis": {
        "account_name": "Opleiding / studiereis",
        "account_type": "expenses",
        "account_id": "46136",
        "rgs_code": "WBedAlkOal",
    },
    "Investering / verbouwing loods Follega": {
        "purchase_account_name": "Verbouwing loods Follega (te activeren)",
        "purchase_account_type": "expenses",
        "purchase_account_id": "46106",
        "purchase_rgs_code": "WBedAlkOal",
        "asset_account_name": "Verbouwing loods Follega",
        "asset_account_type": "non_current_assets",
        "asset_account_id": "02020",
        # Inference: a generic material fixed-assets code is the safest
        # available match for a capitalized loods/verbouwingsproject.
        "asset_rgs_code": "BMva",
    },
    "Bestaand: Vakliteratuur": {
        "reuse_existing_name": "Vakliteratuur",
    },
}

PERIODS = {
    "profit_loss_2025": "20250101..20251231",
    "profit_loss_2026": "20260101..20261231",
    "balance_sheet_2025": "20250101..20251231",
    "balance_sheet_2026": "20260101..20261231",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the Moneybird changes. Without this flag, a dry-run is produced.",
    )
    return parser.parse_args()


def now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def load_rows() -> list[dict[str, Any]]:
    payload = json.loads(PROPOSAL_PATH.read_text(encoding="utf-8"))
    unique_rows: dict[str, dict[str, Any]] = {}
    for row in payload["rows"]:
        unique_rows.setdefault(str(row["detail_id"]), row)
    return list(unique_rows.values())


def report_snapshot(client: Any) -> dict[str, Any]:
    snapshots: dict[str, Any] = {}
    for label, period in PERIODS.items():
        endpoint = "profit_loss" if label.startswith("profit_loss") else "balance_sheet"
        snapshots[label] = client._request(
            "GET",
            f"/{client.administration_id}/reports/{endpoint}.json",
            {"period": period},
        )
    return snapshots


def sum_report_values(report_section: dict[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    if not report_section:
        return result
    for key, value in report_section.items():
        if isinstance(value, dict) and "ledger_accounts" in value:
            for item in value["ledger_accounts"]:
                result[str(item["ledger_account_id"])] = float(item["value"])
    return result


def ensure_accounts(
    client: Any,
    ledger_accounts: list[dict[str, Any]],
    *,
    apply: bool,
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]]]:
    by_name = {item["name"]: item for item in ledger_accounts}
    category_targets: dict[str, dict[str, str]] = {}
    created_accounts: list[dict[str, Any]] = []

    for category, spec in CATEGORY_SPECS.items():
        reuse_name = spec.get("reuse_existing_name")
        if reuse_name:
            existing = by_name[reuse_name]
            category_targets[category] = {
                "purchase_ledger_id": str(existing["id"]),
            }
            continue

        if "purchase_account_name" not in spec:
            account_name = spec["account_name"]
            existing = by_name.get(account_name)
            if existing:
                category_targets[category] = {
                    "purchase_ledger_id": str(existing["id"]),
                }
                continue

            if not apply:
                category_targets[category] = {
                    "purchase_ledger_id": f"DRYRUN:{account_name}",
                }
                created_accounts.append(
                    {
                        "name": account_name,
                        "account_type": spec["account_type"],
                        "account_id": spec["account_id"],
                        "rgs_code": spec["rgs_code"],
                        "dry_run": True,
                    }
                )
                continue

            created = client._request(
                "POST",
                f"/{client.administration_id}/ledger_accounts.json",
                body={
                    "rgs_code": spec["rgs_code"],
                    "ledger_account": {
                        "name": account_name,
                        "account_type": spec["account_type"],
                        "account_id": spec["account_id"],
                    },
                },
            )
            by_name[account_name] = created
            category_targets[category] = {
                "purchase_ledger_id": str(created["id"]),
            }
            created_accounts.append(created)
            continue

        purchase_name = spec["purchase_account_name"]
        asset_name = spec["asset_account_name"]
        purchase_existing = by_name.get(purchase_name)
        asset_existing = by_name.get(asset_name)

        if not purchase_existing:
            if not apply:
                purchase_existing = {"id": f"DRYRUN:{purchase_name}", "name": purchase_name}
                created_accounts.append(
                    {
                        "name": purchase_name,
                        "account_type": spec["purchase_account_type"],
                        "account_id": spec["purchase_account_id"],
                        "rgs_code": spec["purchase_rgs_code"],
                        "dry_run": True,
                    }
                )
            else:
                purchase_existing = client._request(
                    "POST",
                    f"/{client.administration_id}/ledger_accounts.json",
                    body={
                        "rgs_code": spec["purchase_rgs_code"],
                        "ledger_account": {
                            "name": purchase_name,
                            "account_type": spec["purchase_account_type"],
                            "account_id": spec["purchase_account_id"],
                        },
                    },
                )
                by_name[purchase_name] = purchase_existing
                created_accounts.append(purchase_existing)

        if not asset_existing:
            if not apply:
                asset_existing = {"id": f"DRYRUN:{asset_name}", "name": asset_name}
                created_accounts.append(
                    {
                        "name": asset_name,
                        "account_type": spec["asset_account_type"],
                        "account_id": spec["asset_account_id"],
                        "rgs_code": spec["asset_rgs_code"],
                        "dry_run": True,
                    }
                )
            else:
                asset_existing = client._request(
                    "POST",
                    f"/{client.administration_id}/ledger_accounts.json",
                    body={
                        "rgs_code": spec["asset_rgs_code"],
                        "ledger_account": {
                            "name": asset_name,
                            "account_type": spec["asset_account_type"],
                            "account_id": spec["asset_account_id"],
                        },
                    },
                )
                by_name[asset_name] = asset_existing
                created_accounts.append(asset_existing)

        category_targets[category] = {
            "purchase_ledger_id": str(purchase_existing["id"]),
            "asset_ledger_id": str(asset_existing["id"]),
        }

    return category_targets, created_accounts


def build_document_updates(
    client: Any,
    rows: list[dict[str, Any]],
    category_targets: dict[str, dict[str, str]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    originals: dict[str, dict[str, Any]] = {}
    updates: dict[str, dict[str, Any]] = {}
    journal_entries: list[dict[str, Any]] = []

    for row in rows:
        document_id = str(row["document_id"])
        doc_type = str(row["document_type"])
        body_root = "purchase_invoice" if doc_type == "purchase_invoices" else "receipt"
        path_name = "purchase_invoices" if doc_type == "purchase_invoices" else "receipts"

        if document_id not in originals:
            originals[document_id] = client._request(
                "GET",
                f"/{client.administration_id}/documents/{path_name}/{document_id}.json",
            )

        updates.setdefault(
            document_id,
            {
                "document_id": document_id,
                "document_type": doc_type,
                "document_date": row["document_date"],
                "contact_name": row["contact_name"],
                "body_root": body_root,
                "path_name": path_name,
                "details_attributes": {},
                "lines": [],
            },
        )
        update = updates[document_id]
        detail_idx = str(len(update["details_attributes"]))
        update["details_attributes"][detail_idx] = {
            "id": row["detail_id"],
            "ledger_account_id": category_targets[row["proposed_category"]][
                "purchase_ledger_id"
            ],
        }
        update["lines"].append(
            {
                "detail_id": row["detail_id"],
                "description": row["detail_description"],
                "amount_excl_tax": row["detail_total_excl_tax"],
                "from_category": "Ongecategoriseerde uitgaven",
                "to_category": row["proposed_category"],
            }
        )

        if row["proposed_category"] == "Investering / verbouwing loods Follega":
            journal_entries.append(
                {
                    "reference": f"ACT-{document_id}-{row['detail_id']}",
                    "date": row["document_date"],
                    "description": row["detail_description"],
                    "contact_name": row["contact_name"],
                    "document_id": document_id,
                    "detail_id": row["detail_id"],
                    "amount_excl_tax": float(row["detail_total_excl_tax"]),
                    "debit_ledger_account_id": category_targets[row["proposed_category"]][
                        "asset_ledger_id"
                    ],
                    "credit_ledger_account_id": category_targets[row["proposed_category"]][
                        "purchase_ledger_id"
                    ],
                }
            )

    return originals, updates, journal_entries


def apply_updates(client: Any, updates: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for update in updates.values():
        response = client._request(
            "PATCH",
            f"/{client.administration_id}/documents/{update['path_name']}/{update['document_id']}.json",
            body={
                update["body_root"]: {
                    "details_attributes": update["details_attributes"],
                }
            },
        )
        results.append(
            {
                "document_id": update["document_id"],
                "document_type": update["document_type"],
                "document_date": update["document_date"],
                "contact_name": update["contact_name"],
                "updated_detail_ids": [line["detail_id"] for line in update["lines"]],
                "response_version": response.get("version"),
            }
        )
    return results


def apply_journals(client: Any, journals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for journal in journals:
        response = client._request(
            "POST",
            f"/{client.administration_id}/documents/general_journal_documents.json",
            body={
                "general_journal_document": {
                    "reference": journal["reference"],
                    "date": journal["date"],
                    "general_journal_document_entries_attributes": {
                        "0": {
                            "ledger_account_id": journal["debit_ledger_account_id"],
                            "debit": journal["amount_excl_tax"],
                            "credit": 0,
                            "description": journal["description"],
                        },
                        "1": {
                            "ledger_account_id": journal["credit_ledger_account_id"],
                            "debit": 0,
                            "credit": journal["amount_excl_tax"],
                            "description": journal["description"],
                        },
                    },
                }
            },
        )
        results.append(
            {
                "reference": journal["reference"],
                "document_id": response.get("id"),
                "date": journal["date"],
                "amount_excl_tax": journal["amount_excl_tax"],
            }
        )
    return results


def category_totals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "amount_excl_tax": 0.0, "document_years": defaultdict(int)}
    )
    for row in rows:
        item = summary[row["proposed_category"]]
        item["count"] += 1
        item["amount_excl_tax"] += float(row["detail_total_excl_tax"])
        item["document_years"][row["document_date"][:4]] += 1

    result = []
    for category, item in sorted(
        summary.items(), key=lambda kv: (-kv[1]["amount_excl_tax"], kv[0])
    ):
        result.append(
            {
                "category": category,
                "count": item["count"],
                "amount_excl_tax": round(item["amount_excl_tax"], 2),
                "document_years": dict(item["document_years"]),
            }
        )
    return result


def report_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for key in before:
        if key.startswith("profit_loss"):
            before_values = sum_report_values(before[key])
            after_values = sum_report_values(after[key])
            changed = {}
            for ledger_id in set(before_values) | set(after_values):
                diff = after_values.get(ledger_id, 0.0) - before_values.get(ledger_id, 0.0)
                if abs(diff) > 0.004:
                    changed[ledger_id] = round(diff, 2)
            delta[key] = {
                "net_profit_before": before[key].get("net_profit"),
                "net_profit_after": after[key].get("net_profit"),
                "total_expenses_before": before[key].get("total_expenses"),
                "total_expenses_after": after[key].get("total_expenses"),
                "changed_ledger_accounts": changed,
            }
        else:
            delta[key] = deepcopy(after[key])
    return delta


def main() -> None:
    args = parse_args()
    client = get_client()
    ledger_accounts = client.list_ledger_accounts()
    rows = load_rows()

    before_reports = report_snapshot(client)
    category_targets, created_accounts = ensure_accounts(
        client, ledger_accounts, apply=args.apply
    )
    originals, updates, journal_entries = build_document_updates(
        client, rows, category_targets
    )

    applied_updates: list[dict[str, Any]] = []
    applied_journals: list[dict[str, Any]] = []
    if args.apply:
        applied_updates = apply_updates(client, updates)
        applied_journals = apply_journals(client, journal_entries)

    after_reports = report_snapshot(client) if args.apply else before_reports
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "apply": args.apply,
        "administration_id": client.administration_id,
        "proposal_source": str(PROPOSAL_PATH.relative_to(ROOT)),
        "created_accounts": created_accounts,
        "category_targets": category_targets,
        "category_totals": category_totals(rows),
        "documents_touched": len(updates),
        "document_updates": list(updates.values()),
        "applied_updates": applied_updates,
        "general_journal_entries": journal_entries,
        "applied_journals": applied_journals,
        "backup_documents": originals,
        "reports_before": before_reports,
        "reports_after": after_reports,
        "reports_delta": report_delta(before_reports, after_reports),
    }

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"moneybird_reclassification_run_{now_stamp()}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(out_path))


if __name__ == "__main__":
    main()
