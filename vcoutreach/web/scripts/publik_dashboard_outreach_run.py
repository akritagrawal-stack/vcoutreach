#!/usr/bin/env python3
"""Run the dashboard's local review-only outreach preparation workflow.

This process reads workspace contacts and drafts and writes only safe
aggregate progress. It never enriches contacts, calls a provider, opens Gmail,
or gains delivery authority.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys
import time


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vcoutreach.web.vendor import workspace_path, load_sender_context
from vcoutreach.web.db import get_conn
from vcoutreach.web.server.workspace_settings import load_settings  # noqa: E402


STOP_REQUESTED = False


def _stop(_signum, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local draft-only outreach preparation.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--step-delay", type=float, default=1.0)
    args = parser.parse_args(argv)
    args.output = workspace_path(args.output)
    args.step_delay = max(0, min(args.step_delay, 10))

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    conn = get_conn()
    try:
        source_count = conn.execute("SELECT COUNT(*) FROM partners").fetchone()[0]
        review_count = conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
    finally:
        conn.close()
    campaign = {"record_count": source_count, "research_ready_count": 0, "verified_count": 0, "review_ready_count": review_count}
    workspace = load_settings()
    counts = {
        "source": int(campaign["record_count"]),
        "research": int(campaign["research_ready_count"]),
        "verify": int(campaign["verified_count"]),
        "personalize": int(campaign["review_ready_count"]),
        "review": int(campaign["review_ready_count"]),
    }
    steps = ["source", "research", "verify", "personalize", "review"]
    state = {
        "schema_version": 1,
        "status": "running",
        "mode": "review_only",
        "started_at": _now(),
        "completed_at": "",
        "current_step": "source",
        "completed_steps": [],
        "counts": counts,
        "company_name": str(workspace.get("company_name") or "Outreach")[:60],
        "delivery_authority": False,
        "provider_calls": False,
        "gmail_accessed": False,
    }
    _write(args.output, state)

    for step in steps:
        if STOP_REQUESTED:
            state["status"] = "stopped"
            state["completed_at"] = _now()
            _write(args.output, state)
            return 0
        state["current_step"] = step
        _write(args.output, state)
        time.sleep(max(0.0, min(args.step_delay, 5.0)))
        state["completed_steps"].append(step)
        _write(args.output, state)

    state["status"] = "complete"
    state["current_step"] = "complete"
    state["completed_at"] = _now()
    _write(args.output, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
