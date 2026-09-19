"""Hirist adapter.

This is the whole thing. Search, detail fetch, apply, pacing, caps, dedupe and
the review gate all come from ConfigDrivenAdapter + config/portals/hirist.yaml.
Use this file as the template when adding a new portal.
"""
from __future__ import annotations

from .generic import ConfigDrivenAdapter


class HiristAdapter(ConfigDrivenAdapter):
    """No overrides needed -- Hirist uses the standard search/apply shape."""
