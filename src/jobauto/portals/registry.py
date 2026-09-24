"""Adapter registry.

Adapters are resolved from the `adapter:` string in each portal YAML
("module:ClassName"), so a new portal never requires editing this file --
drop in the YAML and the module, and it is picked up.
"""
from __future__ import annotations

import importlib
from typing import Any, Type

from ..config import Config, PortalConfig
from .base import PortalAdapter

_CACHE: dict[str, Type[PortalAdapter]] = {}


def resolve(portal: PortalConfig) -> Type[PortalAdapter]:
    spec = portal.adapter
    if not spec:
        raise ValueError(f"portal {portal.id} has no `adapter:` in its YAML")
    if spec in _CACHE:
        return _CACHE[spec]

    if ":" not in spec:
        raise ValueError(
            f"portal {portal.id} adapter must be 'module:ClassName', got {spec!r}")
    module_name, class_name = spec.split(":", 1)

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise ValueError(
            f"portal {portal.id}: cannot import {module_name!r} ({exc})") from exc

    try:
        cls = getattr(module, class_name)
    except AttributeError as exc:
        raise ValueError(
            f"portal {portal.id}: {module_name} has no class {class_name!r}") from exc

    if not issubclass(cls, PortalAdapter):
        raise TypeError(f"{spec} is not a PortalAdapter subclass")

    _CACHE[spec] = cls
    return cls


def build(portal: PortalConfig, config: Config, page: Any) -> PortalAdapter:
    return resolve(portal)(portal, config, page)


def available(config: Config) -> dict[str, str]:
    """id -> human name, for `list-portals`. Reports import failures inline
    instead of raising, so one broken adapter does not hide the others."""
    out: dict[str, str] = {}
    for pid, portal in config.portals.items():
        if portal.enabled:
            state = "enabled"
        elif pid in getattr(config, "disabled_by_preferences", ()):
            state = "disabled in preferences (dashboard: Portals)"
        else:
            state = "disabled in its yaml"
        try:
            resolve(portal)
        except Exception as exc:
            state = f"BROKEN: {exc}"
        out[pid] = f"{portal.name} [{state}]"
    return out
