"""End-to-end workflow tests: happy path plus every failure scenario in the brief."""
import pytest

from onboarding.engine import PolicyViolation
from onboarding.samples import (SCENARIOS, cheque_text, documents_for, make_account, make_application,
                                make_pan, pan_card_text)
from onboarding.security import sign


def submit(h, form=None, docs=None, **kw):
    form = form or make_application(rng=h.rng, **kw)
    return h.engine.submit_application(form, docs if docs is not None else documents_for(form)), form


def requests_for(h, app_id, ct):
    return [r for r in h.app(app_id)["check_requests"] if r["check_type"] == ct]


# --------------------------------------------------------------------- happy path

def test_happy_path_submission_to_merchant_id(h):
    app_id, _ = submit(h)
    a = h.app(app_id)
    assert a["state"] == "VERIFYING"
    assert all(f["status"] == "VERIFIED_BY_DOCUMENT" for f in a["field_report"].values())
    h.settle(30)
    a = h.app(app_id)
    assert a["state"] == "ACTIVE"
    assert a["merchant_id"].startswith("PLM")
    assert [x["to_state"] for x in a["history"]] == ["SUBMITTED", "VERIFYING", "APPROVED", "PROVISIONING", "ACTIVE"]
    assert {c["check_type"]: c["status"] for c in a["checks"]} == {"PAN": "PASSED", "GST": "PASSED", "BANK": "PASSED", "RISK": "PASSED"}
    assert all(cb["outcome"] == "APPLIED" for cb in a["callbacks"])
    assert h.engine.audit.verify()["valid"]


def test_not_gst_registered_skips_gst(h):
    app_id, _ = submit(h, gst_registered=False, business_type="PROPRIETORSHIP")
    h.settle(30)
    assert h.app(app_id)["state"] == "ACTIVE"
    assert h.check(app_id, "GST")["status"] == "SKIPPED"
    assert requests_for(h, app_id, "GST") == []


# --------------------------------------------------------------------- 1. PAN ok, GST fails

def test_pan_passes_gst_fails_then_only_gst_is_reverified(h):
    app_id, form = submit(h, gst_entity="9")
    h.settle(30)
    a = h.app(app_id)
    assert a["state"] == "AWAITING_INFO"
    assert h.check(app_id, "PAN")["status"] == "PASSED"
    assert h.check(app_id, "GST")["reason_code"] == "CANCELLED"
    assert [i["code"] for i in a["missing_info"]] == ["GST_NOT_ACTIVE"]

    SCENARIOS["gst_fails"].follow_up(h.engine, app_id)   # merchant supplies an active GSTIN + certificate
    h.settle(30)
    assert h.app(app_id)["state"] == "ACTIVE"
    assert len(requests_for(h, app_id, "GST")) == 2
    assert len(requests_for(h, app_id, "PAN")) == 1        # PAN result reused, not re-verified


def test_merchant_can_resolve_gst_failure_by_declaring_not_registered(h):
    app_id, _ = submit(h, gst_entity="9", business_type="PRIVATE_LIMITED")
    h.settle(30)
    h.engine.provide_info(app_id, {"gst_registered": False})
    h.settle(30)
    assert h.app(app_id)["state"] == "ACTIVE"
    assert h.check(app_id, "GST")["status"] == "SKIPPED"


# --------------------------------------------------------------------- 2. duplicate callbacks

def test_duplicate_callbacks_are_idempotent(h):
    h.engine.chaos.add("PAN", times=1, duplicates=2)
    app_id, _ = submit(h)
    h.settle(30)
    a = h.app(app_id)
    pan_cb = [c for c in a["callbacks"] if c["provider"] == "PAN"]
    assert len(pan_cb) == 1 and pan_cb[0]["duplicate_count"] == 2
    completed = [e for e in a["audit"] if e["action"] == "CHECK_COMPLETED" and e["details"]["check"] == "PAN"]
    assert len(completed) == 1
    assert h.actions(app_id).count("CALLBACK_DUPLICATE_IGNORED") == 2
    assert a["state"] == "ACTIVE"


def test_redelivery_through_webhook_endpoint_is_ignored(h):
    app_id, _ = submit(h)
    h.settle(30)
    cb = h.engine.providers.recent()[0]
    status, body = h.engine.redeliver_callback(cb["payload"]["callback_id"])
    assert status == 200 and body["outcome"] == "DUPLICATE"
    assert h.app(app_id)["state"] == "ACTIVE"


# --------------------------------------------------------------------- 3. callback after timeout

def test_late_callback_after_timeout_is_reconciled(h):
    slow = h.settings.check_timeout_s + 4
    h.engine.chaos.add("BANK", times=2, latency_s=slow)
    app_id, _ = submit(h)
    h.settle(h.settings.check_timeout_s + 1)
    assert "CHECK_TIMED_OUT" in h.actions(app_id)
    h.settle(60)
    a = h.app(app_id)
    bank_cbs = [c["outcome"] for c in a["callbacks"] if c["provider"] == "BANK"]
    assert bank_cbs == ["LATE_APPLIED", "STALE_IGNORED"]
    reqs = requests_for(h, app_id, "BANK")
    assert [r["status"] for r in reqs] == ["RESPONDED", "SUPERSEDED"]
    assert a["state"] == "ACTIVE"


def test_provider_that_never_answers_exhausts_retries_to_review(h):
    h.engine.chaos.add("RISK", times=None, drop=True)
    app_id, _ = submit(h)
    h.settle(120)
    a = h.app(app_id)
    assert a["state"] == "MANUAL_REVIEW"
    assert h.check(app_id, "RISK")["status"] == "ERROR"
    assert len(requests_for(h, app_id, "RISK")) == h.settings.max_check_attempts
    assert any(d["kind"] == "CHECK_TIMEOUT" for d in a["dead_letters"])


def test_late_callback_for_changed_data_is_ignored(h):
    h.engine.chaos.add("BANK", times=1, latency_s=20)
    h.engine.chaos.add("BANK", times=1, drop=True)
    app_id, form = submit(h)
    h.settle(5)
    new_acct = make_account(rng=h.rng)
    holder = form["bank"]["account_holder"]
    h.engine.update_bank_details(app_id, {"account_number": new_acct, "ifsc": "ICIC0004321", "account_holder": holder},
                                 {"doc_type": "CANCELLED_CHEQUE", "text": cheque_text(new_acct, "ICIC0004321", holder)})
    h.settle(60)
    outcomes = {c["request_id"]: c["outcome"] for c in h.app(app_id)["callbacks"] if c["provider"] == "BANK"}
    assert "STALE_IGNORED" in outcomes.values()      # answer about the old account is not applied
    assert h.app(app_id)["state"] == "ACTIVE"


# --------------------------------------------------------------------- 4. bank details change after verification

def test_bank_change_after_activation_holds_payouts_until_reverified(h):
    app_id, _ = submit(h)
    h.settle(30)
    mid = h.app(app_id)["merchant_id"]
    SCENARIOS["bank_change_after_verification"].follow_up(h.engine, app_id)
    a = h.app(app_id)
    assert a["payouts_on_hold"] is True
    assert a["state"] == "ACTIVE"
    assert {c["check_type"]: c["status"] for c in a["checks"]}["BANK"] == "PENDING"
    h.settle(30)
    a = h.app(app_id)
    assert a["payouts_on_hold"] is False
    assert a["merchant_id"] == mid
    assert len(requests_for(h, app_id, "BANK")) == 2 and len(requests_for(h, app_id, "RISK")) == 2
    assert len(requests_for(h, app_id, "PAN")) == 1
    assert "PAYOUTS_RELEASED" in h.actions(app_id)


def test_bank_change_after_activation_to_bad_account_keeps_hold(h):
    app_id, form = submit(h)
    h.settle(30)
    h.engine.update_bank_details(app_id, {"account_number": make_account("0000", h.rng), "ifsc": "HDFC0001234",
                                          "account_holder": form["bank"]["account_holder"]})
    h.settle(30)
    a = h.app(app_id)
    assert a["payouts_on_hold"] is True
    assert [i["code"] for i in a["missing_info"]] == ["BANK_ACCOUNT_INVALID"]


def test_bank_change_before_activation_requires_fresh_proof(h):
    h.engine.chaos.add("RISK", times=1, latency_s=20)       # keep the application in VERIFYING for a while
    app_id, form = submit(h)
    h.settle(5)
    assert h.check(app_id, "BANK")["status"] == "PASSED"
    h.engine.update_bank_details(app_id, {"account_number": make_account(rng=h.rng), "ifsc": "HDFC0001234",
                                          "account_holder": form["bank"]["account_holder"]})
    a = h.app(app_id)
    assert a["state"] == "AWAITING_INFO"
    assert [i["code"] for i in a["missing_info"]] == ["DOC_OUTDATED"]


def test_bank_change_blocked_during_provisioning(h):
    h.engine.chaos.add("MID", times=None, fail=True)
    app_id, form = submit(h)
    h.settle(12)
    assert h.app(app_id)["state"] == "PROVISIONING"
    with pytest.raises(PolicyViolation):
        h.engine.update_bank_details(app_id, {"account_number": make_account(rng=h.rng), "ifsc": "HDFC0001234",
                                              "account_holder": form["bank"]["account_holder"]})


# --------------------------------------------------------------------- 5. approval ok, merchant-id creation fails

def test_mid_failures_are_retried_idempotently(h):
    h.engine.chaos.add("MID", times=2, fail=True)
    h.engine.chaos.add("MID", times=1, lose_response=True)   # created server-side, response lost
    app_id, _ = submit(h)
    h.settle(90)
    a = h.app(app_id)
    assert a["state"] == "ACTIVE"
    assert h.engine.mid_service.calls == 4
    assert len(h.engine.mid_service.merchants) == 1           # no duplicate merchant created
    created = [e for e in a["audit"] if e["action"] == "MERCHANT_ID_CREATED"][0]
    assert created["details"]["idempotent_replay"] is True
    assert h.actions(app_id).count("JOB_RETRY_SCHEDULED") == 3


def test_mid_permanent_failure_goes_to_dlq_and_replays(h):
    h.engine.chaos.add("MID", times=h.settings.max_job_attempts, fail=True)
    app_id, _ = submit(h)
    h.settle(120)
    a = h.app(app_id)
    assert a["state"] == "PROVISIONING_FAILED"
    assert [i["code"] for i in a["exceptions"]] == ["PROVISIONING_FAILED"]
    dl = [d for d in h.engine.dead_letters() if d["application_id"] == app_id]
    assert len(dl) == 1 and dl[0]["kind"] == "PROVISION_MERCHANT"
    h.engine.replay_dead_letter(dl[0]["id"])
    h.settle(10)
    assert h.app(app_id)["state"] == "ACTIVE"
    with pytest.raises(PolicyViolation):
        h.engine.replay_dead_letter(dl[0]["id"])


# --------------------------------------------------------------------- 6. blurred / conflicting documents

def test_blurred_document_requests_reupload_without_calling_providers(h):
    app_id = SCENARIOS["blurred_document"].setup(h.engine, h.rng)
    h.settle(20)
    a = h.app(app_id)
    assert a["state"] == "AWAITING_INFO"
    assert [i["code"] for i in a["missing_info"]] == ["DOC_UNREADABLE"]
    assert a["check_requests"] == []                    # no paid verification calls on incomplete data
    SCENARIOS["blurred_document"].follow_up(h.engine, app_id)
    h.settle(30)
    assert h.app(app_id)["state"] == "ACTIVE"


def test_document_conflict_goes_to_review_and_reviewer_can_request_info(h):
    form = make_application(rng=h.rng)
    wrong = make_pan("C", rng=h.rng)
    app_id = h.engine.submit_application(form, documents_for(form, pan=wrong))
    h.settle(30)
    a = h.app(app_id)
    assert a["state"] == "MANUAL_REVIEW"
    assert a["field_report"]["pan"]["status"] == "CONFLICT"
    task = h.engine.review_queue()[0]
    assert task["priority"] == "HIGH"
    h.engine.review_decision(task["id"], "REQUEST_INFO", "reviewer@example.com",
                             info_request={"message": "Upload the PAN card that matches the application", "field": "PAN_CARD"})
    assert h.app(app_id)["state"] == "AWAITING_INFO"
    h.engine.add_document(app_id, {"doc_type": "PAN_CARD", "text": pan_card_text(form["pan"], form["legal_name"])})
    h.settle(30)
    a = h.app(app_id)
    assert a["state"] == "ACTIVE"
    assert len(requests_for(h, app_id, "PAN")) == 1


def test_pan_entity_type_must_match_business_type(h):
    form = make_application(business_type="PROPRIETORSHIP", gst_registered=False, rng=h.rng)
    form["pan"] = make_pan("C", rng=h.rng)
    app_id = h.engine.submit_application(form, documents_for(form))
    h.settle(30)
    a = h.app(app_id)
    assert a["state"] == "MANUAL_REVIEW"
    assert "PAN_ENTITY_TYPE_MISMATCH" in [i["code"] for i in a["exceptions"]]


def test_missing_documents_and_fields_are_requested(h):
    form = make_application(rng=h.rng)
    del form["gstin"]
    docs = [d for d in documents_for(form, gstin="27AAAAA0000A1Z5") if d["doc_type"] != "GST_CERTIFICATE"]
    app_id = h.engine.submit_application(form, docs)
    codes = sorted(i["code"] for i in h.app(app_id)["missing_info"])
    assert h.app(app_id)["state"] == "AWAITING_INFO"
    assert codes == ["DOC_MISSING"]


def test_field_is_autofilled_from_document(h):
    form = make_application(rng=h.rng)
    gstin = form.pop("gstin")
    app_id = h.engine.submit_application(form, documents_for(form, gstin=gstin))
    a = h.app(app_id)
    assert a["field_report"]["gstin"]["status"] == "EXTRACTED"
    assert a["identifiers"]["gstin"]["source"] == "document"
    h.settle(30)
    assert h.app(app_id)["state"] == "ACTIVE"


# --------------------------------------------------------------------- review & policy guards

def test_approval_cannot_bypass_unverified_check(h):
    h.engine.chaos.add("GST", times=h.settings.max_job_attempts, outage=True)
    app_id, _ = submit(h)
    h.settle(90)
    assert h.app(app_id)["state"] == "MANUAL_REVIEW"
    task = h.engine.review_queue()[0]
    with pytest.raises(PolicyViolation, match="GST check is ERROR"):
        h.engine.review_decision(task["id"], "APPROVE", "reviewer@example.com", "looks fine")
    h.engine.review_decision(task["id"], "RETRY_CHECKS", "reviewer@example.com", "provider recovered", check_types=["GST"])
    h.settle(30)
    assert h.app(app_id)["state"] == "ACTIVE"
    assert h.engine.dead_letters() == []


def test_high_risk_is_waived_only_with_justification(h):
    app_id, _ = submit(h, category="GAMING", monthly_volume_inr=60_000_000)
    h.settle(30)
    task = h.engine.review_queue()[0]
    with pytest.raises(Exception, match="justification"):
        h.engine.review_decision(task["id"], "APPROVE", "analyst@example.com", "")
    h.engine.review_decision(task["id"], "APPROVE", "analyst@example.com", "Licensed operator; rolling reserve agreed")
    h.settle(10)
    risk = h.check(app_id, "RISK")
    assert risk["status"] == "WAIVED" and risk["waiver"]["reviewer"] == "analyst@example.com"
    assert h.app(app_id)["state"] == "ACTIVE"
    assert "INTERNATIONAL_CARDS" not in h.app(app_id)["capabilities"]   # high-risk tier gets fewer capabilities


def test_non_waivable_failure_blocks_approval(h):
    form = make_application(rng=h.rng)
    wrong = make_pan("C", rng=h.rng)
    app_id = h.engine.submit_application(form, documents_for(form, pan=wrong))
    h.settle(30)
    task = h.engine.review_queue()[0]
    h.engine.review_decision(task["id"], "REJECT", "reviewer@example.com", "Identity documents inconsistent")
    assert h.app(app_id)["state"] == "REJECTED"


def test_watchlist_hit_auto_rejects(h):
    app_id = SCENARIOS["watchlist_block"].setup(h.engine, h.rng)
    h.settle(30)
    assert h.app(app_id)["state"] == "REJECTED"
    assert h.engine.review_queue() == []


def test_duplicate_application_detected(h):
    form = make_application(rng=h.rng)
    first = h.engine.submit_application(form, documents_for(form))
    second = h.engine.submit_application(form, documents_for(form))
    h.settle(30)
    assert h.app(first)["state"] == "ACTIVE"
    a = h.app(second)
    assert a["state"] == "MANUAL_REVIEW"
    assert "DUPLICATE_APPLICATION" in [i["code"] for i in a["exceptions"]]


def test_shared_bank_account_across_entities_is_flagged(h):
    f1 = make_application(rng=h.rng)
    h.engine.submit_application(f1, documents_for(f1))
    f2 = make_application(name="Unrelated Traders", rng=h.rng)
    f2["bank"]["account_number"] = f1["bank"]["account_number"]
    app2 = h.engine.submit_application(f2, documents_for(f2))
    assert "SHARED_BANK_ACCOUNT" in [i["code"] for i in h.app(app2)["exceptions"]]


# --------------------------------------------------------------------- security & durability

def test_webhook_signature_is_enforced(h):
    app_id, _ = submit(h)
    status, body = h.engine.receive_callback("PAN", b'{"callback_id":"x","request_id":"y"}', "bad")
    assert status == 401
    body_bytes = b'{"callback_id":"CB-1","request_id":"REQ-NOPE","result":{}}'
    status, body = h.engine.receive_callback("PAN", body_bytes, sign(h.settings.webhook_secrets["PAN"], body_bytes))
    assert status == 202 and body["outcome"] == "UNKNOWN_REQUEST"


def test_sensitive_identifiers_never_stored_or_returned_in_clear(h):
    app_id, form = submit(h)
    h.settle(30)
    raw = form["pan"], form["gstin"], form["bank"]["account_number"]
    dump = "\n".join(h.engine.db.conn.iterdump())
    for value in raw:
        assert value not in dump
    api_view = str(h.app(app_id))
    for value in raw:
        assert value not in api_view


def test_audit_chain_detects_tampering(h):
    app_id, _ = submit(h)
    h.settle(30)
    assert h.engine.audit.verify()["valid"]
    h.engine.db.execute("UPDATE audit_log SET details_json='{}' WHERE seq=3")
    result = h.engine.audit.verify()
    assert result["valid"] is False and result["broken_at_seq"] == 3


def test_workflow_survives_restart(tmp_path):
    from conftest import Harness
    from onboarding.db import Database
    from onboarding.engine import Engine

    path = str(tmp_path / "durable.db")
    h1 = Harness(path)
    form = make_application(rng=h1.rng)
    app_id = h1.engine.submit_application(form, documents_for(form))
    h1.settle(0.5)                                  # checks dispatched, callbacks not yet delivered
    assert h1.app(app_id)["state"] == "VERIFYING"

    # "Crash": new engine on the same database. The simulator's pending callbacks are lost,
    # so the platform must recover through its own timeouts and retries.
    engine2 = Engine(h1.settings, h1.clock, Database(path))
    h1.engine = engine2
    h1.settle(60)
    assert h1.app(app_id)["state"] == "ACTIVE"
    assert "CHECK_TIMED_OUT" in h1.actions(app_id)
