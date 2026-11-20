"""Operational metrics computed from durable workflow history."""
from __future__ import annotations

import statistics

from .db import Database


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def compute(db: Database, now: float) -> dict:
    total = db.scalar("SELECT COUNT(*) FROM applications") or 0
    by_state = {r["state"]: r["n"] for r in db.all("SELECT state, COUNT(*) n FROM applications GROUP BY state")}

    hist = db.all("SELECT application_id, to_state, event, at FROM state_history ORDER BY application_id, id")
    durations: dict[str, list[float]] = {}
    touched_exception, touched_info, activation_times = set(), set(), []
    created = {}
    for i, h in enumerate(hist):
        app = h["application_id"]
        created.setdefault(app, h["at"])
        nxt = hist[i + 1] if i + 1 < len(hist) and hist[i + 1]["application_id"] == app else None
        durations.setdefault(h["to_state"], []).append((nxt["at"] if nxt else now) - h["at"])
        if h["to_state"] == "MANUAL_REVIEW":
            touched_exception.add(app)
        if h["to_state"] == "AWAITING_INFO":
            touched_info.add(app)
        if h["to_state"] == "ACTIVE":
            activation_times.append(h["at"] - created[app])

    time_in_state = {s: {"avg_s": round(statistics.mean(v), 1), "p50_s": round(statistics.median(v), 1),
                         "max_s": round(max(v), 1), "samples": len(v)} for s, v in durations.items()}
    active = by_state.get("ACTIVE", 0)
    straight_through = db.scalar(
        "SELECT COUNT(*) FROM applications a WHERE a.state='ACTIVE' AND NOT EXISTS ("
        "SELECT 1 FROM state_history h WHERE h.application_id=a.id AND h.to_state IN ('MANUAL_REVIEW','AWAITING_INFO'))") or 0

    checks = db.all("SELECT check_type, status, COUNT(*) n FROM checks GROUP BY check_type, status")
    check_stats: dict[str, dict] = {}
    for r in checks:
        check_stats.setdefault(r["check_type"], {})[r["status"]] = r["n"]
    turnaround = db.all("SELECT check_type, AVG(responded_at - sent_at) avg_s, COUNT(*) n FROM check_requests "
                        "WHERE responded_at IS NOT NULL GROUP BY check_type")
    for r in turnaround:
        check_stats.setdefault(r["check_type"], {})["avg_turnaround_s"] = round(r["avg_s"], 2)
    req_status = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) n FROM check_requests GROUP BY status")}

    callbacks = {r["outcome"]: r["n"] for r in db.all("SELECT outcome, COUNT(*) n FROM callback_inbox GROUP BY outcome")}
    callbacks["DUPLICATE"] = callbacks.get("DUPLICATE", 0) + (db.scalar("SELECT COALESCE(SUM(duplicate_count),0) FROM callback_inbox") or 0)

    exceptions = {r["code"]: r["n"] for r in db.all(
        "SELECT code, COUNT(*) n FROM issues WHERE kind='EXCEPTION' GROUP BY code ORDER BY n DESC")}
    missing = {r["code"]: r["n"] for r in db.all(
        "SELECT code, COUNT(*) n FROM issues WHERE kind='MISSING_INFO' GROUP BY code ORDER BY n DESC")}

    return {
        "applications": {"total": total, "by_state": by_state},
        "rates": {
            "exception_rate_pct": _pct(len(touched_exception), total),
            "info_request_rate_pct": _pct(len(touched_info), total),
            "straight_through_pct_of_active": _pct(straight_through, active),
            "activation_rate_pct": _pct(active, total),
        },
        "time_to_activate_s": {"avg": round(statistics.mean(activation_times), 1) if activation_times else None,
                               "p50": round(statistics.median(activation_times), 1) if activation_times else None},
        "time_in_state": time_in_state,
        "checks": check_stats,
        "provider_requests": req_status,
        "callbacks": callbacks,
        "exceptions_by_code": exceptions,
        "missing_info_by_code": missing,
        "review_queue": {"open": db.scalar("SELECT COUNT(*) FROM review_tasks WHERE status='OPEN'") or 0,
                         "sla_breached": db.scalar("SELECT COUNT(*) FROM review_tasks WHERE status='OPEN' AND sla_due_at < ?", (now,)) or 0},
        "dead_letters_open": db.scalar("SELECT COUNT(*) FROM dead_letters WHERE status='OPEN'") or 0,
        "jobs": {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) n FROM jobs GROUP BY status")},
    }
