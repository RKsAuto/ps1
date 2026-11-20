"""HTTP API and operator console."""
from __future__ import annotations

import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import metrics as metrics_mod
from .engine import Engine, InvalidInput, NotFound, PolicyViolation
from .providers import CHAOS_KEYS
from .samples import SCENARIOS
from .states import TRANSITIONS, InvalidTransition
from .worker import Worker

STATIC = Path(__file__).parent / "static"


class Document(BaseModel):
    doc_type: str = Field(examples=["PAN_CARD"])
    filename: str | None = None
    media_type: str | None = None
    text: str | None = Field(None, description="OCR text of the document")
    fields: dict[str, Any] | None = Field(None, description="Structured payload from an upstream OCR/IDP vendor")
    ocr_confidence: float | None = None
    field_confidence: dict[str, float] | None = None
    quality: dict[str, Any] | None = None
    content_b64: str | None = Field(None, description="Raw file (image / PDF), base64")

    def as_dict(self) -> dict:
        return self.model_dump(exclude_none=True)


class ApplicationIn(BaseModel):
    form: dict[str, Any]
    documents: list[Document] = []


class InfoIn(BaseModel):
    updates: dict[str, Any] = {}
    documents: list[Document] = []


class BankIn(BaseModel):
    account_number: str
    ifsc: str
    account_holder: str
    document: Document | None = None


class DecisionIn(BaseModel):
    decision: str = Field(examples=["APPROVE", "REJECT", "REQUEST_INFO", "RETRY_CHECKS"])
    reviewer: str
    note: str = ""
    check_types: list[str] | None = None
    info_request: dict[str, Any] | None = None


class ChaosIn(BaseModel):
    target: str = Field(examples=["GST"])
    times: int | None = 1
    effects: dict[str, Any] = Field(examples=[{"outage": True}])


def create_app(engine: Engine | None = None, start_worker: bool = True) -> FastAPI:
    engine = engine or Engine()
    worker = Worker(engine)

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(title="Merchant Onboarding Platform", version="1.0.0", lifespan=lifespan,
                  description="Autonomous merchant onboarding: document intelligence, async verification, "
                              "explicit state machine, human review, audit trail.")
    app.state.engine = engine

    @app.exception_handler(NotFound)
    async def _nf(_, exc):
        return JSONResponse({"error": "not found", "detail": str(exc)}, status_code=404)

    @app.exception_handler(PolicyViolation)
    @app.exception_handler(InvalidTransition)
    async def _pv(_, exc):
        return JSONResponse({"error": "not allowed", "detail": str(exc)}, status_code=409)

    @app.exception_handler(InvalidInput)
    async def _ii(_, exc):
        return JSONResponse({"error": "invalid input", "detail": str(exc)}, status_code=422)

    # ------------------------------------------------------------ console
    @app.get("/", include_in_schema=False)
    def console():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health():
        return {"status": "ok", "audit_chain": engine.audit.verify()["valid"]}

    # ------------------------------------------------------------ merchant
    @app.post("/api/applications", status_code=201)
    def submit(body: ApplicationIn):
        app_id = engine.submit_application(body.form, [d.as_dict() for d in body.documents])
        return engine.get_application(app_id)

    @app.post("/api/applications/{app_id}/documents")
    async def upload(app_id: str, doc_type: str = Form(...), file: UploadFile = File(...)):
        content = await file.read()
        if len(content) > 10 * 1024 * 1024:
            raise InvalidInput("file larger than 10 MB")
        return engine.add_document(app_id, {"doc_type": doc_type, "filename": file.filename,
                                            "media_type": file.content_type, "content": content})

    @app.post("/api/applications/{app_id}/info")
    def provide_info(app_id: str, body: InfoIn):
        return engine.provide_info(app_id, body.updates, [d.as_dict() for d in body.documents])

    @app.put("/api/applications/{app_id}/bank-details")
    def bank_details(app_id: str, body: BankIn):
        doc = body.document.as_dict() if body.document else None
        return engine.update_bank_details(app_id, {"account_number": body.account_number, "ifsc": body.ifsc,
                                                   "account_holder": body.account_holder}, doc)

    # ------------------------------------------------------------ operator / reviewer
    @app.get("/api/applications")
    def list_apps(state: str | None = None):
        return engine.list_applications(state)

    @app.get("/api/applications/{app_id}")
    def get_app(app_id: str):
        return engine.get_application(app_id)

    @app.get("/api/review-queue")
    def review_queue():
        return engine.review_queue()

    @app.post("/api/review-tasks/{task_id}/decision")
    def decide(task_id: str, body: DecisionIn):
        return engine.review_decision(task_id, body.decision, body.reviewer, body.note, body.check_types, body.info_request)

    @app.get("/api/dead-letters")
    def dead_letters(status: str | None = "OPEN"):
        return engine.dead_letters(status or None)

    @app.post("/api/dead-letters/{dl_id}/replay")
    def replay(dl_id: str):
        return engine.replay_dead_letter(dl_id)

    @app.get("/api/metrics")
    def metrics():
        return metrics_mod.compute(engine.db, engine.clock.now())

    @app.get("/api/audit/verify")
    def audit_verify():
        return engine.audit.verify()

    @app.get("/api/state-machine")
    def state_machine():
        return [{"from": f.value, "event": e.value, "to": t.value} for (f, e), t in TRANSITIONS.items()]

    # ------------------------------------------------------------ provider webhooks
    @app.post("/webhooks/{provider}")
    async def webhook(provider: str, request: Request, x_signature: str | None = Header(None)):
        status, body = engine.receive_callback(provider, await request.body(), x_signature)
        return JSONResponse(body, status_code=status)

    # ------------------------------------------------------------ simulator controls
    @app.get("/api/simulator/chaos")
    def chaos_list():
        return engine.chaos.snapshot()

    @app.post("/api/simulator/chaos")
    def chaos_add(body: ChaosIn):
        bad = set(body.effects) - CHAOS_KEYS
        if bad:
            raise InvalidInput(f"unknown effects {sorted(bad)}; allowed {sorted(CHAOS_KEYS)}")
        try:
            return engine.chaos.add(body.target, body.times, **body.effects)
        except ValueError as exc:
            raise InvalidInput(str(exc)) from None

    @app.delete("/api/simulator/chaos")
    def chaos_clear():
        engine.chaos.clear()
        return {"cleared": True}

    @app.get("/api/simulator/callbacks")
    def sim_callbacks():
        return engine.providers.recent()

    @app.post("/api/simulator/callbacks/{callback_id}/redeliver")
    def redeliver(callback_id: str):
        status, body = engine.redeliver_callback(callback_id)
        return JSONResponse(body, status_code=status)

    @app.get("/api/scenarios")
    def scenarios():
        return [{"name": s.name, "title": s.title, "demonstrates": s.demonstrates, "follow_up": s.follow_up_label}
                for s in SCENARIOS.values()]

    @app.post("/api/scenarios/{name}", status_code=201)
    def run_scenario(name: str):
        sc = SCENARIOS.get(name)
        if not sc:
            raise NotFound(name)
        app_id = sc.setup(engine, random.Random())
        return engine.get_application(app_id)

    @app.post("/api/scenarios/{name}/follow-up/{app_id}")
    def scenario_follow_up(name: str, app_id: str):
        sc = SCENARIOS.get(name)
        if not sc or not sc.follow_up:
            raise NotFound(name)
        message = sc.follow_up(engine, app_id)
        return {"message": message, "application": engine.get_application(app_id)}

    return app
