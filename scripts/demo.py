"""Narrated, deterministic walk-through of the onboarding workflow.

Runs every scenario on a simulated clock (no server needed) and prints the
state transitions, callbacks and decisions as they happen.

    python scripts/demo.py                 # all scenarios
    python scripts/demo.py late_callback   # just one
"""
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.WARNING)

from onboarding.clock import FakeClock          # noqa: E402
from onboarding.config import Settings          # noqa: E402
from onboarding.db import Database              # noqa: E402
from onboarding.engine import Engine            # noqa: E402
from onboarding.samples import SCENARIOS        # noqa: E402

INTERESTING = {"STATE_CHANGED", "CHECK_DISPATCHED", "CHECK_COMPLETED", "CHECK_TIMED_OUT", "CHECK_RETRY_SCHEDULED",
               "CALLBACK_DUPLICATE_IGNORED", "CHECK_INVALIDATED", "JOB_RETRY_SCHEDULED", "JOB_DEAD_LETTERED",
               "MERCHANT_ID_CREATED", "PAYOUTS_HELD", "PAYOUTS_RELEASED", "REVIEW_DECISION", "CHECK_WAIVED",
               "DLQ_REPLAYED", "BANK_DETAILS_CHANGED", "INFO_PROVIDED", "DOCUMENT_RECEIVED", "AUTO_REJECTED",
               "FIELD_AUTOFILLED"}


def describe(e: dict) -> str:
    d, a = e["details"], e["action"]
    if a == "STATE_CHANGED":
        return f"STATE  {d['from']} -> {d['to']}  ({d['event']})"
    if a == "CALLBACK_RECEIVED":
        return f"callback {d['request_id']} -> {d['outcome']} (result {d['result']})"
    if a == "CHECK_COMPLETED":
        return f"{d['check']} check {d['status']} ({d['outcome']})"
    if a == "CHECK_DISPATCHED":
        return f"{d['check']} request sent, attempt {d['attempt']}"
    if a == "DOCUMENT_RECEIVED":
        return f"document {d['doc_type']} via {d['method']} readable={d['readable']} {('; '.join(d['warnings']))}"
    keep = {k: v for k, v in d.items() if k not in ("request_id", "callback_id", "document")}
    return f"{a.lower()} {keep}"


def run(name: str, verbose: bool = True) -> str:
    sc = SCENARIOS[name]
    clock = FakeClock()
    engine = Engine(Settings(db_path=":memory:", jitter=False, seed=1), clock, Database(":memory:"))
    print(f"\n{'=' * 78}\n{sc.title}\n  demonstrates: {sc.demonstrates}\n{'=' * 78}")
    app_id = sc.setup(engine, random.Random(3))
    seen = 0
    t0 = clock.now()

    def pump(seconds: float):
        nonlocal seen
        for _ in range(int(seconds / 0.5)):
            clock.advance(0.5)
            engine.tick()
            entries = engine.get_application(app_id)["audit"]
            for e in entries[seen:]:
                if verbose and (e["action"] in INTERESTING or e["action"] == "CALLBACK_RECEIVED"):
                    print(f"  t+{e['at'] - t0:6.1f}s  {describe(e)}")
            seen = len(entries)

    pump(120)
    if sc.follow_up:
        print(f"  ---- follow-up: {sc.follow_up_label}")
        sc.follow_up(engine, app_id)
        pump(60)
    a = engine.get_application(app_id)
    open_items = [i["code"] for i in a["missing_info"] + a["exceptions"]]
    print(f"  RESULT: {a['state']}" + (f"  merchant_id={a['merchant_id']}" if a["merchant_id"] else "")
          + (f"  open={open_items}" if open_items else "") + f"  audit_chain_valid={engine.audit.verify()['valid']}")
    return a["state"]


if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    results = {n: run(n) for n in names}
    print("\nSummary")
    for n, state in results.items():
        print(f"  {n:34s} {state}")
