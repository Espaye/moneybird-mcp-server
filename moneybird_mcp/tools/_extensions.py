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
"""
from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass

from .._registration import registering_as

#: The one supported group. Extensions map any entry-point name to a module path.
ENTRY_POINT_GROUP = "moneybird_mcp.tools"


class ExtensionError(RuntimeError):
    """An installed extension could not be discovered or imported."""


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
    loaded: list[ExtensionEntryPoint] = []
    for declared in discover_extensions():
        with registering_as(declared.distribution, declared.version):
            try:
                importlib.import_module(declared.value)
            except Exception as exc:  # noqa: BLE001 - re-raised with provenance
                raise ExtensionError(
                    f"Extension {declared.label} failed to load: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        loaded.append(declared)
    return loaded
