"""Simulated external services: PAN (NSDL-like), GST (GSTN-like), bank penny-drop,
risk scoring and the internal merchant-id / capability service.

The verification providers behave like real async APIs: ``submit`` returns an
acknowledgement immediately and the result arrives later as a signed webhook.

Deterministic sandbox values (like card-network test numbers):

  PAN   digits 0000 -> INVALID        digits 1111 -> NAME_MISMATCH
  GSTIN entity char (13th) '9' -> CANCELLED
  BANK  account ending 0000 -> ACCOUNT_CLOSED   ending 1111 -> NAME_MISMATCH
  RISK  name contains SANCTIONED -> BLOCKED; category GAMING/CRYPTO/FOREX and
        very high volumes raise the score; a recent bank change adds risk

Infrastructure faults are injected through ``Chaos`` rules (outage, latency,
duplicate delivery, dropped callbacks, merchant-id failures).
"""
from __future__ import annotations

import threading
import uuid

from .extraction import name_similarity


class ProviderUnavailable(Exception):
    """Synchronous failure from a provider (HTTP 5xx / connection refused)."""


class MerchantServiceError(Exception):
    pass


CHAOS_KEYS = {"outage", "latency_s", "duplicates", "drop", "fail", "lose_response"}
CHAOS_TARGETS = {"PAN", "GST", "BANK", "RISK", "MID"}


class Chaos:
    """Fault-injection rules. Each rule applies to the next ``times`` calls (or forever)."""

    def __init__(self):
        self._rules: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def add(self, target: str, times: int | None = 1, application_id: str | None = None, **effects) -> dict:
        target = target.upper()
        if target not in CHAOS_TARGETS:
            raise ValueError(f"unknown chaos target {target}")
        unknown = set(effects) - CHAOS_KEYS
        if unknown:
            raise ValueError(f"unknown chaos effects {unknown}")
        rule = {"id": uuid.uuid4().hex[:8], "target": target, "remaining": times,
                "application_id": application_id, **effects}
        with self._lock:
            self._rules.setdefault(target, []).append(rule)
        return rule

    def consume(self, target: str, application_id: str | None = None) -> dict:
        """Take the first rule for ``target`` that is global or scoped to this application."""
        with self._lock:
            rules = self._rules.get(target, [])
            rule = next((r for r in rules if r["application_id"] in (None, application_id)), None)
            if rule is None:
                return {}
            if rule["remaining"] is not None:
                rule["remaining"] -= 1
                if rule["remaining"] <= 0:
                    rules.remove(rule)
            return {k: v for k, v in rule.items() if k in CHAOS_KEYS}

    def clear(self) -> None:
        with self._lock:
            self._rules.clear()

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(r) for rules in self._rules.values() for r in rules]


def _pan_decision(p: dict) -> dict:
    digits = p["pan"][5:9]
    if digits == "0000":
        return {"outcome": "INVALID", "message": "PAN does not exist in registry"}
    registered = "RAMESH KUMAR GUPTA" if digits == "1111" else p["name"]
    score = name_similarity(registered, p["name"])
    if score < 0.85:
        return {"outcome": "NAME_MISMATCH", "registered_name": registered, "name_match_score": score}
    return {"outcome": "VALID", "registered_name": registered, "name_match_score": score, "pan_status": "OPERATIVE"}


def _gst_decision(p: dict) -> dict:
    gstin = p["gstin"]
    if gstin[12] == "9":
        return {"outcome": "CANCELLED", "message": "GSTIN registration cancelled", "taxpayer_status": "Cancelled"}
    return {"outcome": "ACTIVE", "legal_name": p["legal_name"], "taxpayer_status": "Active",
            "state_code": gstin[:2], "name_match_score": 1.0}


def _bank_decision(p: dict) -> dict:
    acct = p["account_number"]
    if acct.endswith("0000"):
        return {"outcome": "ACCOUNT_CLOSED", "message": "Penny drop failed: account closed"}
    beneficiary = "SOMEONE ELSE TRADING CO" if acct.endswith("1111") else p["account_holder"]
    score = max(name_similarity(beneficiary, p["account_holder"]), name_similarity(beneficiary, p["legal_name"]))
    if score < 0.85:
        return {"outcome": "NAME_MISMATCH", "beneficiary_name": beneficiary, "name_match_score": score}
    return {"outcome": "VERIFIED", "beneficiary_name": beneficiary, "name_match_score": score, "utr": "UTR" + uuid.uuid4().hex[:10].upper()}


_HIGH_RISK_CATEGORIES = {"GAMING": 55, "CRYPTO": 60, "FOREX": 50, "JEWELLERY": 20, "TRAVEL": 15}


def _risk_decision(p: dict) -> dict:
    if "SANCTIONED" in (p.get("legal_name") or "").upper():
        return {"outcome": "BLOCKED", "score": 100, "factors": ["watchlist_hit"]}
    score, factors = 10, []
    cat = (p.get("category") or "").upper()
    if cat in _HIGH_RISK_CATEGORIES:
        score += _HIGH_RISK_CATEGORIES[cat]
        factors.append(f"category:{cat}")
    vol = p.get("monthly_volume_inr") or 0
    if vol > 50_000_000:
        score += 20
        factors.append("very_high_volume")
    elif vol > 10_000_000:
        score += 10
        factors.append("high_volume")
    if p.get("bank_changed_after_activation"):
        score += 15
        factors.append("bank_change_after_activation")
    tier = "LOW" if score < 40 else "MEDIUM" if score < 70 else "HIGH"
    outcome = "HIGH_RISK" if tier == "HIGH" else "ACCEPTABLE"
    return {"outcome": outcome, "score": score, "tier": tier, "factors": factors}


DECISIONS = {"PAN": _pan_decision, "GST": _gst_decision, "BANK": _bank_decision, "RISK": _risk_decision}


class ProviderSimulator:
    """Accepts verification requests and schedules callbacks for later delivery.

    The simulator's state is deliberately kept in memory, outside the platform's
    database and transactions: it is "someone else's system". If the platform
    crashes mid-request the provider may still call back, and if the simulator
    restarts its pending callbacks are lost - the platform's timeouts cover both.
    """

    def __init__(self, clock, settings, chaos: Chaos):
        self.clock, self.settings, self.chaos = clock, settings, chaos
        self._outbox: list[dict] = []
        self._lock = threading.Lock()

    def due_callbacks(self) -> list[dict]:
        now = self.clock.now()
        with self._lock:
            due = [c for c in self._outbox if not c["delivered"] and c["deliver_at"] <= now]
            for c in due:
                c["delivered"] = True
            return sorted(due, key=lambda c: c["deliver_at"])

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [dict(c) for c in self._outbox[-limit:]][::-1]

    def find(self, callback_id: str) -> dict | None:
        with self._lock:
            return next((dict(c) for c in self._outbox if c["payload"]["callback_id"] == callback_id), None)

    def submit(self, check_type: str, request_id: str, payload: dict, application_id: str | None = None) -> dict:
        rule = self.chaos.consume(check_type, application_id)
        if rule.get("outage"):
            raise ProviderUnavailable(f"{check_type} provider returned 503")
        result = DECISIONS[check_type](payload)
        if rule.get("drop"):
            return {"accepted": True, "request_id": request_id}
        latency = float(rule.get("latency_s", self.settings.provider_latency_s.get(check_type, 1.0)))
        callback = {"callback_id": "CB-" + uuid.uuid4().hex[:12], "request_id": request_id,
                    "provider": check_type, "status": "COMPLETED", "result": result}
        for i in range(1 + int(rule.get("duplicates", 0))):
            # at-least-once delivery: the same event id may arrive several times
            with self._lock:
                self._outbox.append({"provider": check_type, "deliver_at": self.clock.now() + latency + i * 0.5,
                                     "payload": callback, "delivered": False, "copy": i + 1})
        return {"accepted": True, "request_id": request_id}


class MerchantIdService:
    """Idempotent merchant creation keyed by application id."""

    def __init__(self, clock, chaos: Chaos):
        self.clock, self.chaos = clock, chaos
        self.merchants: dict[str, dict] = {}
        self.calls = 0
        self._lock = threading.Lock()

    def create(self, idempotency_key: str, payload: dict) -> dict:
        rule = self.chaos.consume("MID", idempotency_key)
        with self._lock:
            self.calls += 1
            if rule.get("fail"):
                raise MerchantServiceError("merchant-id service returned 500")
            existing = self.merchants.get(idempotency_key)
            if existing:
                return {**existing, "replayed": True}
            tier = payload.get("risk_tier", "LOW")
            caps = ["UPI", "CARDS", "NETBANKING"] + (["EMI", "INTERNATIONAL_CARDS"] if tier == "LOW" else [])
            record = {"merchant_id": "PLM" + str(uuid.uuid4().int)[:9], "capabilities": caps}
            self.merchants[idempotency_key] = record
        if rule.get("lose_response"):
            # created on the server, but the response never reached us
            raise MerchantServiceError("timeout waiting for merchant-id service response")
        return {**record, "replayed": False}
