"""Backwards-compatible shim: `uvicorn api:app` still works from a checkout.

The service lives in `docket.api` so it ships with the installed package.
"""
from docket.api import app

__all__ = ["app"]
