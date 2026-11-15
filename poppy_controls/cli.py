"""Company quickstart and local operator commands."""

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from poppy_controls.config import (
    DEFAULTS,
    check_state_contract,
    factory,
    load,
    public_config,
    strict_json,
    trust,
    validate_workflow,
)


def initialize(directory):
    from poppy_controls.protocol.client import initialize as initialize_identity

    directory = Path(directory).resolve()
    initialize_identity(directory)  # Refuses to overwrite an existing setup or key.
    directory.chmod(0o700)
    path = directory / "controls.json"
    path.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
    (directory / ".gitignore").write_text("agent-private.pem\nstate/\n")
    from importlib.resources import files

    (directory / "README.md").write_text(
        files("poppy_controls").joinpath("QUICKSTART.md").read_text()
    )
    (directory / "INTEGRATION.md").write_text(
        files("poppy_controls").joinpath("INTEGRATION.md").read_text()
    )
    return path


def check(path):
    cfg = load(path)
    clients = trust(cfg)
    # Imports configured code; reads existing metadata without starting the backend.
    factory(cfg["backend_factory"])
    workflow = factory(cfg["workflow_factory"])(cfg["workflow_options"])
    validate_workflow(workflow)
    check_state_contract(cfg)
    return {
        "valid": True,
        "trusted_clients": len(clients),
        "config": public_config(cfg),
        "note": "Backend connectivity, credentials and quote bounds are not checked.",
    }


def submit(path, payload, request_id=None, *, wait=True, user="demo-user", wait_seconds=30):
    """Local development client; generated keys are never sent to a remote endpoint."""
    import httpx

    from poppy_controls.protocol.auth import ID
    from poppy_controls.protocol.client import Signer

    cfg = load(path)
    if not cfg["local_http"]:
        raise ValueError("submit uses local demo keys and requires local_http with a loopback host")
    if not isinstance(payload, dict) or set(payload) & {"id", "owner"}:
        raise ValueError("Payload must be a JSON object without id or owner; identity is signed")
    encoded = json.dumps(payload, allow_nan=False).encode()
    if len(encoded) > 16384:
        raise ValueError("Payload must be at most 16384 bytes")
    request_id = request_id or uuid4().hex
    if not ID.fullmatch(request_id):
        raise ValueError("request-id must be 1–256 letters, digits, underscores or hyphens")
    if not user:
        raise ValueError("user must be a nonempty local test identity")
    if not 0 < wait_seconds <= 3600:
        raise ValueError("wait-seconds must be greater than 0 and at most 3600")
    signer = Signer.load(Path(cfg["trust_file"]).parent, user=user)
    host = f"[{cfg['host']}]" if ":" in cfg["host"] else cfg["host"]
    try:
        with httpx.Client(
            base_url=f"http://{host}:{cfg['port']}", timeout=10, trust_env=False
        ) as client:
            route = "/api/workflows"
            response = client.post(
                route,
                content=encoded,
                headers={
                    **signer.headers("POST", route),
                    "Idempotency-Key": request_id,
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            accepted = response.json()
            if not wait:
                return accepted
            job = accepted["job_id"]
            route += "/" + job
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                response = client.get(route, headers=signer.headers("GET", route))
                response.raise_for_status()
                result = response.json()
                if result["state"] not in ("queued", "running"):
                    return result
                time.sleep(0.05)
    except httpx.HTTPStatusError as error:
        response = error.response
        try:
            code = response.json().get("error", "request_rejected")
        except (ValueError, AttributeError):
            code = "request_rejected"
        advice = {
            400: "Check the payload against /openapi.json and the local demo trust configuration.",
            409: "Reuse the original payload and request ID, or choose a new ID for new work.",
            429: "Rate limited; keep the same request ID when retrying.",
            503: "Service unavailable or queue full; keep the same request ID when retrying.",
        }.get(response.status_code, "Check the service log and configuration.")
        retry = response.headers.get("Retry-After")
        raise RuntimeError(
            f"HTTP {response.status_code}: {code}. {advice}"
            + (f" Retry-After: {retry} seconds." if retry else "")
        ) from None
    except httpx.HTTPError as error:
        raise RuntimeError(
            "Local request failed. Start poppy-controls serve with this config and check its port. "
            f"Transport error: {type(error).__name__}"
        ) from None
    raise TimeoutError(
        f"Job {job} is still pending. Run inspect --job {job}, or resubmit with the SAME "
        f"--request-id {request_id} to poll again without duplicate work."
    )


def demo(path, request_id=None):
    cfg = load(path)
    if (
        cfg["backend_factory"] != DEFAULTS["backend_factory"]
        or cfg["workflow_factory"] != DEFAULTS["workflow_factory"]
    ):
        raise ValueError("Custom workflow configured. Use submit --payload request.json instead.")
    return submit(path, {"route": "R0", "bags": 1, "max_fare_cents": 50000}, request_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Poppy execution controls execution controls")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Generate a new local demo setup and signing key")
    init.add_argument("--directory", default="poppy-company")
    for name, help_text in (
        ("check", "Validate configuration, trust and compatibility without starting execution"),
        ("serve", "Run the signed API in one process"),
        ("demo", "Submit a signed flight workflow to the running local demo"),
        ("submit", "Submit custom JSON using local demo keys (loopback only)"),
        ("inspect", "Read budget and operation evidence without modifying state"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", default="poppy-company/controls.json")
        if name == "serve":
            command.add_argument("--ssl-certfile")
            command.add_argument("--ssl-keyfile")
        if name in ("demo", "submit"):
            command.add_argument("--request-id", help="Reuse to demonstrate request deduplication")
        if name == "submit":
            command.add_argument("--payload", required=True, help="Path to a JSON business payload")
            command.add_argument("--user", default="demo-user", help="Local test identity")
            command.add_argument(
                "--no-wait", action="store_true", help="Return after durable acceptance"
            )
            command.add_argument("--wait-seconds", type=float, default=30)
        if name == "inspect":
            command.add_argument("--job")
            command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            path = initialize(args.directory)
            print(
                f"Created {path}\nLocal demo keys only. Next: poppy-controls check --config {path}"
            )
        elif args.command == "check":
            print(json.dumps(check(args.config), indent=2))
        elif args.command == "inspect":
            from poppy_controls.inspect import render, snapshot

            report = snapshot(args.config, args.job)
            print(json.dumps(report, indent=2) if args.json else render(report))
        elif args.command == "demo":
            print(json.dumps(demo(args.config, args.request_id), indent=2))
        elif args.command == "submit":
            payload = strict_json(Path(args.payload).read_text())
            print(
                json.dumps(
                    submit(
                        args.config,
                        payload,
                        args.request_id,
                        wait=not args.no_wait,
                        user=args.user,
                        wait_seconds=args.wait_seconds,
                    ),
                    indent=2,
                )
            )
        else:
            import uvicorn

            from poppy_controls.app import create_app

            cfg = load(args.config)
            if bool(args.ssl_certfile) != bool(args.ssl_keyfile):
                raise ValueError("Supply both --ssl-certfile and --ssl-keyfile")
            if not cfg["local_http"] and not args.ssl_certfile:
                raise ValueError("HTTPS mode requires --ssl-certfile and --ssl-keyfile")
            uvicorn.run(
                create_app(args.config),
                host=cfg["host"],
                port=cfg["port"],
                workers=1,
                proxy_headers=False,
                ssl_certfile=args.ssl_certfile,
                ssl_keyfile=args.ssl_keyfile,
            )
    except ImportError as error:
        print(
            f"Missing dependency ({error.name}). "
            "Reinstall poppy-execution-controls with its dependencies.",
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, RuntimeError, AttributeError, TimeoutError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    return 0
