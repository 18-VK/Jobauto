"""Local web dashboard.

Binds to 127.0.0.1 by default. If you expose it (Cloudflare Tunnel, Tailscale),
set JOBAUTO_WEB_TOKEN first -- the dashboard shows your salary, phone number and
full application history, so an unauthenticated public URL is not acceptable.

The long-running jobs (discover, apply) drive a real browser, so they run in a
background thread and the UI polls /api/task for progress.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from flask import Flask, jsonify, request, render_template, send_from_directory

from ..config import CONFIG_DIR, Config, ConfigError, load_config
from ..db import Database
from ..models import AppStatus
from ..pipeline import Pipeline, within_active_hours
from ..portals import registry
from ..scoring import Scorer


# --------------------------------------------------------------- task state
@dataclass
class TaskState:
    """One background job at a time. Trying to run two browser sessions over
    the same portal profile corrupts the session, so this is deliberate."""
    running: bool = False
    name: str = ""
    started_at: str = ""
    lines: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, line: str) -> None:
        text = str(line).rstrip()
        if not text:
            return
        with self._lock:
            self.lines.append(text)
            del self.lines[:-400]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "name": self.name,
                "started_at": self.started_at,
                "lines": list(self.lines),
                "result": dict(self.result),
                "error": self.error,
            }

    def begin(self, name: str) -> None:
        with self._lock:
            self.running, self.name = True, name
            self.started_at = datetime.now().isoformat(timespec="seconds")
            self.lines, self.result, self.error = [], {}, ""

    def finish(self, result: dict[str, Any] | None = None, error: str = "") -> None:
        with self._lock:
            self.running = False
            self.result = result or {}
            self.error = error


TASK = TaskState()


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["JOBAUTO_CONFIG"] = config
    token = os.environ.get("JOBAUTO_WEB_TOKEN", "").strip()
    app.config["TOKEN"] = token

    def cfg() -> Config:
        """Reloaded per request so preference edits take effect immediately."""
        return load_config()

    # ------------------------------------------------------------- auth
    @app.before_request
    def check_token():
        if not token:
            return None            # localhost-only mode
        if request.endpoint == "static":
            return None
        supplied = (request.headers.get("X-Auth-Token")
                    or request.args.get("token", ""))
        if secrets.compare_digest(supplied, token):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorised"}), 401
        return render_template("login.html"), 401

    # ------------------------------------------------------------ pages
    @app.get("/")
    def index():
        return render_template("index.html", token_required=bool(token))

    # ------------------------------------------------------------- data
    @app.get("/api/summary")
    def api_summary():
        db = Database()
        try:
            c = cfg()
            ok, why = within_active_hours(c)
            return jsonify({
                "stats": db.stats(),
                "portals": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "enabled": p.enabled,
                        "manual_only": p.force_manual_submit,
                        "used_today": db.count_today(p.id),
                        "cap": c.daily_cap(p.id),
                    }
                    for p in c.portals.values()
                ],
                "auto_submit": c.auto_submit,
                "active_hours_ok": ok,
                "active_hours_note": why,
                "thresholds": c.thresholds,
            })
        finally:
            db.close()

    @app.get("/api/shortlist")
    def api_shortlist():
        min_score = request.args.get("min_score", type=float)
        limit = request.args.get("limit", default=60, type=int)
        portal = request.args.get("portal", "")

        db = Database()
        try:
            c = cfg()
            scorer = Scorer(c.preferences, c.profile)
            threshold = (min_score if min_score is not None
                         else float(c.thresholds.get("shortlist", 60)))
            rows = db.shortlist(min_score=threshold, limit=limit)
            out = []
            for r in rows:
                if portal and r["portal"] != portal:
                    continue
                out.append({
                    "fingerprint": r["fingerprint"],
                    "title": r["title"],
                    "company": r["company"],
                    "location": r["location"],
                    "url": r["url"],
                    "portal": r["portal"],
                    "score": r["total"],
                    "band": scorer.band(r["total"]),
                    "salary": _salary_text(r),
                    "reasons": _json_list(r["reasons"]),
                    "components": _json_obj(r["components"]),
                    "sightings": [s["portal"] for s in db.sightings(r["fingerprint"])],
                })
            return jsonify({"jobs": out, "threshold": threshold})
        finally:
            db.close()

    @app.get("/api/pending")
    def api_pending():
        db = Database()
        try:
            return jsonify({"pending": [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "company": r["company"],
                    "url": r["url"],
                    "portal": r["portal"],
                    "score": r["total"] or 0,
                    "escalated": _json_list(r["escalated"]),
                    "answered": _json_obj(r["answered"]),
                    "resume": r["resume_path"] or "",
                    "error": r["error"] or "",
                }
                for r in db.pending_review()
            ]})
        finally:
            db.close()

    @app.post("/api/pending/<int:app_id>/<action>")
    def api_pending_action(app_id: int, action: str):
        """You confirming in the browser IS the human step -- the dashboard
        records the outcome, it never clicks submit on a portal itself."""
        mapping = {
            "submitted": AppStatus.SUBMITTED,
            "skip": AppStatus.SKIPPED,
        }
        if action not in mapping:
            return jsonify({"error": f"unknown action {action}"}), 400
        db = Database()
        try:
            db.set_status(app_id, mapping[action])
            return jsonify({"ok": True, "status": mapping[action].value})
        finally:
            db.close()

    # ------------------------------------------------------ preferences
    @app.get("/api/preferences")
    def api_get_preferences():
        path = _prefs_path()
        return jsonify({
            "path": str(path),
            "yaml": path.read_text(encoding="utf-8"),
            "parsed": yaml.safe_load(path.read_text(encoding="utf-8")),
        })

    @app.post("/api/preferences")
    def api_save_preferences():
        """Validates before writing. A config that fails validation is rejected
        rather than saved, so the dashboard cannot brick the CLI."""
        body = request.get_json(silent=True) or {}
        text = body.get("yaml", "")
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            return jsonify({"error": f"invalid YAML: {exc}"}), 400
        if not isinstance(parsed, dict):
            return jsonify({"error": "preferences must be a YAML mapping"}), 400

        path = _prefs_path()
        backup = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(text, encoding="utf-8")
        try:
            load_config()
        except ConfigError as exc:
            path.write_text(backup, encoding="utf-8")   # roll back
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True, "path": str(path)})

    # ------------------------------------------------------------ tasks
    @app.get("/api/task")
    def api_task():
        return jsonify(TASK.snapshot())

    @app.post("/api/discover")
    def api_discover():
        if TASK.running:
            return jsonify({"error": f"{TASK.name} is already running"}), 409
        body = request.get_json(silent=True) or {}
        portals = body.get("portals") or None
        _spawn("discover", lambda c, db, pipe: pipe.discover(portal_ids=portals))
        return jsonify({"ok": True})

    @app.post("/api/apply")
    def api_apply():
        """Runs the browser-side preparation. Every application still stops at
        the review gate; with no TTY the gate leaves them 'prepared' and they
        show up under Pending for you to finish."""
        if TASK.running:
            return jsonify({"error": f"{TASK.name} is already running"}), 409
        body = request.get_json(silent=True) or {}
        limit = int(body.get("limit", 5))
        portals = body.get("portals") or None
        min_score = body.get("min_score")
        _spawn("apply", lambda c, db, pipe: pipe.apply(
            portal_ids=portals, limit=limit,
            min_score=float(min_score) if min_score is not None else None))
        return jsonify({"ok": True})

    @app.post("/api/refresh")
    def api_refresh():
        if TASK.running:
            return jsonify({"error": f"{TASK.name} is already running"}), 409
        _spawn("refresh", lambda c, db, pipe: pipe.refresh_profiles())
        return jsonify({"ok": True})

    @app.get("/api/doctor")
    def api_doctor():
        try:
            c = cfg()
        except ConfigError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({
            "ok": True,
            "portals": registry.available(c),
            "roles": c.search.get("roles", []),
            "auto_submit": c.auto_submit,
        })

    return app


# ------------------------------------------------------------- internals
def _spawn(name: str, work: Any) -> None:
    TASK.begin(name)

    def runner() -> None:
        db = Database()
        try:
            c = load_config()
            pipe = Pipeline(c, db, log=TASK.log)
            TASK.log(f"starting {name}...")
            result = work(c, db, pipe) or {}
            TASK.log(f"{name} finished")
            TASK.finish(result=result)
        except Exception as exc:
            TASK.log(f"error: {type(exc).__name__}: {exc}")
            TASK.finish(error=f"{type(exc).__name__}: {exc}")
        finally:
            db.close()

    threading.Thread(target=runner, daemon=True, name=f"jobauto-{name}").start()


def _prefs_path() -> Path:
    """Always edit the .local override, never the committed template."""
    local = CONFIG_DIR / "preferences.local.yaml"
    if not local.exists():
        base = CONFIG_DIR / "preferences.yaml"
        local.write_text(base.read_text(encoding="utf-8"), encoding="utf-8")
    return local


def _salary_text(row: Any) -> str:
    if not row["salary_known"] or row["salary_max"] is None:
        return ""
    lo = row["salary_min"] if row["salary_min"] is not None else row["salary_max"]
    if lo == row["salary_max"]:
        return f"{lo:g} LPA"
    return f"{lo:g}-{row['salary_max']:g} LPA"


def _json_list(text: Any) -> list:
    try:
        value = json.loads(text or "[]")
        return value if isinstance(value, list) else []
    except Exception:
        return []


def _json_obj(text: Any) -> dict:
    try:
        value = json.loads(text or "{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def serve(host: str = "127.0.0.1", port: int = 5000, debug: bool = False) -> None:
    app = create_app()
    token = app.config.get("TOKEN")

    print(f"\n  jobauto dashboard -> http://{host}:{port}")
    if token:
        print(f"  token auth ON -- http://{host}:{port}/?token={token}")
    elif host not in ("127.0.0.1", "localhost"):
        print("\n  WARNING: bound beyond localhost with no JOBAUTO_WEB_TOKEN set.")
        print("  This page exposes your phone number, salary and application")
        print("  history. Set JOBAUTO_WEB_TOKEN before exposing it.\n")
    print("  Ctrl-C to stop\n")

    app.run(host=host, port=port, debug=debug, threaded=True)
