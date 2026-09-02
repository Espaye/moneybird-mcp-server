"""Registries that remember who registered what, and stop accepting when sealed.

The server used to assemble its guarded-write surface from module-level dicts
written once, in one distribution, at import time. That is a closed world: the
only way to add an action was to edit those dicts, and the only possible answer
to "where did this come from" was "here".

Once a second distribution may contribute tools and actions, three questions
have to be answerable at runtime rather than by reading the source: which
distribution registered a key, whether two distributions claimed the same key,
and whether registration is still open. A plain dict answers none of them --
`dict[key] = value` silently overwrites, which for a write contract means one
distribution quietly taking over another's guarded action.

So every registry here rejects a duplicate instead of overwriting it, records
the origin of each entry, and can be frozen. Freezing is what makes the
validation pass meaningful: after it, the surface the server validated is the
surface it serves.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

#: Origin recorded for everything this distribution registers itself.
CORE_ORIGIN = "moneybird-mcp"


class RegistryError(RuntimeError):
    """A registration was refused, or attempted after the registry was sealed."""


@dataclass(frozen=True)
class Registration:
    """One entry plus the provenance needed to explain or blame it."""

    key: str
    value: Any
    origin: str
    version: str | None = None

    def describe(self) -> str:
        """Origin and version only -- never the registered object."""
        return f"{self.origin} {self.version}" if self.version else self.origin


_CURRENT_ORIGIN: ContextVar[tuple[str, str | None]] = ContextVar(
    "moneybird_mcp_registration_origin", default=(CORE_ORIGIN, None)
)


def current_origin() -> tuple[str, str | None]:
    """The distribution credited with registrations made right now."""
    return _CURRENT_ORIGIN.get()


@contextlib.contextmanager
def registering_as(origin: str, version: str | None = None) -> Iterator[None]:
    """Credit everything registered inside this block to ``origin``.

    Extension modules register by importing, so attribution cannot be a
    parameter the extension passes -- it would be the extension's own claim.
    The loader states the origin instead, from the installed distribution
    metadata it read the entry point from.
    """
    token = _CURRENT_ORIGIN.set((origin, version))
    try:
        yield
    finally:
        _CURRENT_ORIGIN.reset(token)


class Registry:
    """A keyed collection with provenance, no silent override, and a seal."""

    def __init__(self, subject: str) -> None:
        self._subject = subject
        self._values: dict[str, Any] = {}
        self._registrations: dict[str, Registration] = {}
        self._frozen = False

    @property
    def subject(self) -> str:
        return self._subject

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(
        self,
        key: str,
        value: Any,
        *,
        origin: str | None = None,
        version: str | None = None,
    ) -> Registration:
        if self._frozen:
            raise RegistryError(
                f"{self._subject} {key!r} was registered after validation sealed "
                "the registry; an extension must register while it is being "
                "imported by the loader."
            )
        if not key or not isinstance(key, str):
            raise RegistryError(f"{self._subject} keys must be non-empty strings; got {key!r}")
        if origin is None:
            origin, version = current_origin()
        existing = self._registrations.get(key)
        if existing is not None:
            raise RegistryError(
                f"{self._subject} {key!r} is registered twice: first by "
                f"{existing.describe()}, then by "
                f"{Registration(key, value, origin, version).describe()}. "
                "Two distributions cannot own one key; rename one of them."
            )
        registration = Registration(key, value, origin, version)
        self._registrations[key] = registration
        self._values[key] = value
        return registration

    def freeze(self) -> None:
        self._frozen = True

    def registration(self, key: str) -> Registration:
        return self._registrations[key]

    def origin_of(self, key: str) -> str:
        return self._registrations[key].origin

    def registrations(self) -> Mapping[str, Registration]:
        return MappingProxyType(self._registrations)

    def as_mapping(self) -> Mapping[str, Any]:
        """A live read-only view, so existing ``dict``-shaped callers keep working."""
        return MappingProxyType(self._values)

    def origins(self) -> tuple[str, ...]:
        """Every distribution that contributed, core first, then sorted."""
        found = {registration.origin for registration in self._registrations.values()}
        rest = sorted(found - {CORE_ORIGIN})
        return (*( (CORE_ORIGIN,) if CORE_ORIGIN in found else () ), *rest)

    def structural_problems(self) -> list[str]:
        """Invariants the registry maintains itself, re-checked before sealing.

        These cannot fail unless the registry's own bookkeeping has diverged, and
        that is exactly why they are asserted rather than assumed: the validation
        pass is the last place where a wrong answer is still cheap.
        """
        problems: list[str] = []
        if set(self._values) != set(self._registrations):
            problems.append(
                f"{self._subject}: value and provenance tables disagree on which keys exist"
            )
        for key, registration in self._registrations.items():
            if registration.key != key:
                problems.append(f"{self._subject}: {key!r} is filed under the wrong key")
            if not registration.origin:
                problems.append(f"{self._subject}: {key!r} has no recorded origin")
        return problems

    def __contains__(self, key: object) -> bool:
        return key in self._values

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"<Registry {self._subject!r}: {len(self._values)} from {self.origins()}>"
