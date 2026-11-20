"""Duplicate-application detection and entity matching.

Exact matching uses keyed fingerprints of PAN / GSTIN / bank account (no
decryption needed). Fuzzy matching compares normalised legal names together with
shared contact details.
"""
from __future__ import annotations

from .db import Database
from .extraction import Issue, name_similarity


def find_duplicates(db: Database, app: dict, fingerprints: dict[str, str]) -> list[Issue]:
    issues: list[Issue] = []
    seen: set[str] = set()
    app_id = app["id"]

    def other_apps(field: str, fp: str):
        return db.all(
            """SELECT a.id, a.state, a.legal_name FROM pii_vault v JOIN applications a ON a.id = v.application_id
               WHERE v.field = ? AND v.fingerprint = ? AND a.id != ? AND a.state != 'REJECTED'""",
            (field, fp, app_id))

    for field in ("pan", "gstin"):
        fp = fingerprints.get(field)
        if not fp:
            continue
        for other in other_apps(field, fp):
            if other["id"] in seen:
                continue
            seen.add(other["id"])
            if other["state"] == "ACTIVE":
                issues.append(Issue("EXCEPTION", "EXISTING_MERCHANT",
                                    f"Same {field.upper()} already belongs to active merchant application {other['id']} "
                                    "(new outlet or duplicate?)", field=field, severity="MEDIUM", source="dedupe"))
            else:
                issues.append(Issue("EXCEPTION", "DUPLICATE_APPLICATION",
                                    f"Same {field.upper()} is used by open application {other['id']} ({other['state']})",
                                    field=field, severity="HIGH", source="dedupe"))

    bank_fp, pan_fp = fingerprints.get("bank_account"), fingerprints.get("pan")
    if bank_fp:
        for other in other_apps("bank_account", bank_fp):
            other_pan = db.scalar("SELECT fingerprint FROM pii_vault WHERE application_id=? AND field='pan'", (other["id"],))
            if other_pan and other_pan != pan_fp:
                issues.append(Issue("EXCEPTION", "SHARED_BANK_ACCOUNT",
                                    f"Settlement account is also used by unrelated entity in {other['id']} (possible mule account)",
                                    field="bank_account", severity="HIGH", source="dedupe"))

    if app.get("legal_name"):
        candidates = db.all(
            """SELECT id, state, legal_name, email, phone FROM applications
               WHERE id != ? AND state != 'REJECTED' AND (email = ? OR phone = ?)""",
            (app_id, app.get("email") or "-", app.get("phone") or "-"))
        for other in candidates:
            if other["id"] in seen:
                continue
            sim = name_similarity(app["legal_name"], other["legal_name"])
            if sim >= 0.9:
                issues.append(Issue("EXCEPTION", "POSSIBLE_DUPLICATE_ENTITY",
                                    f"Similar legal name ({sim:.0%}) and shared contact details with {other['id']}",
                                    field="legal_name", severity="MEDIUM", source="dedupe"))
    return issues
