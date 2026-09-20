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
from datetime import datetime, timezone
from typing import Any

import yaml
from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, session as flask_session, url_for)
from sqlalchemy import delete, desc, func, select

from . import auth
from .db import (Agent, Application, CloudJob, Task, User, init_engine,
                 session, utcnow)


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

    # ------------------------------------------------------------ auth
    @app.get("/login")
    def login():
        if flask_session.get("user_id"):
            return redirect(url_for("dashboard"))
        return render_template("login.html", mode="login",
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

    @app.get("/logout")
    def logout():
        flask_session.clear()
        return redirect(url_for("login"))

    # ------------------------------------------------------------ pages
    @app.get("/")
    @auth.login_required
    def dashboard():
        return render_template("app.html", email=g.user.email,
                               welcome=request.args.get("welcome") == "1")

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
            if state:
                stmt = stmt.where(CloudJob.state == state)

            jobs = s.scalars(stmt).all()
            applied = {a.fingerprint for a in s.scalars(
                select(Application).where(Application.user_id == g.user.id)).all()}

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
        if action not in ("submitted", "skip"):
            return jsonify({"error": "unknown action"}), 400
        with session() as s:
            item = s.get(Application, app_id)
            if item is None or item.user_id != g.user.id:
                return jsonify({"error": "no such application"}), 404
            item.status = "submitted" if action == "submitted" else "skipped"
            s.commit()
            return jsonify({"ok": True, "status": item.status})

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
        text = (payload.get("yaml") or "").strip()
        if not text:
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
            tasks = s.scalars(
                select(Task).where(Task.user_id == g.user.id)
                .order_by(desc(Task.created_at)).limit(25)).all()
            return jsonify({"tasks": [{
                "id": t.id, "kind": t.kind, "status": t.status,
                "created_at": _iso(t.created_at),
                "finished_at": _iso(t.finished_at),
                "result": _json(t.result_json),
                "log": t.log,
            } for t in tasks]})

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
        with session() as s:
            uid = g.agent.user_id
            task = s.scalar(select(Task)
                            .where(Task.user_id == uid, Task.status == "queued")
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
                    # Never clobber a user action with a rediscovery.
                    if job.state == "queued":
                        row.pop("state", None)

                job.portal = row.get("portal", "")[:40]
                job.title = row.get("title", "")[:300]
                job.company = row.get("company", "")[:200]
                job.url = row.get("url", "")
                job.location = (row.get("location") or "")[:200]
                job.salary_text = (row.get("salary") or "")[:80]
                job.summary = (row.get("summary") or "")[:2000]
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
                item.portal = row.get("portal", "")[:40]
                item.title = row.get("title", "")[:300]
                item.company = row.get("company", "")[:200]
                item.url = row.get("url", "")
                item.status = row.get("status", "prepared")[:20]
                item.answered_json = json.dumps(row.get("answered") or {})
                item.escalated_json = json.dumps(row.get("escalated") or [])
                item.resume_path = (row.get("resume_path") or "")[:300]
                item.note = row.get("note", "")
                item.updated_at = utcnow()

                # A prepared application is no longer waiting in the queue.
                job = s.scalar(select(CloudJob).where(
                    CloudJob.user_id == uid, CloudJob.fingerprint == fp))
                if job is not None and job.state == "queued":
                    job.state = "done"
            s.commit()
        return jsonify({"ok": True, "count": len(rows)})

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
        auth.touch_agent(g.agent.id, f"finished task {task_id}")
        return jsonify({"ok": True})

    return app


# ------------------------------------------------------------- helpers
def _no_users() -> bool:
    with session() as s:
        return s.scalar(select(User).limit(1)) is None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _json(text: Any, kind: type = dict):
    try:
        value = json.loads(text or ("[]" if kind is list else "{}"))
        return value if isinstance(value, kind) else kind()
    except Exception:
        return kind()


def _validate_preferences(parsed: dict) -> str:
    """Same rules as the local config validator, minus the portal checks the
    cloud has no visibility into."""
    weights = (parsed.get("scoring") or {}).get("weights") or {}
    if not weights:
        return "preferences.scoring.weights is empty"
    total = sum(float(v) for v in weights.values())
    if abs(total - 1.0) > 0.001:
        return f"scoring weights must sum to 1.0, got {total:.3f}"

    roles = (parsed.get("search") or {}).get("roles") or []
    if not roles:
        return "search.roles is empty -- nothing to search for"
    for i, role in enumerate(roles):
        if not isinstance(role, dict) or not role.get("title"):
            return f"search.roles[{i}] needs a title"
    th = parsed.get("thresholds") or {}
    if th and float(th.get("shortlist", 0)) > float(th.get("priority", 100)):
        return "thresholds.shortlist is above thresholds.priority"
    return ""


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
