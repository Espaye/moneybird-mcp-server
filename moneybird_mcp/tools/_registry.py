"""The FastMCP instance and the always-on server instructions."""
from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Callable

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .. import __version__
from .._registration import Registry
from ..config import MoneybirdError
from ..performance_middleware import ToolTelemetryMiddleware

logger = logging.getLogger("moneybird_mcp")

# Always loaded, so it is kept short: Claude Code cuts server instructions off at
# about 2,048 characters, and in an unconfigured server the SETUP INCOMPLETE
# banner is prepended to this text. Everything else lives where it is needed —
# in a tool's own description or in a get_bookkeeping_guide topic.
# tests/test_server_instructions.py holds the budget and the home of every fact
# that was moved out of here.
SERVER_INSTRUCTIONS = """
Moneybird MCP: helps a user process, categorize and understand their bookkeeping.

HARD RULES (never break):
1. Explicit user confirmation is mandatory: prepare_* tool -> show the preview -> wait for
a clear "yes" -> only then execute_approved_action with the returned approval_id. In
compact discovery call_tool is read-only: call execute_approved_action directly so the
client sees its destructive annotation. An approval id is model-callable, not proof of
human intent, so request-context writes stay disabled.
2. Never invent data (invoice numbers, references, amounts, dates, counterparties); if
it is missing, ask or leave it blank.
3. After any change, report the returned verification evidence and any gap. A
reclassification meant to keep a document total must still match it to the cent.
4. When unsure, propose with reasoning and ask for approval; never guess silently.
5. You are not an accountant or tax advisor: defer fiscal judgment calls to the bookkeeper.

START HERE (each tool's description carries its own limits):
- Unfamiliar area: get_bookkeeping_guide(topic) or the moneybird://playbook/bookkeeping
  resource.
- Bank feed: start with suggest_bank_mutation_matches.
- Calls failing or slow: get_server_status.
- New or unsure user: the aan_de_slag prompt; other prompts cover named tasks.
- What the Moneybird API cannot do (booking rules, rate limits, report and list periods):
  get_bookkeeping_guide("grenzen").
"""


mcp = FastMCP(
    name="Moneybird MCP",
    version=__version__,
    instructions=SERVER_INSTRUCTIONS,
    middleware=[ToolTelemetryMiddleware()],
)


def _as_expected_tool_error(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Report a MoneybirdError as the handled condition it is, not as a crash.

    MoneybirdError carries every refusal this server makes on purpose: missing
    credentials, a rejected period, a failed precondition, an invalid argument.
    FastMCP logs an unknown exception type with ``logger.exception``, which its
    RichHandler renders as a boxed multi-frame traceback with source lines — in
    an MCP client log that reads like the server fell over. FastMCP already
    distinguishes expected failures: it logs ``FastMCPError`` with
    ``exc_info=False``. Raising ToolError puts these errors in that category and
    still delivers the message to the caller unmasked.

    The reason itself is logged here, because FastMCP's own line for an expected
    error names only the tool. Its duplicate is silenced via ``log_level``.
    """

    def _translate(exc: MoneybirdError, name: str) -> ToolError:
        logger.error("Tool %s could not run: %s", name, exc)
        return ToolError(str(exc), log_level=logging.DEBUG)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except MoneybirdError as exc:
                raise _translate(exc, fn.__name__) from exc

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except MoneybirdError as exc:
            raise _translate(exc, fn.__name__) from exc

    return wrapper


_register_tool = mcp.tool

#: Every tool name on the server, with the distribution that registered it. The
#: FastMCP instance keeps its own table; this one exists because that table
#: cannot say where a tool came from, and because a second distribution silently
#: replacing a core tool is the failure this boundary has to make impossible.
TOOL_REGISTRY = Registry("tool")


def _tool(*args: Any, **kwargs: Any) -> Any:
    """``mcp.tool`` that registers the error-translating wrapper.

    Every tool reaches MCP through here, including one an extension registers,
    because this is the only registration entry point published. The decorator
    returns the *undecorated* function, so direct Python callers (tests, scripts,
    one-off flows) keep seeing MoneybirdError exactly as before; only the
    MCP-facing callable is wrapped.
    """
    if args and callable(args[0]) and not kwargs:  # bare @mcp.tool
        fn = args[0]
        TOOL_REGISTRY.register(kwargs.get("name") or fn.__name__, fn)
        _register_tool(_as_expected_tool_error(fn), *args[1:])
        return fn

    decorate = _register_tool(*args, **kwargs)

    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        TOOL_REGISTRY.register(kwargs.get("name") or fn.__name__, fn)
        decorate(_as_expected_tool_error(fn))
        return fn

    return register


mcp.tool = _tool  # type: ignore[method-assign]
