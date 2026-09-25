"""Local agent.

Runs on your PC. Polls the cloud outbound over HTTPS, so nothing is exposed on
your network and no port forwarding is involved.

What stays here and never goes up: portal cookies, browser profiles, resume
files. What goes up: job listings, scores, application status. If the cloud
database leaked tomorrow, nobody would gain access to a single job portal.
"""
from __future__ import annotations

import contextlib
import json
import os
import platform
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests
import yaml

from .. import browser as browser_mod
from ..config import Config, ConfigError, load_config, CONFIG_DIR
from ..db import Database
from ..pipeline import Pipeline

DEFAULT_INTERVAL = 30
# How often a running task streams its output to the dashboard.
PROGRESS_INTERVAL = 5
# How often a long run pushes its scored jobs up, so the dashboard fills while
# the run is still going rather than all at once at the end.
STATE_SYNC_INTERVAL = 20
# Must stay under the server's 3 minute online window, or an agent that is
# merely backing off gets reported as offline.
MAX_BACKOFF = 120


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

    def report_detected(self, portal_id: str, payload: dict) -> dict:
        return self._call("POST", f"/api/agent/portals/{portal_id}/detected",
                          json=payload)

    def clear_queued_jobs(self, fingerprints: list[str]) -> dict:
        if not fingerprints:
            return {"ok": True, "cleared": 0}
        return self._call("POST", "/api/agent/jobs/clear",
                          json={"fingerprints": fingerprints})

    def task_progress(self, task_id: int, log: str, status: str = "") -> dict:
        return self._call("POST", f"/api/agent/tasks/{task_id}/progress",
                          json={"log": log, "status": status})

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
        # When each pending portal was last looked at, so a page that cannot
        # be read is not re-opened on every poll.
        self._detect_attempted: dict[str, float] = {}

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
    def push_state(self, db: Database, config: Config,
                   quiet: bool = False) -> None:
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
            # What the portal said, not when we found it -- a listing can
            # already be weeks old the first time a search surfaces it.
            "posted_date": r["posted_date"] or None,
            "score": r["total"],
            "band": scorer.band(r["total"]),
            "reasons": json.loads(r["reasons"] or "[]"),
            "dropped": False,
        } for r in rows]

        if jobs:
            res = self.cloud.push_jobs(jobs)
            if not quiet:
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
        } for r in db.needs_attention()]
        if apps:
            self.cloud.push_applications(apps)
            if not quiet:
                self.log(f"  pushed {len(apps)} applications needing you")

    # -------------------------------------------------------- heartbeat
    @contextlib.contextmanager
    def heartbeat(self, status: str):
        """Keep reporting in while a long task runs.

        hello() is otherwise only called between polls, so during a discover
        across five portals the agent looks dead for as long as the run takes.
        The server uses last_seen to decide a task has been stranded, so
        without this a slow-but-healthy run gets reaped out from under itself.
        """
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(20):
                try:
                    self.cloud.hello(status)
                except Exception:
                    pass          # a missed beat is not worth failing the run

        thread = threading.Thread(target=beat, daemon=True,
                                  name="jobauto-heartbeat")
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)

    # ------------------------------------------------------------ tasks
    def run_task(self, task: dict, db: Database, config: Config) -> None:
        kind = task.get("kind", "")
        payload = task.get("payload") or {}
        lines: list[str] = []

        task_id = task.get("id")
        last_push = [0.0]
        last_sync = [time.monotonic()]
        lock = threading.Lock()

        def capture(line: str) -> None:
            # discover now searches several portals on worker threads, so this
            # is called concurrently. Appending is atomic, but the joins and
            # the timers below are not.
            with lock:
                lines.append(str(line))
                del lines[:-600]
                now = time.monotonic()
                due_push = task_id and (now - last_push[0]) >= PROGRESS_INTERVAL
                due_sync = (now - last_sync[0]) >= STATE_SYNC_INTERVAL
                if due_push:
                    last_push[0] = now
                if due_sync:
                    last_sync[0] = now
                snapshot = "\n".join(lines) if due_push else ""

            self.log(f"    {line}")

            # Throttled: a discover emits a line per job, and one request each
            # would hammer a free tier for no benefit.
            if due_push:
                try:
                    self.cloud.task_progress(task_id, snapshot,
                                             status=f"{kind}: {str(line)[:80]}")
                except Exception:
                    pass          # progress is nice to have, never fatal

            # Jobs used to reach the dashboard only when the whole task
            # finished, so a run across five portals showed nothing for its
            # entire duration. Push what has been scored so far instead.
            #
            # Only from the main thread: this reads SQLite, whose connection
            # belongs to the thread that opened it, and a worker calling it
            # would raise ProgrammingError into a swallowed except -- a sync
            # that silently never happens. Scoring runs on the main thread, so
            # its log lines are the ones that trigger this.
            if due_sync and threading.current_thread() is threading.main_thread():
                try:
                    self.push_state(db, config, quiet=True)
                except Exception as exc:
                    self.log(f"    could not sync mid-run: {type(exc).__name__}")

        pipe = Pipeline(config, db, log=capture)
        if not self.headless:
            pipe.login_hook = lambda portal: self._login_window(portal, config)
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

    # -------------------------------------------------------- decisions
    def settle_decisions(self, decided: list[dict]) -> int:
        """Mirror browser-side outcomes into the local database.

        The cloud is authoritative for what the user chose; this machine is
        authoritative for what it actually did. Only rows still 'prepared'
        move, so this can never rewrite a real local outcome.
        """
        if not decided:
            return 0

        settled = 0
        db = Database()
        try:
            for row in decided:
                fingerprint = str(row.get("fingerprint") or "")
                status = str(row.get("status") or "")
                if not fingerprint or status not in ("submitted", "skipped"):
                    continue
                settled += db.settle_application(
                    fingerprint, status, str(row.get("portal") or ""))
        finally:
            db.close()

        if settled:
            self.log(f"  settled {settled} application(s) decided in the dashboard")
        return settled

    # ------------------------------------------------------------- loop
    # ------------------------------------------------ portal detection
    # Once an hour per portal at most: a page that cannot be read now will
    # not read differently in thirty seconds, and each attempt opens a
    # browser.
    DETECT_RETRY_SECONDS = 3600
    _LOGIN_LINK = re.compile(
        r'href=["\']([^"\']*(?:login|signin|sign-in|log-in)[^"\']*)["\']',
        re.IGNORECASE)
    _LOGIN_URL_MARKERS = ("/login", "/signin", "nlogin", "/auth", "sign-in")

    def detect_pending_portals(self, cfg: Config) -> int:
        """Work out selectors for portals added with only a name and a URL.

        The dashboard cannot see a rendered page; this PC can. For each
        custom portal flagged `detect`, open the search URL in that portal's
        own profile (signed in, if the user has done `login --portal`), find
        the repeating job cards, tokenise the search terms, check the login
        page can be reached, and report it all. The cloud writes it into the
        portal definition, the next preference sync brings it back down, and
        the portal switches on.
        """
        report = getattr(self.cloud, "report_detected", None)
        block = cfg.preferences.get("portals") or {}
        custom = block.get("custom") if isinstance(block, dict) else None
        if report is None or not isinstance(custom, dict):
            return 0

        from ..portals import detect as detect_mod
        roles = cfg.search.get("roles") or [{}]
        first = roles[0] if isinstance(roles[0], dict) else {}
        role = str(first.get("title", ""))
        locs = cfg.search.get("locations", {}).get("preferred") or [""]
        location = str(locs[0] or "")

        done = 0
        for raw_id, data in custom.items():
            pid = str(raw_id).strip().lower()
            if not isinstance(data, dict) or not data.get("detect"):
                continue
            url = str((data.get("search") or {}).get("url_template") or "")
            if not url:
                continue
            last = self._detect_attempted.get(pid, 0.0)
            if time.time() - last < self.DETECT_RETRY_SECONDS:
                continue
            self._detect_attempted[pid] = time.time()

            name = str(data.get("name") or pid)
            login_hint = str((data.get("auth") or {}).get("login_url") or "")
            self.log(f"  looking at {name} to work out its selectors")
            payload = self._detect_one(cfg, pid, name, url, role, location,
                                       login_hint, detect_mod)
            if payload.get("cards"):
                self.log(f"    found {payload['cards']} jobs -- "
                         f"{payload['search']['result_card']}")
            else:
                self.log(f"    {payload.get('error') or 'nothing found'}")
            try:
                report(pid, payload)
                done += 1
            except AgentError as exc:
                self.log(f"    could not report it: {exc}")
        return done

    def _detect_one(self, cfg: Config, pid: str, name: str, url: str,
                    role: str, location: str, login_hint: str,
                    detect_mod: Any) -> dict:
        """One look at one portal: the search page first, then the login
        page. Both are reported as checks the dashboard can show, because
        "added" on its own does not say whether the site will ever work."""
        from ..config import DEFAULT_CUSTOM_ADAPTER, PortalConfig

        m = re.match(r"^(https?://[^/]+)", url)
        origin = m.group(1) if m else url
        stub = PortalConfig(id=pid, name=name, enabled=True, base_url=origin,
                            adapter=DEFAULT_CUSTOM_ADAPTER)
        template, placed = detect_mod.tokenise_search_url(url, role, location)
        out: dict[str, Any] = {"template": template, "placed": placed,
                               "cards": 0, "notes": [], "error": "",
                               "checks": {"search_page": "", "login_page": ""}}
        checks = out["checks"]

        # 1. Open the front door and see what kind of page it is.
        look = self._look_at(stub, cfg, url, detect_mod)
        self._save_debug_page(pid, look)
        if look["error"]:
            checks["search_page"] = "unreachable"
            out["error"] = look["error"]
            return out
        if look["kind"] == "challenge":
            checks["search_page"] = "bot check"
            out["error"] = (f"{name} served a bot check instead of a page, "
                            f"the same wall Indeed puts up. It cannot be read "
                            f"by automation; remove it")
            return out

        # 2. Sign in first. The user's rule, and the right one: a board shows
        # its real results -- and later takes applications -- only with a
        # session, so establish it before anything else. Where the sign-in
        # page is comes from what the user gave, the redirect we just got,
        # or the first sign-in link on the page.
        login_url = login_hint
        if look["kind"] == "login":
            login_url = look["landed"]
        elif not login_url:
            login_url = detect_mod.find_login_link(look["html"], origin)
        signed_out = (look["kind"] == "login"
                      or detect_mod.looks_signed_out(look["html"]))
        window = ""
        if signed_out and login_url.startswith("http"):
            if self.headless:
                checks["login_page"] = "found, but this agent has no screen"
            else:
                self._say_status(f"sign in to {name} in the window on this PC, "
                                 f"then close it")
                self.log(f"    opening {name}'s sign-in page -- sign in there "
                         f"and close the window")
                window = self._wait_for_signin(stub, cfg, login_url)
                checks["login_page"] = f"ok, sign-in window {window}"
                self._say_status("idle")
        if login_url.startswith("http"):
            out["login_url"] = login_url

        # 3. Find the jobs: on the page as it is, or by using the site's own
        # search box the way a person would.
        if look["det"] is None or window:
            look = self._explore(stub, cfg, url, origin, role, location, detect_mod)
            self._save_debug_page(pid, look)

        kind, det = look["kind"], look["det"]
        if det is not None:
            via = " (via the site's search box)" if look.get("via") else ""
            checks["search_page"] = f"ok, {det.cards} jobs{via}"
            out["search"] = det.to_search()
            out["cards"] = det.cards
            out["notes"].extend(det.notes)
            if window:
                out["notes"].append("results appeared after signing in")
            # Which address becomes the template: after a search-box search,
            # the page landed on -- it carries the role and city just typed.
            # When the address the user gave already showed the jobs, that
            # one -- it holds their exact terms, where a site may shorten or
            # canonicalise what it lands on.
            source = look["landed"] if look.get("via") else url
            template, placed = detect_mod.tokenise_search_url(
                source or url, role, location)
            out["template"], out["placed"] = template, placed
        elif kind == "challenge":
            checks["search_page"] = "bot check"
            out["error"] = (f"{name} served a bot check instead of results, "
                            f"the same wall Indeed puts up. It cannot be read "
                            f"by automation; remove it")
        elif kind == "login":
            checks["search_page"] = "needs sign-in"
            out["error"] = (f"the site wants you signed in before it shows "
                            f"results. On this PC run: jobauto login --portal "
                            f"{pid}, then Try again")
        elif kind == "nosearch":
            checks["search_page"] = "no job list and no search box found"
            out["error"] = (f"could not find a job list or a search box on "
                            f"{name}{', even after a sign-in window' if window else ''}. "
                            f"The page as seen is saved at data/debug/{pid}.html "
                            f"on this PC -- paste the address of a results page "
                            f"instead (search on the site first)")
        else:
            checks["search_page"] = "loaded, but no job list found"
            out["error"] = (f"no repeating list of job links on that page"
                            f"{', even after a sign-in window' if window else ''}. "
                            f"The page as seen is saved at data/debug/{pid}.html "
                            f"on this PC -- paste the address of the results "
                            f"page itself, after searching")

        # The sign-in page is reached once, so a wrong address is reported
        # here rather than discovered at `login` time.
        if not checks["login_page"]:
            if login_url.startswith("http"):
                checks["login_page"] = self._probe(stub, cfg, login_url)
                if checks["login_page"] != "ok":
                    out.pop("login_url", None)
            else:
                checks["login_page"] = ("not found on the page -- sign in by "
                                        "hand once if the site needs it")

        if out["cards"] and not out["placed"]:
            out["notes"].append(
                f"your role and city were not found in the results address, "
                f"so it is used as-is. To search other roles, run a search on "
                f"the site for '{role}' in '{location}' and paste that URL "
                f"instead")
        return out

    def _explore(self, stub: Any, cfg: Config, url: str, origin: str,
                 role: str, location: str, detect_mod: Any) -> dict:
        """Get to a results page from wherever `url` lands, as a person would.

        The page itself may already be one. If not, find the search box --
        here, or one Jobs/Search link away -- type the first role and city,
        submit, and read what comes back. kind gains one value: nosearch.
        """
        from ..portals.base import PortalAdapter

        look: dict[str, Any] = {"kind": "empty", "det": None, "html": "",
                                "landed": "", "title": "", "error": "", "via": ""}

        def settle(page: Any) -> None:
            deadline = time.monotonic() + self.DETECT_WAIT_SECONDS
            while True:
                page.wait_for_timeout(1500)
                look["html"] = page.content()
                look["det"] = detect_mod.detect(look["html"])
                if look["det"] is not None or time.monotonic() > deadline:
                    break
            look["landed"] = str(page.url or "")
            try:
                look["title"] = str(page.title() or "")
            except Exception:
                pass

        def classify() -> None:
            landed, title = look["landed"].lower(), look["title"].lower()
            if look["det"] is not None:
                look["kind"] = "jobs"
            elif (any(k in landed for k in PortalAdapter._CHALLENGE_URL_MARKERS)
                  or any(k in title for k in PortalAdapter._CHALLENGE_TITLE_MARKERS)):
                look["kind"] = "challenge"
            elif any(k in landed for k in self._LOGIN_URL_MARKERS):
                look["kind"] = "login"
            else:
                look["kind"] = "empty"

        try:
            with browser_mod.session(stub, cfg, headless=self.headless) as page:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                settle(page)
                classify()
                if look["kind"] != "empty":
                    return look

                form = detect_mod.find_search_form(look["html"])
                if form is None:
                    link = detect_mod.find_jobs_link(look["html"], origin)
                    if link and link.rstrip("/") != url.rstrip("/"):
                        page.goto(link, wait_until="domcontentloaded", timeout=60000)
                        settle(page)
                        classify()
                        if look["kind"] != "empty":
                            return look
                        form = detect_mod.find_search_form(look["html"])
                if form is None:
                    look["kind"] = "nosearch"
                    return look

                # Type what the user would type, and submit the way the site
                # expects -- its own button if it has one, Enter if not.
                box = page.locator(form.keyword).first
                box.fill(role, timeout=10000)
                if form.location and location:
                    try:
                        page.locator(form.location).first.fill(location, timeout=5000)
                    except Exception:
                        pass
                if form.submit:
                    try:
                        page.locator(form.submit).first.click(timeout=10000)
                    except Exception:
                        box.press("Enter")
                else:
                    box.press("Enter")
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    pass
                settle(page)
                classify()
                look["via"] = "search box"
        except Exception as exc:
            look["error"] = f"could not use the site: {type(exc).__name__}: {exc}"[:240]
        return look

    # How long to give a results page to draw its list, polling as it goes.
    # A board that has not drawn anything in this long is not going to.
    DETECT_WAIT_SECONDS = 15
    # How long a sign-in window stays open before detection moves on. The
    # session persists as the user goes, so a timeout is not a failure.
    SIGNIN_WAIT_MINUTES = 5.0

    def _look_at(self, stub: Any, cfg: Config, url: str, detect_mod: Any) -> dict:
        """Open the URL and classify what came back.

        kind is one of: jobs (det set), challenge, login, empty. Polls for
        the list rather than sleeping a fixed time, because a slow board and
        a page with nothing on it look the same at five seconds and quite
        different at fifteen.
        """
        from ..portals.base import PortalAdapter

        look: dict[str, Any] = {"kind": "empty", "det": None, "html": "",
                                "landed": "", "title": "", "error": ""}
        try:
            with browser_mod.session(stub, cfg, headless=self.headless) as page:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                deadline = time.monotonic() + self.DETECT_WAIT_SECONDS
                while True:
                    page.wait_for_timeout(1500)
                    look["html"] = page.content()
                    look["det"] = detect_mod.detect(look["html"])
                    if look["det"] is not None or time.monotonic() > deadline:
                        break
                look["landed"] = str(page.url or "")
                try:
                    look["title"] = str(page.title() or "")
                except Exception:
                    pass
        except Exception as exc:
            look["error"] = f"could not open the page: {type(exc).__name__}: {exc}"[:240]
            return look

        landed, title = look["landed"].lower(), look["title"].lower()
        if look["det"] is not None:
            look["kind"] = "jobs"
        elif (any(k in landed for k in PortalAdapter._CHALLENGE_URL_MARKERS)
              or any(k in title for k in PortalAdapter._CHALLENGE_TITLE_MARKERS)):
            look["kind"] = "challenge"
        elif any(k in landed for k in self._LOGIN_URL_MARKERS):
            look["kind"] = "login"
        return look

    def _wait_for_signin(self, stub: Any, cfg: Config, login_url: str) -> str:
        """Open the sign-in page in the portal's own profile and wait for the
        user to finish and close the window. Cookies persist as they go."""
        try:
            with browser_mod.session(stub, cfg, headless=False) as page:
                page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
                return browser_mod.wait_for_login(page, "", self.SIGNIN_WAIT_MINUTES)
        except Exception as exc:
            return f"could not open: {type(exc).__name__}"

    def _probe(self, stub: Any, cfg: Config, url: str) -> str:
        try:
            with browser_mod.session(stub, cfg, headless=self.headless) as page:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                ok = resp is None or getattr(resp, "ok", True)
                return "ok" if ok else f"returned {getattr(resp, 'status', '?')}"
        except Exception as exc:
            return f"unreachable: {type(exc).__name__}"

    def _save_debug_page(self, pid: str, look: dict) -> None:
        """What detection saw, kept where `dump` keeps its pages, so a miss
        can be looked at instead of guessed about."""
        if not look.get("html"):
            return
        try:
            from ..config import data_dir
            out = data_dir() / "debug"
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{pid}.html").write_text(look["html"], encoding="utf-8")
        except Exception:
            pass

    # How long a sign-in window opened mid-run stays up. Long enough for an
    # OTP; short enough that a scheduled run with nobody at the PC loses a
    # few minutes on that portal, not the morning.
    RUN_SIGNIN_MINUTES = 4.0

    def _login_window(self, portal: Any, config: Config) -> bool:
        """A portal's session expired mid-run. Open its sign-in page here,
        tell the dashboard a window is waiting, and report whether the run
        should try that portal again."""
        self._say_status(f"sign in to {portal.name} in the window on this PC, "
                         f"then close it")
        try:
            return bool(browser_mod.interactive_login(
                portal, config, minutes=self.RUN_SIGNIN_MINUTES))
        finally:
            self._say_status("working")

    def _say_status(self, text: str) -> None:
        """Put a line under the online dot, so a sign-in window waiting on the
        PC is visible from the phone that added the portal."""
        try:
            self.cloud.hello(text[:120])
        except Exception:
            pass

    def tick(self) -> None:
        self.sync_preferences()
        # Detection first, so a portal added a moment ago is looked at on this
        # poll rather than after whatever task is queued.
        try:
            self.detect_pending_portals(load_config())
        except Exception as exc:
            self.log(f"  portal detection skipped: {type(exc).__name__}: {exc}")
        work = self.cloud.work()
        task, queued = work.get("task"), work.get("queued_jobs") or []

        # Apply browser-side decisions first, so an application the user
        # already submitted or skipped is not re-pushed as pending on the way
        # out of this same tick.
        self.settle_decisions(work.get("decided") or [])

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
                with self.heartbeat(f"running {task['kind']}"):
                    self.run_task(task, db, config)
            elif queued:
                with self.heartbeat("applying queued jobs"):
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
            status = "idle"
            try:
                self.tick()
                backoff = self.interval
            except KeyboardInterrupt:
                self.log("\n  agent stopped.\n")
                return
            except AgentError as exc:
                self.log(f"  {exc}")
                status = f"error: {exc}"
                backoff = min(backoff * 2, MAX_BACKOFF)
            except Exception as exc:
                self.log(f"  unexpected: {type(exc).__name__}: {exc}")
                status = f"error: {type(exc).__name__}: {exc}"
                backoff = min(backoff * 2, MAX_BACKOFF)

            # Heartbeat on every pass, including after a failure. Reporting
            # only on success meant one bad cycle made a running agent look
            # offline, which is the opposite of what you need to see: the
            # dashboard should say "online, and here is what is wrong".
            try:
                self.cloud.hello(status[:255])
            except Exception:
                pass          # genuinely unreachable -- offline is then true

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
