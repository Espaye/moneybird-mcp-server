"""Read-only health check against the connected Moneybird administration.

Exercises the real MCP tool functions (the surface the AI model uses) without ever
writing: no *_from_approval tools are called, and no prepare_* staging is executed.
Supply configuration in the parent process; no working-directory ``.env`` is loaded:

    python scripts/healthcheck_readonly.py
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

# Allow running as `python scripts/healthcheck_readonly.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moneybird_mcp import guidance as G
from moneybird_mcp import tools as T

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(label: str, fn) -> object:
    try:
        out = fn()
        summary = ""
        if isinstance(out, dict):
            # show count-ish keys for a quick sanity read
            for key in ("count", "contacts", "ledger_accounts", "tax_rates",
                        "purchase_invoices", "projects", "time_entries", "results"):
                if key in out:
                    val = out[key]
                    summary = f"{key}={len(val) if isinstance(val, list) else val}"
                    break
        results.append((PASS, label, summary))
        return out
    except Exception as exc:  # noqa: BLE001 - we want to record, not crash the sweep
        results.append((FAIL, label, f"{type(exc).__name__}: {exc}"))
        traceback.print_exc()
        return None


print("== READ-ONLY HEALTH CHECK (no writes) ==\n")

# --- Core reference data ---
check("list_administrations", lambda: T.list_administrations())
ledgers = check("list_ledger_accounts", lambda: T.list_ledger_accounts())
check("list_tax_rates", lambda: T.list_tax_rates())
contacts = check("list_contacts(limit=5)", lambda: T.list_contacts(limit=5))

# --- New tools added this session ---
check("list_projects(limit=5)", lambda: T.list_projects(limit=5))
check("list_time_entries(limit=5)", lambda: T.list_time_entries(limit=5))
check("moneybird_request('estimates')", lambda: T.moneybird_request("estimates", {"per_page": 3}))
check("moneybird_request('administrations')", lambda: T.moneybird_request("administrations"))

# --- Documents & search ---
pis = check("list_purchase_documents(purchase_invoice)", lambda: T.list_purchase_documents("purchase_invoice", limit=5))
check("list_purchase_documents(receipt)", lambda: T.list_purchase_documents("receipt", limit=5))
check("suggest_bank_mutation_matches", lambda: T.suggest_bank_mutation_matches(limit=5))
check("search('factuur')", lambda: T.search("factuur", limit=5))

# fetch one real record discovered above (read-only)
def _fetch_first_contact():
    cid = contacts["contacts"][0]["id"] if contacts and contacts.get("contacts") else None
    if not cid:
        return {"results": "no contacts to fetch"}
    return T.fetch(f"contact:{cid}")
check("fetch(contact:<first>)", _fetch_first_contact)

# --- Reports (read-only) ---
check("get_financial_report(profit_loss)", lambda: T.get_financial_report("profit_loss", "this_year"))
check("get_financial_report(balance_sheet)", lambda: T.get_financial_report("balance_sheet", "this_year"))

# --- Guidance layer (skill) ---
check("playbook resource loads", lambda: {"count": len(G.load_playbook())})
check("get_bookkeeping_guide(btw)", lambda: T.get_bookkeeping_guide("btw"))
check("prompt verwerk_achterstand renders", lambda: {"count": len(G.prompt_verwerk_achterstand())})
check("prompt categoriseer_heel_jaar renders", lambda: {"count": len(G.prompt_categoriseer_heel_jaar())})
check("prompt leg_cijfers_uit renders", lambda: {"count": len(G.prompt_leg_cijfers_uit())})

# --- Report ---
print("\n== RESULTS ==")
width = max(len(lbl) for _, lbl, _ in results)
for status, label, summary in results:
    print(f"  [{status}] {label.ljust(width)}  {summary}")

n_fail = sum(1 for s, _, _ in results if s == FAIL)
print(f"\n{len(results) - n_fail}/{len(results)} passed, {n_fail} failed")
