"""Config loading and validation.

Fails loudly at startup rather than halfway through a run: a typo in a scoring
weight should not surface as a mysterious ranking three hundred jobs later.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parents[1]
# Templates that ship inside the wheel, so `pip install jobauto` has portal
# selectors and a preferences file without a repo checkout.
DEFAULTS_DIR = PACKAGE_DIR / "defaults"


def _running_from_checkout() -> bool:
    """A source checkout keeps config/ and data/ beside the code, which is what
    contributors expect. An installed copy must not write into site-packages."""
    return (ROOT / "config" / "portals").is_dir() and (ROOT / "pyproject.toml").exists()


def user_dir() -> Path:
    """Where an installed copy keeps config, browser profiles and history."""
    override = os.environ.get("JOBAUTO_HOME", "").strip()
    base = Path(override) if override else Path.home() / ".jobauto"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _seed_config(target: Path) -> None:
    """First run of an installed copy: lay down the packaged templates."""
    import shutil

    target.mkdir(parents=True, exist_ok=True)
    (target / "portals").mkdir(exist_ok=True)
    if not DEFAULTS_DIR.is_dir():
        return
    for src in DEFAULTS_DIR.glob("*.yaml"):
        dst = target / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
    for src in (DEFAULTS_DIR / "portals").glob("*.yaml"):
        dst = target / "portals" / src.name
        if not dst.exists():
            shutil.copy2(src, dst)


def _default_config_dir() -> Path:
    if _running_from_checkout():
        return ROOT / "config"
    target = user_dir() / "config"
    _seed_config(target)
    return target


CONFIG_DIR = _default_config_dir()


class ConfigError(Exception):
    pass


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return data


def _load_with_local_override(name: str) -> dict[str, Any]:
    """`profile.local.yaml` wins over `profile.yaml` if present, so the
    committed file stays a template and your real details stay untracked."""
    local = CONFIG_DIR / f"{name}.local.yaml"
    return _read_yaml(local if local.exists() else CONFIG_DIR / f"{name}.yaml")


@dataclass
class PortalConfig:
    id: str
    name: str
    enabled: bool
    base_url: str
    adapter: str
    auth: dict[str, Any] = field(default_factory=dict)
    search: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    apply: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    profile_refresh: dict[str, Any] = field(default_factory=dict)
    mode: str = "search"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def force_manual_submit(self) -> bool:
        return bool(self.risk.get("force_manual_submit", False))

    @property
    def pacing_multiplier(self) -> float:
        return float(self.risk.get("pacing_multiplier", 1.0))


@dataclass
class Config:
    profile: dict[str, Any]
    preferences: dict[str, Any]
    portals: dict[str, PortalConfig]
    # Portal ids switched off by preferences.portals.disabled rather than by
    # their own YAML. Kept apart so `doctor` can say which, and so pausing
    # every portal from the dashboard is a state rather than a config error.
    disabled_by_preferences: set[str] = field(default_factory=set)

    # -- convenience accessors so call sites don't dig through raw dicts ----
    @property
    def search(self) -> dict[str, Any]:
        return self.preferences.get("search", {})

    @property
    def thresholds(self) -> dict[str, Any]:
        return self.preferences.get("thresholds", {})

    @property
    def application(self) -> dict[str, Any]:
        return self.preferences.get("application", {})

    @property
    def auto_submit(self) -> bool:
        return bool(self.application.get("auto_submit", False))

    def enabled_portals(self) -> list[PortalConfig]:
        return [p for p in self.portals.values() if p.enabled]

    def daily_cap(self, portal_id: str) -> int:
        return int(self.application.get("daily_caps", {}).get(portal_id, 10))


def load_config(config_dir: Path | None = None) -> Config:
    global CONFIG_DIR
    if config_dir is not None:
        CONFIG_DIR = Path(config_dir)

    profile = _load_with_local_override("profile")
    preferences = _load_with_local_override("preferences")

    portals: dict[str, PortalConfig] = {}
    portal_dir = CONFIG_DIR / "portals"
    for path in sorted(portal_dir.glob("*.yaml")):
        if path.name.endswith(".local.yaml"):
            continue
        local = path.with_suffix("").with_suffix(".local.yaml")
        data = _read_yaml(local if local.exists() else path)
        pid = data.get("id") or path.stem
        portals[pid] = PortalConfig(
            id=pid,
            name=data.get("name", pid),
            enabled=bool(data.get("enabled", True)),
            base_url=data.get("base_url", ""),
            adapter=data.get("adapter", ""),
            auth=data.get("auth", {}),
            search=data.get("search", {}),
            detail=data.get("detail", {}),
            apply=data.get("apply", {}),
            risk=data.get("risk", {}),
            profile_refresh=data.get("profile_refresh", {}),
            mode=data.get("mode", "search"),
            raw=data,
        )

    cfg = Config(profile=profile, preferences=preferences, portals=portals)
    apply_portal_preferences(cfg)
    validate(cfg)
    return cfg


def apply_portal_preferences(cfg: Config) -> None:
    """Switch off the portals named in preferences.portals.disabled.

    Preferences are what the cloud syncs, so this is how a portal turned off
    in the dashboard stays off on the PC. The portal's own YAML `enabled:`
    still applies; this can only turn portals off, never on, so a portal
    disabled in its file for a reason cannot be re-enabled from a phone.
    Unknown ids are ignored: the dashboard may name a portal this checkout
    does not have.
    """
    block = cfg.preferences.get("portals") or {}
    names = block.get("disabled") if isinstance(block, dict) else None
    if not isinstance(names, list):
        return
    for name in names:
        pid = str(name).strip().lower()
        portal = cfg.portals.get(pid)
        if portal is not None:
            portal.enabled = False
            cfg.disabled_by_preferences.add(pid)


def validate(cfg: Config) -> None:
    """Catch the mistakes that would otherwise produce silently wrong rankings."""
    errors: list[str] = []

    weights = cfg.preferences.get("scoring", {}).get("weights", {})
    if not weights:
        errors.append("preferences.scoring.weights is empty")
    else:
        total = sum(float(v) for v in weights.values())
        if abs(total - 1.0) > 0.001:
            errors.append(
                f"scoring weights must sum to 1.0, got {total:.3f} "
                f"({', '.join(f'{k}={v}' for k, v in weights.items())})"
            )

    roles = cfg.search.get("roles") or []
    if not roles:
        errors.append("preferences.search.roles is empty -- nothing to search for")
    for i, role in enumerate(roles):
        if not isinstance(role, dict) or not role.get("title"):
            errors.append(f"preferences.search.roles[{i}] needs a `title`")

    th = cfg.thresholds
    shortlist = float(th.get("shortlist", 0))
    priority = float(th.get("priority", 100))
    if shortlist > priority:
        errors.append(
            f"thresholds.shortlist ({shortlist}) is above thresholds.priority "
            f"({priority}) -- no job could ever be a priority"
        )

    comp = cfg.search.get("compensation", {})
    if comp.get("expected_ctc_lpa") and comp.get("minimum_acceptable_lpa"):
        if float(comp["minimum_acceptable_lpa"]) > float(comp["expected_ctc_lpa"]):
            errors.append(
                "compensation.minimum_acceptable_lpa is above expected_ctc_lpa"
            )

    if not cfg.portals:
        errors.append("no portal configs found in config/portals/")
    # A config where the portal files themselves disable everything is a
    # mistake worth failing on. Every portal switched off from the dashboard
    # is a deliberate pause, and failing here would make the agent reject the
    # whole synced preferences file -- undoing every other edit in it.
    if not cfg.enabled_portals() and not cfg.disabled_by_preferences:
        errors.append("every portal is disabled -- nothing would run")

    # Only warn about identity at run time, not load time: `doctor` and
    # `score` are useful before you've filled the profile in.
    if errors:
        raise ConfigError(
            "Invalid configuration:\n" + "\n".join(f"  - {e}" for e in errors)
        )


def require_identity(cfg: Config) -> None:
    """Called only by commands that actually fill a form."""
    missing = [
        f"profile.identity.{k}"
        for k in ("full_name", "email", "phone")
        if not (cfg.profile.get("identity", {}).get(k) or "").strip()
    ]
    if missing:
        raise ConfigError(
            "Cannot fill applications until these are set in "
            "config/profile.local.yaml:\n"
            + "\n".join(f"  - {m}" for m in missing)
        )


def data_dir() -> Path:
    """Browser profiles, the local database and the agent link live here.

    A checkout keeps them beside the code; an installed copy uses ~/.jobauto,
    because writing into site-packages breaks on upgrade and needs admin on
    some systems.
    """
    override = os.environ.get("JOBAUTO_DATA_DIR", "").strip()
    if override:
        d = Path(override)
    elif _running_from_checkout():
        d = ROOT / "data"
    else:
        d = user_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d
