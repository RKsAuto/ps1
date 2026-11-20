"""Append-only, hash-chained audit trail. Each entry commits to the previous one,
so any edit or deletion of history is detectable with ``verify()``."""
from __future__ import annotations

import hashlib

from .db import Database, dumps

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, db: Database, clock):
        self.db, self.clock = db, clock

    @staticmethod
    def _digest(prev_hash, application_id, actor, action, details_json, at) -> str:
        material = "|".join([prev_hash, application_id or "", actor, action, details_json, repr(at)])
        return hashlib.sha256(material.encode()).hexdigest()

    def record(self, application_id: str | None, actor: str, action: str, details: dict | None = None) -> None:
        details_json = dumps(details or {})
        at = self.clock.now()
        with self.db.tx():
            prev = self.db.scalar("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1") or GENESIS
            h = self._digest(prev, application_id, actor, action, details_json, at)
            self.db.execute(
                "INSERT INTO audit_log(application_id, actor, action, details_json, at, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                (application_id, actor, action, details_json, at, prev, h))

    def verify(self) -> dict:
        prev = GENESIS
        rows = self.db.all("SELECT * FROM audit_log ORDER BY seq")
        for r in rows:
            expected = self._digest(prev, r["application_id"], r["actor"], r["action"], r["details_json"], r["at"])
            if r["prev_hash"] != prev or r["hash"] != expected:
                return {"valid": False, "entries": len(rows), "broken_at_seq": r["seq"]}
            prev = r["hash"]
        return {"valid": True, "entries": len(rows), "head": prev}
