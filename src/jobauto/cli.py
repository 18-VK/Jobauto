"""Command line interface.

  python -m jobauto doctor              validate config, no browser
  python -m jobauto login --portal X    one-time manual sign-in per portal
  python -m jobauto discover            search + score, applies to nothing
  python -m jobauto shortlist           ranked list with reasons
  python -m jobauto apply               prepare + review gate
  python -m jobauto review              resume anything left prepared
  python -m jobauto refresh             daily profile touch (Naukri)
  python -m jobauto stats               what you have sent, and where
  python -m jobauto web                 local dashboard (no cloud needed)

Cloud mode -- a hosted dashboard you can use with this PC switched off:
  python -m jobauto link --url URL --token T   link this PC to your deployment
  python -m jobauto agent                      run the sync agent (keep it on)
  python -m jobauto cloud                      run the cloud app locally, to test
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .agent.runner import CloudClient, LocalAgent
from .config import Config, ConfigError, load_config, require_identity
from .db import Database
from .models import AppStatus
from .pipeline import Pipeline, within_active_hours
from .portals import registry
from .scoring import Scorer


def _sync_cloud_preferences_if_linked() -> None:
    try:
        from .agent.runner import load_agent_config
        url, token = load_agent_config()
    except Exception:
        return

    try:
        agent = LocalAgent(CloudClient(url, token), interval=30)
        agent.sync_preferences()
    except Exception:
        return


def invocation() -> str:
    """How this copy was launched, so help text matches what the user typed.

    An installed copy gets a `jobauto` entry point; a checkout is run with
    `python -m jobauto`. Printing the wrong one sends people down a path that
    does not work for them.
    """
    import sys
    from pathlib import Path as _Path

    stem = _Path(sys.argv[0]).stem.lower()
    return "jobauto" if stem in ("jobauto", "jobauto-script") else "python -m jobauto"


def _load() -> Config:
    _sync_cloud_preferences_if_linked()
    try:
        return load_config()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        raise SystemExit(2)


# --------------------------------------------------------------- commands
def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _load()
    print("\n  config: OK (scoring weights sum to 1.0, thresholds consistent)")

    print("\n  portals:")
    for pid, desc in registry.available(cfg).items():
        mark = "x" if "BROKEN" in desc else ("-" if "disabled" in desc else "+")
        print(f"    [{mark}] {pid:<12} {desc}")

    print("\n  roles configured:")
    for role in cfg.search.get("roles", []):
        aliases = ", ".join(role.get("aliases") or [])
        print(f"    - {role.get('title')} (weight {role.get('weight', 1.0)})"
              + (f"  aka {aliases}" if aliases else ""))

    locs = cfg.search.get("locations", {})
    print(f"\n  locations: {', '.join(locs.get('preferred', [])) or 'none'}"
          f"  |  modes: {', '.join(locs.get('work_mode', []))}")

    comp = cfg.search.get("compensation", {})
    print(f"  compensation: expect {comp.get('expected_ctc_lpa')} LPA, "
          f"floor {comp.get('minimum_acceptable_lpa')} LPA")

    th = cfg.thresholds
    print(f"  thresholds: shortlist {th.get('shortlist')}, "
          f"tailor {th.get('auto_tailor')}, priority {th.get('priority')}")

    print(f"\n  auto_submit: {cfg.auto_submit}"
          + ("  (review gate active)" if not cfg.auto_submit else "  (!! no prompt)"))
    forced = [p.name for p in cfg.enabled_portals() if p.force_manual_submit]
    if forced:
        print(f"  always manual regardless: {', '.join(forced)}")

    ok, why = within_active_hours(cfg)
    print(f"  active hours: {'inside' if ok else 'OUTSIDE -- ' + why}")

    # "discover found nothing and did not even open a browser" is otherwise
    # indistinguishable from a broken install.
    db = Database()
    try:
        if getattr(args, "clear_cooldown", ""):
            target = args.clear_cooldown
            db.clear_challenge(target)
            print(f"\n  cool-off cleared for {target}")
        parked = [r for r in db.portal_states() if db.cooling_until(r["portal"])]
        if parked:
            print("\n  cooling off after a bot check:")
            for row in parked:
                until = db.cooling_until(row["portal"])
                print(f"    {row['portal']:<12} until {until:%H:%M on %d %b}"
                      f"  ({row['strikes']} in a row)")
            print("    clear one with: python -m jobauto doctor "
                  "--clear-cooldown <portal>")
            print("    or stop searching it at all: set enabled: false in "
                  "config/portals/<portal>.yaml")
    finally:
        db.close()

    try:
        require_identity(cfg)
        print("\n  profile: identity complete")
    except ConfigError as exc:
        print(f"\n  {exc}")
        print("  (discover and shortlist work without this; apply does not)")

    print()
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    from .browser import interactive_login
    cfg = _load()
    targets = ([cfg.portals[p] for p in args.portal if p in cfg.portals]
               if args.portal else cfg.enabled_portals())
    if not targets:
        known = ", ".join(sorted(cfg.portals)) or "none configured"
        print(f"  No matching portal. Available: {known}")
        return 1
    for portal in targets:
        interactive_login(portal, cfg)
    return 0


def cmd_export_session(args: argparse.Namespace) -> int:
    """Carry your signed-in sessions to the machine that will run the agent."""
    from .browser import bundle_sessions, export_session
    from .config import data_dir

    cfg = _load()
    targets = ([cfg.portals[p] for p in args.portal if p in cfg.portals]
               if args.portal else cfg.enabled_portals())
    if not targets:
        print(f"  No matching portal. Available: {', '.join(sorted(cfg.portals))}")
        return 1

    out = Path(args.out) if args.out else data_dir() / "sessions.json"
    states: dict[str, dict] = {}
    for portal in targets:
        try:
            state = export_session(portal, cfg)
        except Exception as exc:
            print(f"  {portal.name}: could not read the session "
                  f"({type(exc).__name__}: {exc})")
            continue
        n = len(state.get("cookies") or [])
        states[portal.id] = state
        print(f"  {portal.name}: {n} cookies"
              + ("   <- looks signed out" if n == 0 else ""))

    if not states:
        print("  Nothing exported.")
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle_sessions(states), indent=2),
                   encoding="utf-8")
    print(f"  Written to {out}")
    print("  This file IS your logins. Move it over a channel you trust,")
    print("  import it on the other machine, then delete both copies.")
    return 0


def cmd_import_session(args: argparse.Namespace) -> int:
    from .browser import import_session, read_bundle

    cfg = _load()
    try:
        portals = read_bundle(Path(args.file))
    except RuntimeError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 2

    imported = 0
    for pid in (args.portal or list(portals)):
        if pid not in portals:
            print(f"  {pid}: not in the bundle")
            continue
        portal = cfg.portals.get(pid)
        if portal is None:
            print(f"  {pid}: not configured on this machine")
            continue
        try:
            n = import_session(portal, cfg, portals[pid])
        except Exception as exc:
            print(f"  {portal.name}: {type(exc).__name__}: {exc}")
            continue
        imported += 1
        print(f"  {portal.name}: {n} cookies restored")

    if not imported:
        print("  Nothing imported.")
        return 1
    print("  Check it worked:  python -m jobauto doctor")
    print("  Then delete the bundle file.")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    cfg = _load()
    db = Database()
    pipe = Pipeline(cfg, db)
    run_id = db.start_run("discover", args.portal or
                          [p.id for p in cfg.enabled_portals()])
    try:
        counts = pipe.discover(portal_ids=args.portal, headless=args.headless)
        db.finish_run(run_id, found=counts.get("found", 0),
                      shortlisted=counts.get("shortlisted", 0))
        print(f"\n  seen {counts.get('found', 0)}"
              f"  |  new {counts.get('new', 0)}"
              f"  |  shortlisted {counts.get('shortlisted', 0)}"
              f"  |  filtered out {counts.get('dropped', 0)}\n")
        print("  Next: python -m jobauto shortlist\n")
        return 0
    finally:
        db.close()


def cmd_dump(args: argparse.Namespace) -> int:
    """Save what a portal's results page actually looks like, signed in.

    Every portal here draws its results in JavaScript, so the only way to
    write a selector that matches is to look at the rendered page from the
    browser that is logged in -- which is this PC, not wherever the config was
    written. This opens the search page in the persistent profile, waits for
    it to draw, and saves the HTML and a screenshot under data/debug/. It
    applies to nothing and changes nothing on the portal.
    """
    from .browser import session
    from .config import data_dir
    from .portals import registry

    cfg = _load()
    targets = ([cfg.portals[p] for p in args.portal if p in cfg.portals]
               if args.portal else cfg.enabled_portals())
    if not targets:
        print(f"  No matching portal. Available: {', '.join(sorted(cfg.portals))}")
        return 1

    roles = cfg.search.get("roles") or [{"title": "developer"}]
    locations = (cfg.search.get("locations", {}).get("preferred") or [""])
    out_dir = data_dir() / "debug"
    out_dir.mkdir(parents=True, exist_ok=True)

    for portal in targets:
        print()
        print(f"  {portal.name}")
        try:
            with session(portal, cfg, headless=args.headless) as page:
                adapter = registry.build(portal, cfg, page)
                url = adapter.build_search_url(roles[0], locations[0])
                print(f"    opening {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                if hasattr(adapter, "note_landing"):
                    adapter.note_landing()

                # The same scrape the real run does, so the diagnostic below
                # is the one discover would have printed -- not a guess.
                found = []
                try:
                    found = list(adapter._scrape_page())
                except Exception as exc:
                    print(f"    scrape raised {type(exc).__name__}: {exc}")

                html_path = out_dir / f"{portal.id}.html"
                png_path = out_dir / f"{portal.id}.png"
                html_path.write_text(page.content(), encoding="utf-8")
                try:
                    page.screenshot(path=str(png_path), full_page=True)
                except Exception:
                    png_path = None

                landed = getattr(adapter, "landed_url", "") or page.url
                title = getattr(adapter, "landed_title", "") or ""
                print(f"    landed  {landed}")
                if title:
                    print(f"    title   {title}")
                print(f"    cards   {len(found)} matched search.result_card")
                if not found and hasattr(adapter, "why_no_results"):
                    print(f"    because {adapter.why_no_results()}")
                print(f"    saved   {html_path}")
                if png_path:
                    print(f"    saved   {png_path}")
        except Exception as exc:
            print(f"    could not open it: {type(exc).__name__}: {exc}")

    print()
    print("  Send the .html (and .png) for the portal that found nothing, and")
    print("  the selectors in config/portals/<portal>.yaml can be written")
    print("  against the real page. The HTML is your own signed-in view;")
    print("  it lives under data/, which is not committed.")
    print()
    return 0


def cmd_shortlist(args: argparse.Namespace) -> int:
    cfg = _load()
    db = Database()
    try:
        scorer = Scorer(cfg.preferences, cfg.profile)
        threshold = (args.min_score if args.min_score is not None
                     else float(cfg.thresholds.get("shortlist", 60)))
        rows = db.shortlist(min_score=threshold, limit=args.limit)
        if not rows:
            print("\n  Nothing at or above "
                  f"{threshold}. Run `discover`, or lower "
                  "thresholds.shortlist in config/preferences.yaml.\n")
            return 0

        print(f"\n  {len(rows)} job(s) at or above {threshold}\n")
        for row in rows:
            band = scorer.band(row["total"])
            mark = {"priority": "**", "tailor": " *"}.get(band, "  ")
            print(f"  {mark} {row['total']:5.1f}  {row['title'][:42]:<42} "
                  f"{row['company'][:22]:<22} {row['portal']}")
            if args.why:
                import json
                for reason in json.loads(row["reasons"] or "[]")[:4]:
                    print(f"           - {reason}")
                print(f"           {row['url']}")
        print()
        return 0
    finally:
        db.close()


def cmd_apply(args: argparse.Namespace) -> int:
    cfg = _load()
    if not args.dry_run:
        try:
            require_identity(cfg)
        except ConfigError as exc:
            print(f"\n{exc}\n", file=sys.stderr)
            return 2

    db = Database()
    pipe = Pipeline(cfg, db)
    run_id = db.start_run("apply", args.portal or
                          [p.id for p in cfg.enabled_portals()])
    try:
        results = pipe.apply(portal_ids=args.portal, limit=args.limit,
                             min_score=args.min_score, dry_run=args.dry_run,
                             headless=args.headless)
        db.finish_run(run_id, prepared=results.get("prepared", 0),
                      submitted=results.get("submitted", 0))
        if results:
            print("\n  " + "  |  ".join(f"{k} {v}" for k, v in results.items() if v))
        if results.get("prepared"):
            print("  Prepared but not sent -- finish with: python -m jobauto review")
        print()
        return 0
    finally:
        db.close()


def cmd_review(args: argparse.Namespace) -> int:
    cfg = _load()
    db = Database()
    try:
        rows = db.pending_review()
        if not rows:
            print("\n  Nothing waiting for review.\n")
            return 0
        print(f"\n  {len(rows)} application(s) prepared but not submitted:\n")
        for row in rows:
            print(f"    {row['total'] or 0:5.1f}  {row['title'][:42]:<42} "
                  f"{row['company'][:22]:<22} {row['portal']}")
            print(f"           {row['url']}")
            if row["escalated"] and row["escalated"] != "[]":
                import json
                for q in json.loads(row["escalated"]):
                    print(f"           ? {q}")
        print("\n  These were filled in the browser but never submitted.")
        print("  Open each link, check the answers, and submit by hand.\n")
        return 0
    finally:
        db.close()


def cmd_refresh(args: argparse.Namespace) -> int:
    cfg = _load()
    db = Database()
    try:
        results = Pipeline(cfg, db).refresh_profiles(headless=args.headless)
        if not results:
            print("\n  No portal has profile_refresh enabled.\n")
            return 0
        print()
        for pid, state in results.items():
            print(f"  {pid:<12} {state}")
        print()
        return 0
    finally:
        db.close()


def cmd_web(args: argparse.Namespace) -> int:
    _load()     # fail fast on a bad config rather than inside the server
    try:
        from .web import serve
    except ImportError:
        print("\n  Flask is not installed. Run: pip install -r requirements.txt\n",
              file=sys.stderr)
        return 2
    serve(host=args.host, port=args.port, debug=args.debug)
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    """Save cloud URL + agent token so `agent` knows where to connect."""
    from .agent import AgentError, CloudClient, save_agent_config

    url = args.url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        print("\n  --url must start with http:// or https://\n", file=sys.stderr)
        return 2

    print(f"\n  checking {url} ...")
    try:
        info = CloudClient(url, args.token).hello("linking")
    except AgentError as exc:
        print(f"  {exc}\n", file=sys.stderr)
        return 1

    path = save_agent_config(url, args.token)
    print(f"  linked as {info.get('user')}")
    print(f"  saved to {path}")

    # Linking and then telling someone to "start the agent and leave it
    # running" is what produced every offline dashboard: the agent lived in a
    # window, and closing the window stopped it for good. A linked PC that
    # does not stay connected is not linked in any useful sense, so set it up
    # here rather than leaving it as a step to remember.
    if not args.no_autostart:
        from .agent import autostart as auto
        try:
            print(f"\n  {auto.register()}")
            print("  you can close this window; it keeps running")
        except auto.AutostartError as exc:
            print(f"\n  could not set it to run on its own: {exc}")
            print("\n  Until that is sorted, start it by hand each time:")
            print(f"    {invocation()} agent")
            return 0
    print()
    return 0


def _print_schedule_state() -> None:
    """What the cloud says about the daily run, best effort.

    Read-only and failure-tolerant on purpose: this is a footnote to a local
    status command, and an unreachable site should not turn it into an error.
    """
    try:
        from .agent.runner import CloudClient, load_agent_config
        url, token = load_agent_config()
        prefs = CloudClient(url, token).preferences() or {}
    except Exception as exc:
        print(f"\n  daily run: could not ask the site ({type(exc).__name__})")
        return

    import yaml as _yaml
    try:
        parsed = _yaml.safe_load(prefs.get("yaml") or "") or {}
    except Exception:
        parsed = {}
    sched = parsed.get("schedule") or {}

    if not sched.get("enabled"):
        print("\n  daily run: OFF -- the agent is up but has nothing to do.")
        print("    turn it on in the dashboard: Preferences -> Run automatically")
        return
    days = ", ".join(sched.get("days") or []) or "every day"
    print(f"\n  daily run: ON at {sched.get('time', '09:00')} "
          f"{sched.get('timezone', '')} ({days})")
    print(f"    discover, then apply in batches of "
          f"{sched.get('batch_size', 5)}, up to "
          f"{sched.get('max_batches', 4)} batches")


def cmd_autostart(args: argparse.Namespace) -> int:
    """Keep the agent running, so the dashboard stops saying offline."""
    from .agent import autostart as auto

    if args.status:
        st = auto.status()
        if not st.registered:
            print(f"\n  autostart: not set up -- {st.detail}\n")
            return 1
        mark = "running" if st.running else "NOT running"
        print(f"\n  autostart: registered, {mark}")
        for label, value in (("last run", st.last_run),
                             ("next check", st.next_run)):
            if value:
                print(f"    {label:<12} {value}")
        if st.last_result:
            meaning = auto.explain_result(st.last_result)
            print(f"    {'last result':<12} {st.last_result}"
                  + (f"  -- {meaning}" if meaning else ""))
        if not st.running:
            # The heartbeat trigger will pick it up, but someone standing at
            # the machine now should not have to wait a quarter of an hour to
            # find out whether any of this works.
            print(f"\n  it restarts within {auto.HEARTBEAT_MINUTES} minutes "
                  f"on its own, or immediately with:")
            print("    schtasks /Run /TN jobauto-agent")

        # A running agent and a disabled schedule look identical from here --
        # both are "nothing happens all day" -- so answer the whole question
        # rather than the half this command is named after.
        _print_schedule_state()
        print()
        return 0 if st.healthy else 1

    try:
        if args.remove:
            message = auto.remove()
        elif args.startup_folder:
            message = auto.install_startup_shortcut()
        else:
            message = auto.register()
    except auto.AutostartError as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 2
    print(f"\n  {message}\n")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    from .agent import AgentError, CloudClient, LocalAgent, load_agent_config

    try:
        url, token = load_agent_config()
    except AgentError as exc:
        print(f"\n  {exc}\n", file=sys.stderr)
        return 2

    _load()     # fail fast on a broken local config
    agent = LocalAgent(CloudClient(url, token), interval=args.interval,
                       headless=args.headless)
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        print("\n  agent stopped.\n")
    return 0


def cmd_cloud(args: argparse.Namespace) -> int:
    """Run the cloud app on this machine -- useful for trying it before you
    deploy, and for developing against it."""
    try:
        from .cloud.app import create_app
    except ImportError as exc:
        print(f"\n  cloud dependencies missing ({exc}).\n"
              "  Run: pip install -r requirements.txt\n", file=sys.stderr)
        return 2

    import os
    os.environ.setdefault("JOBAUTO_ALLOW_SIGNUP", "1")
    os.environ.setdefault("JOBAUTO_HTTPS", "0")
    app = create_app()
    print(f"\n  jobauto cloud -> http://{args.host}:{args.port}")
    print("  first visit creates the owner account\n")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0
def cmd_stats(args: argparse.Namespace) -> int:
    db = Database()
    try:
        s = db.stats()
        print("\n  jobs seen       {jobs_seen}\n"
              "  scored          {scored}\n"
              "  filtered out    {dropped}\n"
              "  submitted       {submitted}\n"
              "  prepared        {prepared}\n"
              "  skipped         {skipped}\n"
              "  external        {external}   (handed to you)\n"
              "  failed          {failed}   (will be retried)\n"
              "  companies       {companies}\n".format(**s))
        return 0
    finally:
        db.close()


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jobauto",
        description="Preference-driven job application assistant. "
                    "Fills applications; you submit them.")
    sub = p.add_subparsers(dest="command", required=True)

    def portal_arg(sp: Any) -> None:
        sp.add_argument("--portal", action="append",
                        help="limit to this portal id (repeatable)")

    def headless_arg(sp: Any) -> None:
        sp.add_argument("--headless", action="store_true",
                        help="run without a visible window (more detectable)")

    sp = sub.add_parser("doctor", help="validate config, no browser")
    sp.add_argument("--clear-cooldown", default="", metavar="PORTAL",
                    help="try a portal again now, ignoring its bot-check "
                         "cool-off")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("login", help="one-time manual sign-in per portal")
    portal_arg(sp)
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("export-session",
                        help="copy your portal logins to another machine")
    portal_arg(sp)
    sp.add_argument("--out", help="output file (default: data/sessions.json)")
    sp.set_defaults(func=cmd_export_session)

    sp = sub.add_parser("import-session",
                        help="restore portal logins exported elsewhere")
    portal_arg(sp)
    sp.add_argument("--file", required=True, help="the exported bundle")
    sp.set_defaults(func=cmd_import_session)

    sp = sub.add_parser("discover", help="search and score, applies to nothing")
    portal_arg(sp)
    headless_arg(sp)
    sp.set_defaults(func=cmd_discover)

    sp = sub.add_parser("dump", help="save a portal's real results page "
                                     "for fixing selectors")
    portal_arg(sp)
    headless_arg(sp)
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser("shortlist", help="ranked list of scored jobs")
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--min-score", type=float, default=None)
    sp.add_argument("--why", action="store_true", help="show scoring reasons")
    sp.set_defaults(func=cmd_shortlist)

    sp = sub.add_parser("apply", help="prepare applications, review each one")
    portal_arg(sp)
    headless_arg(sp)
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--min-score", type=float, default=None)
    sp.add_argument("--dry-run", action="store_true",
                    help="show what would be applied to, touch nothing")
    sp.set_defaults(func=cmd_apply)

    sp = sub.add_parser("review", help="list anything prepared but not sent")
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("refresh", help="daily profile touch for visibility")
    headless_arg(sp)
    sp.set_defaults(func=cmd_refresh)

    sp = sub.add_parser("stats", help="application history summary")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("link", help="link this PC to your cloud deployment")
    sp.add_argument("--url", required=True, help="https://your-app.onrender.com")
    sp.add_argument("--token", required=True, help="agent token from the Devices tab")
    sp.add_argument("--no-autostart", action="store_true",
                    help="link only; do not set the agent to run on its own")
    sp.set_defaults(func=cmd_link)

    sp = sub.add_parser("autostart",
                        help="keep the agent running via Task Scheduler")
    sp.add_argument("--status", action="store_true",
                    help="is it set up, and is it actually running?")
    sp.add_argument("--remove", action="store_true", help="undo it")
    sp.add_argument("--startup-folder", action="store_true",
                    help="fallback for machines that forbid the scheduler")
    sp.set_defaults(func=cmd_autostart)

    sp = sub.add_parser("agent", help="run the sync agent (keep this running)")
    sp.add_argument("--interval", type=int, default=30, help="poll seconds")
    headless_arg(sp)
    sp.set_defaults(func=cmd_agent)

    sp = sub.add_parser("cloud", help="run the cloud app locally for testing")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=5058)
    sp.add_argument("--debug", action="store_true")
    sp.set_defaults(func=cmd_cloud)

    sp = sub.add_parser("web", help="local dashboard in your browser")
    sp.add_argument("--host", default="127.0.0.1",
                    help="bind address; set JOBAUTO_WEB_TOKEN before leaving localhost")
    # Not 5000: Windows reserves blocks of ports for Hyper-V/WinNAT and 5000 is
    # very commonly in one. Flask appears to start, logs a socket permission
    # error, and nothing listens -- which reads as the app being broken.
    sp.add_argument("--port", type=int, default=5057)
    sp.add_argument("--debug", action="store_true")
    sp.set_defaults(func=cmd_web)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n  Interrupted. Nothing further was submitted.\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
