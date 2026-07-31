"""Shared fixtures for purchase reconciliation and review tests."""
from decimal import Decimal

LEDGER_ZAK = "100000000000000001"
TAX_21 = "100000000000000002"
LEDGER_PRIV = "100000000000000003"
TAX_GEEN = "100000000000000004"


def line(detail_id, description, price, ledger, tax):
    return {
        "id": detail_id,
        "description": description,
        "price": str(price),
        "amount": "1",
        "ledger_account_id": ledger,
        "tax_rate_id": tax,
    }


def reference_june(doc_id="ref"):
    return {
        "id": doc_id,
        "version": 10,
        "updated_at": "2026-06-19T10:00:00Z",
        "date": "2026-06-19",
        "state": "paid",
        "reference": "1000000001",
        "prices_are_incl_tax": True,
        "total_price_incl_tax": "825.0",
        "contact": {"id": "C1", "company_name": "Example Energy B.V."},
        "details": [
            line("r1", "Termijnnota juni 2026 – stroom zakelijk (60%)", "296.54", LEDGER_ZAK, TAX_21),
            line("r2", "Termijnnota juni 2026 – gas zakelijk (25%)", "82.70", LEDGER_ZAK, TAX_21),
            line("r3", "Termijnnota juni 2026 – stroom privé (40%)", "197.68", LEDGER_PRIV, TAX_GEEN),
            line("r4", "Termijnnota juni 2026 – gas privé (75%)", "248.08", LEDGER_PRIV, TAX_GEEN),
        ],
    }


def target_july(doc_id="tgt", total="825.0"):
    return {
        "id": doc_id,
        "version": 20,
        "updated_at": "2026-07-19T10:00:00Z",
        "date": "2026-07-19",
        "state": "new",
        "reference": "1000000002",
        "prices_are_incl_tax": False,
        "total_price_incl_tax": total,
        "contact": {"id": "C1", "company_name": "Example Energy B.V."},
        "details": [
            line("L1", "Termijnnota juli 2026 – gas en stroom", "681.82", LEDGER_ZAK, TAX_21),
            line("L2", "Termijnnota juli 2026 – stroom privé (40%)", "0.0", LEDGER_PRIV, TAX_GEEN),
        ],
    }


class FakeClient:
    def __init__(self, documents):
        self.administration_id = "ADMIN"
        self._docs = {str(document["id"]): document for document in documents}
        self.update_calls = 0

    def get_document(self, kind, document_id):
        return self._docs[str(document_id)]

    def list_documents(self, kind, *, limit=100, page=1, filter="", period=""):
        documents = list(self._docs.values())
        start = (page - 1) * limit
        return documents[start : start + limit]

    def list_ledger_accounts(self):
        ids = {
            str(item.get("ledger_account_id") or "")
            for document in self._docs.values()
            for item in (document.get("details") or [])
        }
        return [
            {"id": item_id, "active": True, "allowed_document_types": ["purchase_invoice"]}
            for item_id in ids
        ]

    def list_tax_rates(self):
        ids = {
            str(item.get("tax_rate_id") or "")
            for document in self._docs.values()
            for item in (document.get("details") or [])
        }
        return [
            {
                "id": item_id,
                "active": True,
                "tax_rate_type": "purchase_invoice",
                "percentage": "21" if item_id == TAX_21 else None,
            }
            for item_id in ids
        ]

    def update_document(self, kind, document_id, document):
        self.update_calls += 1
        target = self._docs[str(document_id)]
        target["prices_are_incl_tax"] = document.get(
            "prices_are_incl_tax", target.get("prices_are_incl_tax")
        )
        existing = {str(item.get("id")): item for item in target.get("details") or []}
        resulting = list(target.get("details") or [])
        next_id = 1
        for operation in document.get("details_attributes", {}).values():
            detail_id = str(operation.get("id") or "")
            if operation.get("_destroy") == "true":
                resulting = [item for item in resulting if str(item.get("id")) != detail_id]
            elif detail_id:
                existing[detail_id].update(operation)
            else:
                resulting.append({**operation, "id": f"created-{next_id}"})
                next_id += 1
        target["details"] = resulting
        target["total_price_incl_tax"] = str(
            sum((Decimal(str(item.get("price") or "0")) for item in resulting), Decimal("0"))
        )
        target["version"] = int(target.get("version") or 0) + 1
        target["updated_at"] = "2026-07-22T15:00:00Z"
        return target
