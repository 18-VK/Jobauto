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
import sys
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
        print("  No matching portal. Try: python -m jobauto doctor")
        return 1
    for portal in targets:
        interactive_login(portal, cfg)
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
    print("\n  Now start the agent and leave it running:")
    print("    python -m jobauto agent\n")
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
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("login", help="one-time manual sign-in per portal")
    portal_arg(sp)
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("discover", help="search and score, applies to nothing")
    portal_arg(sp)
    headless_arg(sp)
    sp.set_defaults(func=cmd_discover)

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
    sp.set_defaults(func=cmd_link)

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
    sp.add_argument("--port", type=int, default=5000)
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
