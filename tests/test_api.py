import json

import pytest
from fastapi.testclient import TestClient

from conftest import Harness
from onboarding.api import create_app
from onboarding.samples import documents_for, make_application
from onboarding.security import sign


@pytest.fixture
def ctx():
    h = Harness()
    client = TestClient(create_app(h.engine, start_worker=False))
    return h, client


def test_submit_and_track_via_api(ctx):
    h, client = ctx
    form = make_application(rng=h.rng)
    r = client.post("/api/applications", json={"form": form, "documents": documents_for(form)})
    assert r.status_code == 201
    app_id = r.json()["id"]
    assert form["pan"] not in r.text
    h.settle(30)
    assert client.get(f"/api/applications/{app_id}").json()["state"] == "ACTIVE"
    assert client.get("/api/applications?state=ACTIVE").json()[0]["id"] == app_id
    m = client.get("/api/metrics").json()
    assert m["applications"]["by_state"]["ACTIVE"] == 1 and m["rates"]["straight_through_pct_of_active"] == 100.0
    assert client.get("/api/audit/verify").json()["valid"]


def test_document_upload_endpoint(ctx):
    h, client = ctx
    form = make_application(rng=h.rng)
    docs = [d for d in documents_for(form) if d["doc_type"] != "PAN_CARD"]
    app_id = client.post("/api/applications", json={"form": form, "documents": docs}).json()["id"]
    assert client.get(f"/api/applications/{app_id}").json()["state"] == "AWAITING_INFO"
    text = f"INCOME TAX DEPARTMENT\n{form['pan']}\nName: {form['legal_name']}\n".encode()
    r = client.post(f"/api/applications/{app_id}/documents", data={"doc_type": "PAN_CARD"},
                    files={"file": ("pan.txt", text, "text/plain")})
    assert r.status_code == 200 and r.json()["state"] == "VERIFYING"


def test_webhook_endpoint_rejects_bad_signature_and_accepts_good(ctx):
    h, client = ctx
    body = json.dumps({"callback_id": "CB-X", "request_id": "REQ-UNKNOWN", "result": {}}).encode()
    assert client.post("/webhooks/PAN", content=body, headers={"X-Signature": "nope"}).status_code == 401
    r = client.post("/webhooks/PAN", content=body, headers={"X-Signature": sign(h.settings.webhook_secrets["PAN"], body)})
    assert r.status_code == 202 and r.json()["outcome"] == "UNKNOWN_REQUEST"


def test_policy_and_validation_errors_map_to_http(ctx):
    h, client = ctx
    bad = make_application(rng=h.rng)
    bad["business_type"] = "TRUST_ME"
    assert client.post("/api/applications", json={"form": bad}).status_code == 422
    r = client.post("/api/scenarios/provider_outage")
    app_id = r.json()["id"]
    h.settle(90)
    task = client.get("/api/review-queue").json()[0]
    r = client.post(f"/api/review-tasks/{task['id']}/decision", json={"decision": "APPROVE", "reviewer": "a@b.c", "note": "ok"})
    assert r.status_code == 409 and "GST check is ERROR" in r.json()["detail"]
    dl = client.get("/api/dead-letters").json()[0]
    assert client.post(f"/api/dead-letters/{dl['id']}/replay").status_code == 200
    h.settle(30)
    assert client.get(f"/api/applications/{app_id}").json()["state"] == "ACTIVE"
    assert client.get("/api/applications/APP-NOPE").status_code == 404


def test_scenarios_and_chaos_endpoints(ctx):
    h, client = ctx
    names = [s["name"] for s in client.get("/api/scenarios").json()]
    assert {"gst_fails", "late_callback", "mid_failure_permanent", "blurred_document"} <= set(names)
    assert client.post("/api/simulator/chaos", json={"target": "GST", "times": 1, "effects": {"outage": True}}).status_code == 200
    assert client.post("/api/simulator/chaos", json={"target": "GST", "effects": {"explode": True}}).status_code == 422
    assert len(client.get("/api/simulator/chaos").json()) == 1
    client.delete("/api/simulator/chaos")
    assert client.get("/api/simulator/chaos").json() == []
    assert len(client.get("/api/state-machine").json()) > 10
    assert client.get("/").status_code == 200
