"""Read-only operator snapshots. Never start an engine or load a backend plugin."""

import json
import sqlite3
from collections import Counter
from pathlib import Path

from poppy_controls.config import contract_id, load, public_config
from poppy_controls.metered.backend import fingerprint


def read(path, queries):
    path = Path(path)
    if not path.exists():
        return None
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        return [[dict(row) for row in db.execute(query)] for query in queries]
    finally:
        db.close()


def snapshot(config_path, job_id=None):
    cfg = load(config_path)
    state = Path(cfg["state_dir"])
    inbox = read(
        state / "inbox.sqlite",
        [
            "SELECT id,owner,state,estimate,charge,accepted,finished FROM jobs ORDER BY seq DESC",
            "SELECT key,value FROM metadata WHERE key IN ('effective_config','contract')",
        ],
    )
    journal = read(
        state / "journal.sqlite",
        [
            """SELECT key,owner,action,estimate,charge,state,created,finished,
        workflow_key,request_key
        FROM operations ORDER BY seq""",
            "SELECT * FROM workflow_holds",
            "SELECT * FROM decisions ORDER BY seq",
        ],
    )
    jobs, metadata = inbox if inbox else ([], [])
    operations, holds, decisions = journal if journal else ([], [], [])
    saved = {row["key"]: row["value"] for row in metadata}
    effective = json.loads(saved["effective_config"]) if "effective_config" in saved else None
    contract_changed = "contract" in saved and saved["contract"] != contract_id(cfg)
    open_holds = {h["key"]: h for h in holds if h["state"] == "open"}
    # Completed receipts inside an open envelope are already covered by its bound.
    settled = sum((r["charge"] or 0) for r in operations if r["workflow_key"] not in open_holds)
    reserved = sum(h["bound"] for h in open_holds.values()) + sum(
        r["estimate"]
        for r in operations
        if r["state"] == "running" and r["workflow_key"] not in open_holds
    )
    budget = (effective or cfg)["company_budget_units"]
    result = {
        "initialized": inbox is not None,
        "configured": public_config(cfg),
        "last_started_config": effective,
        "config_changed_since_start": effective is not None
        and (effective != public_config(cfg) or contract_changed),
        "execution_contract_changed": contract_changed,
        "snapshot_note": (
            "Read-only; each database snapshot is consistent. Cross-database reads may lag."
        ),
        "budget": {
            "units": "execution_units",
            "company_limit": budget,
            "settled_including_provisional": settled,
            "reserved": reserved,
            "remaining": max(0, budget - settled - reserved),
            "verified_charges": sum(
                r["charge"] or 0 for r in operations if r["state"] == "completed"
            ),
            "provisional_charges": sum(
                r["charge"] or 0 for r in operations if r["state"] == "uncertain"
            ),
            "unresolved_operations": sum(
                r["state"] in ("running", "uncertain") for r in operations
            ),
        },
        "job_counts": dict(Counter(job["state"] for job in jobs)),
        "recent_jobs": [{k: v for k, v in job.items() if k != "owner"} for job in jobs[:20]],
    }
    if job_id:
        job = next((j for j in jobs if j["id"] == job_id), None)
        if job is None:
            raise ValueError("Job not found in this state directory")
        user_rows = [row for row in operations if row["owner"] == job["owner"]]
        user_settled = sum(
            (row["charge"] or 0) for row in user_rows if row["workflow_key"] not in open_holds
        )
        user_reserved = sum(
            h["bound"] for h in open_holds.values() if h["owner"] == job["owner"]
        ) + sum(
            row["estimate"]
            for row in user_rows
            if row["state"] == "running" and row["workflow_key"] not in open_holds
        )
        user_limit = (effective or cfg)["user_budget_units"]
        result["user_budget"] = {
            "scope": "current budget of this job's verified user across all their requests",
            "limit": user_limit,
            "settled_including_provisional": user_settled,
            "reserved": user_reserved,
            "remaining": max(0, user_limit - user_settled - user_reserved),
        }
        key = fingerprint([job["owner"], job_id])
        result["job"] = {k: v for k, v in job.items() if k != "owner"}
        result["operations"] = [
            {k: v for k, v in row.items() if k != "owner"}
            for row in operations
            if row["request_key"] == key
        ]
        result["workflow_holds"] = [
            {k: v for k, v in h.items() if k != "owner"} for h in holds if h["request_key"] == key
        ]
        result["decisions"] = [d for d in decisions if d["request_key"] == key]
    return result


def render(report):
    cfg = report["last_started_config"] or report["configured"]
    budget = report["budget"]
    lines = [
        "Poppy execution controls — operator snapshot",
        f"State: {'initialized' if report['initialized'] else 'not started'}",
        f"Scheduling: {cfg['scheduler']} | Admission: {cfg['admission']}",
        f"Capacity: {cfg['capacity']} | Per-user limit: {cfg['user_budget_units']} units",
        f"Company: {budget['company_limit']} limit | "
        f"{budget['settled_including_provisional']} settled (includes provisional) | "
        f"{budget['reserved']} reserved | {budget['remaining']} remaining",
        f"Receipts: {budget['verified_charges']} verified units | "
        f"{budget['provisional_charges']} provisional units | "
        f"{budget['unresolved_operations']} unresolved operations",
        "Jobs: " + (", ".join(f"{k}={v}" for k, v in report["job_counts"].items()) or "none"),
    ]
    if report["execution_contract_changed"]:
        lines.append("Execution contract changed: restore the original config or migrate state.")
    elif report["config_changed_since_start"]:
        lines.append("Configuration differs from last startup; restart required to apply changes.")
    if "job" in report:
        user = report["user_budget"]
        lines.append(
            f"This user's current budget: {user['limit']} limit | "
            f"{user['settled_including_provisional']} settled | {user['reserved']} reserved | "
            f"{user['remaining']} remaining"
        )
        lines.append(
            f"Job: {report['job']['state']} | quoted workflow bound: "
            f"{report['job']['estimate']} units | charged: {report['job']['charge']}"
        )
        lines.append(
            json.dumps(
                {k: report[k] for k in ("job", "operations", "workflow_holds", "decisions")},
                indent=2,
            )
        )
    else:
        lines.extend(
            f"  {j['id']}  {j['state']}  charged={j['charge']}" for j in report["recent_jobs"]
        )
    lines.append(report["snapshot_note"])
    return "\n".join(lines)
