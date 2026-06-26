from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from moneybird_mcp_server import get_client


SOURCE_PATH = ROOT / "outputs" / "moneybird_remaining_uncategorized_sources_2025_2026.json"
OUTPUT_DIR = ROOT / "outputs"
UNCAT_ID = "463484440937497702"

REUSE_ACCOUNT_NAMES = {
    "Afvalverwerking": "Afvalverwerking",
    "Bestaand: Huisvestingskosten": "Huisvestingskosten",
    "Bestaand: Vakliteratuur": "Vakliteratuur",
    "Gereedschap, kleine inventaris en werkkleding": "Gereedschap, kleine inventaris en werkkleding",
    "Huur machines en gereedschap": "Huur machines en gereedschap",
    "Onderhoud en reparatie machines/voertuigen": "Onderhoud en reparatie machines/voertuigen",
    "Investering / verbouwing loods Follega": "Verbouwing loods Follega (te activeren)",
}

FOLLEGA_ASSET_ACCOUNT = "Verbouwing loods Follega"
JOURNAL_PREFIX = "ACTR2-"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def load_rows() -> list[dict[str, Any]]:
    payload = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    unique_rows: dict[str, dict[str, Any]] = {}
    for row in payload["rows"]:
        unique_rows.setdefault(str(row["detail_id"]), row)
    rows = list(unique_rows.values())
    for row in rows:
        row["proposed_category"] = classify(row)
    return rows


def classify(row: dict[str, Any]) -> str:
    contact = row["contact_name"]
    description = row["description"].lower()

    if contact in {
        "Spekco Montage-, Dak- en Geveltechniek",
        "K. Wijma B.V.",
        "Bouwbedrijf Zeilstra",
        "Visser handel en training",
        "Handelsbedrijf Mineralis B.V.",
        "Beekagri B.V.",
    }:
        return "Investering / verbouwing loods Follega"

    if contact in {
        "Technisch Bedrijf Minnesma Munnekeburen B.V.",
        "H. Overeem Bedrijfsdeuren B.V.",
        "Saval B.V.",
        "Sloten Fix",
        "Megapraxis",
    }:
        return "Bestaand: Huisvestingskosten"

    if contact == "Reparatiebedrijf H. en A. de Vries":
        if "gereedschap" in description:
            return "Gereedschap, kleine inventaris en werkkleding"
        return "Onderhoud en reparatie machines/voertuigen"

    if contact == "Ferdinands Reparatie & Verhuur":
        return "Huur machines en gereedschap"

    if contact in {
        "Handelsonderneming Koopstra B.V.",
        "A.Th. de Boer & Zonen B.V.",
        "VRB Friesland B.V.",
        "Karwei",
        "Olega",
        "Welkoop Winkel B.V.",
        "HypoStore B.V.",
        "ACTION",
    }:
        return "Gereedschap, kleine inventaris en werkkleding"

    if contact == "Tjitte de Wolff":
        return "Onderhoud en reparatie machines/voertuigen"

    if contact == "Renewi Nederland B.V.":
        return "Afvalverwerking"

    if contact == "Land- en Tuinbouw Organisatie Noord (bij verkorting LTO Noord)":
        return "Bestaand: Vakliteratuur"

    raise RuntimeError(f"No category mapping for row: {row}")


def resolve_ledger_ids(client: Any) -> dict[str, str]:
    ledger_accounts = client.list_ledger_accounts()
    by_name = {item["name"]: item for item in ledger_accounts}
    resolved: dict[str, str] = {}
    for category, account_name in REUSE_ACCOUNT_NAMES.items():
        resolved[category] = str(by_name[account_name]["id"])
    resolved[FOLLEGA_ASSET_ACCOUNT] = str(by_name[FOLLEGA_ASSET_ACCOUNT]["id"])
    return resolved


def existing_journal_refs(client: Any) -> set[str]:
    docs = client._request(
        "GET",
        f"/{client.administration_id}/documents/general_journal_documents.json",
        {"filter": "period:20250101..20261231", "per_page": 100, "page": 1},
    )
    return {
        str(doc.get("reference") or "")
        for doc in docs
        if str(doc.get("reference") or "").startswith(JOURNAL_PREFIX)
    }


def build_changes(
    client: Any,
    rows: list[dict[str, Any]],
    ledger_ids: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    originals: dict[str, dict[str, Any]] = {}
    updates: dict[str, dict[str, Any]] = {}
    journals: list[dict[str, Any]] = []

    for row in rows:
        document_id = str(row["document_id"])
        path_name = row["source_type"]
        body_root = "purchase_invoice" if path_name == "purchase_invoices" else "receipt"

        if document_id not in originals:
            originals[document_id] = client._request(
                "GET",
                f"/{client.administration_id}/documents/{path_name}/{document_id}.json",
            )
            time.sleep(0.08)

        update = updates.setdefault(
            document_id,
            {
                "document_id": document_id,
                "path_name": path_name,
                "body_root": body_root,
                "date": row["date"],
                "contact_name": row["contact_name"],
                "details_attributes": {},
                "lines": [],
            },
        )

        index_key = str(len(update["details_attributes"]))
        update["details_attributes"][index_key] = {
            "id": row["detail_id"],
            "ledger_account_id": ledger_ids[row["proposed_category"]],
        }
        update["lines"].append(
            {
                "detail_id": row["detail_id"],
                "description": row["description"],
                "amount_excl_tax": row["amount_excl_tax"],
                "to_category": row["proposed_category"],
            }
        )

        if row["proposed_category"] == "Investering / verbouwing loods Follega":
            journals.append(
                {
                    "reference": f"{JOURNAL_PREFIX}{document_id}-{row['detail_id']}",
                    "date": row["date"],
                    "document_id": document_id,
                    "detail_id": row["detail_id"],
                    "description": row["description"],
                    "contact_name": row["contact_name"],
                    "amount_excl_tax": float(row["amount_excl_tax"]),
                    "purchase_ledger_account_id": ledger_ids[row["proposed_category"]],
                    "asset_ledger_account_id": ledger_ids[FOLLEGA_ASSET_ACCOUNT],
                }
            )

    return updates, journals, originals


def apply_document_updates(client: Any, updates: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for update in updates.values():
        response = client._request(
            "PATCH",
            f"/{client.administration_id}/documents/{update['path_name']}/{update['document_id']}.json",
            body={update["body_root"]: {"details_attributes": update["details_attributes"]}},
        )
        results.append(
            {
                "document_id": update["document_id"],
                "path_name": update["path_name"],
                "response_version": response.get("version"),
                "detail_ids": [line["detail_id"] for line in update["lines"]],
            }
        )
        time.sleep(0.12)
    return results


def apply_journals(
    client: Any,
    journals: list[dict[str, Any]],
    existing_refs: set[str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for journal in journals:
        if journal["reference"] in existing_refs:
            results.append({"reference": journal["reference"], "skipped": True})
            continue

        amount = abs(float(journal["amount_excl_tax"]))
        if journal["amount_excl_tax"] >= 0:
            debit_account = journal["asset_ledger_account_id"]
            credit_account = journal["purchase_ledger_account_id"]
        else:
            debit_account = journal["purchase_ledger_account_id"]
            credit_account = journal["asset_ledger_account_id"]

        response = client._request(
            "POST",
            f"/{client.administration_id}/documents/general_journal_documents.json",
            body={
                "general_journal_document": {
                    "reference": journal["reference"],
                    "date": journal["date"],
                    "general_journal_document_entries_attributes": {
                        "0": {
                            "ledger_account_id": debit_account,
                            "debit": amount,
                            "credit": 0,
                            "description": journal["description"],
                        },
                        "1": {
                            "ledger_account_id": credit_account,
                            "debit": 0,
                            "credit": amount,
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
                "amount_excl_tax": journal["amount_excl_tax"],
            }
        )
        time.sleep(0.12)
    return results


def verify_remaining(client: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    remaining: list[dict[str, Any]] = []
    for row in rows:
        path_name = row["source_type"]
        doc = client._request(
            "GET",
            f"/{client.administration_id}/documents/{path_name}/{row['document_id']}.json",
        )
        detail = next(d for d in doc["details"] if str(d["id"]) == str(row["detail_id"]))
        if str(detail["ledger_account_id"]) == UNCAT_ID:
            remaining.append(
                {
                    "document_id": row["document_id"],
                    "detail_id": row["detail_id"],
                    "contact_name": row["contact_name"],
                    "description": row["description"],
                }
            )
        time.sleep(0.08)
    return {"remaining_count": len(remaining), "remaining": remaining}


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "amount": 0.0, "rows": []}
    )
    for row in rows:
        item = summary[row["proposed_category"]]
        item["count"] += 1
        item["amount"] += float(row["amount_excl_tax"])
        if len(item["rows"]) < 10:
            item["rows"].append(
                {
                    "contact_name": row["contact_name"],
                    "description": row["description"],
                    "amount_excl_tax": row["amount_excl_tax"],
                }
            )
    return [
        {
            "category": category,
            "count": item["count"],
            "amount_excl_tax": round(item["amount"], 2),
            "rows": item["rows"],
        }
        for category, item in sorted(summary.items(), key=lambda kv: -kv[1]["amount"])
    ]


def main() -> None:
    args = parse_args()
    client = get_client()
    rows = load_rows()
    ledger_ids = resolve_ledger_ids(client)
    updates, journals, originals = build_changes(client, rows, ledger_ids)

    applied_updates: list[dict[str, Any]] = []
    applied_journals: list[dict[str, Any]] = []
    verification = {"remaining_count": None, "remaining": []}

    if args.apply:
        applied_updates = apply_document_updates(client, updates)
        applied_journals = apply_journals(client, journals, existing_journal_refs(client))
        verification = verify_remaining(client, rows)

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "apply": args.apply,
        "source": str(SOURCE_PATH.relative_to(ROOT)),
        "summary": summarize(rows),
        "updates": list(updates.values()),
        "journals": journals,
        "applied_updates": applied_updates,
        "applied_journals": applied_journals,
        "backup_documents": originals,
        "verification": verification,
    }

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"moneybird_remaining_uncategorized_run_{now_stamp()}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(out_path))


if __name__ == "__main__":
    main()
