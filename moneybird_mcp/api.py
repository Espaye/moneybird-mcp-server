"""The supported surface for tool packages that live outside this distribution.

Everything here is public API under semantic versioning. The underscore modules
behind it are not, and may be rearranged in a patch release. An out-of-tree
distribution that imports from those modules will break; one that imports only
from here will not.

The surface is deliberately small, and it is small in a particular way: it
exposes the *entry points* to this server's machinery, not the machinery. There
is no ``mcp`` object here, because holding the server object is how a tool ends
up registered without the error translation every other tool goes through --
:data:`tool` is the same decorator this distribution's own tools use, so an
extension cannot accidentally get a weaker one. There is no client constructor
either, because building a client directly is how credential mode, tenant
confinement and the test patch point get bypassed; :func:`get_client` resolves
through the same seam the built-in tools use, at call time.

A minimal extension looks like this, in a distribution declaring
``moneybird_mcp.tools`` entry points::

    from moneybird_mcp.api import (
        MoneybirdError, PREPARE_ANNOTATIONS, WriteSpec, ApprovalId,
        get_client, mark_write_dispatch_started, mark_write_verifying,
        register_approval_executor, register_write_spec,
        run_approved_write, stage_write, tool,
    )

    @tool(annotations=PREPARE_ANNOTATIONS, tags={"domain:example"})
    def prepare_example(record_id: MoneybirdId) -> dict:
        \"\"\"Preview an action; nothing is sent until it is approved.\"\"\"
        client = get_client()
        ...
        return stage_write("example", summary=..., payload=..., preview=...)

    def _execute(client, payload):
        mark_write_dispatch_started()   # the last safe retry boundary
        ...                             # the mutation
        mark_write_verifying()          # everything after this is a read-back
        ...

    def example_from_approval(approval_id: ApprovalId) -> dict:
        return run_approved_write(get_client(), approval_id, "example", _execute)

    register_write_spec("example", WriteSpec(1, "...", "...", "...", "..."))
    register_approval_executor("example", example_from_approval)

Some of what is here is vocabulary rather than machinery, and
:func:`validate_explicit_document_lines` is the clearest case. An extension that
lets a user state exact document lines has to prove every ledger account and tax
rate, parse the prices as exact decimals, and total them -- and a second
implementation of that is a second rounding behaviour. The built-in tools call
the same function, so there is one.

Growing this surface is a compatible change; removing or renaming anything in
:data:`__all__` is not. ``tests/test_extension_boundary.py`` pins the list.
"""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from ._registration import Registration
from .config import (
    MONTH_CAPPED_REPORTS,
    PAGINATED_REPORTS,
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    WRITE_ANNOTATIONS,
    MoneybirdError,
    MoneybirdHTTPError,
)
from .document_lines import (
    ExplicitDocumentLines,
    validate_explicit_document_lines,
)
from .formatting import (
    clean_dict,
    compact_general_journal_summary,
    duplicate_fingerprint,
    money_decimal,
    report_period_months,
    symbolic_period_months,
)
from .invoicing import details_attributes_payload, prepare_general_journal_entries
from .rate_budget import affordable_batches as rate_budget_affordable_batches
from .rate_budget import reset_seconds as rate_budget_reset_seconds
from .write_contracts import (
    WriteSpec,
    register_write_spec,
    verify_general_journal_payload,
)

#: Version of this seam. Bumped when something is removed or changes meaning;
#: additions do not bump it.
API_VERSION = 1

# Nothing under `moneybird_mcp.tools` may be imported while this module is
# executing. Importing any submodule of that package runs its `__init__`, which
# loads installed extensions -- and an extension's first line is
# `from moneybird_mcp.api import ...`, reaching this module while it is still
# half-built. A process that imported the seam before the tools package would
# then fail on any machine with an extension installed, which is to say: on
# exactly the installations this seam exists to serve.
#
# So every name that lives behind the tools package is resolved on use instead.
# By then the package is fully imported, or importing it is the right thing to do.
_LAZY_EXPORTS = {
    "ApprovalId": ("moneybird_mcp.tools._params", "ApprovalId"),
    "DateString": ("moneybird_mcp.tools._params", "DateString"),
    "Limit": ("moneybird_mcp.tools._params", "Limit"),
    "MoneybirdId": ("moneybird_mcp.tools._params", "MoneybirdId"),
    "OptionalDateString": ("moneybird_mcp.tools._params", "OptionalDateString"),
    "Page": ("moneybird_mcp.tools._params", "Page"),
    "Period": ("moneybird_mcp.tools._params", "Period"),
    "PriceString": ("moneybird_mcp.tools._params", "PriceString"),
    "ReportName": ("moneybird_mcp.tools._params", "ReportName"),
    "mark_write_dispatch_started": (
        "moneybird_mcp.tools._writes",
        "mark_write_dispatch_started",
    ),
    "mark_write_verifying": ("moneybird_mcp.tools._writes", "mark_write_verifying"),
    "run_approved_write": ("moneybird_mcp.tools._writes", "run_approved_write"),
    "stage_write": ("moneybird_mcp.tools._writes", "stage_write"),
}

if TYPE_CHECKING:  # never executed: the names above exist for readers and linters
    from .tools._params import (
        ApprovalId,
        DateString,
        Limit,
        MoneybirdId,
        OptionalDateString,
        Page,
        Period,
        PriceString,
        ReportName,
    )
    from .tools._writes import (
        mark_write_dispatch_started,
        mark_write_verifying,
        run_approved_write,
        stage_write,
    )


def __getattr__(name: str) -> Any:
    """Resolve the tools-backed names on first use (PEP 562)."""
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return getattr(importlib.import_module(module_name), attribute)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))


def tool(*args: Any, **kwargs: Any) -> Any:
    """Register an MCP tool.

    Forwards to the wrapped decorator the built-in tools use, which installs the
    error translation that reports a ``MoneybirdError`` as a handled refusal
    rather than a server crash, and records the tool name against the registering
    distribution so two of them cannot claim one name. Both the bare
    ``@tool`` and the parametrised ``@tool(...)`` forms work.
    """
    from .tools._registry import mcp

    return mcp.tool(*args, **kwargs)


def get_client() -> Any:
    """The Moneybird client for the current request, via the supported seam.

    Resolved on every call through ``moneybird_mcp.tools._context`` rather than
    bound at import, which is what keeps credential mode, administration
    confinement and the single test patch point applying to extension tools
    exactly as they apply to built-in ones. Constructing ``MoneybirdClient``
    directly bypasses all three.
    """
    from .tools import _context

    return _context.get_client()


def provider_request(
    client: Any,
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    retry_safe: bool | None = None,
) -> Any:
    """Send one authenticated request for an endpoint this distribution does not wrap.

    The built-in client covers the endpoints the built-in tools need, and it will
    never cover every endpoint an extension might: adding a typed method for one
    would mean this distribution advertising a capability it does not implement.
    So the seam offers the transport rather than the vocabulary.

    ``path`` is relative to the current administration -- ``"assets.json"``, not a
    full URL -- and is confined to that administration by the same check every
    built-in call goes through. Everything else the client does still applies:
    the shared per-IP rate budget, retry policy, redirect refusal, error mapping
    and the rule that only a provably repeatable request is retried. Passing
    ``retry_safe=True`` for something that mutates would be a way to lose money,
    so it defaults to the method's own semantics and should be set only for a
    read expressed as a POST, which is how Moneybird models batch fetches.

    This is deliberately not a method on the client: it is an extension seam, and
    keeping it here means the client's own surface still describes exactly what
    this distribution supports.
    """
    if not isinstance(method, str) or not method.strip():
        raise MoneybirdError("An HTTP method is required.")
    raw = str(path or "").strip().lstrip("/")
    if not raw:
        raise MoneybirdError("A request path is required.")
    administration_id = getattr(client, "administration_id", None)
    if administration_id is None:
        raise MoneybirdError(
            "An administration id is required for an extension provider request."
        )
    endpoint = f"/{administration_id}/{raw}"
    if not endpoint.endswith(".json"):
        endpoint = f"{endpoint}.json"
    return client._request(
        method.strip().upper(),
        endpoint,
        query=query,
        body=body,
        retry_safe=retry_safe,
    )


def register_approval_executor(action: str, executor: Any) -> None:
    """Bind the executor that carries out one guarded action.

    Re-exported from the tools package so an extension never has to import a
    private module. Both this and :func:`register_write_spec` must be called for
    the same action, from the same distribution, before validation runs; neither
    takes a distribution name, because the loader supplies it.
    """
    from .tools.approvals import register_approval_executor as _register

    _register(action, executor)


__all__ = [
    "API_VERSION",
    "ApprovalId",
    "DateString",
    "ExplicitDocumentLines",
    "Limit",
    "MONTH_CAPPED_REPORTS",
    "MoneybirdError",
    "MoneybirdHTTPError",
    "MoneybirdId",
    "OptionalDateString",
    "PAGINATED_REPORTS",
    "PREPARE_ANNOTATIONS",
    "Page",
    "Period",
    "PriceString",
    "READ_ONLY_ANNOTATIONS",
    "Registration",
    "ReportName",
    "WRITE_ANNOTATIONS",
    "WriteSpec",
    "clean_dict",
    "compact_general_journal_summary",
    "details_attributes_payload",
    "duplicate_fingerprint",
    "get_client",
    "mark_write_dispatch_started",
    "mark_write_verifying",
    "money_decimal",
    "prepare_general_journal_entries",
    "provider_request",
    "rate_budget_affordable_batches",
    "rate_budget_reset_seconds",
    "register_approval_executor",
    "register_write_spec",
    "report_period_months",
    "run_approved_write",
    "stage_write",
    "symbolic_period_months",
    "tool",
    "validate_explicit_document_lines",
    "verify_general_journal_payload",
]
