"""Local independent provider with atomic receipt storage and test fault injection.

This implements the provider guarantees that a real company adapter must obtain
from its upstream API. The local catalogue and execution-unit tariff are fictional.
"""

import asyncio
import json
import sqlite3

from poppy_controls.metered.backend import Receipt, fingerprint


class OrderBackend:
    def __init__(self, state_dir, options):
        if set(options) - {"fault"} or options.get("fault", "none") not in (
            "none",
            "lost_response",
            "unavailable",
            "pause_after_commit",
        ):
            raise ValueError("Unsupported order backend options")
        self.fault = options.get("fault", "none")
        self.state_dir = state_dir
        self.db = sqlite3.connect(state_dir / "orders-provider.sqlite")
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS receipts (
                key TEXT PRIMARY KEY, fingerprint TEXT, document TEXT
            );
            CREATE TABLE IF NOT EXISTS attempts (key TEXT, action TEXT);
        """)

    def quote(self, action, params):
        expected = {"order": {"order_id": "ORDER-100"}, "delivery": {"shipment_id": "SHIP-100"}}
        if action not in expected or params != expected[action]:
            raise ValueError("Unknown order tool or parameters")
        return 4  # Conservative bound; actual tariff below is 2 or 3.

    def quote_workflow(self, task):
        self.quote("order", {"order_id": task["order_id"]})
        return 8

    async def execute(self, action, params, key):
        self.quote(action, params)
        digest = fingerprint([action, params])
        with self.db:
            self.db.execute("INSERT INTO attempts VALUES (?,?)", (key, action))
            prior = self.db.execute(
                "SELECT fingerprint,document FROM receipts WHERE key=?", (key,)
            ).fetchone()
            if prior:
                if prior[0] != digest:
                    raise ValueError("Provider idempotency conflict")
                return Receipt(**json.loads(prior[1]))
            if self.fault == "unavailable":
                raise TimeoutError("No response; billing outcome unknown to the controller")
            data = (
                [{"order_id": "ORDER-100", "shipment_id": "SHIP-100"}]
                if action == "order"
                else [{"shipment_id": "SHIP-100", "status": "in_transit"}]
            )
            document = {
                "key": key,
                "data": data,
                "actual_units": 2 if action == "order" else 3,
                "usage": {"lookups": 1},
            }
            self.db.execute(
                "INSERT INTO receipts VALUES (?,?,?)", (key, digest, json.dumps(document))
            )
        if action == "order" and self.fault == "pause_after_commit":
            (self.state_dir / "provider-committed").write_text(key)
            await asyncio.Event().wait()  # Audit kills the process, then recovers this receipt.
        if self.fault == "lost_response":
            raise TimeoutError("Response lost after durable provider commit")
        await asyncio.sleep(0)
        return Receipt(**document)

    async def lookup(self, key):
        row = self.db.execute("SELECT document FROM receipts WHERE key=?", (key,)).fetchone()
        return Receipt(**json.loads(row[0])) if row else None

    def close(self):
        self.db.close()


def build(state_dir, options):
    return OrderBackend(state_dir, options)
