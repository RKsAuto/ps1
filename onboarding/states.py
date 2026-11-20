"""Explicit application state machine.

Every state change goes through ``next_state``. A transition that is not in
``TRANSITIONS`` raises ``InvalidTransition`` - there is no way to, say, move an
application to ACTIVE without passing through APPROVED and PROVISIONING.
"""
from __future__ import annotations

from enum import Enum


class AppState(str, Enum):
    SUBMITTED = "SUBMITTED"
    AWAITING_INFO = "AWAITING_INFO"            # merchant must fix / supply something
    VERIFYING = "VERIFYING"                    # async provider checks in progress
    MANUAL_REVIEW = "MANUAL_REVIEW"            # human-in-the-loop queue
    APPROVED = "APPROVED"                      # all configured checks passed / waived
    PROVISIONING = "PROVISIONING"              # creating merchant id + capabilities
    PROVISIONING_FAILED = "PROVISIONING_FAILED"  # retries exhausted, in DLQ
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"


class Event(str, Enum):
    DOCS_VALIDATED = "DOCS_VALIDATED"
    INFO_REQUIRED = "INFO_REQUIRED"
    DATA_CHANGED = "DATA_CHANGED"
    EXCEPTION_RAISED = "EXCEPTION_RAISED"
    ALL_CHECKS_PASSED = "ALL_CHECKS_PASSED"
    AUTO_REJECTED = "AUTO_REJECTED"
    REVIEW_APPROVED = "REVIEW_APPROVED"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    REVIEW_REQUESTED_INFO = "REVIEW_REQUESTED_INFO"
    REVIEW_RETRY_CHECKS = "REVIEW_RETRY_CHECKS"
    DLQ_REPLAYED = "DLQ_REPLAYED"
    PROVISIONING_STARTED = "PROVISIONING_STARTED"
    MERCHANT_CREATED = "MERCHANT_CREATED"
    PROVISIONING_EXHAUSTED = "PROVISIONING_EXHAUSTED"


S, E = AppState, Event

TRANSITIONS: dict[tuple[AppState, Event], AppState] = {
    (S.SUBMITTED, E.DOCS_VALIDATED): S.VERIFYING,
    (S.SUBMITTED, E.INFO_REQUIRED): S.AWAITING_INFO,

    (S.AWAITING_INFO, E.DATA_CHANGED): S.VERIFYING,
    (S.AWAITING_INFO, E.REVIEW_REJECTED): S.REJECTED,

    (S.VERIFYING, E.ALL_CHECKS_PASSED): S.APPROVED,
    (S.VERIFYING, E.INFO_REQUIRED): S.AWAITING_INFO,
    (S.VERIFYING, E.EXCEPTION_RAISED): S.MANUAL_REVIEW,
    (S.VERIFYING, E.AUTO_REJECTED): S.REJECTED,

    (S.MANUAL_REVIEW, E.REVIEW_APPROVED): S.APPROVED,
    (S.MANUAL_REVIEW, E.REVIEW_REJECTED): S.REJECTED,
    (S.MANUAL_REVIEW, E.REVIEW_REQUESTED_INFO): S.AWAITING_INFO,
    (S.MANUAL_REVIEW, E.REVIEW_RETRY_CHECKS): S.VERIFYING,
    (S.MANUAL_REVIEW, E.DLQ_REPLAYED): S.VERIFYING,
    (S.MANUAL_REVIEW, E.DATA_CHANGED): S.VERIFYING,
    (S.MANUAL_REVIEW, E.INFO_REQUIRED): S.AWAITING_INFO,

    (S.APPROVED, E.PROVISIONING_STARTED): S.PROVISIONING,

    (S.PROVISIONING, E.MERCHANT_CREATED): S.ACTIVE,
    (S.PROVISIONING, E.PROVISIONING_EXHAUSTED): S.PROVISIONING_FAILED,

    (S.PROVISIONING_FAILED, E.DLQ_REPLAYED): S.PROVISIONING,
    (S.PROVISIONING_FAILED, E.REVIEW_REJECTED): S.REJECTED,
}

# States in which the merchant (or support on their behalf) may edit data.
EDITABLE_STATES = {S.SUBMITTED, S.AWAITING_INFO, S.VERIFYING, S.MANUAL_REVIEW}
TERMINAL_STATES = {S.REJECTED}


class InvalidTransition(Exception):
    pass


def next_state(current: AppState | str, event: Event) -> AppState:
    current = AppState(current)
    try:
        return TRANSITIONS[(current, event)]
    except KeyError:
        raise InvalidTransition(f"{event.value} is not allowed from {current.value}") from None


class CheckStatus(str, Enum):
    PENDING = "PENDING"                  # queued for dispatch
    IN_FLIGHT = "IN_FLIGHT"              # request sent, awaiting callback
    RETRY_SCHEDULED = "RETRY_SCHEDULED"  # timed out, retry queued
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"                      # provider never gave an answer
    WAIVED = "WAIVED"                    # failed, but a reviewer accepted it
    SKIPPED = "SKIPPED"                  # not required by policy


SETTLED = {CheckStatus.PASSED, CheckStatus.FAILED, CheckStatus.ERROR, CheckStatus.WAIVED, CheckStatus.SKIPPED}
SATISFIED = {CheckStatus.PASSED, CheckStatus.WAIVED, CheckStatus.SKIPPED}
