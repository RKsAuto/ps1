"""Synthetic sample applications, documents and named demo scenarios.

All identifiers are randomly generated sandbox values - none belong to real
people or businesses. Document images are rendered on the fly and watermarked
"SPECIMEN".
"""
from __future__ import annotations

import io
import random
import string
from dataclasses import dataclass
from typing import Callable

from .extraction import make_gstin

_U = string.ascii_uppercase


def _rng(rng):
    return rng or random.Random()


def make_pan(entity: str = "C", digits: str | None = None, rng=None) -> str:
    r = _rng(rng)
    if digits is None:
        digits = str(r.randint(2000, 9899))
    return "".join(r.choice(_U) for _ in range(3)) + entity + r.choice(_U) + digits + r.choice(_U)


def make_account(ending: str | None = None, rng=None) -> str:
    r = _rng(rng)
    acct = "50" + "".join(str(r.randint(0, 9)) for _ in range(12))
    if ending:
        return acct[: -len(ending)] + ending
    if acct.endswith(("0000", "1111")):
        acct = acct[:-1] + "7"
    return acct


ENTITY = {"PROPRIETORSHIP": "P", "PARTNERSHIP": "F", "LLP": "F", "PRIVATE_LIMITED": "C", "PUBLIC_LIMITED": "C"}
NAMES = ["Sharma Electronics", "Lakshmi Textiles", "Green Leaf Organics", "Bombay Chai House", "Kaveri Hardware",
         "Neelgiri Pharma Distributors", "Urban Threads", "Saffron Kitchen", "Patel Auto Spares", "Sunrise Stationers"]


def make_application(name: str | None = None, business_type: str = "PRIVATE_LIMITED", category: str = "ELECTRONICS",
                     monthly_volume_inr: float = 2_500_000, gst_registered: bool = True, pan_digits: str | None = None,
                     gst_entity: str = "1", account_ending: str | None = None, rng=None) -> dict:
    r = _rng(rng)
    trade = name or r.choice(NAMES)
    suffix = {"PRIVATE_LIMITED": " Private Limited", "PUBLIC_LIMITED": " Limited", "LLP": " LLP"}.get(business_type, "")
    legal = trade + suffix
    pan = make_pan(ENTITY[business_type], pan_digits, r)
    slug = "".join(ch for ch in trade.lower() if ch.isalnum())[:12]
    form = {
        "legal_name": legal, "business_name": trade, "business_type": business_type, "category": category,
        "monthly_volume_inr": monthly_volume_inr, "gst_registered": gst_registered, "pan": pan,
        "bank": {"account_number": make_account(account_ending, r), "ifsc": "HDFC0001234", "account_holder": legal},
        "contact": {"email": f"accounts@{slug}{r.randint(10, 99)}.example", "phone": f"+9198{r.randint(10000000, 99999999)}"},
        "address": {"city": "Mumbai", "state": "Maharashtra", "pincode": "400001"},
    }
    if gst_registered:
        form["gstin"] = make_gstin("27", pan, gst_entity)
    return form


def pan_card_text(pan: str, name: str) -> str:
    return (f"INCOME TAX DEPARTMENT    GOVT. OF INDIA\nPermanent Account Number Card\n{pan}\n"
            f"Name: {name}\nDate of Incorporation/Formation: 12/03/2016\nSPECIMEN - SANDBOX DOCUMENT\n")


def gst_certificate_text(gstin: str, legal: str, trade: str) -> str:
    return (f"Government of India\nForm GST REG-06\nRegistration Certificate\nRegistration Number: {gstin}\n"
            f"1. Legal Name: {legal}\n2. Trade Name, if any: {trade}\n3. Constitution of Business: Private Limited Company\n"
            "SPECIMEN - SANDBOX DOCUMENT\n")


def cheque_text(account: str, ifsc: str, holder: str) -> str:
    return (f"HDFC BANK LTD\nBranch: Fort, Mumbai    IFSC: {ifsc}\nAccount Holder: {holder}\n"
            f"A/C No: {account}\n*** CANCELLED ***\nSPECIMEN - SANDBOX DOCUMENT\n")


def documents_for(form: dict, **overrides) -> list[dict]:
    """Text 'OCR output' documents consistent with the form, unless overridden."""
    pan = overrides.get("pan", form["pan"])
    legal = overrides.get("legal_name", form["legal_name"])
    acct = overrides.get("account_number", form["bank"]["account_number"])
    docs = [{"doc_type": "PAN_CARD", "filename": "pan_card.txt", "text": pan_card_text(pan, legal)},
            {"doc_type": "CANCELLED_CHEQUE", "filename": "cancelled_cheque.txt",
             "text": cheque_text(acct, form["bank"]["ifsc"], form["bank"]["account_holder"])}]
    if form.get("gst_registered"):
        docs.insert(1, {"doc_type": "GST_CERTIFICATE", "filename": "gst_certificate.txt",
                        "text": gst_certificate_text(overrides.get("gstin") or form.get("gstin", ""), legal, form["business_name"])})
    return docs


def render_document_png(lines: list[str], blur: float = 0.0) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    img = Image.new("RGB", (900, 520), (246, 243, 236))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=30)
        small = ImageFont.load_default(size=22)
    except TypeError:  # very old Pillow
        font = small = ImageFont.load_default()
    draw.rectangle([10, 10, 890, 510], outline=(60, 60, 60), width=3)
    y = 40
    for i, line in enumerate(lines):
        draw.text((40, y), line, fill=(20, 20, 20), font=font if i else small)
        y += 52
    draw.text((560, 460), "SPECIMEN", fill=(200, 60, 60), font=font)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def pan_card_image(pan: str, name: str, blur: float = 0.0) -> dict:
    png = render_document_png(["INCOME TAX DEPARTMENT - GOVT. OF INDIA", "Permanent Account Number Card",
                               pan, f"Name: {name}", "Date of Incorporation: 12/03/2016"], blur)
    return {"doc_type": "PAN_CARD", "filename": "pan_card_photo.png", "media_type": "image/png", "content": png}


# ------------------------------------------------------------------------- scenarios

@dataclass
class Scenario:
    name: str
    title: str
    demonstrates: str
    setup: Callable
    follow_up: Callable | None = None
    follow_up_label: str | None = None


def _submit(engine, form, docs, name, chaos: list[dict] | None = None):
    """Submit with fault rules scoped to this application only, so scenarios can run concurrently."""
    from .engine import new_id

    app_id = new_id("APP")
    for rule in chaos or []:
        engine.chaos.add(application_id=app_id, **rule)
    engine.submit_application(form, docs, app_id=app_id)
    engine.db.execute("UPDATE applications SET sandbox_scenario=? WHERE id=?", (name, app_id))
    return app_id


def _happy(engine, rng):
    form = make_application(rng=rng)
    return _submit(engine, form, documents_for(form), "happy_path")


def _gst_fails(engine, rng):
    form = make_application(name="Lakshmi Textiles", gst_entity="9", rng=rng)   # entity '9' => cancelled in sandbox
    return _submit(engine, form, documents_for(form), "gst_fails")


def _gst_fix(engine, app_id):
    app = engine.get_application(app_id)
    values = engine._values(app_id)
    gstin = make_gstin("27", values["pan"], "2")
    doc = {"doc_type": "GST_CERTIFICATE", "filename": "gst_certificate_new.txt",
           "text": gst_certificate_text(gstin, values["legal_name"], app["business"]["business_name"])}
    engine.provide_info(app_id, {"gstin": gstin}, [doc])
    return "merchant supplied an active GSTIN and its certificate"


def _dup_callbacks(engine, rng):
    form = make_application(name="Green Leaf Organics", rng=rng)
    return _submit(engine, form, documents_for(form), "duplicate_callbacks",
                   [{"target": "PAN", "times": 1, "duplicates": 2}])


def _late_callback(engine, rng):
    # first request and its retry are both slow, so the original answer lands while the retry is in flight
    form = make_application(name="Bombay Chai House", category="RESTAURANT", rng=rng)
    return _submit(engine, form, documents_for(form), "late_callback",
                   [{"target": "BANK", "times": 2, "latency_s": engine.s.check_timeout_s + 4}])


def _bank_change(engine, rng):
    form = make_application(name="Kaveri Hardware", category="RETAIL", rng=rng)
    return _submit(engine, form, documents_for(form), "bank_change_after_verification")


def _bank_change_fix(engine, app_id):
    values = engine._values(app_id)
    acct = make_account()
    doc = {"doc_type": "CANCELLED_CHEQUE", "filename": "new_cheque.txt",
           "text": cheque_text(acct, "ICIC0004321", values["account_holder"])}
    engine.update_bank_details(app_id, {"account_number": acct, "ifsc": "ICIC0004321",
                                        "account_holder": values["account_holder"]}, document=doc)
    return "merchant changed settlement account"


def _mid_transient(engine, rng):
    form = make_application(name="Urban Threads", category="APPAREL", rng=rng)
    return _submit(engine, form, documents_for(form), "mid_failure_transient",
                   [{"target": "MID", "times": 2, "fail": True}, {"target": "MID", "times": 1, "lose_response": True}])


def _mid_permanent(engine, rng):
    form = make_application(name="Saffron Kitchen", category="RESTAURANT", rng=rng)
    return _submit(engine, form, documents_for(form), "mid_failure_permanent",
                   [{"target": "MID", "times": engine.s.max_job_attempts, "fail": True}])


def _replay_dlq(engine, app_id):
    for dl in engine.dead_letters():
        if dl["application_id"] == app_id:
            engine.replay_dead_letter(dl["id"])
            return f"operator replayed dead letter {dl['id']}"
    return "no open dead letter for this application"


def _blurred(engine, rng):
    form = make_application(name="Patel Auto Spares", category="AUTOMOTIVE", rng=rng)
    docs = documents_for(form)
    docs[0] = pan_card_image(form["pan"], form["legal_name"], blur=6)
    return _submit(engine, form, docs, "blurred_document")


def _blurred_fix(engine, app_id):
    values = engine._values(app_id)
    engine.add_document(app_id, {"doc_type": "PAN_CARD", "filename": "pan_card_rescan.txt",
                                 "text": pan_card_text(values["pan"], values["legal_name"])})
    return "merchant re-uploaded a clear PAN card"


def _conflict(engine, rng):
    form = make_application(name="Neelgiri Pharma Distributors", category="PHARMACY", rng=rng)
    other_pan = make_pan("C", rng=rng)
    return _submit(engine, form, documents_for(form, pan=other_pan), "document_conflict")


def _high_risk(engine, rng):
    form = make_application(name="Royal Fantasy Games", category="GAMING", monthly_volume_inr=60_000_000, rng=rng)
    return _submit(engine, form, documents_for(form), "high_risk")


def _approve(engine, app_id):
    task = next((t for t in engine.review_queue() if t["application_id"] == app_id), None)
    if not task:
        return "no open review task"
    engine.review_decision(task["id"], "APPROVE", "risk.analyst@pinelabs.example",
                           "Underwriting reviewed: licensed skill-gaming operator, reserve agreed.")
    return "reviewer approved with written justification"


def _outage(engine, rng):
    form = make_application(name="Sunrise Stationers", category="RETAIL", rng=rng)
    return _submit(engine, form, documents_for(form), "provider_outage",
                   [{"target": "GST", "times": engine.s.max_job_attempts, "outage": True}])


def _duplicate(engine, rng):
    form = make_application(name="Sharma Electronics", rng=rng)
    _submit(engine, form, documents_for(form), "duplicate_application")   # original
    second = _submit(engine, form, documents_for(form), "duplicate_application")
    return second


def _blocked(engine, rng):
    form = make_application(name="Sanctioned Imports", category="RETAIL", rng=rng)
    return _submit(engine, form, documents_for(form), "watchlist_block")


def _closed_account(engine, rng):
    form = make_application(name="Kaveri Hardware", category="RETAIL", account_ending="0000", rng=rng)
    return _submit(engine, form, documents_for(form), "bank_account_closed")


SCENARIOS: dict[str, Scenario] = {s.name: s for s in [
    Scenario("happy_path", "Clean application", "straight-through processing to ACTIVE", _happy),
    Scenario("gst_fails", "PAN passes, GST fails", "partial failure; only GST is re-verified after the fix",
             _gst_fails, _gst_fix, "Merchant supplies active GSTIN"),
    Scenario("duplicate_callbacks", "Same callback 3x", "idempotent inbox ignores duplicates", _dup_callbacks),
    Scenario("late_callback", "Callback after timeout", "timeout -> retry -> late answer accepted, retry's answer ignored",
             _late_callback),
    Scenario("bank_change_after_verification", "Bank changed after verification",
             "bank + risk re-verification; payouts held if already ACTIVE", _bank_change, _bank_change_fix,
             "Merchant changes bank account"),
    Scenario("mid_failure_transient", "Merchant-ID creation flaky", "retries with backoff; idempotency prevents duplicate MIDs",
             _mid_transient),
    Scenario("mid_failure_permanent", "Merchant-ID creation down", "retries exhausted -> DLQ -> operator replay",
             _mid_permanent, _replay_dlq, "Operator replays DLQ"),
    Scenario("blurred_document", "Blurred PAN card photo", "image quality gate -> merchant asked to re-upload",
             _blurred, _blurred_fix, "Merchant re-uploads clear copy"),
    Scenario("document_conflict", "Document conflicts with form", "PAN on card != PAN on form -> human review", _conflict),
    Scenario("high_risk", "High-risk category", "risk exception -> reviewer approves with justification (waiver)",
             _high_risk, _approve, "Reviewer approves"),
    Scenario("provider_outage", "GST provider outage", "dispatch retries -> DLQ -> manual review -> replay",
             _outage, _replay_dlq, "Operator replays DLQ"),
    Scenario("duplicate_application", "Duplicate application", "PAN fingerprint match against open application", _duplicate),
    Scenario("watchlist_block", "Watchlist hit", "risk BLOCKED -> automatic rejection", _blocked),
    Scenario("bank_account_closed", "Closed bank account", "penny-drop failure -> merchant asked for another account",
             _closed_account, _bank_change_fix, "Merchant supplies another account"),
]}
