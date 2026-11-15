"""Reproducible CLI/HTTP onboarding audit against an installed wheel, outside the repo.

Run with the isolated environment's Python and --output PATH.
No external service is contacted. Timings are automated command time, not human onboarding time.
"""

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import company_support
import httpx

import poppy_controls


class Audit:
    def __init__(self, output):
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.root = Path(tempfile.mkdtemp(prefix="poppy-company-onboarding-"))
        self.binary = str(Path(sys.executable).parent / "poppy-controls")
        self.events, self.results = [], {}
        self.env = dict(os.environ)
        self.env.pop("PYTHONPATH", None)
        self.started = time.monotonic()
        for module in (poppy_controls, company_support):
            assert "/site-packages/" in module.__file__, module.__file__

    def command(self, name, *args, expected=0):
        start = time.monotonic()
        result = subprocess.run(
            [self.binary, *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=40,
        )
        self.events.append(
            {
                "name": name,
                "args": list(args),
                "seconds": time.monotonic() - start,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if expected is not None:
            assert result.returncode == expected, self.events[-1]
        return result

    def setup(self, name, **settings):
        directory = self.root / name
        self.command(name + ":init", "init", "--directory", str(directory))
        path = directory / "controls.json"
        cfg = json.loads(path.read_text())
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cfg.update(
            {
                "backend_factory": "company_support.backend:build",
                "workflow_factory": "company_support.workflow:build",
                "contract_version": "order-support-v1",
                "user_budget_units": 8,
                "company_budget_units": 80,
                "port": port,
                **settings,
            }
        )
        path.write_text(json.dumps(cfg, indent=2))
        (directory / "request.json").write_text('{"order_id":"ORDER-100"}\n')
        return path

    @contextmanager
    def serve(self, config, name):
        cfg = json.loads(config.read_text())
        log = (self.output / (name + "-server.log")).open("w")
        server = subprocess.Popen(
            [self.binary, "serve", "--config", str(config)],
            cwd=self.root,
            env=self.env,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(trust_env=False, timeout=1) as client:
                for _ in range(200):
                    assert server.poll() is None, f"Server failed: {name}"
                    try:
                        if client.get(f"http://127.0.0.1:{cfg['port']}/healthz").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.025)
                else:
                    raise TimeoutError("Server did not become ready")
            yield server
        finally:
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=10)
            log.close()

    def inspect(self, config, job=None):
        args = ["inspect", "--config", str(config), "--json"]
        if job:
            args += ["--job", job]
        return json.loads(self.command("inspect", *args).stdout)

    def submit(self, config, request_id, *, wait=True):
        args = [
            "submit",
            "--config",
            str(config),
            "--payload",
            str(config.parent / "request.json"),
            "--request-id",
            request_id,
        ]
        if not wait:
            args.append("--no-wait")
        return json.loads(self.command("submit:" + request_id, *args).stdout)

    def meter(self, config):
        db = sqlite3.connect(config.parent / "state" / "orders-provider.sqlite")
        try:
            receipts = [json.loads(r[0]) for r in db.execute("SELECT document FROM receipts")]
            return {
                "receipts": len(receipts),
                "actual_units": sum(r["actual_units"] for r in receipts),
                "attempts": db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0],
            }
        finally:
            db.close()

    def run(self):
        self.command("help", "--help")
        # A missing company plugin should not suggest reinstalling the controls package.
        broken = self.setup("bad-factory", backend_factory="not_installed_company:build")
        self.command("missing-plugin", "check", "--config", str(broken), expected=2)
        # Returning an object with a schema but no workflow methods must fail check, not serve.
        bad_module = Path(company_support.__file__).parent / "bad_workflow.py"
        bad_module.write_text(
            "class Bad:\n    input_schema = {}\ndef build(options): return Bad()\n"
        )
        broken_workflow = self.setup(
            "bad-workflow", workflow_factory="company_support.bad_workflow:build"
        )
        self.command(
            "missing-workflow-methods",
            "check",
            "--config",
            str(broken_workflow),
            expected=2,
        )
        for scheduler in ("fifo",):
            config = self.setup(scheduler, scheduler=scheduler)
            self.command("check:" + scheduler, "check", "--config", str(config))
            assert not (config.parent / "state").exists()
            with self.serve(config, scheduler):
                self.command("custom-demo", "demo", "--config", str(config), expected=2)
                first = self.submit(config, "order-1")
                repeated = self.submit(config, "order-1")
                rejected = self.submit(config, "order-2")
                assert first == repeated
                assert first["state"] == "completed" and first["charged_units"] == 5
                assert first["result"]["delivery"]["status"] == "in_transit"
                assert rejected["state"] == "budget_rejected" and rejected["charged_units"] == 0
                report = self.inspect(config, rejected["job_id"])
                assert report["budget"]["verified_charges"] == 5
                assert report["decisions"] and not report["operations"]
                assert report["user_budget"]["remaining"] == 3
                self.command(
                    "inspect-text", "inspect", "--config", str(config), "--job", rejected["job_id"]
                )
                meter = self.meter(config)
                assert meter["receipts"] == 2 and meter["actual_units"] == 5
                self.results[scheduler] = {
                    "first": first,
                    "rejected": rejected,
                    "meter": meter,
                    "inspection": report,
                }
            # Detect changes to a pinned contract before trying to start the server.
            original = config.read_text()
            cfg = json.loads(original)
            cfg["contract_version"] = "changed-version"
            config.write_text(json.dumps(cfg))
            self.command(
                "changed-contract",
                "check",
                "--config",
                str(config),
                expected=2,
            )
            config.write_text(original)
        # Lost response: retries use the same provider operation key, no extra billing.
        lost = self.setup("lost-response", backend_options={"fault": "lost_response"})
        with self.serve(lost, "lost-response"):
            result = self.submit(lost, "lost")
            assert result["state"] == "completed" and result["charged_units"] == 5
            meter = self.meter(lost)
            assert meter == {"receipts": 2, "actual_units": 5, "attempts": 4}
            self.results["lost_response"] = meter
        unknown = self.setup("unknown", backend_options={"fault": "unavailable"})
        with self.serve(unknown, "unknown"):
            result = self.submit(unknown, "unknown")
            report = self.inspect(unknown, result["job_id"])
            assert result["state"] == "uncertain" and result["charged_units"] == 4
            assert report["budget"]["provisional_charges"] == 4
            assert report["budget"]["reserved"] == 0
            self.results["unknown"] = report["budget"]
        interrupted = self.setup(
            "interrupted", backend_options={"fault": "pause_after_commit"}, timeout_seconds=30
        )
        with self.serve(interrupted, "interrupted") as server:
            accepted = self.submit(interrupted, "resume-me", wait=False)
            marker = interrupted.parent / "state" / "provider-committed"
            for _ in range(200):
                if marker.exists():
                    break
                time.sleep(0.025)
            assert marker.exists()
            held = self.inspect(interrupted, accepted["job_id"])
            assert held["budget"]["reserved"] == 8
            server.kill()  # Actual abrupt process death after independent provider commit.
            server.wait(timeout=10)
        assert self.inspect(interrupted)["budget"]["reserved"] == 8
        with self.serve(interrupted, "recovered"):
            resumed = self.submit(interrupted, "resume-me")
            assert resumed["state"] == "completed" and resumed["charged_units"] == 5
            assert self.meter(interrupted) == {"receipts": 2, "actual_units": 5, "attempts": 2}
            final = self.inspect(interrupted, resumed["job_id"])
            assert final["budget"]["reserved"] == 0
            self.results["recovery"] = {"before": held["budget"], "after": final["budget"]}
        return {
            "passed": True,
            "phase": "standalone",
            "elapsed_seconds": time.monotonic() - self.started,
            "timing_scope": (
                "Automated CLI and HTTP exercise; excludes installation, reading and coding."
            ),
            "environment": {
                "python": sys.version,
                "root": str(self.root),
                "package": poppy_controls.__file__,
                "plugin": company_support.__file__,
            },
            "events": self.events,
            "results": self.results,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    audit = Audit(args.output)
    try:
        report = audit.run()
    except BaseException:
        (audit.output / "partial.json").write_text(json.dumps(audit.events, indent=2))
        raise
    report["audit_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    (audit.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {k: report[k] for k in ("passed", "phase", "elapsed_seconds", "timing_scope")}, indent=2
        )
    )


if __name__ == "__main__":
    main()
