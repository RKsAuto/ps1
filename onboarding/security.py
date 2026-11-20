"""Handling of sensitive onboarding data.

* PAN, GSTIN and bank account numbers are encrypted at rest (Fernet / AES-128-CBC + HMAC).
* Only masked values ever leave the API or reach the audit log.
* A keyed HMAC fingerprint allows duplicate detection without decrypting.
* Provider webhooks are authenticated with HMAC-SHA256 signatures.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re

from cryptography.fernet import Fernet

log = logging.getLogger(__name__)

SENSITIVE_FIELDS = ("pan", "gstin", "bank_account")

_DEV_KEY = base64.urlsafe_b64encode(hashlib.sha256(b"merchant-onboarding-dev-only").digest())


class Vault:
    def __init__(self, key: str | None, fingerprint_key: str):
        if not key:
            log.warning("ONBOARDING_ENCRYPTION_KEY not set - using a DEV key. Never do this in production.")
            key = _DEV_KEY.decode()
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        self._fp_key = fingerprint_key.encode()

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()

    def fingerprint(self, value: str) -> str:
        normalized = re.sub(r"\s+", "", value or "").upper()
        return hmac.new(self._fp_key, normalized.encode(), hashlib.sha256).hexdigest()


def mask(field: str, value: str | None) -> str | None:
    if not value:
        return value
    v = str(value)
    if field == "pan" and len(v) == 10:
        return f"{v[:3]}XXXX{v[7:]}"            # ABCXXXX34F
    if field == "gstin" and len(v) == 15:
        return f"{v[:5]}XXXXX{v[10:]}"           # 27ABCXXXXX4F1Z5
    if field in ("bank_account", "account_number"):
        return "X" * max(len(v) - 4, 0) + v[-4:]
    if len(v) <= 4:
        return "X" * len(v)
    return v[:2] + "X" * (len(v) - 4) + v[-2:]


_PAN_RE = re.compile(r"\b([A-Z]{5})(\d{4})([A-Z])\b")
_GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
_ACCT_RE = re.compile(r"\b(\d{9,18})\b")


def redact(text: str | None) -> str | None:
    """Scrub identifiers out of free text (reviewer notes, error messages)."""
    if not text:
        return text
    text = _GSTIN_RE.sub(lambda m: mask("gstin", m.group(1)), text)
    text = _PAN_RE.sub(lambda m: mask("pan", m.group(0)), text)
    text = _ACCT_RE.sub(lambda m: mask("bank_account", m.group(1)), text)
    return text


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(sign(secret, body), signature)
