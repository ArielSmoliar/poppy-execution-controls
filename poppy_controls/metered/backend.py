"""Local provider with independent, durable usage receipts and a fictional tariff.

Queries execute against a real SQLite catalogue. No response times or success
labels are sampled. Prices are execution units, not money or airline API prices.
"""

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import dataclass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def units(value):
    if type(value) is not int or value < 0 or value > 10**9:
        raise ValueError("Use nonnegative integer execution units, at most 1e9")
    return value


@dataclass(frozen=True)
class Receipt:
    key: str
    data: list
    actual_units: int
    usage: dict


class LocalAirlineAPI:
    """A provider SDK deliberately separate from the controller adapter contract.

    Every accepted operation atomically stores its result and meter receipt.
    Retrying the same key and payload never executes or bills a second query.
    One unit per executed query plus one per returned row; lookup is unbilled.
    """

    def __init__(self, path, fault="none", *, query_units=1, row_units=1):
        if fault not in ("none", "lost_response", "transient", "unavailable"):
            raise ValueError("Unknown provider fault")
        self.pricing = {"query_units": units(query_units), "row_units": units(row_units)}
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS flights (
                id TEXT PRIMARY KEY, route TEXT, fare_cents INTEGER,
                bag_cents INTEGER, status TEXT
            );
            CREATE TABLE IF NOT EXISTS receipts (
                key TEXT PRIMARY KEY, fingerprint TEXT, document TEXT
            );
            CREATE TABLE IF NOT EXISTS attempts (
                seq INTEGER PRIMARY KEY, key TEXT, kind TEXT
            );
        """)
        with self.db:
            for route in range(4):
                for flight in range(3 + route % 2):
                    self.db.execute(
                        "INSERT OR IGNORE INTO flights VALUES (?,?,?,?,?)",
                        (
                            f"R{route}-F{flight}",
                            f"R{route}",
                            18000 + 1700 * flight + 200 * route,
                            6000 if flight == 0 else 1500,
                            "cancelled" if flight == 2 else "operating",
                        ),
                    )
        self.fault = fault
        self.seen = set()

    def close(self):
        self.db.close()

    @staticmethod
    def validate(action, params):
        if not isinstance(params, dict):
            raise ValueError("Expected tool parameters")
        if action == "search":
            if (
                set(params) != {"route"}
                or not isinstance(params["route"], str)
                or params["route"] not in {"R0", "R1", "R2", "R3"}
            ):
                raise ValueError("Unknown route")
        elif action in ("fares", "status"):
            ids = params.get("ids")
            if (
                set(params) != {"ids"}
                or not isinstance(ids, list)
                or not 1 <= len(ids) <= 4
                or any(not isinstance(i, str) for i in ids)
                or len(set(ids)) != len(ids)
            ):
                raise ValueError("Expected one to four distinct flight IDs")
        else:
            raise ValueError("Unsupported tool")

    async def request(self, action, params, request_key):
        self.validate(action, params)
        await asyncio.sleep(0)  # Yield to concurrent callers; no simulated service delay.
        digest = fingerprint([action, params])
        prior = self.db.execute("SELECT * FROM receipts WHERE key=?", (request_key,)).fetchone()
        with self.db:
            self.db.execute(
                "INSERT INTO attempts(key,kind) VALUES (?,?)",
                (request_key, "retry" if prior else "request"),
            )
        if prior:
            if prior["fingerprint"] != digest:
                raise ValueError("Provider idempotency conflict")
            return json.loads(prior["document"])
        first = request_key not in self.seen
        self.seen.add(request_key)
        if self.fault == "unavailable" or (self.fault == "transient" and first):
            raise TimeoutError("No provider response")
        with self.db:
            if action == "search":
                rows = self.db.execute(
                    "SELECT id FROM flights WHERE route=? ORDER BY id LIMIT 4", (params["route"],)
                ).fetchall()
            else:
                columns = "id,fare_cents,bag_cents" if action == "fares" else "id,status"
                placeholders = ",".join("?" for _ in params["ids"])
                rows = self.db.execute(
                    f"SELECT {columns} FROM flights WHERE id IN ({placeholders}) ORDER BY id",
                    params["ids"],
                ).fetchall()
            document = {
                "request_key": request_key,
                "result": [dict(r) for r in rows],
                "usage": {"queries": 1, "returned_rows": len(rows)},
                "charge_units": self.pricing["query_units"] + self.pricing["row_units"] * len(rows),
                "tariff": {"kind": "fictional_execution_units", **self.pricing},
            }
            self.db.execute(
                "INSERT INTO receipts VALUES (?,?,?)", (request_key, digest, json.dumps(document))
            )
        if self.fault == "lost_response" and first:
            raise TimeoutError("Result lost AFTER provider metering committed")
        return document

    async def receipt(self, key):
        row = self.db.execute("SELECT document FROM receipts WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def meter(self):
        receipts = [json.loads(r[0]) for r in self.db.execute("SELECT document FROM receipts")]
        return {
            "actual_units": sum(r["charge_units"] for r in receipts),
            "executed_queries": sum(r["usage"]["queries"] for r in receipts),
            "returned_rows": sum(r["usage"]["returned_rows"] for r in receipts),
            "transport_attempts": self.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
            "receipts": receipts,
        }

    def expected(self, task):
        # Evaluator independently reads full catalogue; controllers never receive this oracle.
        candidates = []
        for row in self.db.execute("SELECT * FROM flights WHERE route=?", (task["route"],)):
            price = row["fare_cents"] + task["bags"] * row["bag_cents"]
            if row["status"] == "operating" and price <= task["max_fare_cents"]:
                candidates.append({"id": row["id"], "total_cents": price})
        return sorted(candidates, key=lambda r: (r["total_cents"], r["id"]))[:2]


class AirlineBackend:
    """Reference adapter; participants implement this contract independently."""

    def __init__(self, api):
        self.api = api

    def quote(self, action, params):
        self.api.validate(action, params)
        rows = 4 if action == "search" else len(params["ids"])
        return self.api.pricing["query_units"] + self.api.pricing["row_units"] * rows

    def quote_workflow(self, task):
        """Unbilled bound for three queries, each returning at most four rows.

        Uses tariff/shape metadata only, never a query or the evaluator's answer.
        """
        self.api.validate("search", {"route": task["route"]})
        return 3 * (self.api.pricing["query_units"] + 4 * self.api.pricing["row_units"])

    @staticmethod
    def convert(document):
        return Receipt(
            document["request_key"],
            document["result"],
            units(document["charge_units"]),
            document["usage"],
        )

    async def execute(self, action, params, key):
        return self.convert(await self.api.request(action, params, key))

    async def lookup(self, key):
        document = await self.api.receipt(key)
        return self.convert(document) if document else None


def build(api):
    return AirlineBackend(api)
