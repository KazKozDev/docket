"""Internal helpers for retiring public API without surprising callers."""

from __future__ import annotations

import warnings


def warn_deprecated(
    name: str,
    *,
    replacement: str | None = None,
    removal: str | None = None,
    stacklevel: int = 2,
) -> None:
    """Emit the project's standard ``DeprecationWarning`` for a public name."""
    message = f"{name} is deprecated"
    if replacement:
        message += f"; use {replacement} instead"
    if removal:
        message += f"; it will be removed in {removal}"
    warnings.warn(message, DeprecationWarning, stacklevel=stacklevel)


__all__ = ["warn_deprecated"]
