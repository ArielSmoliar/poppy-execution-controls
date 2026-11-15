"""Durable signed-request inbox connected to the metered execution engine."""

import asyncio
import json
from pathlib import Path

from poppy_controls.config import contract_id, factory, public_config, validate_workflow
from poppy_controls.metered.backend import units
from poppy_controls.metered.engine import Engine
from poppy_controls.protocol.auth import ProtocolError, digest

PATH = "/api/workflows"


class Service:
    def __init__(self, store, clients, *, config, backend=None):
        self.store, self.clients, self.config = store, clients, config
        self.workers = {}
        self.closing, self.fatal = False, None
        self.engine = self.backend = None
        # Changes to implementation/options require a new state directory or an explicit migration.
        contract = contract_id(config)
        previous = store.db.execute("SELECT value FROM metadata WHERE key='contract'").fetchone()
        if previous and previous[0] != contract:
            raise ValueError("Execution contract changed; use a new state directory or migrate it")
        try:
            self.workflow = factory(config["workflow_factory"])(config["workflow_options"])
            self.backend = (
                backend
                if backend is not None
                else factory(config["backend_factory"])(
                    Path(config["state_dir"]), config["backend_options"]
                )
            )
            for name in ("quote", "quote_workflow", "execute", "lookup", "close"):
                if not callable(getattr(self.backend, name, None)):
                    raise ValueError(f"Backend must implement {name}")
            validate_workflow(self.workflow)
            self.engine = Engine(
                Path(config["state_dir"]) / "journal.sqlite",
                self.backend,
                config["scheduler"],
                user_budget=config["user_budget_units"],
                provider_budget=config["company_budget_units"],
                capacity=config["capacity"],
                timeout=config["timeout_seconds"],
                retries=config["retries"],
                admission=config["admission"],
                workflow=self.workflow,
            )
            with store.db:
                store.db.execute(
                    "INSERT OR IGNORE INTO metadata VALUES ('contract',?)", (contract,)
                )
        except BaseException:
            if self.engine is not None:
                self.engine.close()
            if self.backend is not None and callable(getattr(self.backend, "close", None)):
                self.backend.close()
            raise

    async def start(self):
        await self.engine.start()  # Reconcile receipts before replaying the same operation IDs.
        with self.store.db:
            # Receipt recovery may revise a terminal uncertain job's provisional charge.
            # Its business outcome still needs review; do not restart it automatically.
            for row in self.store.db.execute("SELECT id,owner FROM jobs WHERE charge IS NOT NULL"):
                self.store.db.execute(
                    "UPDATE jobs SET charge=? WHERE id=?",
                    (self.engine.accounted(row["owner"], row["id"]), row["id"]),
                )
            self.store.db.execute("UPDATE jobs SET state='queued' WHERE state='running'")
            self.store.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('effective_config',?)",
                (json.dumps(public_config(self.config)),),
            )
        for job in self.store.queued():
            self.enqueue(job)

    @property
    def available(self):
        return not self.closing and not self.fatal and not self.engine.fatal

    async def submit(self, identity, request_id, payload):
        if not self.available:
            raise ProtocolError("unavailable", 503, {"Retry-After": "1"})
        # Accepted retries bypass today's validators, price quotes and admission limits.
        existing = self.store.db.execute(
            "SELECT * FROM jobs WHERE client=? AND sub=? AND request_id=?",
            (identity.client, identity.user, request_id),
        ).fetchone()
        if existing:
            try:
                matches = existing["fingerprint"] == digest(["POST", PATH, payload])
            except (TypeError, ValueError):
                matches = False
            if not matches:
                raise ProtocolError("request_id_conflict", 409)
            return dict(existing)
        if not isinstance(payload, dict) or set(payload) & {"owner", "id"}:
            raise ProtocolError("invalid_request")
        task = {**payload, "owner": identity.owner, "id": "validation"}
        try:
            self.workflow.validate(task)
            estimate = units(self.backend.quote_workflow(task))
            json.dumps(payload, allow_nan=False)
        except (ValueError, TypeError, KeyError):
            raise ProtocolError("invalid_request") from None
        job, created = self.store.accept(
            identity,
            request_id,
            payload,
            estimate,
            self.config["max_pending"],
            self.config["rate_per_minute"],
            path=PATH,
        )
        if created:
            self.enqueue(job)
        return job

    def enqueue(self, job):
        task = asyncio.create_task(self.execute(job))
        self.workers[job["id"]] = task
        task.add_done_callback(lambda _: self.workers.pop(job["id"], None))

    async def execute(self, job):
        try:
            if job["client"] not in self.clients or not self.store.session_active(job):
                self.engine.abandon(job["owner"], job["id"])
                result = {"state": "rejected", "error": "session_ended"}
            else:
                self.store.running(job["id"])
                task = {**json.loads(job["payload"]), "owner": job["owner"], "id": job["id"]}

                def authorize():
                    if job["client"] not in self.clients or not self.store.session_active(job):
                        raise ProtocolError("session_ended")

                try:
                    result = await self.engine.run(task, authorize=authorize)
                except ProtocolError:
                    result = {"state": "rejected", "error": "session_ended"}
            self.store.finish(
                job["id"], result["state"], self.engine.accounted(job["owner"], job["id"]), result
            )
        except asyncio.CancelledError:
            raise  # Leave the durable row for startup recovery; never invent a refund.
        except Exception as error:
            self.fatal = type(error).__name__  # Stop admission; preserve recoverable evidence.
            self.engine.fatal = True

    def balances(self, identity):
        key = "user:" + identity.owner
        spent, reserved = self.engine.ledger.spent(key), self.engine.ledger.reserved(key)
        limit = self.config["user_budget_units"]
        return {
            "units": "execution_units",
            "limit": limit,
            "spent": spent,
            "reserved": reserved,
            "remaining": max(0, limit - spent - reserved),
        }

    async def drain(self):
        while self.workers:
            await asyncio.gather(*list(self.workers.values()))
        if self.fatal:
            raise RuntimeError(f"Execution stopped: {self.fatal}")

    async def close(self):
        self.closing = True
        tasks = list(self.workers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            self.engine.close()
        finally:
            self.backend.close()
