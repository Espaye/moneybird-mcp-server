"""Load the tool modules other installed distributions contribute.

An extension is a normal installed distribution that declares entry points in
the ``moneybird_mcp.tools`` group. Importing one of its modules is what
registers its tools, write contracts and executors, exactly as importing a
module in this package registers ours.

Entry points are the whole mechanism. There is no directory scan, no
import-by-string setting, and no name of any particular extension anywhere in
this distribution: the core cannot name what it must not depend on, and an
installed distribution is the only thing that may claim to extend it.

Discovery walks installed distributions rather than the flat
``entry_points(group=...)`` view because the distribution is the provenance.
Every registration has to be attributable to the thing that can be uninstalled,
and a bare entry point is not reliably back-referenced to one.

A failure here aborts the import of :mod:`moneybird_mcp.tools`, so the server
does not start. That is deliberate: a partially loaded extension would leave a
guarded write surface that nobody validated.

The loader also refuses to run underneath itself. Importing an extension's own
capability module directly reaches this package while that module is half
executed; the loader would then import the extension, get the half-executed
module back from ``sys.modules``, and treat it as loaded. What follows is either
an ``AttributeError`` about a circular import or, worse, a surface assembled from
a module that never finished. Both are diagnosed here instead, before anything is
imported, because at this point the cause is still visible.
"""
from __future__ import annotations

import importlib.metadata
import sys
from dataclasses import dataclass

from .._registration import registering_as

#: The one supported group. Extensions map any entry-point name to a module path.
ENTRY_POINT_GROUP = "moneybird_mcp.tools"


class ExtensionError(RuntimeError):
    """An installed extension could not be discovered or imported."""


#: Set for the duration of :func:`load_extensions`. Module-level rather than a
#: context variable on purpose: re-entry through a fresh context would be just as
#: wrong, and there is only ever one assembly of one process's tool surface.
_LOADING = False


def _initializing_modules(package: str) -> list[str]:
    """Modules of ``package`` that are part-way through their own import.

    ``__spec__._initializing`` is what the import system itself consults to tell
    a finished module from one still executing, and it is the only way to see the
    half-imported module that ``sys.modules`` is otherwise happy to hand back.
    """
    prefix = f"{package}."
    found = []
    for name, module in list(sys.modules.items()):
        if name != package and not name.startswith(prefix):
            continue
        spec = getattr(module, "__spec__", None)
        if getattr(spec, "_initializing", False):
            found.append(name)
    return sorted(found)


@dataclass(frozen=True)
class ExtensionEntryPoint:
    """One declared extension module, with the distribution that declares it."""

    distribution: str
    version: str | None
    name: str
    value: str

    @property
    def label(self) -> str:
        version = f" {self.version}" if self.version else ""
        return f"{self.distribution}{version} [{self.name} -> {self.value}]"

    def sort_key(self) -> tuple[str, str, str]:
        return (self.distribution, self.name, self.value)


def discover_extensions() -> list[ExtensionEntryPoint]:
    """Every declared extension module, in a stable order.

    Ordering is by distribution, then entry-point name, then target module, so
    two machines with the same packages installed load them the same way and any
    duplicate-key failure is reproducible rather than dependent on filesystem
    order. Identical declarations reached through a repeated ``sys.path`` entry
    collapse into one.
    """
    found: dict[tuple[str, str, str, str | None], ExtensionEntryPoint] = {}
    for distribution in importlib.metadata.distributions():
        metadata = distribution.metadata
        name = (metadata["Name"] if metadata is not None else None) or ""
        if not name:
            continue
        version = metadata["Version"] if metadata is not None else None
        for entry_point in distribution.entry_points:
            if entry_point.group != ENTRY_POINT_GROUP:
                continue
            declared = ExtensionEntryPoint(
                distribution=name,
                version=version,
                name=entry_point.name,
                value=entry_point.value,
            )
            found[(name, entry_point.name, entry_point.value, version)] = declared
    return sorted(found.values(), key=ExtensionEntryPoint.sort_key)


def load_extensions() -> list[ExtensionEntryPoint]:
    """Import every declared extension module, crediting each to its distribution.

    Returns what was loaded so a caller can report the assembled surface. Raises
    :class:`ExtensionError` naming the distribution and module if one fails to
    import; the underlying exception is chained, and nothing here formats the
    extension's own data into the message.
    """
    global _LOADING

    if _LOADING:
        raise ExtensionError(
            "The extension loader was re-entered while it was already running. "
            "Extensions are imported exactly once, in one pass, and a second "
            "pass would register into a surface the first pass is still "
            "assembling."
        )

    loaded: list[ExtensionEntryPoint] = []
    _LOADING = True
    try:
        for declared in discover_extensions():
            _refuse_if_already_importing(declared)
            with registering_as(declared.distribution, declared.version):
                try:
                    importlib.import_module(declared.value)
                except Exception as exc:  # noqa: BLE001 - re-raised with provenance
                    raise ExtensionError(
                        f"Extension {declared.label} failed to load: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
            loaded.append(declared)
    finally:
        _LOADING = False
    return loaded


def _refuse_if_already_importing(declared: ExtensionEntryPoint) -> None:
    """Refuse to load a distribution whose own import brought us here.

    Reached when something imported one of the extension's capability modules
    directly. That module's first line reaches the seam, the seam reaches this
    package, this package reaches the loader -- and the loader is now about to
    import the very package that is still executing above it on the stack.
    Importing it would hand back the half-built module, so the registrations that
    module has not made yet would never be made, and the ones it makes after this
    returns would land outside every attribution context.

    Diagnostics name module paths only.
    """
    package = declared.value.split(".")[0]
    in_flight = _initializing_modules(package)
    if not in_flight:
        return
    raise ExtensionError(
        f"Extension {declared.label} cannot be loaded: {', '.join(in_flight)} "
        "is already part-way through its own import, so the loader is running "
        "underneath it. An extension is loaded by installing it and letting the "
        "entry point be discovered; importing one of its modules directly starts "
        "the server assembly from inside that module and cannot complete. Import "
        "moneybird_mcp.tools first, or just let the server start."
    )
