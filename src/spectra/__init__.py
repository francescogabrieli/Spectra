"""Spectra — local-first finance import, categorization, and dashboard."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("spectra")
except PackageNotFoundError:
    __version__ = "0.4.0"
