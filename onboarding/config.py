"""Runtime configuration and onboarding policy.

Everything that a risk / compliance team would want to tune lives here, so the
workflow engine itself contains no magic numbers.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

CHECK_TYPES = ("PAN", "GST", "BANK", "RISK")

# PAN 4th character encodes the holder type. Used to cross-check business type.
PAN_ENTITY_CODE = {
    "PROPRIETORSHIP": "P",
    "PARTNERSHIP": "F",
    "LLP": "F",
    "PRIVATE_LIMITED": "C",
    "PUBLIC_LIMITED": "C",
}

# Check failures a human reviewer may waive (with a written justification).
# Anything not listed here can never be overridden: e.g. an invalid PAN, a
# closed bank account or a check that never completed.
WAIVABLE_FAILURES = {
    ("PAN", "NAME_MISMATCH"),
    ("GST", "NAME_MISMATCH"),
    ("BANK", "NAME_MISMATCH"),
    ("RISK", "HIGH_RISK"),
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.getenv("ONBOARDING_DB", "onboarding.db"))

    # Async verification behaviour
    check_timeout_s: float = field(default_factory=lambda: _env_float("CHECK_TIMEOUT_S", 8.0))
    max_check_attempts: int = 3        # provider requests per check before giving up (timeouts)
    max_job_attempts: int = 4          # attempts for a job (dispatch / provisioning) before DLQ
    backoff_base_s: float = 2.0
    backoff_cap_s: float = 30.0
    jitter: bool = True
    seed: int | None = None

    # Simulated provider latency (seconds) when no chaos rule overrides it
    provider_latency_s: dict = field(default_factory=lambda: {
        "PAN": 1.0, "GST": 2.0, "BANK": 1.5, "RISK": 2.5,
    })

    # Document intelligence
    confidence_threshold: float = 0.75
    name_match_threshold: float = 0.85
    blur_threshold: float = 150.0      # Laplacian variance below this => blurred

    # Secrets. Dev defaults are deterministic so the demo DB survives restarts;
    # production must inject real keys (see README "Security").
    encryption_key: str | None = field(default_factory=lambda: os.getenv("ONBOARDING_ENCRYPTION_KEY"))
    fingerprint_key: str = field(default_factory=lambda: os.getenv("ONBOARDING_FINGERPRINT_KEY", "dev-fingerprint-key"))
    webhook_secrets: dict = field(default_factory=lambda: {
        p: os.getenv(f"WEBHOOK_SECRET_{p}", f"dev-{p.lower()}-webhook-secret") for p in CHECK_TYPES
    })

    # Optional multimodal extraction through Claude (images / scanned PDFs)
    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))
    extraction_model: str = field(default_factory=lambda: os.getenv("EXTRACTION_MODEL", "claude-sonnet-5-5"))

    worker_interval_s: float = 0.5

    def required_checks(self, app: dict) -> list[str]:
        checks = ["PAN", "BANK", "RISK"]
        if app.get("gst_registered"):
            checks.insert(1, "GST")
        return checks

    def required_documents(self, app: dict) -> list[str]:
        docs = ["PAN_CARD", "CANCELLED_CHEQUE"]
        if app.get("gst_registered"):
            docs.insert(1, "GST_CERTIFICATE")
        return docs

    def required_fields(self, app: dict) -> list[str]:
        fields = ["legal_name", "pan", "bank_account", "ifsc", "account_holder"]
        if app.get("gst_registered"):
            fields.insert(2, "gstin")
        return fields
