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


def interactive_login(portal: PortalConfig, config: Config) -> bool:
    """Open the login page and wait for you to sign in by hand.

    Returns True once the portal's logged-in marker appears. The cookies then
    persist in the profile dir, so this is a once-per-portal chore.
    """
    login_url = portal.auth.get("login_url") or portal.base_url
    marker = portal.auth.get("logged_in_selector")

    s = BrowserSession(portal, config, headless=False)
    page = s.start()
    try:
        page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        print(f"\n  A browser window is open on {portal.name}.")
        print("  Sign in there (including any OTP or MFA step).")
        print("  Waiting up to 5 minutes...\n")

        if not marker:
            input("  Press Enter here once you are signed in... ")
            return True

        try:
            page.locator(marker).first.wait_for(timeout=300000)
            print(f"  Signed in to {portal.name}. Session saved to "
                  f"{s.profile_dir}\n")
            return True
        except Exception:
            print(f"  Could not confirm sign-in to {portal.name}.")
            print(f"  If you are actually signed in, the "
                  f"`auth.logged_in_selector` in config/portals/"
                  f"{portal.id}.yaml is probably stale.\n")
            return False
    finally:
        s.stop()
