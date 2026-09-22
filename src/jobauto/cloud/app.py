"""Cloud web app.

Two audiences:
  - the browser (session cookie auth) -- browse jobs, queue work, edit prefs
  - the agent on your PC (X-Agent-Token) -- pull work, push results

The agent always connects *outbound*, so nothing is exposed on your home
network and no port forwarding is needed.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import yaml
from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, session as flask_session, url_for)
from sqlalchemy import delete, desc, func, or_ as sa_or, select

from . import auth, schedule
from .db import (Agent, Application, CloudJob, Task, User, init_engine,
                 session, utcnow)

# Outcomes the user chose in the browser. Nothing the agent pushes may
# overwrite one, and nothing in these states counts as pending.
TERMINAL_STATUSES = ("submitted", "skipped")

# CloudJob.state values that came from the user rather than from a search. A
# rediscovery may refresh a job's details but must never move it out of one of
# these: the portal goes on listing a job long after you are done with it, so
# letting a search reset the state puts it back every single run.
_USER_CHOSEN_STATES = ("queued", "applied", "skipped", "external")

# Application statuses that mean the jobs list is finished with this job.
# Wider than TERMINAL_STATUSES, and deliberately so: `external` is not a
# decision you made, but there is still nothing left to do about it *there* --
# the application has been handed to you and lives in Applications now.
# Leaving it in Jobs shows the same job in two places, one of which cannot
# act on it.
_RETIRES_JOB_STATUSES = ("submitted", "skipped", "external")

# Retention runs off the agent's poll rather than a scheduler, because free
# tiers have no cron and a sleeping instance runs no background threads. The
# throttle is in memory: a redeploy may cause one extra run, which is harmless
# because purging is idempotent, and it needs no schema change on an existing
# deployment.
_PURGE_INTERVAL_HOURS = 12
_last_purge: dict[int, datetime] = {}


def maybe_purge(user_id: int) -> None:
    from . import retention

    now = datetime.now(timezone.utc)
    previous = _last_purge.get(user_id)
    if previous and (now - previous) < timedelta(hours=_PURGE_INTERVAL_HOURS):
        return
    _last_purge[user_id] = now
    try:
        retention.purge_user(user_id)
    except Exception:
        # Housekeeping must never take the request down with it.
        import logging
        logging.getLogger("jobauto.retention").exception("purge failed")


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Render/Fly terminate TLS upstream; cookies must still be HTTPS-only.
        SESSION_COOKIE_SECURE=bool(os.environ.get("JOBAUTO_HTTPS", "1") == "1"
                                   and not app.debug),
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,
    )
    init_engine()

    @app.after_request
    def no_store(response):
        """Keep signed-in pages out of the browser cache.

        Without this the dashboard is cached and the back button -- or simply
        revisiting the site -- redisplays it after signing out, which looks
        exactly like the sign-out having failed. Static assets are excluded so
        they still cache normally.
        """
        if request.endpoint != "static":
            response.headers["Cache-Control"] = (
                "no-store, no-cache, must-revalidate, max-age=0")
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    # ------------------------------------------------------------ auth
    @app.get("/login")
    def login():
        if flask_session.get("user_id"):
            return redirect(url_for("dashboard"))
        return render_template("login.html", mode="login",
                               signed_out=request.args.get("signed_out") == "1",
                               signup_open=auth.signup_open() or _no_users(),
                               needs_code=bool(auth.signup_code()))

    @app.post("/login")
    def do_login():
        try:
            user = auth.authenticate(request.form.get("email", ""),
                                     request.form.get("password", ""))
        except auth.AuthError as exc:
            return render_template("login.html", mode="login", error=str(exc),
                                   signup_open=auth.signup_open() or _no_users(),
                                   needs_code=bool(auth.signup_code())), 400
        flask_session.clear()
        flask_session["user_id"] = user.id
        flask_session.permanent = True
        nxt = request.args.get("next", "")
        return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))

    @app.get("/signup")
    def signup():
        if not (auth.signup_open() or _no_users()):
            return redirect(url_for("login"))
        return render_template("login.html", mode="signup",
                               first=_no_users(),
                               needs_code=bool(auth.signup_code()))

    @app.post("/signup")
    def do_signup():
        try:
            user = auth.create_user(request.form.get("email", ""),
                                    request.form.get("password", ""),
                                    request.form.get("code", ""))
        except auth.AuthError as exc:
            return render_template("login.html", mode="signup", error=str(exc),
                                   first=_no_users(),
                                   needs_code=bool(auth.signup_code())), 400
        flask_session.clear()
        flask_session["user_id"] = user.id
        flask_session.permanent = True
        return redirect(url_for("dashboard", welcome=1))

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        flask_session.clear()
        return redirect(url_for("login", signed_out=1))

    # ------------------------------------------------- password reset
    @app.get("/forgot")
    def forgot():
        return render_template("login.html", mode="forgot")

    @app.post("/forgot")
    def do_forgot():
        from . import mail

        auth.purge_expired_resets()
        email = request.form.get("email", "")
        issued = auth.begin_password_reset(email)

        delivered_by_email = False
        if issued:
            token, address = issued
            link = url_for("reset", token=token, _external=True)
            delivered_by_email = mail.send_password_reset(
                address, link, auth.RESET_TTL_MINUTES)

        # Always the same response, whether or not the account exists --
        # otherwise this page becomes a way to discover who has an account.
        return render_template(
            "login.html", mode="forgot_sent",
            smtp_on=mail.smtp_configured() and delivered_by_email)

    @app.get("/reset/<token>")
    def reset(token: str):
        if auth.check_reset_token(token) is None:
            return render_template(
                "login.html", mode="forgot",
                error="That reset link has expired or was already used. "
                      "Request a new one."), 400
        return render_template("login.html", mode="reset", token=token)

    @app.post("/reset/<token>")
    def do_reset(token: str):
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        if password != confirm:
            return render_template("login.html", mode="reset", token=token,
                                   error="The two passwords do not match."), 400
        try:
            user = auth.complete_password_reset(token, password)
        except auth.AuthError as exc:
            return render_template("login.html", mode="reset", token=token,
                                   error=str(exc)), 400

        # Sign them straight in; making someone re-type a password they set
        # ten seconds ago achieves nothing.
        flask_session.clear()
        flask_session["user_id"] = user.id
        flask_session.permanent = True
        return redirect(url_for("dashboard"))

    @app.post("/api/change-password")
    @auth.login_required
    def api_change_password():
        body = request.get_json(silent=True) or {}
        try:
            auth.change_password(g.user.id, body.get("current", ""),
                                 body.get("new", ""))
        except auth.AuthError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"ok": True})

    # ------------------------------------------------------------ pages
    @app.get("/")
    @auth.login_required
    def dashboard():
        return render_template("app.html", email=g.user.email,
                               welcome=request.args.get("welcome") == "1")

    # ----------------------------------------------------- installer
    # Public on purpose: the installer runs before anyone has signed in, and
    # neither endpoint carries a secret. The agent token is typed in by the
    # person running it.
    @app.get("/install.ps1")
    def install_script():
        from . import installer
        from flask import Response

        base = request.url_root.rstrip("/")
        return Response(installer.installer_script(base),
                        mimetype="text/plain; charset=utf-8")

    @app.get("/agent.zip")
    def agent_zip():
        from . import installer
        from flask import Response

        return Response(
            installer.build_agent_zip(),
            mimetype="application/zip",
            headers={"Content-Disposition": "attachment; filename=jobauto-agent.zip"})

    @app.get("/healthz")
    def healthz():
        """Free hosts ping this to decide whether the instance is alive."""
        return jsonify({"ok": True, "time": utcnow().isoformat()})

    # -------------------------------------------------- browser API
    @app.get("/api/summary")
    @auth.login_required
    def api_summary():
        with session() as s:
            uid = g.user.id
            _reap_stale_tasks(s, uid)
            _expire_queued_tasks(s, uid)
            counts = {
                "jobs": s.scalar(select(func.count(CloudJob.id))
                                 .where(CloudJob.user_id == uid,
                                        CloudJob.dropped == False)) or 0,   # noqa: E712
                "dropped": s.scalar(select(func.count(CloudJob.id))
                                    .where(CloudJob.user_id == uid,
                                           CloudJob.dropped == True)) or 0,  # noqa: E712
                "queued": s.scalar(select(func.count(CloudJob.id))
                                   .where(CloudJob.user_id == uid,
                                          CloudJob.state == "queued")) or 0,
                "submitted": s.scalar(select(func.count(Application.id))
                                      .where(Application.user_id == uid,
                                             Application.status == "submitted")) or 0,
                "pending": s.scalar(select(func.count(Application.id))
                                    .where(Application.user_id == uid,
                                           Application.status == "prepared")) or 0,
            }
            agents = s.scalars(select(Agent).where(Agent.user_id == uid)).all()
            tasks = s.scalars(
                select(Task).where(Task.user_id == uid)
                .order_by(desc(Task.created_at)).limit(5)).all()

            return jsonify({
                "email": g.user.email,
                "counts": counts,
                "agents": [{
                    "id": a.id, "name": a.name, "online": a.online,
                    "last_seen": _iso(a.last_seen),
                    "last_status": a.last_status,
                } for a in agents],
                "any_agent_online": any(a.online for a in agents),
                "tasks": [{
                    "id": t.id, "kind": t.kind, "status": t.status,
                    "created_at": _iso(t.created_at),
                    "result": _json(t.result_json),
                } for t in tasks],
            })

    @app.get("/api/jobs")
    @auth.login_required
    def api_jobs():
        min_score = request.args.get("min_score", type=float)
        portal = request.args.get("portal", "")
        max_age = request.args.get("max_age_days", type=int)
        state = request.args.get("state", "")
        limit = min(request.args.get("limit", default=100, type=int), 300)

        with session() as s:
            stmt = (select(CloudJob)
                    .where(CloudJob.user_id == g.user.id,
                           CloudJob.dropped == False)     # noqa: E712
                    .order_by(desc(CloudJob.score)).limit(limit))
            if min_score is not None:
                stmt = stmt.where(CloudJob.score >= min_score)
            if portal:
                stmt = stmt.where(CloudJob.portal == portal)
            if max_age and max_age > 0:
                # Undated listings are kept rather than hidden: most Indian
                # portals omit a date, and dropping them would silently remove
                # most of the results.
                edge = date.today() - timedelta(days=max_age)
                stmt = stmt.where(sa_or(CloudJob.posted_date.is_(None),
                                        CloudJob.posted_date >= edge))
            if state:
                stmt = stmt.where(CloudJob.state == state)

            applied = {a.fingerprint for a in s.scalars(
                select(Application).where(
                    Application.user_id == g.user.id,
                    Application.status.in_(_RETIRES_JOB_STATUSES))).all()}

            # A job you have applied to is finished with. Tagging it "applied"
            # and leaving it in the list means the list never shrinks -- every
            # run adds to it and nothing ever leaves, so the jobs actually
            # worth looking at are buried under ones already dealt with.
            if applied and not request.args.get("include_applied"):
                stmt = stmt.where(CloudJob.fingerprint.notin_(applied))

            jobs = s.scalars(stmt).all()

            return jsonify({"jobs": [{
                "id": j.id,
                "fingerprint": j.fingerprint,
                "title": j.title,
                "company": j.company,
                "location": j.location,
                "salary": j.salary_text,
                "url": j.url,
                "portal": j.portal,
                "score": j.score,
                "band": j.band,
                "posted_date": j.posted_date.isoformat() if j.posted_date else None,
                "age_days": _age_days(j.posted_date),
                "reasons": _json(j.reasons_json, list),
                "state": j.state,
                "applied": j.fingerprint in applied,
            } for j in jobs]})

    @app.post("/api/jobs/<int:job_id>/queue")
    @auth.login_required
    def api_queue_job(job_id: int):
        """Mark a job for the agent to apply to on its next run. This is the
        core PC-off action."""
        with session() as s:
            job = s.get(CloudJob, job_id)
            if job is None or job.user_id != g.user.id:
                return jsonify({"error": "no such job"}), 404
            job.state = "queued" if job.state != "queued" else "new"
            s.commit()
            return jsonify({"ok": True, "state": job.state})

    @app.get("/api/applications")
    @auth.login_required
    def api_applications():
        with session() as s:
            apps = s.scalars(
                select(Application)
                .where(Application.user_id == g.user.id)
                .order_by(desc(Application.updated_at)).limit(200)).all()
            return jsonify({"applications": [{
                "id": a.id, "title": a.title, "company": a.company,
                "url": a.url, "portal": a.portal, "status": a.status,
                "answered": _json(a.answered_json, dict),
                "escalated": _json(a.escalated_json, list),
                "note": a.note,
                "updated_at": _iso(a.updated_at),
            } for a in apps]})

    @app.post("/api/applications/<int:app_id>/<action>")
    @auth.login_required
    def api_application_action(app_id: int, action: str):
        """You confirming you sent it. The cloud never submits anything."""
        statuses = {"submitted": "submitted", "skip": "skipped",
                    "reopen": "prepared"}
        if action not in statuses:
            return jsonify({"error": "unknown action"}), 400
        with session() as s:
            item = s.get(Application, app_id)
            if item is None or item.user_id != g.user.id:
                return jsonify({"error": "no such application"}), 404
            item.status = statuses[action]
            item.updated_at = utcnow()

            # Move the job too, not just the application. They are separate
            # rows joined by fingerprint, and updating only one is what left
            # a job you had finished with sitting in the jobs list, coming
            # back after every search.
            job = s.scalar(select(CloudJob).where(
                CloudJob.user_id == g.user.id,
                CloudJob.fingerprint == item.fingerprint))
            if job is not None:
                job.state = {"submitted": "applied", "skip": "skipped",
                             "reopen": "new"}[action]

            s.commit()
            return jsonify({"ok": True, "status": item.status})

    # ------------------------------------------------------ retention
    @app.get("/api/retention")
    @auth.login_required
    def api_retention():
        """What the next cleanup would remove, without removing it."""
        from . import retention

        with session() as s:
            user = s.get(User, g.user.id)
            settings = retention.settings_for(user)

        preview = retention.purge_user(g.user.id, dry_run=True)
        last = _last_purge.get(g.user.id)
        return jsonify({
            "settings": settings,
            "defaults": retention.DEFAULTS,
            "would_delete": {k: v for k, v in preview.deleted.items() if v},
            "protected": preview.kept,
            "total": preview.total,
            "last_run": _iso(last) if last else None,
            "every_hours": _PURGE_INTERVAL_HOURS,
        })

    @app.post("/api/retention/purge")
    @auth.login_required
    def api_retention_purge():
        from . import retention

        report = retention.purge_user(g.user.id)
        _last_purge[g.user.id] = datetime.now(timezone.utc)
        return jsonify({"ok": True,
                        "deleted": {k: v for k, v in report.deleted.items() if v},
                        "total": report.total,
                        "summary": report.summary()})

    @app.get("/api/schedule")
    @auth.login_required
    def api_schedule():
        with session() as s:
            user = s.get(User, g.user.id)
            settings = schedule.settings_for(user)
            upcoming = schedule.next_run(settings)
            return jsonify({
                "settings": settings,
                "defaults": schedule.DEFAULTS,
                "next_run": _iso(upcoming),
                "last_run": _iso(user.schedule_last_run),
                # Surfaced rather than only logged: a server without the
                # timezone database shifts every run by the UTC offset, and
                # the only symptom the user sees is a time they did not pick.
                "timezone_ok": schedule.zone_available(
                    settings.get("timezone", "UTC")),
                "per_day": (int(settings.get("batch_size", 5))
                            * int(settings.get("max_batches", 4))),
            })

    @app.get("/api/preferences")
    @auth.login_required
    def api_get_prefs():
        with session() as s:
            user = s.get(User, g.user.id)
            raw = (user.preferences_yaml or "").strip()
            if not raw:
                user.preferences_yaml = _default_preferences_yaml()
                user.preferences_updated = utcnow()
                s.commit()
            else:
                safe_yaml = _safe_preferences_yaml(raw)
                if safe_yaml != raw:
                    user.preferences_yaml = safe_yaml
                    user.preferences_updated = utcnow()
                    s.commit()
            return jsonify({"yaml": user.preferences_yaml,
                            "updated": _iso(user.preferences_updated)})

    @app.post("/api/preferences")
    @auth.login_required
    def api_save_prefs():
        payload = request.get_json(silent=True) or {}
        # Store exactly what was sent. Stripping here silently rewrote the
        # user's file on every save, so what came back never quite matched
        # what they typed.
        text = payload.get("yaml") or ""
        if not text.strip():
            return jsonify({"error": "preferences yaml cannot be empty"}), 400
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            return jsonify({"error": f"invalid YAML: {exc}"}), 400
        if not isinstance(parsed, dict):
            return jsonify({"error": "preferences must be a YAML mapping"}), 400

        problem = _validate_preferences(parsed)
        if problem:
            return jsonify({"error": problem}), 400

        with session() as s:
            user = s.get(User, g.user.id)
            user.preferences_yaml = text
            user.preferences_updated = utcnow()
            s.commit()
            return jsonify({"ok": True,
                            "yaml": user.preferences_yaml,
                            "updated": _iso(user.preferences_updated)})

    @app.post("/api/tasks")
    @auth.login_required
    def api_create_task():
        body = request.get_json(silent=True) or {}
        kind = body.get("kind", "")
        if kind not in ("discover", "apply", "refresh"):
            return jsonify({"error": "unknown task kind"}), 400
        with session() as s:
            payload = body.get("payload") or {}
            norm_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            existing = s.scalar(
                select(Task).where(
                    Task.user_id == g.user.id,
                    Task.kind == kind,
                    Task.status.in_(("queued", "running")),
                    Task.payload_json == norm_payload,
                )
                .order_by(Task.created_at.desc())
                .limit(1)
            )
            if existing is not None:
                return jsonify({"ok": True, "task_id": existing.id,
                                "already_pending": True})

            pending = s.scalar(
                select(func.count(Task.id)).where(
                    Task.user_id == g.user.id,
                    Task.status.in_(("queued", "running"))))
            if pending and pending >= 5:
                return jsonify({"error": "too many tasks already queued"}), 409

            task = Task(user_id=g.user.id, kind=kind,
                        payload_json=norm_payload)
            s.add(task)
            s.commit()
            return jsonify({"ok": True, "task_id": task.id})

    @app.get("/api/tasks")
    @auth.login_required
    def api_list_tasks():
        with session() as s:
            # Also reap here. The reaper used to run only on the agent's poll,
            # which meant a task stranded by a stopped agent could only be
            # cleared by the very thing that had stopped -- so it sat "running"
            # indefinitely and the dashboard looked wedged.
            _reap_stale_tasks(s, g.user.id)
            _expire_queued_tasks(s, g.user.id)
            tasks = s.scalars(
                select(Task).where(Task.user_id == g.user.id)
                .order_by(desc(Task.created_at)).limit(25)).all()
            return jsonify({"tasks": [{
                "id": t.id, "kind": t.kind, "status": t.status,
                "created_at": _iso(t.created_at),
                "claimed_at": _iso(t.claimed_at),
                "finished_at": _iso(t.finished_at),
                "result": _json(t.result_json),
                "log": t.log or "",
            } for t in tasks]})

    @app.post("/api/tasks/<int:task_id>/cancel")
    @auth.login_required
    def api_cancel_task(task_id: int):
        """Give up on a task without waiting for it to age out."""
        with session() as s:
            task = s.get(Task, task_id)
            if task is None or task.user_id != g.user.id:
                return jsonify({"error": "no such task"}), 404
            if task.status in ("done", "failed", "cancelled"):
                return jsonify({"ok": True, "status": task.status})
            task.status = "cancelled"
            task.finished_at = utcnow()
            task.log = ((task.log or "") + "\ncancelled from the dashboard").strip()
            s.commit()
            return jsonify({"ok": True, "status": task.status})

    @app.get("/api/agents")
    @auth.login_required
    def api_agents():
        with session() as s:
            agents = s.scalars(select(Agent).where(
                Agent.user_id == g.user.id)).all()
            return jsonify({"agents": [{
                "id": a.id, "name": a.name, "online": a.online,
                "token": a.token, "revoked": a.revoked,
                "last_seen": _iso(a.last_seen), "last_status": a.last_status,
            } for a in agents]})

    @app.post("/api/agents/<int:agent_id>/rotate")
    @auth.login_required
    def api_rotate_agent(agent_id: int):
        try:
            token = auth.rotate_agent_token(g.user.id, agent_id)
        except auth.AuthError as exc:
            return jsonify({"error": str(exc)}), 404
        return jsonify({"ok": True, "token": token})

    # ---------------------------------------------------- agent API
    @app.post("/api/agent/hello")
    @auth.agent_required
    def agent_hello():
        body = request.get_json(silent=True) or {}
        auth.touch_agent(g.agent.id, body.get("status", "idle"))
        with session() as s:
            user = s.get(User, g.agent.user_id)
            return jsonify({
                "ok": True,
                "user": user.email,
                "preferences_updated": _iso(user.preferences_updated),
            })

    @app.get("/api/agent/preferences")
    @auth.agent_required
    def agent_preferences():
        """The cloud is the source of truth for preferences -- you edit them in
        the browser with the PC off, and the agent picks them up next run."""
        auth.touch_agent(g.agent.id)
        with session() as s:
            user = s.get(User, g.agent.user_id)
            raw = (user.preferences_yaml or "").strip()
            if not raw:
                user.preferences_yaml = _default_preferences_yaml()
                user.preferences_updated = utcnow()
                s.commit()
            else:
                safe_yaml = _safe_preferences_yaml(raw)
                if safe_yaml != raw:
                    user.preferences_yaml = safe_yaml
                    user.preferences_updated = utcnow()
                    s.commit()
            return jsonify({"yaml": user.preferences_yaml,
                            "updated": _iso(user.preferences_updated)})

    @app.get("/api/agent/work")
    @auth.agent_required
    def agent_work():
        """One poll: claims the oldest queued task and reports queued jobs."""
        auth.touch_agent(g.agent.id, "polling")
        maybe_purge(g.agent.user_id)
        with session() as s:
            uid = g.agent.user_id
            # An agent asking for work has finished with anything it was
            # running, whatever the status says.
            _reap_orphaned_tasks(s, uid)
            _reap_stale_tasks(s, uid)
            _expire_queued_tasks(s, uid)
            # Free tiers have no cron, so the agent's poll is the clock.
            schedule.maybe_start(s, uid)
            task = s.scalar(select(Task)
                            .where(Task.user_id == uid,
                                   Task.status == "queued",
                                   Task.created_at <= utcnow())
                            .order_by(Task.created_at).limit(1))
            payload: dict[str, Any] = {"task": None, "queued_jobs": []}

            if task is not None:
                task.status = "running"
                task.claimed_at = utcnow()
                s.commit()
                payload["task"] = {
                    "id": task.id, "kind": task.kind,
                    "payload": _json(task.payload_json),
                }

            queued = s.scalars(select(CloudJob).where(
                CloudJob.user_id == uid, CloudJob.state == "queued")).all()
            payload["queued_jobs"] = [{
                "fingerprint": j.fingerprint, "url": j.url,
                "portal": j.portal, "title": j.title, "company": j.company,
            } for j in queued]

            # Decisions made in the browser, so the agent can settle them in
            # its own database. Without this the agent keeps re-pushing them
            # as 'prepared' forever, and `jobauto review` on the PC still lists
            # applications the user already dealt with in the dashboard.
            decided = s.scalars(select(Application).where(
                Application.user_id == uid,
                Application.status.in_(TERMINAL_STATUSES))).all()
            payload["decided"] = [{
                "fingerprint": a.fingerprint,
                "portal": a.portal,
                "status": a.status,
            } for a in decided]
            return jsonify(payload)

    @app.post("/api/agent/jobs/clear")
    @auth.agent_required
    def agent_clear_queued_jobs():
        """Consume the queue once the agent has attempted the queued apply."""
        body = request.get_json(silent=True) or {}
        fps = [str(p) for p in (body.get("fingerprints") or []) if str(p).strip()]
        if not fps:
            return jsonify({"ok": True, "cleared": 0})

        with session() as s:
            cleared = 0
            for job in s.scalars(
                select(CloudJob).where(
                    CloudJob.user_id == g.agent.user_id,
                    CloudJob.fingerprint.in_(fps),
                    CloudJob.state == "queued",
                )
            ).all():
                job.state = "done"
                cleared += 1
            s.commit()
            return jsonify({"ok": True, "cleared": cleared})

    @app.post("/api/agent/jobs")
    @auth.agent_required
    def agent_push_jobs():
        """Bulk upsert of everything the agent discovered and scored."""
        body = request.get_json(silent=True) or {}
        rows = body.get("jobs") or []
        if len(rows) > 500:
            return jsonify({"error": "too many jobs in one push"}), 413

        uid = g.agent.user_id
        added = updated = 0
        with session() as s:
            existing = {j.fingerprint: j for j in s.scalars(
                select(CloudJob).where(CloudJob.user_id == uid)).all()}
            for row in rows:
                fp = (row.get("fingerprint") or "")[:32]
                if not fp:
                    continue
                job = existing.get(fp)
                if job is None:
                    job = CloudJob(user_id=uid, fingerprint=fp)
                    s.add(job)
                    added += 1
                else:
                    updated += 1
                    # Never clobber a user action with a rediscovery. Applied
                    # and skipped matter as much as queued here: without them
                    # a job you had dealt with came back on the next search,
                    # every search, because the portal still lists it.
                    if job.state in _USER_CHOSEN_STATES:
                        row.pop("state", None)

                job.portal = row.get("portal", "")[:40]
                job.title = row.get("title", "")[:300]
                job.company = row.get("company", "")[:200]
                job.url = row.get("url", "")
                job.location = (row.get("location") or "")[:200]
                job.salary_text = (row.get("salary") or "")[:80]
                job.summary = (row.get("summary") or "")[:2000]
                job.posted_date = _parse_date(row.get("posted_date"))
                job.score = float(row.get("score") or 0)
                job.band = (row.get("band") or "")[:20]
                job.reasons_json = json.dumps(row.get("reasons") or [])
                job.dropped = bool(row.get("dropped"))
            s.commit()
        auth.touch_agent(g.agent.id, f"pushed {len(rows)} jobs")
        return jsonify({"ok": True, "added": added, "updated": updated})

    @app.post("/api/agent/applications")
    @auth.agent_required
    def agent_push_applications():
        body = request.get_json(silent=True) or {}
        rows = body.get("applications") or []
        uid = g.agent.user_id
        with session() as s:
            for row in rows:
                fp = (row.get("fingerprint") or "")[:32]
                item = s.scalar(select(Application).where(
                    Application.user_id == uid,
                    Application.fingerprint == fp,
                    Application.portal == row.get("portal", "")))
                if item is None:
                    item = Application(user_id=uid, fingerprint=fp)
                    s.add(item)

                # A decision you made in the browser is final. The agent
                # re-pushes everything still 'prepared' in its local database
                # on every poll, and it has no way to know you already marked
                # this submitted or skipped -- so without this guard your
                # choice got overwritten within about thirty seconds and the
                # application reappeared as pending.
                decided = item.status in TERMINAL_STATUSES
                if not decided:
                    item.status = row.get("status", "prepared")[:20]
                    item.answered_json = json.dumps(row.get("answered") or {})
                    item.escalated_json = json.dumps(row.get("escalated") or [])
                    item.note = row.get("note", "")
                    item.updated_at = utcnow()

                # Descriptive fields are safe to refresh either way.
                item.portal = row.get("portal", "")[:40]
                item.title = row.get("title", "")[:300]
                item.company = row.get("company", "")[:200]
                item.url = row.get("url", "")
                item.resume_path = (row.get("resume_path") or "")[:300]

                # A prepared application is no longer waiting in the queue,
                # and one handed off to an employer site is finished with in
                # the jobs list entirely -- it lives in Applications now,
                # where the "apply by hand" link is.
                job = s.scalar(select(CloudJob).where(
                    CloudJob.user_id == uid, CloudJob.fingerprint == fp))
                if job is not None:
                    if item.status == "external":
                        job.state = "external"
                    elif job.state == "queued":
                        job.state = "done"
            s.commit()
        return jsonify({"ok": True, "count": len(rows)})

    @app.post("/api/agent/tasks/<int:task_id>/progress")
    @auth.agent_required
    def agent_task_progress(task_id: int):
        """Live output while a task runs.

        The log used to arrive only with the final result, so a run that takes
        ten minutes showed nothing at all until it was over -- there was no way
        to tell working from wedged.
        """
        body = request.get_json(silent=True) or {}
        with session() as s:
            task = s.get(Task, task_id)
            if task is None or task.user_id != g.agent.user_id:
                return jsonify({"error": "no such task"}), 404
            # Keep the tail: a long discover produces more than anyone reads,
            # and the recent lines are the interesting ones.
            task.log = (body.get("log") or "")[-20000:]
            if task.status == "queued":
                task.status = "running"
            s.commit()
        auth.touch_agent(g.agent.id, body.get("status", "working")[:255])
        return jsonify({"ok": True})

    @app.post("/api/agent/tasks/<int:task_id>/result")
    @auth.agent_required
    def agent_task_result(task_id: int):
        body = request.get_json(silent=True) or {}
        with session() as s:
            task = s.get(Task, task_id)
            if task is None or task.user_id != g.agent.user_id:
                return jsonify({"error": "no such task"}), 404
            task.status = body.get("status", "done")[:20]
            task.result_json = json.dumps(body.get("result") or {})
            task.log = (body.get("log") or "")[-20000:]
            task.finished_at = utcnow()
            s.commit()

            # A scheduled run is a chain: discover, then apply in batches.
            # Queued only now, so a stalled agent cannot pile them up.
            user = s.get(User, g.agent.user_id)
            if user is not None:
                try:
                    schedule.next_step(s, user, task)
                except Exception:
                    import logging
                    logging.getLogger("jobauto.schedule").exception(
                        "could not queue the next scheduled step")
        auth.touch_agent(g.agent.id, f"finished task {task_id}")
        return jsonify({"ok": True})

    return app


# ------------------------------------------------------------- helpers
# An agent that dies mid-task -- crash, reboot, closed laptop -- leaves the task
# it claimed stuck in `running`. Nothing ever clears it, and because a queued
# task of the same shape is deduped against it, every later Discover silently
# returns the dead task instead of starting a new one. The button stops working
# with no error anywhere.
# The agent heartbeats every 20s while working, so this much silence means it
# is gone rather than busy.
AGENT_SILENT_MINUTES = 5
# A task claimed this recently may belong to a poll still in flight.
CLAIM_GRACE_SECONDS = 60
# A queued task waits for a PC that may be off, which is the whole point of
# queueing -- but not forever. Long enough to queue from a phone in the morning
# and have it run that evening.
QUEUE_MAX_HOURS = 24
# Backstop for an agent that is alive but wedged on a task it will never finish.
TASK_MAX_HOURS = 6


def _reap_orphaned_tasks(s, user_id: int) -> int:
    """Called when an agent asks for work.

    The agent runs a task synchronously and only polls again once it is idle,
    so an agent asking for work while one of its tasks is still 'running' has
    demonstrably abandoned it -- it crashed, was restarted, or the machine
    rebooted. That is a far faster and more certain signal than any timeout.

    The grace period covers the task claimed moments ago by a concurrent poll.
    """
    cutoff = utcnow() - timedelta(seconds=CLAIM_GRACE_SECONDS)
    orphaned = s.scalars(select(Task).where(
        Task.user_id == user_id,
        Task.status == "running",
        Task.claimed_at.is_not(None),
        Task.claimed_at < cutoff)).all()

    for task in orphaned:
        task.status = "failed"
        task.finished_at = utcnow()
        task.log = ((task.log or "") +
                    "\nthe agent asked for new work without finishing this "
                    "-- it restarted or stopped mid-task").strip()
    if orphaned:
        s.commit()
    return len(orphaned)


def _expire_queued_tasks(s, user_id: int) -> int:
    """Give up on work nothing ever collected.

    Queued tasks deliberately survive an offline PC -- queueing from a phone
    and having it run later is the point. But a request nobody answers should
    not sit in the list indefinitely pretending it is still going to happen.
    """
    cutoff = utcnow() - timedelta(hours=QUEUE_MAX_HOURS)
    stale = s.scalars(select(Task).where(
        Task.user_id == user_id,
        Task.status == "queued",
        Task.created_at < cutoff)).all()

    for task in stale:
        task.status = "expired"
        task.finished_at = utcnow()
        task.log = ((task.log or "") +
                    f"\nno agent collected this within {QUEUE_MAX_HOURS} hours"
                    " -- your PC was never online to run it").strip()
    if stale:
        s.commit()
    return len(stale)


def _reap_stale_tasks(s, user_id: int) -> int:
    """Fail tasks whose agent is not coming back.

    Liveness, not a stopwatch: the agent heartbeats every 20 seconds while a
    task runs, so silence means it is gone. A blind timer would instead kill a
    slow-but-healthy run -- a discover across five portals can legitimately
    outlast any threshold worth setting.

    TASK_MAX_HOURS is the backstop for the other shape of stuck: an agent that
    is alive and heartbeating but wedged on a task it will never finish.
    """
    running = s.scalars(select(Task).where(
        Task.user_id == user_id,
        Task.status == "running",
        Task.claimed_at.is_not(None))).all()
    if not running:
        return 0

    last_seen = s.scalar(select(func.max(Agent.last_seen)).where(
        Agent.user_id == user_id))
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)

    now = utcnow()
    silence_cutoff = now - timedelta(minutes=AGENT_SILENT_MINUTES)
    agent_alive = last_seen is not None and last_seen > silence_cutoff
    hard_cutoff = now - timedelta(hours=TASK_MAX_HOURS)

    reaped = 0
    for task in running:
        claimed = task.claimed_at
        if claimed.tzinfo is None:
            claimed = claimed.replace(tzinfo=timezone.utc)

        # Give every task a grace period, so one claimed moments before the
        # agent's next heartbeat is never mistaken for an abandoned one.
        too_young = claimed > now - timedelta(minutes=AGENT_SILENT_MINUTES)
        if too_young:
            continue

        if agent_alive and claimed > hard_cutoff:
            continue

        task.status = "failed"
        task.finished_at = now
        note = (f"gave up after {TASK_MAX_HOURS}h -- the agent kept reporting in "
                "but never finished this task"
                if agent_alive else
                f"no word from the agent for over {AGENT_SILENT_MINUTES} "
                "minutes -- it stopped mid-task")
        task.log = ((task.log or "") + "\n" + note).strip()
        reaped += 1

    if reaped:
        s.commit()
    return reaped


def _no_users() -> bool:
    with session() as s:
        return s.scalar(select(User).limit(1)) is None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse_date(value: Any):
    """Accept an ISO date string, a date, or nothing."""
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _age_days(posted: Any) -> int | None:
    if not posted:
        return None
    try:
        return (date.today() - posted).days
    except Exception:
        return None


def _json(text: Any, kind: type = dict):
    try:
        value = json.loads(text or ("[]" if kind is list else "{}"))
        return value if isinstance(value, kind) else kind()
    except Exception:
        return kind()


def _validate_preferences(parsed: dict) -> str:
    """Same rules as the local config validator, minus the portal checks the
    cloud has no visibility into.

    Reports every problem rather than only the first: bailing out early meant a
    config with no roles AND no weights only ever complained about weights, so
    you fixed one thing, saved, and got a fresh error for the next.
    """
    problems: list[str] = []

    roles = (parsed.get("search") or {}).get("roles") or []
    if not roles:
        problems.append("search.roles is empty -- nothing to search for")
    else:
        for i, role in enumerate(roles):
            if not isinstance(role, dict) or not role.get("title"):
                problems.append(f"search.roles[{i}] needs a title")

    weights = (parsed.get("scoring") or {}).get("weights") or {}
    if not weights:
        problems.append("preferences.scoring.weights is empty")
    else:
        try:
            total = sum(float(v) for v in weights.values())
        except (TypeError, ValueError):
            problems.append("scoring weights must all be numbers")
        else:
            if abs(total - 1.0) > 0.001:
                problems.append(
                    f"scoring weights must sum to 1.0, got {total:.3f}")

    th = parsed.get("thresholds") or {}
    if th:
        try:
            if float(th.get("shortlist", 0)) > float(th.get("priority", 100)):
                problems.append("thresholds.shortlist is above thresholds.priority")
        except (TypeError, ValueError):
            problems.append("thresholds must be numbers")

    return "; ".join(problems)


def _default_preferences_yaml() -> str:
    template = Path(__file__).resolve().parents[3] / "config" / "preferences.yaml"
    try:
        return template.read_text(encoding="utf-8")
    except Exception:
        return "search:\n  roles:\n    - title: \"Software Developer\"\n"


def _safe_preferences_yaml(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return _default_preferences_yaml()
    try:
        parsed = yaml.safe_load(text)
        if not isinstance(parsed, dict):
            raise ValueError("preferences must be a YAML mapping")
        if _validate_preferences(parsed):
            raise ValueError(_validate_preferences(parsed))
        return text
    except Exception:
        return _default_preferences_yaml()


app = None


def get_app() -> Flask:
    """WSGI entry point for gunicorn: `gunicorn 'jobauto.cloud.app:get_app()'`"""
    global app
    if app is None:
        app = create_app()
    return app
