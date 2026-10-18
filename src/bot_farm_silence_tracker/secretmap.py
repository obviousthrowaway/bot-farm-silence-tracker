"""A dictionary of credential values that never shows the values.

Credentials travel through several objects and function arguments. Python prints
them whenever a value is formatted: in a dataclass ``repr``, a log record, or a
traceback printed with local variables. :class:`SecretDict` keeps the usual
dictionary behaviour but formats as the credential names only, so none of those
places can reveal a value.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class SecretDict(dict[str, str]):
    """A ``dict`` whose ``repr`` and ``str`` list the keys and never the values."""

    __slots__ = ()

    def __repr__(self) -> str:
        names = ", ".join(sorted(self))
        return f"SecretDict({names or 'empty'})"

    __str__ = __repr__

    def copy(self) -> SecretDict:
        return SecretDict(self)

    def __reduce__(self) -> Any:
        raise TypeError("credentials are never serialized")


def secret_dict(values: Mapping[str, str] | None = None) -> SecretDict:
    """``values`` as a :class:`SecretDict`: the same object when it already is one (so
    clearing it clears every holder), else a new one (empty for ``None``)."""
    if isinstance(values, SecretDict):
        return values
    return SecretDict(values or {})
