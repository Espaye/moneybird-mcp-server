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
        get_client, register_approval_executor, register_write_spec,
        run_approved_write, stage_write, tool,
    )

    @tool(annotations=PREPARE_ANNOTATIONS, tags={"domain:example"})
    def prepare_example(record_id: MoneybirdId) -> dict:
        \"\"\"Preview an action; nothing is sent until it is approved.\"\"\"
        client = get_client()
        ...
        return stage_write("example", summary=..., payload=..., preview=...)

    def _execute(client, payload): ...

    def example_from_approval(approval_id: ApprovalId) -> dict:
        return run_approved_write(get_client(), approval_id, "example", _execute)

    register_write_spec("example", WriteSpec(1, "...", "...", "...", "..."))
    register_approval_executor("example", example_from_approval)

Growing this surface is a compatible change; removing or renaming anything in
:data:`__all__` is not. ``tests/test_extension_boundary.py`` pins the list.
"""
from __future__ import annotations

from typing import Any

from ._registration import Registration
from .config import (
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    WRITE_ANNOTATIONS,
    MoneybirdError,
)
from .formatting import duplicate_fingerprint
from .tools._params import ApprovalId, MoneybirdId
from .tools._registry import mcp as _mcp
from .tools._writes import run_approved_write, stage_write
from .write_contracts import WriteSpec, register_write_spec

#: Version of this seam. Bumped when something is removed or changes meaning;
#: additions do not bump it.
API_VERSION = 1

#: Register an MCP tool. This is the wrapped decorator the built-in tools use:
#: it installs the error translation that reports a ``MoneybirdError`` as a
#: handled refusal rather than a server crash, and it records the tool name
#: against the registering distribution so two of them cannot claim one name.
tool = _mcp.tool


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
    "MoneybirdError",
    "MoneybirdId",
    "PREPARE_ANNOTATIONS",
    "READ_ONLY_ANNOTATIONS",
    "Registration",
    "WRITE_ANNOTATIONS",
    "WriteSpec",
    "duplicate_fingerprint",
    "get_client",
    "register_approval_executor",
    "register_write_spec",
    "run_approved_write",
    "stage_write",
    "tool",
]
