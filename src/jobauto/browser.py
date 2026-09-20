"""Browser session management.

Deliberately uses a *persistent* Playwright context rather than storing
credentials: you log in by hand once per portal, the session cookies live in
data/browser/<portal>/, and MFA never has to be automated. Nothing in this
project ever reads or stores a password.

Each portal gets its own profile directory so a ban or a cookie reset on one
cannot cascade to the others.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import Config, PortalConfig, data_dir

# A stock Playwright Chromium advertises HeadlessChrome and navigator.webdriver.
# We run headed by default and blunt the most obvious automation tells; this is
# about not tripping naive checks, not about defeating real detection.
_STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


class BrowserSession:
    """Owns one persistent browser context for one portal."""

    @staticmethod
    def _cleanup_stale_browser_session(profile_dir: Path) -> None:
        """Close Chrome/Chromium instances still holding the same profile.

        Persistent Playwright contexts are keyed by the profile directory. If a
        stale browser from a previous run is still alive, the next launch fails
        with "Opening in existing browser session" even though the user is not
        running anything manually from the UI. This is a platform-safe cleanup:
        we only kill the processes whose command line contains the exact profile
        directory we are about to reopen.
        """
        if os.name != "nt" or not profile_dir:
            return

        profile_str = str(profile_dir).lower().replace("\\", "/")
        try:
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "(Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'chrome|chromium|msedge' } | "
                    "Select-Object ProcessId, Name, CommandLine | ConvertTo-Json -Compress)",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return

        if proc.returncode not in (0, 1):
            return

        try:
            rows = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return

        if isinstance(rows, dict):
            rows = [rows]

        pids: list[int] = []
        for row in rows:
            cmd = str(row.get("CommandLine", "") or "")
            if not cmd:
                continue
            lower_cmd = cmd.lower()
            if profile_str in lower_cmd.replace("\\", "/") or str(profile_dir).lower() in lower_cmd:
                pid = row.get("ProcessId")
                if isinstance(pid, (int, str)):
                    try:
                        pids.append(int(pid))
                    except ValueError:
                        pass

        for pid in sorted(set(pids)):
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except Exception:
                pass

    def __init__(self, portal: PortalConfig, config: Config, headless: bool = False):
        self.portal = portal
        self.config = config
        # Headless is far more detectable. Default headed; the caller may
        # override for the portals that tolerate it.
        self.headless = headless
        self._pw: Any = None
        self._ctx: Any = None
        self.page: Any = None

    @property
    def profile_dir(self) -> Path:
        d = data_dir() / "browser" / self.portal.id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def start(self) -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run:\n"
                "  pip install -r requirements.txt\n"
                "  python -m playwright install chromium"
            ) from exc

        self._cleanup_stale_browser_session(self.profile_dir)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1440, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            user_agent=DEFAULT_UA,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        self._ctx.add_init_script(_STEALTH)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.set_default_timeout(20000)
        return self.page

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            if self._ctx:
                self._ctx.close()
        with contextlib.suppress(Exception):
            if self._pw:
                self._pw.stop()

    def __enter__(self) -> Any:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


@contextlib.contextmanager
def session(portal: PortalConfig, config: Config,
            headless: bool = False) -> Iterator[Any]:
    s = BrowserSession(portal, config, headless=headless)
    try:
        yield s.start()
    finally:
        s.stop()


def wait_for_login(page: Any, marker: str, minutes: float,
                   on_tick: Any = None) -> bool:
    """Poll for the signed-in marker instead of one long blocking wait.

    A single wait_for() cannot tell "still typing an OTP" from "this
    selector is stale", and whichever it was, the window got closed the
    moment it expired -- usually mid-login. Polling lets us notice success
    early, keep the window alive, and report progress while you work.
    """
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        try:
            if page.locator(marker).first.is_visible(timeout=1500):
                return True
        except Exception:
            pass
        if on_tick:
            on_tick(max(0, deadline - time.monotonic()))
        time.sleep(2)
    return False


def interactive_login(portal: PortalConfig, config: Config,
                      minutes: float = 10.0) -> bool:
    """Open the login page and wait for you to sign in by hand.

    The cookies persist in the profile dir as you go, so what matters most
    is that this window stays open until you are actually done -- not that
    we manage to recognise the logged-in marker.
    """
    login_url = portal.auth.get("login_url") or portal.base_url
    marker = portal.auth.get("logged_in_selector")

    s = BrowserSession(portal, config, headless=False)
    page = s.start()
    try:
        page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        print(f"\n  A browser window is open on {portal.name}.")
        print("  Sign in there, including any OTP or MFA step.")
        print(f"  I will watch for up to {minutes:g} minutes, then ask."
              f"  Take as long as you need -- the window stays open.\n")

        if marker and wait_for_login(page, marker, minutes):
            print(f"  Signed in to {portal.name}. Session saved to "
                  f"{s.profile_dir}\n")
            return True

        # Not finding the marker does not mean the login failed. These
        # selectors go stale constantly, and closing a window you can
        # plainly see is signed in helps nobody.
        if marker:
            print(f"  Could not confirm sign-in automatically.")
            print(f"  (auth.logged_in_selector in config/portals/"
                  f"{portal.id}.yaml may be stale: {marker})")
        try:
            reply = input("  Are you signed in in that window? [y/N] ")
        except EOFError:
            reply = ""
        if reply.strip().lower() in ("y", "yes"):
            print(f"  Session saved to {s.profile_dir}\n")
            return True
        print("  Nothing confirmed. The session is kept either way; "
              "re-run this if you need another go.\n")
        return False
    finally:
        s.stop()


# ------------------------------------------------------------- portability
# Moving the agent to an always-on machine needs the portal sessions to come
# with it, and that machine usually has no screen to log in on. These carry the
# cookies across. The file they produce IS the login -- anyone holding it is
# signed in as you -- so it lands under data/ (gitignored) and the CLI tells
# you to delete it once it has been imported.
SESSION_BUNDLE_VERSION = 1


def bundle_sessions(states: dict[str, dict]) -> dict:
    """Wrap per-portal storage states in a versioned envelope."""
    return {
        "version": SESSION_BUNDLE_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "portals": states,
    }


def read_bundle(path: Path) -> dict[str, dict]:
    """Unwrap a bundle, failing loudly rather than importing half a session."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict) or "portals" not in data:
        raise RuntimeError(f"{path} is not a jobauto session bundle.")
    if data.get("version") != SESSION_BUNDLE_VERSION:
        raise RuntimeError(
            f"{path} is a version {data.get('version')} bundle; this build "
            f"writes version {SESSION_BUNDLE_VERSION}.")
    portals = data["portals"]
    if not isinstance(portals, dict):
        raise RuntimeError(f"{path} has no per-portal sessions in it.")
    return portals


def export_session(portal: PortalConfig, config: Config) -> dict:
    """Read the cookies out of this portal profile directory."""
    s = BrowserSession(portal, config, headless=True)
    s.start()
    try:
        state = s._ctx.storage_state()
    finally:
        s.stop()
    return {"cookies": state.get("cookies", [])}


def import_session(portal: PortalConfig, config: Config, state: dict) -> int:
    """Write cookies into this portal profile directory. Returns the count."""
    cookies = state.get("cookies") or []
    if not cookies:
        return 0
    s = BrowserSession(portal, config, headless=True)
    s.start()
    try:
        s._ctx.add_cookies(cookies)
    finally:
        s.stop()
    return len(cookies)
