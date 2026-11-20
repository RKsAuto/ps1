"""Workflow engine: the durable, event-driven onboarding state machine.

Inputs that move an application forward:
  * merchant actions      - submit, provide info, upload documents, change bank details
  * provider callbacks    - async results of PAN / GST / bank / risk verification
  * the clock             - request timeouts and scheduled retries (``tick``)
  * reviewer decisions    - approve / reject / request info / retry checks
  * operator actions      - replay dead letters

Every handler runs in one database transaction, so state, issues, jobs and
audit entries are always mutually consistent.
"""
from __future__ import annotations

import json
import logging
import random
import uuid

from . import dedupe, extraction
from .audit import AuditLog
from .clock import Clock
from .config import CHECK_TYPES, WAIVABLE_FAILURES, Settings
from .db import Database, dumps, loads
from .providers import Chaos, MerchantIdService, ProviderSimulator
from .security import SENSITIVE_FIELDS, Vault, mask, redact, sign, verify_signature
from .states import (EDITABLE_STATES, SATISFIED, SETTLED, AppState, CheckStatus, Event,
                     next_state)

log = logging.getLogger(__name__)
S, C, E = AppState, CheckStatus, Event

COLUMN_FIELDS = ("legal_name", "business_name", "business_type", "category", "monthly_volume_inr",
                 "gst_registered", "email", "phone", "city", "ifsc", "account_holder")
BUSINESS_TYPES = ("PROPRIETORSHIP", "PARTNERSHIP", "LLP", "PRIVATE_LIMITED", "PUBLIC_LIMITED")
BANK_FIELDS = ("bank_account", "ifsc", "account_holder")

# (check, provider outcome) -> (check status, issue to raise or None)
# issue = (kind, code, field, message, severity)
RESULT_POLICY = {
    ("PAN", "VALID"): (C.PASSED, None),
    ("PAN", "INVALID"): (C.FAILED, ("MISSING_INFO", "PAN_INVALID", "pan",
                                    "PAN was not found in the income-tax registry. Please check and correct the PAN.", "HIGH")),
    ("PAN", "NAME_MISMATCH"): (C.FAILED, ("EXCEPTION", "PAN_NAME_MISMATCH", "legal_name",
                                          "Name registered against the PAN does not match the legal name.", "MEDIUM")),
    ("GST", "ACTIVE"): (C.PASSED, None),
    ("GST", "CANCELLED"): (C.FAILED, ("MISSING_INFO", "GST_NOT_ACTIVE", "gstin",
                                      "GSTIN registration is cancelled. Provide an active GSTIN with its certificate, "
                                      "or declare the business as not GST-registered.", "HIGH")),
    ("GST", "NAME_MISMATCH"): (C.FAILED, ("EXCEPTION", "GST_NAME_MISMATCH", "legal_name",
                                          "Legal name on GST registry differs from the application.", "MEDIUM")),
    ("BANK", "VERIFIED"): (C.PASSED, None),
    ("BANK", "ACCOUNT_CLOSED"): (C.FAILED, ("MISSING_INFO", "BANK_ACCOUNT_INVALID", "bank_account",
                                            "Penny-drop failed: the settlement account is closed. Provide another account "
                                            "with a cancelled cheque.", "HIGH")),
    ("BANK", "NAME_MISMATCH"): (C.FAILED, ("EXCEPTION", "BANK_NAME_MISMATCH", "account_holder",
                                           "Beneficiary name returned by the bank does not match the applicant.", "HIGH")),
    ("RISK", "ACCEPTABLE"): (C.PASSED, None),
    ("RISK", "HIGH_RISK"): (C.FAILED, ("EXCEPTION", "RISK_HIGH", None,
                                       "Risk score requires underwriting review.", "HIGH")),
    ("RISK", "BLOCKED"): (C.FAILED, ("EXCEPTION", "WATCHLIST_HIT", None,   # auto-reject, see _advance
                                     "Applicant matched a sanctions / watchlist entry; application auto-rejected.", "HIGH")),
}


class NotFound(Exception):
    pass


class PolicyViolation(Exception):
    """The request is well-formed but not allowed by workflow policy (HTTP 409)."""


class InvalidInput(Exception):
    """HTTP 422."""


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def flatten_form(form: dict) -> dict:
    """Accept the nested API shape and return canonical flat fields."""
    out = {k: form.get(k) for k in ("legal_name", "business_name", "business_type", "category",
                                     "monthly_volume_inr", "gst_registered", "pan", "gstin") if k in form}
    bank = form.get("bank") or {}
    if "account_number" in bank:
        out["bank_account"] = bank["account_number"]
    for k in ("ifsc", "account_holder"):
        if k in bank:
            out[k] = bank[k]
    contact = form.get("contact") or {}
    for k in ("email", "phone"):
        if k in contact:
            out[k] = contact[k]
    if "city" in (form.get("address") or {}):
        out["city"] = form["address"]["city"]
    for k in ("bank_account", "ifsc", "account_holder", "email", "phone", "city"):
        if k in form:
            out[k] = form[k]
    return out


class Engine:
    def __init__(self, settings: Settings | None = None, clock: Clock | None = None, db: Database | None = None,
                 llm_transport=None):
        self.s = settings or Settings()
        self.clock = clock or Clock()
        self.db = db or Database(self.s.db_path)
        self.vault = Vault(self.s.encryption_key, self.s.fingerprint_key)
        self.chaos = Chaos()
        self.providers = ProviderSimulator(self.clock, self.s, self.chaos)
        self.mid_service = MerchantIdService(self.clock, self.chaos)
        self.audit = AuditLog(self.db, self.clock)
        self.rng = random.Random(self.s.seed)
        self.llm_transport = llm_transport
        self._job_handlers = {"DISPATCH_CHECK": self._job_dispatch_check, "PROVISION_MERCHANT": self._job_provision}
        self._dead_handlers = {"DISPATCH_CHECK": self._dead_dispatch, "PROVISION_MERCHANT": self._dead_provision}

    # ================================================================== merchant actions

    def submit_application(self, form: dict, documents: list[dict] | None = None, actor: str = "merchant",
                           app_id: str | None = None) -> str:
        fields = flatten_form(form)
        self._validate_input(fields)
        app_id = app_id or new_id("APP")
        now = self.clock.now()
        with self.db.tx():
            self.db.execute(
                "INSERT INTO applications(id, state, gst_registered, created_at, updated_at, state_entered_at) VALUES (?,?,?,?,?,?)",
                (app_id, S.SUBMITTED.value, 0, now, now, now))
            self.db.execute("INSERT INTO state_history(application_id, from_state, to_state, event, at) VALUES (?,?,?,?,?)",
                            (app_id, None, S.SUBMITTED.value, "SUBMITTED", now))
            for k, v in fields.items():
                self._set_field(app_id, k, v)
            self.audit.record(app_id, actor, "APPLICATION_SUBMITTED", {
                "legal_name": fields.get("legal_name"), "business_type": fields.get("business_type"),
                "category": fields.get("category"), "gst_registered": bool(fields.get("gst_registered")),
                "pan": mask("pan", fields.get("pan")), "documents": [d.get("doc_type") for d in documents or []]})
            for doc in documents or []:
                self._ingest_document(app_id, doc, actor)
            self._evaluate_after_change(app_id)
        return app_id

    def add_document(self, app_id: str, doc: dict, actor: str = "merchant") -> dict:
        return self.provide_info(app_id, {}, [doc], actor)

    def provide_info(self, app_id: str, updates: dict, documents: list[dict] | None = None,
                     actor: str = "merchant") -> dict:
        app = self._app(app_id)
        if S(app["state"]) not in EDITABLE_STATES:
            raise PolicyViolation(f"application is {app['state']}; information can no longer be changed here")
        fields = flatten_form(updates)
        self._validate_input(fields, partial=True)
        with self.db.tx():
            for k, v in fields.items():
                self._set_field(app_id, k, v)
            for doc in documents or []:
                self._ingest_document(app_id, doc, actor)
            self.audit.record(app_id, actor, "INFO_PROVIDED", {
                "fields": sorted(fields), "documents": [d.get("doc_type") for d in documents or []]})
            # Reviewer requests are considered answered once the merchant responds;
            # validation and re-verification decide whether the answer is good enough.
            self._resolve_issues(app_id, "source = 'review' AND kind = 'MISSING_INFO'", by=actor)
            self._evaluate_after_change(app_id)
        return self.get_application(app_id)

    def update_bank_details(self, app_id: str, bank: dict, document: dict | None = None,
                            actor: str = "merchant") -> dict:
        app = self._app(app_id)
        state = S(app["state"])
        if state not in EDITABLE_STATES | {S.ACTIVE}:
            raise PolicyViolation(f"bank details cannot be changed while application is {state.value}")
        fields = flatten_form({"bank": bank})
        if set(fields) != set(BANK_FIELDS):
            raise InvalidInput("account_number, ifsc and account_holder are all required")
        self._validate_input(fields, partial=True)
        old = self._values(app_id)
        with self.db.tx():
            for k, v in fields.items():
                self._set_field(app_id, k, v)
            if document:
                self._ingest_document(app_id, document, actor)
            self.audit.record(app_id, actor, "BANK_DETAILS_CHANGED", {
                "from": {"account": mask("bank_account", old.get("bank_account")), "ifsc": old.get("ifsc")},
                "to": {"account": mask("bank_account", fields["bank_account"]), "ifsc": fields["ifsc"]},
                "state": state.value})
            if state == S.ACTIVE:
                # Post-activation: hold settlements until the new account is re-verified
                # and the risk score is recomputed.
                self.db.execute("UPDATE applications SET payouts_on_hold=1, updated_at=? WHERE id=?", (self.clock.now(), app_id))
                self.audit.record(app_id, "system", "PAYOUTS_HELD", {"reason": "bank details changed after activation"})
                self._reconcile_checks(app_id, dispatch=True)
            else:
                self._evaluate_after_change(app_id)
        return self.get_application(app_id)

    # ================================================================== reviewer / operator actions

    def review_decision(self, task_id: str, decision: str, reviewer: str, note: str = "",
                        check_types: list[str] | None = None, info_request: dict | None = None) -> dict:
        task = self.db.one("SELECT * FROM review_tasks WHERE id=?", (task_id,))
        if not task:
            raise NotFound(task_id)
        if task["status"] != "OPEN":
            raise PolicyViolation(f"review task is {task['status']}")
        if not (reviewer or "").strip():
            raise InvalidInput("reviewer is required")
        decision = (decision or "").upper()
        app_id = task["application_id"]
        app = self._app(app_id)
        if app["state"] != S.MANUAL_REVIEW.value:
            raise PolicyViolation(f"application is {app['state']}, not in manual review")
        note = redact(note or "")
        actor = f"reviewer:{reviewer}"

        with self.db.tx():
            if decision == "APPROVE":
                if not note:
                    raise InvalidInput("a justification note is required to approve")
                to_waive, blockers = [], []
                for ct in self.s.required_checks(app):
                    chk = self._check(app_id, ct)
                    st = C(chk["status"])
                    if st in SATISFIED:
                        continue
                    if st == C.FAILED and (ct, chk["reason_code"]) in WAIVABLE_FAILURES:
                        to_waive.append(chk)
                    else:
                        blockers.append(f"{ct} check is {st.value}" + (f" ({chk['reason_code']})" if chk["reason_code"] else ""))
                if self._open_issues(app_id, "MISSING_INFO"):
                    blockers.append("merchant information is still missing")
                if blockers:
                    raise PolicyViolation("cannot approve: " + "; ".join(blockers))
                for chk in to_waive:
                    self.db.execute("UPDATE checks SET status=?, waiver_json=?, updated_at=? WHERE id=?",
                                    (C.WAIVED.value, dumps({"reviewer": reviewer, "note": note, "at": self.clock.now(),
                                                            "original_reason": chk["reason_code"]}), self.clock.now(), chk["id"]))
                    self.audit.record(app_id, actor, "CHECK_WAIVED", {"check": chk["check_type"], "reason": chk["reason_code"], "note": note})
                self._resolve_issues(app_id, "kind = 'EXCEPTION'", by=actor)
                self._close_task(task_id, decision, reviewer, note)
                self._transition(app_id, E.REVIEW_APPROVED, actor)
                self._start_provisioning(app_id, actor)
            elif decision == "REJECT":
                if not note:
                    raise InvalidInput("a reason is required to reject")
                self._close_task(task_id, decision, reviewer, note)
                self._transition(app_id, E.REVIEW_REJECTED, actor)
            elif decision == "REQUEST_INFO":
                req = info_request or {}
                message = redact(req.get("message") or note)
                if not message:
                    raise InvalidInput("tell the merchant what is needed")
                self._add_issue(app_id, extraction.Issue("MISSING_INFO", "REVIEWER_REQUEST", message,
                                                         field=req.get("field"), severity="MEDIUM", source="review"))
                self._close_task(task_id, decision, reviewer, note or message)
                self._transition(app_id, E.REVIEW_REQUESTED_INFO, actor)
            elif decision == "RETRY_CHECKS":
                types = [t.upper() for t in (check_types or [])]
                if not types:
                    raise InvalidInput("check_types is required")
                for ct in types:
                    chk = self._check(app_id, ct)
                    if chk["status"] not in (C.FAILED.value, C.ERROR.value):
                        raise InvalidInput(f"{ct} check is {chk['status']}; only FAILED/ERROR checks can be retried")
                for ct in types:
                    self.db.execute(
                        "UPDATE dead_letters SET status='REPLAYED', replayed_at=? WHERE application_id=? AND status='OPEN' "
                        "AND kind IN ('DISPATCH_CHECK','CHECK_TIMEOUT') AND json_extract(payload_json, '$.check_type') = ?",
                        (self.clock.now(), app_id, ct))
                self._close_task(task_id, decision, reviewer, note)
                self._transition(app_id, E.REVIEW_RETRY_CHECKS, actor)
                self._reconcile_checks(app_id, force=types, dispatch=True)
                self._advance(app_id)
            else:
                raise InvalidInput("decision must be APPROVE, REJECT, REQUEST_INFO or RETRY_CHECKS")
            self.audit.record(app_id, actor, "REVIEW_DECISION", {"task": task_id, "decision": decision, "note": note,
                                                                   "check_types": check_types or []})
        return self.get_application(app_id)

    def replay_dead_letter(self, dl_id: str, actor: str = "operator") -> dict:
        dl = self.db.one("SELECT * FROM dead_letters WHERE id=?", (dl_id,))
        if not dl:
            raise NotFound(dl_id)
        if dl["status"] != "OPEN":
            raise PolicyViolation(f"dead letter already {dl['status']}")
        app_id, payload = dl["application_id"], loads(dl["payload_json"], {})
        with self.db.tx():
            app = self._app(app_id)
            if dl["kind"] == "PROVISION_MERCHANT":
                if app["state"] != S.PROVISIONING_FAILED.value:
                    raise PolicyViolation(f"application is {app['state']}")
                self._resolve_issues(app_id, "code = 'PROVISIONING_FAILED'", by=actor)
                self._transition(app_id, E.DLQ_REPLAYED, actor)
                self._enqueue("PROVISION_MERCHANT", app_id, {})
            else:
                ct = payload["check_type"]
                if self._check(app_id, ct)["status"] == C.ERROR.value:
                    if app["state"] == S.MANUAL_REVIEW.value:
                        self._transition(app_id, E.DLQ_REPLAYED, actor)
                    self._reconcile_checks(app_id, force=[ct], dispatch=app["state"] != S.AWAITING_INFO.value)
                    self._advance(app_id)
            self.db.execute("UPDATE dead_letters SET status='REPLAYED', replayed_at=? WHERE id=?", (self.clock.now(), dl_id))
            self.audit.record(app_id, actor, "DLQ_REPLAYED", {"dead_letter": dl_id, "kind": dl["kind"]})
        return self.get_application(app_id)

    # ================================================================== provider callbacks

    def receive_callback(self, provider: str, body: bytes, signature: str | None) -> tuple[int, dict]:
        provider = (provider or "").upper()
        secret = self.s.webhook_secrets.get(provider)
        if not secret:
            return 404, {"error": f"unknown provider {provider}"}
        if not verify_signature(secret, body, signature):
            self.audit.record(None, f"provider:{provider}", "CALLBACK_REJECTED", {"reason": "invalid signature"})
            return 401, {"outcome": "INVALID_SIGNATURE"}
        try:
            payload = json.loads(body)
            cb_id, request_id = payload["callback_id"], payload["request_id"]
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "malformed callback"}
        now = self.clock.now()
        with self.db.tx():
            if self.db.one("SELECT 1 FROM callback_inbox WHERE callback_id=?", (cb_id,)):
                self.db.execute("UPDATE callback_inbox SET duplicate_count = duplicate_count + 1 WHERE callback_id=?", (cb_id,))
                app_id = self.db.scalar("SELECT application_id FROM callback_inbox WHERE callback_id=?", (cb_id,))
                self.audit.record(app_id, f"provider:{provider}", "CALLBACK_DUPLICATE_IGNORED",
                                  {"callback_id": cb_id, "request_id": request_id})
                return 200, {"outcome": "DUPLICATE"}
            req = self.db.one("SELECT * FROM check_requests WHERE request_id=?", (request_id,))
            if not req:
                self._record_inbox(cb_id, provider, request_id, None, "UNKNOWN_REQUEST", payload)
                self.audit.record(None, f"provider:{provider}", "CALLBACK_UNKNOWN_REQUEST", {"callback_id": cb_id})
                return 202, {"outcome": "UNKNOWN_REQUEST"}
            app_id = req["application_id"]
            chk = self.db.one("SELECT * FROM checks WHERE id=?", (req["check_id"],))
            if req["status"] == "RESPONDED":
                outcome = "DUPLICATE"
            elif req["status"] == "SENT" and chk["request_id"] == request_id and chk["status"] == C.IN_FLIGHT.value:
                outcome = "APPLIED"
            elif (chk["status"] in (C.IN_FLIGHT.value, C.RETRY_SCHEDULED.value)
                  and req["input_hash"] == chk["input_hash"]):
                # The request timed out but its answer still arrived, and the data it
                # verified has not changed since: accept it instead of waiting for the retry.
                outcome = "LATE_APPLIED"
            else:
                outcome = "STALE_IGNORED"
            self._record_inbox(cb_id, provider, request_id, app_id, outcome, payload)
            self.audit.record(app_id, f"provider:{provider}", "CALLBACK_RECEIVED", {
                "callback_id": cb_id, "request_id": request_id, "outcome": outcome,
                "request_status": req["status"], "result": (payload.get("result") or {}).get("outcome")})
            if outcome in ("APPLIED", "LATE_APPLIED"):
                if chk["request_id"] and chk["request_id"] != request_id:
                    self.db.execute("UPDATE check_requests SET status='SUPERSEDED' WHERE request_id=? AND status='SENT'",
                                    (chk["request_id"],))
                self.db.execute("UPDATE check_requests SET status='RESPONDED', responded_at=? WHERE request_id=?",
                                (now, request_id))
                self._apply_result(app_id, chk, payload.get("result") or {}, request_id)
                self._advance(app_id)
        return 200, {"outcome": outcome}

    # ================================================================== clock-driven work

    def tick(self) -> dict:
        delivered = self.deliver_due_callbacks()
        timed_out = self.sweep_timeouts()
        jobs = self.run_due_jobs()
        return {"callbacks": delivered, "timeouts": timed_out, "jobs": jobs}

    def deliver_due_callbacks(self) -> int:
        n = 0
        for cb in self.providers.due_callbacks():
            body = dumps(cb["payload"]).encode()
            self.receive_callback(cb["provider"], body, sign(self.s.webhook_secrets[cb["provider"]], body))
            n += 1
        return n

    def redeliver_callback(self, callback_id: str) -> tuple[int, dict]:
        """Simulator helper: re-send a callback that was already delivered."""
        cb = self.providers.find(callback_id)
        if not cb:
            raise NotFound(callback_id)
        body = dumps(cb["payload"]).encode()
        return self.receive_callback(cb["provider"], body, sign(self.s.webhook_secrets[cb["provider"]], body))

    def sweep_timeouts(self) -> int:
        now = self.clock.now()
        overdue = self.db.all("SELECT id FROM checks WHERE status=? AND deadline_at <= ?", (C.IN_FLIGHT.value, now))
        for row in overdue:
            with self.db.tx():
                chk = self.db.one("SELECT * FROM checks WHERE id=?", (row["id"],))
                if chk["status"] != C.IN_FLIGHT.value or chk["deadline_at"] > now:
                    continue
                app_id, ct = chk["application_id"], chk["check_type"]
                self.db.execute("UPDATE check_requests SET status='TIMED_OUT' WHERE request_id=?", (chk["request_id"],))
                self.audit.record(app_id, "system", "CHECK_TIMED_OUT", {"check": ct, "request_id": chk["request_id"],
                                                                         "attempt": chk["attempt"]})
                if chk["attempt"] < self.s.max_check_attempts:
                    delay = self._backoff(chk["attempt"])
                    self.db.execute("UPDATE checks SET status=?, updated_at=? WHERE id=?", (C.RETRY_SCHEDULED.value, now, chk["id"]))
                    self._enqueue("DISPATCH_CHECK", app_id, {"check_type": ct, "input_hash": chk["input_hash"]}, delay=delay)
                    self.audit.record(app_id, "system", "CHECK_RETRY_SCHEDULED", {"check": ct, "in_seconds": round(delay, 1),
                                                                                   "next_attempt": chk["attempt"] + 1})
                else:
                    self._fail_check_unavailable(app_id, chk, "TIMEOUT_EXHAUSTED",
                                                 f"{ct} provider did not respond after {chk['attempt']} attempts")
                    self._dead_letter("CHECK_TIMEOUT", app_id, {"check_type": ct, "input_hash": chk["input_hash"]},
                                      f"no callback after {chk['attempt']} attempts")
                    self._advance(app_id)
        return len(overdue)

    def run_due_jobs(self, limit: int = 100) -> int:
        due = self.db.all("SELECT * FROM jobs WHERE status='PENDING' AND run_at <= ? ORDER BY run_at, created_at LIMIT ?",
                          (self.clock.now(), limit))
        for job in due:
            payload = loads(job["payload_json"], {})
            try:
                with self.db.tx():
                    current = self.db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))
                    if current["status"] != "PENDING":
                        continue
                    self._job_handlers[job["kind"]](job, payload)
                    self.db.execute("UPDATE jobs SET status='DONE', attempts=attempts+1 WHERE id=?", (job["id"],))
            except Exception as exc:  # noqa: BLE001 - every failure is retried, then dead-lettered
                err = redact(f"{type(exc).__name__}: {exc}")
                attempts = job["attempts"] + 1
                with self.db.tx():
                    if attempts >= self.s.max_job_attempts:
                        self.db.execute("UPDATE jobs SET status='DEAD', attempts=?, last_error=? WHERE id=?", (attempts, err, job["id"]))
                        self._dead_letter(job["kind"], job["application_id"], payload, err)
                        self.audit.record(job["application_id"], "system", "JOB_DEAD_LETTERED",
                                          {"kind": job["kind"], "attempts": attempts, "error": err})
                        self._dead_handlers[job["kind"]](job, payload, err)
                    else:
                        delay = self._backoff(attempts)
                        self.db.execute("UPDATE jobs SET attempts=?, run_at=?, last_error=? WHERE id=?",
                                        (attempts, self.clock.now() + delay, err, job["id"]))
                        self.audit.record(job["application_id"], "system", "JOB_RETRY_SCHEDULED",
                                          {"kind": job["kind"], "attempt": attempts, "in_seconds": round(delay, 1), "error": err})
        return len(due)

    # ================================================================== job handlers

    def _job_dispatch_check(self, job: dict, payload: dict) -> None:
        app_id, ct = job["application_id"], payload["check_type"]
        app = self._app(app_id)
        chk = self._check(app_id, ct)
        if chk["status"] not in (C.PENDING.value, C.RETRY_SCHEDULED.value) or chk["input_hash"] != payload["input_hash"]:
            return  # obsolete: superseded by newer data or already answered (late callback)
        if app["state"] not in (S.VERIFYING.value, S.ACTIVE.value):
            return  # will be re-queued when the application is back in verification
        inputs = self._check_inputs(ct, app, self._values(app_id))
        request_id = new_id("REQ")
        attempt = chk["attempt"] + 1
        now = self.clock.now()
        deadline = now + self.s.check_timeout_s
        self.db.execute("INSERT INTO check_requests VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (request_id, chk["id"], app_id, ct, attempt, chk["input_hash"], "SENT", now, deadline, None))
        self.db.execute("UPDATE checks SET status=?, attempt=?, request_id=?, deadline_at=?, updated_at=? WHERE id=?",
                        (C.IN_FLIGHT.value, attempt, request_id, deadline, now, chk["id"]))
        self.providers.submit(ct, request_id, inputs, application_id=app_id)   # raises ProviderUnavailable -> rollback + job retry
        self.audit.record(app_id, "system", "CHECK_DISPATCHED", {"check": ct, "request_id": request_id, "attempt": attempt})

    def _dead_dispatch(self, job: dict, payload: dict, error: str) -> None:
        app_id, ct = job["application_id"], payload["check_type"]
        chk = self._check(app_id, ct)
        if chk["status"] in (C.PENDING.value, C.RETRY_SCHEDULED.value) and chk["input_hash"] == payload["input_hash"]:
            self._fail_check_unavailable(app_id, chk, "PROVIDER_UNAVAILABLE",
                                         f"{ct} provider unavailable after {self.s.max_job_attempts} attempts ({error})")
            self._advance(app_id)

    def _job_provision(self, job: dict, payload: dict) -> None:
        app_id = job["application_id"]
        app = self._app(app_id)
        if app["state"] != S.PROVISIONING.value:
            return
        risk = loads(self._check(app_id, "RISK")["result_json"], {}) or {}
        result = self.mid_service.create(idempotency_key=app_id, payload={
            "legal_name": app["legal_name"], "category": app["category"], "risk_tier": risk.get("tier", "MEDIUM")})
        self.db.execute("UPDATE applications SET merchant_id=?, capabilities_json=?, updated_at=? WHERE id=?",
                        (result["merchant_id"], dumps(result["capabilities"]), self.clock.now(), app_id))
        self.audit.record(app_id, "system", "MERCHANT_ID_CREATED", {
            "merchant_id": result["merchant_id"], "capabilities": result["capabilities"],
            "idempotent_replay": result["replayed"]})
        self._transition(app_id, E.MERCHANT_CREATED, "system")

    def _dead_provision(self, job: dict, payload: dict, error: str) -> None:
        app_id = job["application_id"]
        if self._app(app_id)["state"] == S.PROVISIONING.value:
            self._add_issue(app_id, extraction.Issue(
                "EXCEPTION", "PROVISIONING_FAILED",
                f"Merchant ID creation failed after {self.s.max_job_attempts} attempts ({error}). Replay from the dead-letter queue.",
                severity="HIGH", source="system"))
            self._transition(app_id, E.PROVISIONING_EXHAUSTED, "system")

    # ================================================================== core workflow logic

    def _evaluate_after_change(self, app_id: str) -> None:
        """Re-validate after any data change and decide where the application goes next."""
        self._revalidate(app_id)
        self._reconcile_checks(app_id, dispatch=False)
        app = self._app(app_id)
        state = S(app["state"])
        if self._open_issues(app_id, "MISSING_INFO"):
            if state in (S.SUBMITTED, S.VERIFYING, S.MANUAL_REVIEW):
                self._transition(app_id, E.INFO_REQUIRED, "system")
            return
        if state == S.SUBMITTED:
            self._transition(app_id, E.DOCS_VALIDATED, "system")
        elif state in (S.AWAITING_INFO, S.MANUAL_REVIEW):
            self._transition(app_id, E.DATA_CHANGED, "system")
        self._reconcile_checks(app_id, dispatch=True)
        self._advance(app_id)

    def _advance(self, app_id: str) -> None:
        """Decision point, evaluated whenever a check settles."""
        app = self._app(app_id)
        state = S(app["state"])
        if state == S.ACTIVE:
            self._advance_active(app)
            return
        if state != S.VERIFYING:
            return
        checks = [self._check(app_id, ct) for ct in self.s.required_checks(app)]
        if any(C(c["status"]) not in SETTLED for c in checks):
            return  # wait until every configured verification has an answer
        blocked = [c for c in checks if c["reason_code"] == "BLOCKED"]
        if blocked:
            self.audit.record(app_id, "system", "AUTO_REJECTED", {"reason": "risk watchlist hit"})
            self._transition(app_id, E.AUTO_REJECTED, "system")
            return
        if self._open_issues(app_id, "MISSING_INFO"):
            self._transition(app_id, E.INFO_REQUIRED, "system")
            return
        if self._open_issues(app_id, "EXCEPTION") or any(C(c["status"]) not in SATISFIED for c in checks):
            self._transition(app_id, E.EXCEPTION_RAISED, "system")
            return
        self._transition(app_id, E.ALL_CHECKS_PASSED, "system")
        self._start_provisioning(app_id, "system")

    def _advance_active(self, app: dict) -> None:
        if not app["payouts_on_hold"]:
            return
        bank, risk = self._check(app["id"], "BANK"), self._check(app["id"], "RISK")
        if C(bank["status"]) in SETTLED and C(risk["status"]) in SETTLED:
            if C(bank["status"]) == C.PASSED and C(risk["status"]) == C.PASSED:
                self.db.execute("UPDATE applications SET payouts_on_hold=0, updated_at=? WHERE id=?", (self.clock.now(), app["id"]))
                self.audit.record(app["id"], "system", "PAYOUTS_RELEASED", {"reason": "new bank account verified"})
            else:
                self.audit.record(app["id"], "system", "PAYOUTS_HOLD_MAINTAINED", {
                    "bank": bank["status"], "risk": risk["status"]})

    def _start_provisioning(self, app_id: str, actor: str) -> None:
        app = self._app(app_id)
        unsatisfied = [ct for ct in self.s.required_checks(app) if C(self._check(app_id, ct)["status"]) not in SATISFIED]
        if unsatisfied:  # defence in depth - approval can never bypass configured checks
            raise PolicyViolation(f"cannot provision: checks not satisfied {unsatisfied}")
        self._transition(app_id, E.PROVISIONING_STARTED, actor)
        self._enqueue("PROVISION_MERCHANT", app_id, {})

    def _apply_result(self, app_id: str, chk: dict, result: dict, request_id: str) -> None:
        ct, outcome = chk["check_type"], result.get("outcome")
        policy = RESULT_POLICY.get((ct, outcome))
        if policy is None:
            status, issue = C.ERROR, ("EXCEPTION", "UNEXPECTED_PROVIDER_RESPONSE", None,
                                      f"{ct} provider returned unsupported outcome {outcome!r}", "HIGH")
        else:
            status, issue = policy
        self.db.execute("UPDATE checks SET status=?, reason_code=?, result_json=?, deadline_at=NULL, updated_at=? WHERE id=?",
                        (status.value, outcome, dumps(result), self.clock.now(), chk["id"]))
        if issue:
            kind, code, field, message, severity = issue
            if ct == "RISK" and outcome == "HIGH_RISK":
                message = f"Risk score {result.get('score')} ({', '.join(result.get('factors', []))}) requires underwriting review."
            self._add_issue(app_id, extraction.Issue(kind, code, message, field=field, severity=severity, source=f"check:{ct}"))
        self.audit.record(app_id, "system", "CHECK_COMPLETED", {"check": ct, "status": status.value, "outcome": outcome,
                                                                 "request_id": request_id})

    def _fail_check_unavailable(self, app_id: str, chk: dict, reason: str, message: str) -> None:
        self.db.execute("UPDATE checks SET status=?, reason_code=?, deadline_at=NULL, updated_at=? WHERE id=?",
                        (C.ERROR.value, reason, self.clock.now(), chk["id"]))
        self._add_issue(app_id, extraction.Issue("EXCEPTION", reason, message + ". Retry or replay from the dead-letter queue.",
                                                 severity="HIGH", source=f"check:{chk['check_type']}"))

    def _reconcile_checks(self, app_id: str, force: list[str] | tuple = (), dispatch: bool = True) -> None:
        """Make the set of checks match policy and current data.

        A check whose inputs changed (e.g. new bank account) is invalidated - its
        previous result and issues are discarded - and queued for re-verification.
        Checks whose inputs did not change keep their results (PAN stays PASSED when
        only the GSTIN is corrected).
        """
        app = self._app(app_id)
        values = self._values(app_id)
        required = self.s.required_checks(app)
        now = self.clock.now()
        for ct in CHECK_TYPES:
            chk = self._check(app_id, ct, create=True)
            if ct not in required:
                if chk["status"] != C.SKIPPED.value:
                    self._supersede_request(chk)
                    self._resolve_issues(app_id, "source = ?", (f"check:{ct}",), by="policy")
                    self.db.execute("UPDATE checks SET status=?, input_hash=NULL, reason_code=NULL, result_json=NULL, "
                                    "deadline_at=NULL, updated_at=? WHERE id=?", (C.SKIPPED.value, now, chk["id"]))
                continue
            h = self._input_hash(ct, app, values)
            needs_reset = (chk["input_hash"] != h or ct in force or chk["status"] == C.SKIPPED.value)
            if needs_reset:
                previous = chk["status"]
                self._supersede_request(chk)
                self._resolve_issues(app_id, "source = ?", (f"check:{ct}",), by="re-verification")
                self.db.execute("UPDATE checks SET status=?, attempt=0, request_id=NULL, input_hash=?, deadline_at=NULL, "
                                "reason_code=NULL, result_json=NULL, waiver_json=NULL, updated_at=? WHERE id=?",
                                (C.PENDING.value, h, now, chk["id"]))
                if previous in (C.PASSED.value, C.FAILED.value, C.WAIVED.value, C.ERROR.value, C.IN_FLIGHT.value):
                    self.audit.record(app_id, "system", "CHECK_INVALIDATED", {
                        "check": ct, "previous_status": previous,
                        "reason": "retry requested" if ct in force else "verified data changed"})
            if dispatch:
                chk = self._check(app_id, ct)
                if (chk["status"] in (C.PENDING.value, C.RETRY_SCHEDULED.value)
                        and not self._has_pending_dispatch(app_id, ct)):
                    self._enqueue("DISPATCH_CHECK", app_id, {"check_type": ct, "input_hash": chk["input_hash"]})

    def _revalidate(self, app_id: str) -> None:
        app = self._app(app_id)
        docs = self._active_docs(app_id)
        values = self._values(app_id)
        times = loads(app["field_updated_json"], {})
        report, issues, autofill = extraction.validate(app, values, times, docs, self.s)
        for f, v in autofill.items():
            self._set_field(app_id, f, v, source="document")
            self.audit.record(app_id, "system", "FIELD_AUTOFILLED", {"field": f, "value": mask(f, v) if f in SENSITIVE_FIELDS else v})
        self.db.execute("UPDATE applications SET field_report_json=? WHERE id=?", (dumps(self._mask_report(report)), app_id))
        if app["state"] != S.ACTIVE.value:
            fps = {f: self.db.scalar("SELECT fingerprint FROM pii_vault WHERE application_id=? AND field=?", (app_id, f))
                   for f in SENSITIVE_FIELDS}
            issues += dedupe.find_duplicates(self.db, self._app(app_id), fps)
        # Diff against currently open validation/dedupe issues
        wanted = {i.fingerprint: i for i in issues}
        open_rows = self.db.all("SELECT id, fingerprint FROM issues WHERE application_id=? AND status='OPEN' "
                                "AND source IN ('validation','dedupe')", (app_id,))
        for row in open_rows:
            if row["fingerprint"] not in wanted:
                self.db.execute("UPDATE issues SET status='RESOLVED', resolved_by='revalidation', resolved_at=? WHERE id=?",
                                (self.clock.now(), row["id"]))
        for issue in issues:
            self._add_issue(app_id, issue)

    # ================================================================== helpers: data

    def _validate_input(self, fields: dict, partial: bool = False) -> None:
        bt = fields.get("business_type")
        if bt is not None and bt not in BUSINESS_TYPES:
            raise InvalidInput(f"business_type must be one of {BUSINESS_TYPES}")
        vol = fields.get("monthly_volume_inr")
        if vol is not None:
            try:
                if float(vol) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise InvalidInput("monthly_volume_inr must be a non-negative number") from None
        unknown = set(fields) - set(COLUMN_FIELDS) - set(SENSITIVE_FIELDS)
        if unknown:
            raise InvalidInput(f"unknown fields {sorted(unknown)}")

    def _set_field(self, app_id: str, field: str, value, source: str = "form") -> None:
        now = self.clock.now()
        if field in SENSITIVE_FIELDS:
            value = extraction.normalize_value(field, value)
            if value:
                self.db.execute(
                    "INSERT INTO pii_vault VALUES (?,?,?,?,?,?,?) ON CONFLICT(application_id, field) DO UPDATE SET "
                    "ciphertext=excluded.ciphertext, masked=excluded.masked, fingerprint=excluded.fingerprint, "
                    "source=excluded.source, updated_at=excluded.updated_at",
                    (app_id, field, self.vault.encrypt(value), mask(field, value), self.vault.fingerprint(value), source, now))
            else:
                self.db.execute("DELETE FROM pii_vault WHERE application_id=? AND field=?", (app_id, field))
        elif field in COLUMN_FIELDS:
            if field == "gst_registered":
                value = 1 if value in (True, 1, "true", "True", "yes") else 0
            elif field == "ifsc":
                value = extraction.normalize_value("ifsc", value)
            elif field == "monthly_volume_inr" and value is not None:
                value = float(value)
            elif isinstance(value, str):
                value = " ".join(value.split()) or None
            self.db.execute(f"UPDATE applications SET {field}=?, updated_at=? WHERE id=?", (value, now, app_id))
        else:
            raise InvalidInput(f"unknown field {field}")
        if source == "form":
            times = loads(self.db.scalar("SELECT field_updated_json FROM applications WHERE id=?", (app_id,)), {})
            times[field] = now
            self.db.execute("UPDATE applications SET field_updated_json=? WHERE id=?", (dumps(times), app_id))

    def _values(self, app_id: str) -> dict:
        app = self._app(app_id)
        values = {k: app.get(k) for k in COLUMN_FIELDS}
        for row in self.db.all("SELECT field, ciphertext FROM pii_vault WHERE application_id=?", (app_id,)):
            values[row["field"]] = self.vault.decrypt(row["ciphertext"])
        return values

    def _ingest_document(self, app_id: str, doc: dict, actor: str) -> str:
        try:
            ex = extraction.extract(doc, self.s, transport=self.llm_transport)
        except ValueError as exc:
            raise InvalidInput(str(exc)) from None
        doc_id = new_id("DOC")
        now = self.clock.now()
        self.db.execute("UPDATE documents SET status='SUPERSEDED' WHERE application_id=? AND doc_type=? AND status='ACTIVE'",
                        (app_id, doc["doc_type"]))
        masked_fields = {k: {"value": self._mask_doc_field(k, v["value"]), "confidence": v["confidence"]}
                         for k, v in ex.fields.items()}
        self.db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            doc_id, app_id, doc["doc_type"], doc.get("filename"), doc.get("media_type"), ex.sha256, ex.method,
            "ACTIVE", int(ex.readable), dumps({**ex.quality, "warnings": ex.warnings}),
            self.vault.encrypt(dumps(ex.fields)), dumps(masked_fields), now))
        self.audit.record(app_id, actor, "DOCUMENT_RECEIVED", {
            "document": doc_id, "doc_type": doc["doc_type"], "method": ex.method, "readable": ex.readable,
            "confidence": {k: v["confidence"] for k, v in ex.fields.items()}, "warnings": ex.warnings})
        return doc_id

    def _active_docs(self, app_id: str) -> list[dict]:
        docs = []
        for d in self.db.all("SELECT * FROM documents WHERE application_id=? AND status='ACTIVE'", (app_id,)):
            q = loads(d["quality_json"], {})
            docs.append({"id": d["id"], "doc_type": d["doc_type"], "readable": bool(d["readable"]), "method": d["method"],
                         "fields": loads(self.vault.decrypt(d["extracted_enc"]), {}), "created_at": d["created_at"],
                         "warnings": q.get("warnings", [])})
        return docs

    @staticmethod
    def _mask_doc_field(name: str, value):
        if name in ("pan", "gstin"):
            return mask(name, value)
        if name == "account_number":
            return mask("bank_account", value)
        return value

    @staticmethod
    def _mask_report(report: dict) -> dict:
        out = {}
        for f, e in report.items():
            e = dict(e)
            if f in SENSITIVE_FIELDS:
                e["form_value"] = mask(f, e.get("form_value"))
                e["resolved_value"] = mask(f, e.get("resolved_value"))
                e["candidates"] = [{**c, "value": mask(f, c["value"])} for c in e["candidates"]]
            out[f] = e
        return out

    def _check_inputs(self, ct: str, app: dict, v: dict) -> dict:
        if ct == "PAN":
            return {"pan": v.get("pan"), "name": v.get("legal_name")}
        if ct == "GST":
            return {"gstin": v.get("gstin"), "legal_name": v.get("legal_name")}
        if ct == "BANK":
            return {"account_number": v.get("bank_account"), "ifsc": v.get("ifsc"),
                    "account_holder": v.get("account_holder"), "legal_name": v.get("legal_name")}
        # Data minimisation: the risk engine gets fingerprints, never raw identifiers.
        return {"legal_name": v.get("legal_name"), "business_name": v.get("business_name"),
                "category": v.get("category"), "monthly_volume_inr": v.get("monthly_volume_inr"),
                "city": v.get("city"),
                "pan_fp": self.vault.fingerprint(v["pan"]) if v.get("pan") else None,
                "bank_fp": self.vault.fingerprint(v["bank_account"]) if v.get("bank_account") else None,
                "bank_changed_after_activation": bool(app.get("payouts_on_hold"))}

    def _input_hash(self, ct: str, app: dict, values: dict) -> str:
        inputs = self._check_inputs(ct, app, values)
        inputs.pop("bank_changed_after_activation", None)   # context flag, not verified data
        return self.vault.fingerprint(ct + dumps(inputs))

    # ================================================================== helpers: state & records

    def _app(self, app_id: str) -> dict:
        app = self.db.one("SELECT * FROM applications WHERE id=?", (app_id,))
        if not app:
            raise NotFound(app_id)
        return app

    def _check(self, app_id: str, ct: str, create: bool = False) -> dict:
        chk = self.db.one("SELECT * FROM checks WHERE application_id=? AND check_type=?", (app_id, ct))
        if chk is None and create:
            self.db.execute("INSERT INTO checks(id, application_id, check_type, status, updated_at) VALUES (?,?,?,?,?)",
                            (new_id("CHK"), app_id, ct, C.PENDING.value, self.clock.now()))
            chk = self.db.one("SELECT * FROM checks WHERE application_id=? AND check_type=?", (app_id, ct))
        if chk is None:
            return {"status": C.PENDING.value, "reason_code": None, "result_json": None, "check_type": ct}
        return chk

    def _transition(self, app_id: str, event: Event, actor: str) -> None:
        app = self._app(app_id)
        frm = S(app["state"])
        to = next_state(frm, event)
        now = self.clock.now()
        self.db.execute("UPDATE applications SET state=?, state_entered_at=?, updated_at=? WHERE id=?", (to.value, now, now, app_id))
        self.db.execute("INSERT INTO state_history(application_id, from_state, to_state, event, at) VALUES (?,?,?,?,?)",
                        (app_id, frm.value, to.value, event.value, now))
        self.audit.record(app_id, actor, "STATE_CHANGED", {"from": frm.value, "to": to.value, "event": event.value})
        if frm == S.MANUAL_REVIEW:
            self.db.execute("UPDATE review_tasks SET status='SUPERSEDED', decided_at=? WHERE application_id=? AND status='OPEN'",
                            (now, app_id))
        if to == S.MANUAL_REVIEW:
            self._create_review_task(app_id)

    def _create_review_task(self, app_id: str) -> None:
        issues = self._open_issues(app_id, "EXCEPTION")
        failed = [{"check": c["check_type"], "status": c["status"], "reason": c["reason_code"]}
                  for c in self.db.all("SELECT * FROM checks WHERE application_id=? AND status IN ('FAILED','ERROR')", (app_id,))]
        reasons = {"issues": [{"code": i["code"], "message": i["message"], "severity": i["severity"], "source": i["source"]}
                              for i in issues], "failed_checks": failed}
        high = any(i["severity"] == "HIGH" for i in issues)
        now = self.clock.now()
        self.db.execute("INSERT INTO review_tasks(id, application_id, reasons_json, priority, sla_due_at, status, created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (new_id("REV"), app_id, dumps(reasons), "HIGH" if high else "MEDIUM",
                         now + (4 if high else 24) * 3600, "OPEN", now))

    def _close_task(self, task_id: str, decision: str, reviewer: str, note: str) -> None:
        self.db.execute("UPDATE review_tasks SET status='DONE', decision=?, reviewer=?, note=?, decided_at=? WHERE id=?",
                        (decision, reviewer, note, self.clock.now(), task_id))

    def _add_issue(self, app_id: str, issue: extraction.Issue) -> None:
        fp = issue.fingerprint
        if self.db.one("SELECT 1 FROM issues WHERE application_id=? AND fingerprint=? AND "
                       "(status='OPEN' OR (status='RESOLVED' AND resolved_by LIKE 'reviewer:%' AND source IN ('validation','dedupe')))",
                       (app_id, fp)):
            return  # already open, or a reviewer explicitly accepted this exact finding
        self.db.execute("INSERT INTO issues VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            new_id("ISS"), app_id, issue.kind, issue.code, issue.field, issue.message, issue.severity, issue.source,
            fp, "OPEN", None, self.clock.now(), None))

    def _resolve_issues(self, app_id: str, where: str, params: tuple = (), by: str = "system") -> None:
        self.db.execute(f"UPDATE issues SET status='RESOLVED', resolved_by=?, resolved_at=? "
                        f"WHERE application_id=? AND status='OPEN' AND ({where})",
                        (by, self.clock.now(), app_id, *params))

    def _open_issues(self, app_id: str, kind: str | None = None) -> list[dict]:
        if kind:
            return self.db.all("SELECT * FROM issues WHERE application_id=? AND status='OPEN' AND kind=? ORDER BY created_at",
                               (app_id, kind))
        return self.db.all("SELECT * FROM issues WHERE application_id=? AND status='OPEN' ORDER BY created_at", (app_id,))

    def _supersede_request(self, chk: dict) -> None:
        if chk.get("request_id"):
            self.db.execute("UPDATE check_requests SET status='SUPERSEDED' WHERE request_id=? AND status IN ('SENT','TIMED_OUT')",
                            (chk["request_id"],))

    def _has_pending_dispatch(self, app_id: str, ct: str) -> bool:
        return bool(self.db.one("SELECT 1 FROM jobs WHERE kind='DISPATCH_CHECK' AND application_id=? AND status='PENDING' "
                                "AND json_extract(payload_json, '$.check_type') = ?", (app_id, ct)))

    def _enqueue(self, kind: str, app_id: str, payload: dict, delay: float = 0.0) -> str:
        job_id = new_id("JOB")
        now = self.clock.now()
        self.db.execute("INSERT INTO jobs(id, kind, application_id, payload_json, run_at, status, created_at) VALUES (?,?,?,?,?,?,?)",
                        (job_id, kind, app_id, dumps(payload), now + delay, "PENDING", now))
        return job_id

    def _dead_letter(self, kind: str, app_id: str, payload: dict, error: str) -> None:
        self.db.execute("INSERT INTO dead_letters VALUES (?,?,?,?,?,?,?,?)",
                        (new_id("DLQ"), kind, app_id, dumps(payload), error, "OPEN", self.clock.now(), None))

    def _record_inbox(self, cb_id, provider, request_id, app_id, outcome, payload) -> None:
        self.db.execute("INSERT INTO callback_inbox VALUES (?,?,?,?,?,?,?,?)",
                        (cb_id, provider, request_id, app_id, outcome, 0, dumps(payload), self.clock.now()))

    def _backoff(self, attempt: int) -> float:
        delay = min(self.s.backoff_cap_s, self.s.backoff_base_s * (2 ** max(attempt - 1, 0)))
        if self.s.jitter:
            delay *= self.rng.uniform(0.8, 1.2)
        return delay

    # ================================================================== read models

    def get_application(self, app_id: str) -> dict:
        app = self._app(app_id)
        now = self.clock.now()
        pii = {r["field"]: {"masked": r["masked"], "source": r["source"]}
               for r in self.db.all("SELECT field, masked, source FROM pii_vault WHERE application_id=?", (app_id,))}
        checks = []
        for c in self.db.all("SELECT * FROM checks WHERE application_id=? ORDER BY check_type", (app_id,)):
            c["result"] = loads(c.pop("result_json"), None)
            c["waiver"] = loads(c.pop("waiver_json"), None)
            checks.append(c)
        order = {ct: i for i, ct in enumerate(CHECK_TYPES)}
        checks.sort(key=lambda c: order.get(c["check_type"], 9))
        docs = []
        for d in self.db.all("SELECT * FROM documents WHERE application_id=? ORDER BY created_at DESC", (app_id,)):
            d.pop("extracted_enc")
            d["fields"] = loads(d.pop("extracted_masked_json"), {})
            d["quality"] = loads(d.pop("quality_json"), {})
            docs.append(d)
        history = self.db.all("SELECT * FROM state_history WHERE application_id=? ORDER BY id", (app_id,))
        for i, h in enumerate(history):
            end = history[i + 1]["at"] if i + 1 < len(history) else now
            h["duration_s"] = round(end - h["at"], 2)
        tasks = self.db.all("SELECT * FROM review_tasks WHERE application_id=? ORDER BY created_at DESC", (app_id,))
        for t in tasks:
            t["reasons"] = loads(t.pop("reasons_json"), {})
        audit = self.db.all("SELECT seq, actor, action, details_json, at, hash FROM audit_log WHERE application_id=? ORDER BY seq",
                            (app_id,))
        for a in audit:
            a["details"] = loads(a.pop("details_json"), {})
        callbacks = self.db.all("SELECT callback_id, provider, request_id, outcome, duplicate_count, received_at "
                                "FROM callback_inbox WHERE application_id=? ORDER BY received_at", (app_id,))
        return {
            "id": app["id"], "state": app["state"], "state_entered_at": app["state_entered_at"],
            "time_in_state_s": round(now - app["state_entered_at"], 2),
            "created_at": app["created_at"], "updated_at": app["updated_at"],
            "business": {k: app[k] for k in ("legal_name", "business_name", "business_type", "category",
                                              "monthly_volume_inr", "city")},
            "gst_registered": bool(app["gst_registered"]),
            "contact": {"email": app["email"], "phone": mask("phone", app["phone"]) if app["phone"] else None},
            "identifiers": pii,
            "bank": {"account": pii.get("bank_account", {}).get("masked"), "ifsc": app["ifsc"], "account_holder": app["account_holder"]},
            "sandbox_scenario": app["sandbox_scenario"],
            "merchant_id": app["merchant_id"], "capabilities": loads(app["capabilities_json"], None),
            "payouts_on_hold": bool(app["payouts_on_hold"]),
            "required_checks": self.s.required_checks(app),
            "field_report": loads(app["field_report_json"], {}),
            "documents": docs, "checks": checks,
            "check_requests": self.db.all("SELECT request_id, check_type, attempt, status, sent_at, deadline_at, responded_at "
                                          "FROM check_requests WHERE application_id=? ORDER BY sent_at", (app_id,)),
            "callbacks": callbacks,
            "missing_info": self._open_issues(app_id, "MISSING_INFO"),
            "exceptions": self._open_issues(app_id, "EXCEPTION"),
            "resolved_issues": self.db.all("SELECT * FROM issues WHERE application_id=? AND status='RESOLVED' ORDER BY resolved_at",
                                           (app_id,)),
            "review_tasks": tasks,
            "pending_jobs": self.db.all("SELECT id, kind, payload_json, run_at, attempts, last_error FROM jobs "
                                        "WHERE application_id=? AND status='PENDING'", (app_id,)),
            "dead_letters": self.db.all("SELECT * FROM dead_letters WHERE application_id=? ORDER BY created_at", (app_id,)),
            "history": history, "audit": audit,
        }

    def list_applications(self, state: str | None = None) -> list[dict]:
        sql = ("SELECT a.id, a.state, a.legal_name, a.business_type, a.category, a.merchant_id, a.payouts_on_hold, "
               "a.created_at, a.state_entered_at, "
               "(SELECT COUNT(*) FROM issues i WHERE i.application_id=a.id AND i.status='OPEN' AND i.kind='MISSING_INFO') AS missing_count, "
               "(SELECT COUNT(*) FROM issues i WHERE i.application_id=a.id AND i.status='OPEN' AND i.kind='EXCEPTION') AS exception_count "
               "FROM applications a")
        params: tuple = ()
        if state:
            sql += " WHERE a.state=?"
            params = (state,)
        rows = self.db.all(sql + " ORDER BY a.created_at DESC", params)
        now = self.clock.now()
        for r in rows:
            r["time_in_state_s"] = round(now - r["state_entered_at"], 2)
        return rows

    def review_queue(self) -> list[dict]:
        rows = self.db.all("SELECT t.*, a.legal_name, a.state FROM review_tasks t JOIN applications a ON a.id=t.application_id "
                           "WHERE t.status='OPEN' ORDER BY CASE t.priority WHEN 'HIGH' THEN 0 ELSE 1 END, t.created_at")
        now = self.clock.now()
        for r in rows:
            r["reasons"] = loads(r.pop("reasons_json"), {})
            r["sla_breached"] = now > r["sla_due_at"]
        return rows

    def dead_letters(self, status: str | None = "OPEN") -> list[dict]:
        if status:
            return self.db.all("SELECT * FROM dead_letters WHERE status=? ORDER BY created_at DESC", (status,))
        return self.db.all("SELECT * FROM dead_letters ORDER BY created_at DESC")
