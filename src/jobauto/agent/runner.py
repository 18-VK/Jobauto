"""Local agent.

Runs on your PC. Polls the cloud outbound over HTTPS, so nothing is exposed on
your network and no port forwarding is involved.

What stays here and never goes up: portal cookies, browser profiles, resume
files. What goes up: job listings, scores, application status. If the cloud
database leaked tomorrow, nobody would gain access to a single job portal.
"""
from __future__ import annotations

import json
import os
import platform
import random
import time
from pathlib import Path
from typing import Any, Callable

import requests
import yaml

from ..config import Config, ConfigError, load_config, CONFIG_DIR
from ..db import Database
from ..pipeline import Pipeline

DEFAULT_INTERVAL = 30


class AgentError(Exception):
    pass


class CloudClient:
    def __init__(self, base_url: str, token: str, timeout: int = 30):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.http = requests.Session()
        self.http.headers.update({
            "X-Agent-Token": token,
            "Content-Type": "application/json",
            "User-Agent": f"jobauto-agent/{platform.node()}",
        })

    def _call(self, method: str, path: str, **kw: Any) -> dict:
        url = f"{self.base}{path}"
        try:
            res = self.http.request(method, url, timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise AgentError(f"cannot reach {self.base}: {type(exc).__name__}") from exc
        if res.status_code == 401:
            raise AgentError("agent token rejected -- rotate it in the dashboard")
        if res.status_code >= 400:
            raise AgentError(f"{path} returned {res.status_code}: {res.text[:200]}")
        try:
            return res.json()
        except ValueError:
            raise AgentError(f"{path} did not return JSON")

    def hello(self, status: str = "idle") -> dict:
        return self._call("POST", "/api/agent/hello", json={"status": status})

    def preferences(self) -> dict:
        return self._call("GET", "/api/agent/preferences")

    def work(self) -> dict:
        return self._call("GET", "/api/agent/work")

    def push_jobs(self, jobs: list[dict]) -> dict:
        # Free Postgres tiers are happier with modest batches.
        out = {"added": 0, "updated": 0}
        for i in range(0, len(jobs), 200):
            chunk = jobs[i:i + 200]
            res = self._call("POST", "/api/agent/jobs", json={"jobs": chunk})
            out["added"] += res.get("added", 0)
            out["updated"] += res.get("updated", 0)
        return out

    def push_applications(self, apps: list[dict]) -> dict:
        if not apps:
            return {"ok": True, "count": 0}
        return self._call("POST", "/api/agent/applications",
                          json={"applications": apps})

    def clear_queued_jobs(self, fingerprints: list[str]) -> dict:
        if not fingerprints:
            return {"ok": True, "cleared": 0}
        return self._call("POST", "/api/agent/jobs/clear",
                          json={"fingerprints": fingerprints})

    def task_result(self, task_id: int, status: str, result: dict,
                    log: str = "") -> dict:
        return self._call("POST", f"/api/agent/tasks/{task_id}/result",
                          json={"status": status, "result": result, "log": log})


class LocalAgent:
    def __init__(self, cloud: CloudClient, interval: int = DEFAULT_INTERVAL,
                 headless: bool = False, log: Callable[[str], None] = print):
        self.cloud = cloud
        self.interval = max(10, interval)
        self.headless = headless
        self.log = log
        self._prefs_stamp: str | None = None

    # ------------------------------------------------------- preferences
    def sync_preferences(self) -> None:
        """Cloud preferences win. They are written to preferences.local.yaml so
        the local CLI and the agent always agree on what to search for."""
        data = self.cloud.preferences()
        stamp, text = data.get("updated"), data.get("yaml") or ""
        if not text or stamp == self._prefs_stamp:
            return
        try:
            parsed = yaml.safe_load(text)
            if not isinstance(parsed, dict):
                raise ValueError("not a mapping")
        except Exception as exc:
            self.log(f"  cloud preferences are invalid, keeping local: {exc}")
            return

        target = CONFIG_DIR / "preferences.local.yaml"
        target.write_text(text, encoding="utf-8")
        try:
            load_config()
        except ConfigError as exc:
            self.log(f"  cloud preferences failed validation, reverting: {exc}")
            target.unlink(missing_ok=True)
            if (CONFIG_DIR / "preferences.yaml").exists():
                self.log("  restored the shipped default preferences")
            return
        self._prefs_stamp = stamp
        self.log("  preferences synced from cloud")

    # -------------------------------------------------------- uploading
    def push_state(self, db: Database, config: Config) -> None:
        from ..scoring import Scorer
        scorer = Scorer(config.preferences, config.profile)

        rows = db.shortlist(min_score=0, limit=400, exclude_applied=False)
        jobs = [{
            "fingerprint": r["fingerprint"],
            "portal": r["portal"],
            "title": r["title"],
            "company": r["company"],
            "url": r["url"],
            "location": r["location"] or "",
            "salary": _salary_text(r),
            "summary": (r["summary"] or "")[:2000],
            "score": r["total"],
            "band": scorer.band(r["total"]),
            "reasons": json.loads(r["reasons"] or "[]"),
            "dropped": False,
        } for r in rows]

        if jobs:
            res = self.cloud.push_jobs(jobs)
            self.log(f"  pushed {len(jobs)} jobs "
                     f"({res['added']} new, {res['updated']} updated)")

        apps = [{
            "fingerprint": r["fingerprint"],
            "portal": r["portal"],
            "title": r["title"],
            "company": r["company"],
            "url": r["url"],
            "status": r["status"],
            "answered": json.loads(r["answered"] or "{}"),
            "escalated": json.loads(r["escalated"] or "[]"),
            "resume_path": r["resume_path"] or "",
            "note": r["error"] or "",
        } for r in db.pending_review()]
        if apps:
            self.cloud.push_applications(apps)
            self.log(f"  pushed {len(apps)} pending applications")

    # ------------------------------------------------------------ tasks
    def run_task(self, task: dict, db: Database, config: Config) -> None:
        kind = task.get("kind", "")
        payload = task.get("payload") or {}
        lines: list[str] = []

        def capture(line: str) -> None:
            lines.append(str(line))
            self.log(f"    {line}")

        pipe = Pipeline(config, db, log=capture)
        try:
            if kind == "discover":
                result = pipe.discover(portal_ids=payload.get("portals"),
                                       headless=self.headless)
            elif kind == "apply":
                fps = payload.get("fingerprints") or []
                if isinstance(fps, list):
                    fps = [str(item.get("fingerprint") if isinstance(item, dict) else item)
                           for item in fps if item]
                result = pipe.apply(portal_ids=payload.get("portals"),
                                    limit=int(payload.get("limit", 5)),
                                    min_score=payload.get("min_score"),
                                    headless=self.headless,
                                    fingerprints=fps or None,
                                    interactive=False)
            elif kind == "refresh":
                result = pipe.refresh_profiles(headless=self.headless)
            else:
                raise AgentError(f"unknown task kind {kind!r}")

            self.push_state(db, config)
            self.cloud.task_result(task["id"], "done", result or {},
                                   "\n".join(lines))
            self.log(f"  task {task['id']} ({kind}) done")
        except Exception as exc:
            lines.append(f"error: {type(exc).__name__}: {exc}")
            self.cloud.task_result(task["id"], "failed",
                                   {"error": str(exc)}, "\n".join(lines))
            self.log(f"  task {task['id']} ({kind}) FAILED: {exc}")

    def apply_queued(self, fingerprints: list[dict], db: Database,
                     config: Config) -> None:
        """Jobs the user starred in the browser while the PC was off."""
        if not fingerprints:
            return
        self.log(f"  {len(fingerprints)} job(s) queued from the dashboard")
        lines: list[str] = []
        pipe = Pipeline(config, db, log=lambda l: (lines.append(str(l)),
                                                   self.log(f"    {l}")))
        # Bound before the try: the finally below reads it, and an exception in
        # the comprehension would otherwise mask the real error.
        fps = [str(item.get("fingerprint", "")) for item in fingerprints
               if isinstance(item, dict) and item.get("fingerprint")]
        try:
            pipe.apply(limit=len(fps), headless=self.headless,
                       fingerprints=fps, interactive=False)
            self.push_state(db, config)
        finally:
            if fps:
                self.cloud.clear_queued_jobs(fps)

    # ------------------------------------------------------------- loop
    def tick(self) -> None:
        self.sync_preferences()
        work = self.cloud.work()
        task, queued = work.get("task"), work.get("queued_jobs") or []

        if not task and not queued:
            return

        queued_fps = [str(item.get("fingerprint", "")) for item in queued
                      if item.get("fingerprint")]

        # An apply task and the queue are one request: the dashboard's Apply
        # button sends no fingerprints, so without this the queued jobs get
        # consumed while the run applies to unrelated shortlisted jobs.
        apply_task = bool(task) and task.get("kind") == "apply"
        if apply_task and queued_fps:
            payload = dict(task.get("payload") or {})
            if not payload.get("fingerprints"):
                payload["fingerprints"] = queued_fps
                payload.setdefault("limit", len(queued_fps))
                task["payload"] = payload

        try:
            config = load_config()
            db = Database()
        except Exception as exc:
            # The task was claimed by /work before we got here. Bailing out
            # silently leaves it `running` forever, which then blocks every
            # later task of the same shape behind the already-pending check.
            if task:
                self.log(f"  task {task['id']} could not start: {exc}")
                try:
                    self.cloud.task_result(task["id"], "failed",
                                           {"error": str(exc)}, str(exc))
                except AgentError as report_exc:
                    self.log(f"  could not report the failure: {report_exc}")
            raise

        try:
            if task:
                self.log(f"  picked up task {task['id']}: {task['kind']}")
                self.run_task(task, db, config)
            elif queued:
                self.apply_queued(queued, db, config)
        finally:
            db.close()
            # Only once the work has actually been attempted. Clearing before
            # the run marks jobs done that were never opened.
            if apply_task and queued_fps:
                try:
                    self.cloud.clear_queued_jobs(queued_fps)
                except AgentError as exc:
                    self.log(f"  could not clear the queue: {exc}")

    def run_forever(self) -> None:
        info = self.cloud.hello("starting")
        self.log(f"\n  connected to {self.cloud.base} as {info.get('user')}")
        self.log(f"  polling every {self.interval}s -- Ctrl-C to stop\n")

        # An immediate push means the dashboard has data even before the first
        # discover, if the local DB already has history.
        try:
            config = load_config()
            db = Database()
            try:
                self.push_state(db, config)
            finally:
                db.close()
        except Exception as exc:
            self.log(f"  initial sync skipped: {type(exc).__name__}: {exc}")

        backoff = self.interval
        while True:
            try:
                self.tick()
                self.cloud.hello("idle")
                backoff = self.interval
            except AgentError as exc:
                self.log(f"  {exc}")
                backoff = min(backoff * 2, 600)     # cloud may be asleep
            except KeyboardInterrupt:
                self.log("\n  agent stopped.\n")
                return
            except Exception as exc:
                self.log(f"  unexpected: {type(exc).__name__}: {exc}")
                backoff = min(backoff * 2, 600)
            # Jitter keeps many agents from hammering a free tier in lockstep.
            time.sleep(backoff + random.uniform(0, 3))


def _salary_text(row: Any) -> str:
    if not row["salary_known"] or row["salary_max"] is None:
        return ""
    lo = row["salary_min"] if row["salary_min"] is not None else row["salary_max"]
    if lo == row["salary_max"]:
        return f"{lo:g} LPA"
    return f"{lo:g}-{row['salary_max']:g} LPA"


# ------------------------------------------------------------- config io
def agent_config_path() -> Path:
    from ..config import data_dir
    return data_dir() / "agent.json"


def save_agent_config(url: str, token: str) -> Path:
    path = agent_config_path()
    path.write_text(json.dumps({"url": url.rstrip("/"), "token": token},
                               indent=2), encoding="utf-8")
    return path


def load_agent_config() -> tuple[str, str]:
    url = os.environ.get("JOBAUTO_CLOUD_URL", "").strip()
    token = os.environ.get("JOBAUTO_AGENT_TOKEN", "").strip()
    if url and token:
        return url.rstrip("/"), token

    path = agent_config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("url", "").rstrip("/"), data.get("token", "")
        except Exception:
            pass
    raise AgentError(
        "This machine is not linked yet. Run:\n"
        "  python -m jobauto link --url https://your-app.onrender.com "
        "--token YOUR_AGENT_TOKEN\n"
        "(the token is on the Devices tab of your dashboard)")
