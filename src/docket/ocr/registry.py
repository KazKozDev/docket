"""Built-in and plugin OCR backends, by name.

Third-party packages add a backend through the `docket.ocr_backends` entry
point; the entry point's name is the backend name and its target is an
`OcrBackend` subclass (or any callable taking `OcrSettings` and returning an
`OcrBackend`)::

    [project.entry-points."docket.ocr_backends"]
    my_engine = "my_package.ocr:MyEngineBackend"

Built-ins are imported lazily, so an optional engine that isn't installed
costs nothing until someone asks for it.
"""
from __future__ import annotations

import importlib
import re
from importlib.metadata import entry_points
from typing import Callable, Union

from pydantic import BaseModel

from .base import BackendStatus, BackendUnavailable, Capabilities, OcrBackend, OcrSettings

ENTRY_POINT_GROUP = "docket.ocr_backends"

BackendFactory = Callable[[OcrSettings], OcrBackend]

# Name -> "module:attribute" (lazy) or a factory.
_BUILTIN: dict[str, str] = {
    "pdf_text": "docket.ocr.pdftext:PDFTextBackend",
    "tesseract": "docket.ocr.tesseract:TesseractBackend",
    "vlm": "docket.ocr.vlm:VlmBackend",
}
_REGISTRY: dict[str, Union[str, BackendFactory]] = dict(_BUILTIN)
_plugins_loaded = False


class OcrBackendError(ValueError):
    """Unknown backend name or invalid registration."""


class BackendInfo(BaseModel):
    name: str
    builtin: bool
    capabilities: Capabilities | None
    status: BackendStatus


def _load_plugins() -> None:
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name not in _REGISTRY:
            _REGISTRY[ep.name] = ep.value


def register_ocr_backend(name: str, factory: BackendFactory, *, replace: bool = False) -> None:
    """Make `factory` (an OcrBackend subclass or a callable taking OcrSettings)
    selectable by `name`."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise OcrBackendError(f"backend name {name!r} must be lowercase snake_case")
    if name in {"auto"}:
        raise OcrBackendError("'auto' is reserved")
    _load_plugins()
    if name in _BUILTIN:
        raise OcrBackendError(f"{name!r} is a built-in backend and cannot be replaced")
    if name in _REGISTRY and not replace:
        raise OcrBackendError(f"OCR backend {name!r} is already registered")
    _REGISTRY[name] = factory


def unregister_ocr_backend(name: str) -> None:
    if name in _BUILTIN:
        raise OcrBackendError(f"{name!r} is built in and cannot be removed")
    if _REGISTRY.pop(name, None) is None:
        raise OcrBackendError(f"unknown OCR backend {name!r}")


def backend_names() -> list[str]:
    _load_plugins()
    return list(_REGISTRY)


def _factory(name: str) -> BackendFactory:
    _load_plugins()
    target = _REGISTRY.get(name)
    if target is None:
        raise OcrBackendError(
            f"unknown OCR backend {name!r}; available: {', '.join(backend_names())}"
        )
    if isinstance(target, str):
        module_name, _, attr = target.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise BackendUnavailable(name, f"cannot import {module_name}: {exc}") from exc
        target = getattr(module, attr)
    return target  # type: ignore[return-value]


def get_ocr_backend(name: str, settings: OcrSettings | None = None) -> OcrBackend:
    """Instantiate a backend by name. Does not check availability — call
    `.availability()` or `.require_available()` for that."""
    backend = _factory(name)(settings or OcrSettings())
    if not isinstance(backend, OcrBackend):
        raise OcrBackendError(f"backend {name!r} did not produce an OcrBackend")
    return backend


def list_ocr_backends(settings: OcrSettings | None = None) -> list[BackendInfo]:
    """Every registered backend with its capabilities and whether it can run here."""
    infos = []
    for name in backend_names():
        try:
            backend = get_ocr_backend(name, settings)
            infos.append(
                BackendInfo(
                    name=name,
                    builtin=name in _BUILTIN,
                    capabilities=backend.capabilities,
                    status=backend.availability(),
                )
            )
        except BackendUnavailable as exc:
            infos.append(
                BackendInfo(
                    name=name,
                    builtin=name in _BUILTIN,
                    capabilities=None,
                    status=BackendStatus(
                        name=name, available=False, reason=exc.reason, install_hint=exc.install_hint
                    ),
                )
            )
    return infos


__all__ = [
    "BackendFactory",
    "BackendInfo",
    "ENTRY_POINT_GROUP",
    "OcrBackendError",
    "backend_names",
    "get_ocr_backend",
    "list_ocr_backends",
    "register_ocr_backend",
    "unregister_ocr_backend",
]
