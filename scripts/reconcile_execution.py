"""Inspect or explicitly reconcile unresolved local write executions.

This is intentionally a local operator command, not an MCP tool.  It can unlock
an occurrence only after the operator records why Moneybird proves the write was
absent, or adopt a write only after exact live verification.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from moneybird.credentials import set_active_administration_id
from moneybird.safety import (
    approval_execution_state,
    list_unresolved_approval_executions,
    reconcile_approval_execution,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--administration-id", required=True)
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="State directory containing moneybird_approvals.sqlite3.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    show = subparsers.add_parser("show")
    show.add_argument("approval_id")
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("approval_id")
    resolve.add_argument(
        "--resolution",
        required=True,
        choices=("proven_absent", "succeeded_verified", "manual_review"),
    )
    resolve.add_argument("--evidence", required=True)
    resolve.add_argument("--reconciled-by", required=True)
    resolve.add_argument(
        "--confirm-approval-id",
        required=True,
        help="Repeat the full approval id to prevent resolving the wrong row.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.data_dir:
        os.environ["MONEYBIRD_MCP_DATA_DIR"] = str(args.data_dir.resolve())
    set_active_administration_id(args.administration_id)
    if args.command == "list":
        result = list_unresolved_approval_executions(
            administration_id=args.administration_id
        )
    elif args.command == "show":
        result = approval_execution_state(
            args.approval_id,
            administration_id=args.administration_id,
        )
    else:
        if args.confirm_approval_id != args.approval_id:
            raise SystemExit("--confirm-approval-id must exactly match approval_id")
        result = reconcile_approval_execution(
            args.approval_id,
            args.resolution,
            evidence=args.evidence,
            reconciled_by=args.reconciled_by,
            administration_id=args.administration_id,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
