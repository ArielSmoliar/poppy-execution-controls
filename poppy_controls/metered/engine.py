"""Durable budgeted workflow execution and recovery.

One process per journal, enforced by a file lock. Backend receipts are a separate
source of truth. Unknown outcomes are provisionally charged, never silently freed.
"""

import asyncio
import fcntl
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from poppy_controls.metered.backend import Receipt, fingerprint, units
from poppy_controls.metered.dispatch import Dispatcher
from poppy_controls.metered.ledger import AtomicLedger, DeniedError
from poppy_controls.metered.workflow import FlightWorkflow


class UncertainError(Exception):
    pass


@dataclass
class WorkflowHold:
    key: str
    request_key: str
    owner: str
    bound: int
    tokens: object = None


class Journal:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = path.with_suffix(".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Use one process per metered journal") from None
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                seq INTEGER PRIMARY KEY, key TEXT UNIQUE, owner TEXT, action TEXT,
                fingerprint TEXT, estimate INTEGER, charge INTEGER, state TEXT,
                result TEXT, created REAL, finished REAL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                seq INTEGER PRIMARY KEY, request_key TEXT, step TEXT, reason TEXT, created REAL
            );
            CREATE TABLE IF NOT EXISTS workflow_holds (
                key TEXT PRIMARY KEY, request_key TEXT, owner TEXT, bound INTEGER, state TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS open_workflow_request
                ON workflow_holds(request_key) WHERE state='open';
        """)
        if "workflow_key" not in {r[1] for r in self.db.execute("PRAGMA table_info(operations)")}:
            self.db.execute("ALTER TABLE operations ADD COLUMN workflow_key TEXT")
        if "request_key" not in {r[1] for r in self.db.execute("PRAGMA table_info(operations)")}:
            self.db.execute("ALTER TABLE operations ADD COLUMN request_key TEXT")
        # Create after the legacy-column migration. Recovery and inspection query by
        # request/hold; scanning all historical operations for each job is quadratic.
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS operation_request ON operations(request_key);
            CREATE INDEX IF NOT EXISTS operation_workflow ON operations(workflow_key);
            CREATE INDEX IF NOT EXISTS workflow_request ON workflow_holds(request_key);
        """)

    def close(self):
        self.db.close()
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()

    def request(self, owner, task):
        key, digest = fingerprint([owner, task["id"]]), fingerprint(task)
        with self.db:
            row = self.db.execute("SELECT fingerprint FROM requests WHERE key=?", (key,)).fetchone()
            if row and row[0] != digest:
                raise ValueError("Request ID reused with different content")
            self.db.execute("INSERT OR IGNORE INTO requests VALUES (?,?)", (key, digest))

    def get(self, key):
        row = self.db.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def begin(self, key, owner, action, params, estimate, workflow_key=None, request_key=None):
        with self.db:
            self.db.execute(
                """INSERT INTO operations
                (key,owner,action,fingerprint,estimate,state,created,workflow_key,request_key)
                VALUES (?,?,?,?,?,'running',?,?,?)""",
                (
                    key,
                    owner,
                    action,
                    fingerprint([action, params]),
                    estimate,
                    time.time(),
                    workflow_key,
                    request_key,
                ),
            )

    def decision(self, request_key, step, reason):
        with self.db:
            self.db.execute(
                "INSERT INTO decisions(request_key,step,reason,created) VALUES (?,?,?,?)",
                (request_key, step, reason, time.time()),
            )

    def request_rows(self, request_key):
        return [
            dict(r)
            for r in self.db.execute(
                """SELECT * FROM operations WHERE request_key=? OR workflow_key IN
            (SELECT key FROM workflow_holds WHERE request_key=?) ORDER BY seq""",
                (request_key, request_key),
            )
        ]

    def hold_begin(self, flow):
        with self.db:
            self.db.execute(
                "INSERT INTO workflow_holds VALUES (?,?,?,?,'open')",
                (flow.key, flow.request_key, flow.owner, flow.bound),
            )

    def hold_close(self, key):
        with self.db:
            self.db.execute("UPDATE workflow_holds SET state='closed' WHERE key=?", (key,))

    def holds(self, open_only=False):
        where = " WHERE state='open'" if open_only else ""
        return [dict(r) for r in self.db.execute("SELECT * FROM workflow_holds" + where)]

    def flow_rows(self, key):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM operations WHERE workflow_key=? ORDER BY seq", (key,)
            )
        ]

    def flow_charge(self, key):
        return sum(r["charge"] or 0 for r in self.flow_rows(key))

    def finish(self, key, state, charge, result):
        encoded = json.dumps(result, allow_nan=False)
        with self.db:
            self.db.execute(
                """UPDATE operations SET state=?,charge=?,result=?,finished=?
                WHERE key=?""",
                (state, units(charge), encoded, time.time(), key),
            )

    def totals(self):
        totals = {"provider": 0}
        for row in self.db.execute("SELECT owner,charge FROM operations WHERE charge IS NOT NULL"):
            key = "user:" + row["owner"]
            totals[key] = totals.get(key, 0) + row["charge"]
            totals["provider"] += row["charge"]
        return totals

    def rows(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM operations ORDER BY seq")]


def validate_receipt(receipt, key):
    if not isinstance(receipt, Receipt) or receipt.key != key:
        raise ValueError("Receipt must belong to this operation")
    units(receipt.actual_units)
    if not isinstance(receipt.data, list) or not isinstance(receipt.usage, dict):
        raise ValueError("Invalid receipt data/usage")
    json.dumps(receipt.data, allow_nan=False)
    json.dumps(receipt.usage, allow_nan=False)
    return receipt


class Engine:
    def __init__(
        self,
        path,
        backend,
        controller="fifo",
        *,
        user_budget=100,
        provider_budget=10000,
        capacity=4,
        timeout=2,
        retries=1,
        admission="tool",
        workflow=None,
    ):
        if controller != "fifo":
            raise ValueError("Only fifo scheduling is supported")
        if admission not in ("tool", "workflow"):
            raise ValueError("Expected tool or workflow admission")
        if type(retries) is not int or retries < 0 or retries > 3 or timeout <= 0:
            raise ValueError("Invalid retry/timeout limits")
        self.user_budget, self.provider_budget = units(user_budget), units(provider_budget)
        self.backend = backend
        self.workflow = workflow if workflow is not None else FlightWorkflow()
        self.admission, self.flows = admission, {}
        self.dispatch = Dispatcher(controller, capacity, 1000)
        self.ledger = AtomicLedger()
        self.journal = Journal(path)
        self.timeout, self.retries = timeout, retries
        self.locks, self.request_locks = {}, {}
        self.sequence = 0
        self.active = self.peak = self.retried = 0
        self.ready, self.fatal = False, False

    async def start(self):
        if self.ready:
            raise RuntimeError("Engine already started")
        if self.admission != "workflow" and self.journal.holds(open_only=True):
            raise ValueError("Resume open workflow commitments with workflow admission")
        for row in self.journal.rows():
            if row["state"] in ("running", "uncertain"):
                try:
                    receipt = await asyncio.wait_for(self.backend.lookup(row["key"]), self.timeout)
                    if receipt is not None:
                        validate_receipt(receipt, row["key"])
                except Exception:
                    receipt = None
                if receipt is None:
                    self.journal.finish(row["key"], "uncertain", row["estimate"], None)
                else:
                    self.journal.finish(row["key"], "completed", receipt.actual_units, receipt.data)
        totals = self.journal.totals()
        restored = []
        for hold in self.journal.holds(open_only=True):
            rows = self.journal.flow_rows(hold["key"])
            charged = sum(r["charge"] or 0 for r in rows)
            if any(r["state"] != "completed" for r in rows) or charged > hold["bound"]:
                # No automatic continuation of an unknown call or an exceeded envelope.
                self.journal.hold_close(hold["key"])
                continue
            totals["provider"] -= charged
            user = "user:" + hold["owner"]
            totals[user] = totals.get(user, 0) - charged
            restored.append(
                WorkflowHold(hold["key"], hold["request_key"], hold["owner"], hold["bound"])
            )
        self.ledger.restore(totals)
        for flow in restored:
            flow.tokens = self.ledger.restore_hold(("user:" + flow.owner, "provider"), flow.bound)
            self.flows[flow.request_key] = flow
        self.ready = True

    def close(self):
        if self.active or self.dispatch.active:
            raise RuntimeError("Await or cancel active work before closing")
        self.journal.close()

    def prepare_workflow(self, task):
        """Get the active envelope; reserve only at the first scheduler-selected tool grant."""
        request_key = fingerprint([task["owner"], task["id"]])
        if request_key not in self.flows:
            bound = units(self.backend.quote_workflow(task))
            # A previously closed partial request may be resumed with a new envelope;
            # already settled cached steps are not charged or reserved a second time.
            prior = {r["key"]: r for r in self.journal.request_rows(request_key)}
            if isinstance(self.workflow, FlightWorkflow):  # Legacy journals lacked request_key.
                for step in ("search", "fares", "status"):
                    row = self.journal.get(fingerprint([task["owner"], task["id"], step]))
                    if row:
                        prior[row["key"]] = row
            paid = sum(r["charge"] or 0 for r in prior.values())
            self.flows[request_key] = WorkflowHold(
                uuid4().hex, request_key, task["owner"], max(0, bound - paid)
            )
        return self.flows[request_key]

    def accounted(self, owner, task_id):
        return sum(
            r["charge"] or 0 for r in self.journal.request_rows(fingerprint([owner, task_id]))
        )

    def abandon(self, owner, task_id):
        flow = self.flows.get(fingerprint([owner, task_id]))
        if flow is not None:
            self.finish_workflow(flow)

    def admit_workflow(self, flow):
        if flow.tokens is None:
            flow.tokens = self.ledger.reserve(
                {"user:" + flow.owner: self.user_budget, "provider": self.provider_budget},
                flow.bound,
            )
            try:
                self.journal.hold_begin(flow)
            except BaseException:
                self.ledger.cancel(flow.tokens)
                flow.tokens = None
                raise

    def finish_workflow(self, flow):
        if flow.tokens is not None and not self.fatal:
            if any(r["state"] == "running" for r in self.journal.flow_rows(flow.key)):
                self.fatal = True
                return
            try:
                self.journal.hold_close(flow.key)
                self.ledger.settle(flow.tokens, self.journal.flow_charge(flow.key))
                flow.tokens = None
            except BaseException:
                self.fatal = True
                raise
        if flow.tokens is None:
            self.flows.pop(flow.request_key, None)

    async def call(self, owner, task_id, step, action, params, *, flow=None, authorize=None):
        if not self.ready or self.fatal:
            raise RuntimeError("Engine is not available")
        if (self.admission == "workflow") != (flow is not None):
            raise ValueError("Workflow admission requires an envelope from Engine.run")
        if flow is not None and (
            flow.owner != owner
            or flow.request_key != fingerprint([owner, task_id])
            or self.flows.get(flow.request_key) is not flow
        ):
            raise ValueError("Workflow envelope belongs to another request")
        key = fingerprint([owner, task_id, step])
        async with self.locks.setdefault(key, asyncio.Lock()):
            prior = self.journal.get(key)
            if prior:
                if prior["fingerprint"] != fingerprint([action, params]):
                    raise ValueError("Operation ID reused with different content")
                if prior["state"] == "completed":
                    return json.loads(prior["result"])
                raise UncertainError("An earlier outcome needs reconciliation; not retried")
            estimate = units(self.backend.quote(action, params))
            self.sequence += 1
            job = {
                "id": key,
                "owner": owner,
                "seq": self.sequence,
                "estimate": estimate,
                "sid": task_id,
            }
            await self.dispatch.enqueue(job)
            handle = await self.dispatch.acquire(job)
            tokens = None
            try:
                if self.fatal:
                    raise RuntimeError("Execution stopped after an uncertain persistence failure")
                if authorize is not None:
                    authorize()
                if flow is None:
                    tokens = self.ledger.reserve(
                        {"user:" + owner: self.user_budget, "provider": self.provider_budget},
                        estimate,
                    )
                else:
                    self.admit_workflow(flow)
                    if estimate + self.journal.flow_charge(flow.key) > flow.bound:
                        raise DeniedError("Tool quote exceeds remaining workflow envelope")
                try:
                    self.journal.begin(
                        key,
                        owner,
                        action,
                        params,
                        estimate,
                        workflow_key=flow.key if flow else None,
                        request_key=fingerprint([owner, task_id]),
                    )
                except BaseException:
                    if tokens is not None:
                        self.ledger.cancel(tokens)
                    tokens = None
                    raise
                self.active += 1
                self.peak = max(self.peak, self.active)
                receipt = None
                try:
                    for attempt in range(self.retries + 1):
                        try:
                            receipt = validate_receipt(
                                await asyncio.wait_for(
                                    self.backend.execute(action, params, key), self.timeout
                                ),
                                key,
                            )
                            break
                        except (TimeoutError, asyncio.TimeoutError):
                            if attempt < self.retries:
                                self.retried += 1
                    if receipt is None:
                        recovered = await asyncio.wait_for(self.backend.lookup(key), self.timeout)
                        if recovered is not None:
                            receipt = validate_receipt(recovered, key)
                except asyncio.CancelledError:
                    # Keep the durable running row; startup reconciles it against provider metering.
                    self.fatal = True
                    raise
                except Exception:
                    # A malformed response or unknown outcome cannot justify releasing its budget.
                    receipt = None
                finally:
                    self.active -= 1
                state, charge, data = (
                    ("uncertain", estimate, None)
                    if receipt is None
                    else ("completed", receipt.actual_units, receipt.data)
                )
                try:
                    self.journal.finish(key, state, charge, data)
                    if tokens is not None:
                        self.ledger.settle(tokens, charge)
                    tokens = None
                except BaseException:
                    self.fatal = True
                    raise
                if receipt is None:
                    raise UncertainError(
                        "No verified receipt; estimate retained as provisional charge"
                    )
                return data
            except DeniedError as error:
                self.journal.decision(fingerprint([owner, task_id]), step, str(error))
                raise
            finally:
                await self.dispatch.release(job, handle)

    async def run(self, task, *, authorize=None):
        if not isinstance(task, dict) or not all(
            isinstance(task.get(k), str) and task[k] for k in ("id", "owner")
        ):
            raise ValueError("Invalid task identity")
        self.workflow.validate(task)
        owner = task[
            "owner"
        ]  # Trusted application identity; never copy from an unauthenticated body.
        async with self.request_locks.setdefault((owner, task["id"]), asyncio.Lock()):
            self.journal.request(owner, task)
            flow = self.prepare_workflow(task) if self.admission == "workflow" else None
            try:
                tool_lock = asyncio.Lock()

                async def call(step, action, params):
                    # One tool at a time per workflow keeps its envelope accounting atomic.
                    async with tool_lock:
                        return await self.call(
                            owner, task["id"], step, action, params, flow=flow, authorize=authorize
                        )

                data = await self.workflow.execute(task, call)
                if not isinstance(data, dict) or "state" in data:
                    raise ValueError("Workflow must return a dictionary without a state field")
                json.dumps(data, allow_nan=False)
                return {"state": "completed", **data}
            except DeniedError:
                return {"state": "budget_rejected", "options": None}
            except UncertainError:
                return {"state": "uncertain", "options": None}
            finally:
                if flow is not None:
                    self.finish_workflow(flow)
