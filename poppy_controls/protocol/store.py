"""Single-writer SQLite inbox, idempotency records, and durable cost attribution."""

from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from poppy_controls.protocol.auth import ProtocolError, digest


class Store:
    def __init__(self, path, company):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = path.with_suffix(path.suffix + ".lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Reference service requires a single process per database") from None
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                client TEXT, sid TEXT, sub TEXT, agent TEXT, version TEXT, extensions TEXT,
                expires REAL, ended INTEGER DEFAULT 0, PRIMARY KEY(client,sid)
            );
            CREATE TABLE IF NOT EXISTS proofs (
                client TEXT, jti TEXT, expires REAL, PRIMARY KEY(client,jti)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                client TEXT, sub TEXT, sid TEXT, owner TEXT, provider TEXT,
                request_id TEXT, fingerprint TEXT, payload TEXT,
                state TEXT NOT NULL, estimate INTEGER NOT NULL, charge REAL,
                accepted REAL, started REAL, finished REAL, result TEXT,
                UNIQUE(client,sub,request_id)
            );
            CREATE INDEX IF NOT EXISTS pending_jobs ON jobs(state,seq);
        """)
        previous = self.db.execute("SELECT value FROM metadata WHERE key='company'").fetchone()
        if previous and previous[0] != company:
            self.close()
            raise ValueError("A database cannot be reused for a different company")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('company',?)", (company,))

    def close(self):
        self.db.close()
        fcntl.flock(self.lock, fcntl.LOCK_UN)
        self.lock.close()

    def authenticate(self, identity, jti, lifetime=3600):
        now = time.time()
        with self.db:
            row = self.db.execute(
                "SELECT * FROM sessions WHERE client=? AND sid=?",
                (identity.client, identity.session),
            ).fetchone()
            if row:
                if (
                    row["sub"] != identity.user
                    or row["agent"] != identity.agent
                    or row["version"] != identity.version
                    or row["extensions"] != identity.extensions
                ):
                    raise ProtocolError("invalid_session")
                if row["ended"] or row["expires"] <= now:
                    raise ProtocolError("session_ended")
            else:
                self.db.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,0)",
                    (
                        identity.client,
                        identity.session,
                        identity.user,
                        identity.agent,
                        identity.version,
                        identity.extensions,
                        now + lifetime,
                    ),
                )
            self.db.execute("DELETE FROM proofs WHERE expires < ?", (now,))
            try:
                self.db.execute(
                    "INSERT INTO proofs VALUES (?,?,?)", (identity.client, jti, now + 61)
                )
            except sqlite3.IntegrityError:
                raise ProtocolError("invalid_session") from None

    def session_active(self, job):
        row = self.db.execute(
            "SELECT * FROM sessions WHERE client=? AND sid=? AND sub=?",
            (job["client"], job["sid"], job["sub"]),
        ).fetchone()
        return row is not None and not row["ended"] and row["expires"] > time.time()

    def end_session(self, identity):
        with self.db:
            self.db.execute(
                "UPDATE sessions SET ended=1 WHERE client=? AND sid=? AND sub=?",
                (identity.client, identity.session, identity.user),
            )

    def accept(
        self,
        identity,
        request_id,
        payload,
        estimate,
        max_pending,
        rate_per_minute,
        *,
        path="/api/searches",
    ):
        now = time.time()
        fingerprint = digest(["POST", path, payload])
        with self.db:
            existing = self.db.execute(
                "SELECT * FROM jobs WHERE client=? AND sub=? AND request_id=?",
                (identity.client, identity.user, request_id),
            ).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ProtocolError("request_id_conflict", 409)
                return dict(existing), False
            recent = self.db.execute(
                "SELECT accepted FROM jobs WHERE client=? AND accepted>? ORDER BY accepted",
                (identity.client, now - 60),
            ).fetchall()
            if len(recent) >= rate_per_minute:
                wait = max(1, int(recent[0][0] + 60 - now) + 1)
                raise ProtocolError("rate_limited", 429, {"Retry-After": str(wait)})
            pending = self.db.execute(
                "SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')"
            ).fetchone()[0]
            if pending >= max_pending:
                raise ProtocolError("overloaded", 503, {"Retry-After": "1"})
            job_id = uuid4().hex
            self.db.execute(
                """INSERT INTO jobs
                (id,client,sub,sid,owner,provider,request_id,fingerprint,payload,state,estimate,accepted)
                VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                (
                    job_id,
                    identity.client,
                    identity.user,
                    identity.session,
                    identity.owner,
                    identity.provider,
                    request_id,
                    fingerprint,
                    json.dumps(payload),
                    estimate,
                    now,
                ),
            )
        return self.job(job_id), True

    def job(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def queued(self):
        return [
            dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY seq")
        ]

    def recover(self):
        # Never automatically repeat work whose external outcome may already exist.
        with self.db:
            self.db.execute(
                """UPDATE jobs SET state='uncertain', charge=estimate, finished=?,
                result=? WHERE state='running'""",
                (
                    time.time(),
                    json.dumps({"error": "interrupted_execution", "charge_is_estimate": True}),
                ),
            )

    def spent(self):
        totals = {}
        for row in self.db.execute(
            "SELECT owner,provider,charge FROM jobs WHERE charge IS NOT NULL"
        ):
            for key in ("user:" + row["owner"], "provider:" + row["provider"]):
                totals[key] = totals.get(key, 0) + row["charge"]
        return totals

    def running(self, job_id):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='running', started=? WHERE id=? AND state='queued'",
                (time.time(), job_id),
            )

    def finish(self, job_id, state, charge, result):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state=?, charge=?, result=?, finished=? WHERE id=?",
                (state, charge, json.dumps(result, allow_nan=False), time.time(), job_id),
            )
