"""Check the assembled guarded-write surface once, then seal it.

Before extensions existed, ``approvals.py`` asserted at import time that the
executor table and the write-contract table held the same actions. That check
could not survive the surface being assembled from more than one distribution:
it ran while the tables were still being built, so it could only ever compare
the core against itself, and it would fire on the first extension that declared
a contract a moment before its executor.

The same guarantee therefore moves here, to a pass that runs after every
extension has been imported and before the server serves anything. It is
stricter than the check it replaces, not merely later: matching keys were the
only thing asserted before, and five further invariants are asserted now.

Failure raises and the import of :mod:`moneybird_mcp.tools` fails with it, so
there is no half-configured server. Diagnostics name actions, tools and
distributions -- never payloads, credentials or approval contents.
"""
from __future__ import annotations

import inspect
from typing import Any

from .._registration import Registry
from ..write_contracts import WRITE_SPEC_REGISTRY, WriteSpec
from ._registry import TOOL_REGISTRY
from .approvals import APPROVAL_EXECUTOR_REGISTRY

#: The schema version an executor's contract must declare.
SUPPORTED_WRITE_SPEC_SCHEMA = 1

_REQUIRED_SPEC_FIELDS = ("precondition", "verifier", "idempotency", "reconciliation")


class RegistryValidationError(RuntimeError):
    """The assembled registries are not a surface this server will serve."""


def _executor_contract_problem(action: str, executor: Any) -> str | None:
    """Why ``executor`` cannot be called as ``executor(approval_id)``, if it cannot."""
    if not callable(executor):
        return f"executor for {action!r} is not callable"
    try:
        signature = inspect.signature(executor)
    except (TypeError, ValueError):
        # A builtin or C-implemented callable exposes no signature. Callable is
        # as much as can be established, and claiming more would be invention.
        return None
    try:
        signature.bind("approval-id")
    except TypeError:
        return (
            f"executor for {action!r} does not accept a single approval id; "
            f"its signature is {signature}"
        )
    return None


def _spec_problems(action: str, spec: Any) -> list[str]:
    if not isinstance(spec, WriteSpec):
        return [f"write spec for {action!r} is not a WriteSpec"]
    problems: list[str] = []
    if spec.schema_version != SUPPORTED_WRITE_SPEC_SCHEMA:
        problems.append(
            f"write spec for {action!r} declares schema version "
            f"{spec.schema_version}, not {SUPPORTED_WRITE_SPEC_SCHEMA}"
        )
    for field in _REQUIRED_SPEC_FIELDS:
        value = getattr(spec, field, None)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"write spec for {action!r} has an empty {field}")
    return problems


def collect_problems(
    specs: Registry | None = None,
    executors: Registry | None = None,
    tools: Registry | None = None,
) -> list[str]:
    """Every reason the assembled surface should not be served.

    Separated from :func:`validate_registries` so the invariants can be exercised
    against constructed registries without installing anything.
    """
    specs = WRITE_SPEC_REGISTRY if specs is None else specs
    executors = APPROVAL_EXECUTOR_REGISTRY if executors is None else executors
    tools = TOOL_REGISTRY if tools is None else tools

    problems: list[str] = []

    # (2) No duplicate registration. Registration refuses one outright, so this
    # re-checks that each registry's own bookkeeping still agrees with itself.
    for registry in (specs, executors, tools):
        problems.extend(registry.structural_problems())

    # (1) Exactly the same actions on both sides.
    missing_executors = sorted(set(specs) - set(executors))
    orphan_executors = sorted(set(executors) - set(specs))
    for action in missing_executors:
        problems.append(
            f"action {action!r} has a write contract from "
            f"{specs.origin_of(action)} but no executor"
        )
    for action in orphan_executors:
        problems.append(
            f"action {action!r} has an executor from "
            f"{executors.origin_of(action)} but no write contract"
        )

    for action in sorted(set(specs) & set(executors)):
        # (3) One distribution owns both halves of an action.
        spec_origin = specs.origin_of(action)
        executor_origin = executors.origin_of(action)
        if spec_origin != executor_origin:
            problems.append(
                f"action {action!r} is split across distributions: its write "
                f"contract comes from {spec_origin} and its executor from "
                f"{executor_origin}. One distribution must own both, so the "
                "contract cannot be honoured by code that did not agree to it."
            )
        # (4) The contract is complete.
        problems.extend(_spec_problems(action, specs[action]))
        # (5) The executor can be called the way the server calls it.
        problem = _executor_contract_problem(action, executors[action])
        if problem:
            problems.append(problem)

    return problems


def validate_registries(
    specs: Registry | None = None,
    executors: Registry | None = None,
    tools: Registry | None = None,
) -> None:
    """Assert the six invariants, then seal all three registries.

    Sealing last is what makes the result durable: after this returns, the
    surface that was validated is the surface that gets served, because nothing
    can be added to it. On failure nothing is sealed and nothing is served --
    the exception propagates out of the package import.
    """
    specs = WRITE_SPEC_REGISTRY if specs is None else specs
    executors = APPROVAL_EXECUTOR_REGISTRY if executors is None else executors
    tools = TOOL_REGISTRY if tools is None else tools

    problems = collect_problems(specs, executors, tools)
    if problems:
        raise RegistryValidationError(
            "The Moneybird tool surface was not accepted:\n  - "
            + "\n  - ".join(problems)
        )

    # (6) Freeze. Anything arriving later missed validation by definition.
    for registry in (specs, executors, tools):
        registry.freeze()
