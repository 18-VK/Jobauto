"""Pushing applications costs a fixed number of queries, however many rows.

The endpoint ran two SELECTs per application against a Postgres a network
hop away. The agent re-pushes every pending application on each poll, and
with a few hundred of them the request outlived its timeout -- logged as
"cannot reach ...: ReadTimeout" right after a jobs push that had worked.
"""
from __future__ import annotations

from sqlalchemy import event

from jobauto.cloud import db as clouddb

from .test_cloud import H, agent_token, app, client, signup  # noqa: F401


def _rows(n: int, status: str = "prepared"):
    return [{"fingerprint": f"f{i}", "portal": "naukri", "title": f"Job {i}",
             "company": "Acme", "url": f"https://x/{i}", "status": status}
            for i in range(n)]


def _count_statements(fn):
    engine = clouddb._engine
    seen = {"n": 0}

    def _tick(*_a, **_k):
        seen["n"] += 1
    event.listen(engine, "before_cursor_execute", _tick)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", _tick)
    return seen["n"]


def test_query_count_does_not_grow_with_the_batch(client):
    signup(client)
    tok = agent_token(client)
    client.post("/api/agent/jobs", headers=H(tok), json={"jobs": [
        {**r, "score": 90.0} for r in _rows(60)]})
    # Rows that already exist are the steady state: every poll re-pushes them.
    client.post("/api/agent/applications", headers=H(tok),
                json={"applications": _rows(60)})

    small = _count_statements(lambda: client.post(
        "/api/agent/applications", headers=H(tok), json={"applications": _rows(3)}))
    large = _count_statements(lambda: client.post(
        "/api/agent/applications", headers=H(tok), json={"applications": _rows(60)}))
    # Per-row UPDATEs are unavoidable on a changed row; what must not scale
    # is the lookups. Allow one statement per row plus a fixed overhead.
    assert large - small <= 57 + 5, (small, large)
    # And the lookups themselves are a handful, not two per row.
    assert small <= 12, small


def test_a_batch_of_pending_applications_round_trips(client):
    signup(client)
    tok = agent_token(client)
    res = client.post("/api/agent/applications", headers=H(tok),
                      json={"applications": _rows(120)})
    assert res.status_code == 200
    assert res.get_json()["count"] == 120
    pending = client.get("/api/applications").get_json()
    rows = pending.get("applications", pending)
    assert len(rows) == 120


def test_an_oversized_push_is_refused_not_timed_out(client):
    signup(client)
    tok = agent_token(client)
    res = client.post("/api/agent/applications", headers=H(tok),
                      json={"applications": _rows(501)})
    assert res.status_code == 413


def test_the_agent_chunks_a_long_list():
    from jobauto.agent.runner import CloudClient
    sent: list[int] = []
    c = CloudClient("https://x", "t")
    c._call = lambda method, path, **kw: (
        sent.append(len(kw["json"]["applications"])) or {"ok": True, "count": sent[-1]})
    out = c.push_applications(_rows(450))
    assert sent == [200, 200, 50]
    assert out["count"] == 450
