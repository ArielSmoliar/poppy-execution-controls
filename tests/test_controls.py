"""Installed-company flow: authenticated requests, real receipts, inspection and recovery."""

import asyncio
import json
import stat

import httpx
import pytest

from poppy_controls.app import create_app
from poppy_controls.cli import check, initialize, main
from poppy_controls.config import load
from poppy_controls.demo import DemoBackend
from poppy_controls.inspect import snapshot
from poppy_controls.metered.backend import LocalAirlineAPI
from poppy_controls.protocol.client import Signer

PAYLOAD = {"route": "R0", "bags": 1, "max_fare_cents": 50000}
PATH = "/api/workflows"


@pytest.fixture
def config(tmp_path):
    return initialize(tmp_path / "company")


def change(path, **values):
    cfg = json.loads(path.read_text())
    path.write_text(json.dumps({**cfg, **values}))


async def post(client, signer, key="one", payload=None):
    return await client.post(
        PATH,
        json=PAYLOAD if payload is None else payload,
        headers={**signer.headers("POST", PATH), "Idempotency-Key": key},
    )


def test_init_check_and_inspect_do_not_create_execution_state(config):
    assert stat.S_IMODE((config.parent / "agent-private.pem").stat().st_mode) == 0o600
    assert check(config)["valid"]
    assert not snapshot(config)["initialized"]
    assert not (config.parent / "state").exists()
    with pytest.raises(FileExistsError):
        initialize(config.parent)
    assert main(["inspect", "--config", str(config), "--json"]) == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"capacity": True},
        {"retries": 4},
        {"max_pending": 1001},
        {"user_budget_units": -1},
        {"company_budget_units": 1.5},
        {"company": "https://example.org/path"},
        {"company": "http://example.org"},
        {"timeout_seconds": 0},
        {"local_http": "false"},
        {"host": "0.0.0.0"},
        {"scheduler": "magic"},
        {"scheduler": "loco"},
        {"unknown": 1},
        {"backend_options": []},
    ],
)
def test_invalid_configuration_fails_before_state_creation(config, patch):
    change(config, **patch)
    with pytest.raises(ValueError):
        load(config)
    assert not (config.parent / "state").exists()


def test_paths_are_relative_to_config_and_duplicate_keys_rejected(config, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert load(config)["state_dir"] == str(config.parent / "state")
    config.write_text('{"capacity": 1, "capacity": 2}')
    with pytest.raises(ValueError, match="Duplicate"):
        load(config)


@pytest.mark.parametrize("scheduler", ["default", "fifo"])
async def test_signed_workflow_dedup_budget_and_inspection(config, scheduler):
    if scheduler == "default":
        assert load(config)["scheduler"] == "fifo"
        assert load(config)["admission"] == "workflow"

    else:
        change(config, scheduler=scheduler)
    signer, other = Signer.load(config.parent), Signer.load(config.parent, user="other")
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            assert (await client.post(PATH, json=PAYLOAD)).status_code == 400
            schema = (await client.get("/openapi.json")).json()
            assert PATH in schema["paths"] and "/api/searches" not in schema["paths"]
            discovery = (await client.get("/.well-known/poppy.json")).json()
            assert discovery["organization"]["name"] == load(config)["company"]
            assert discovery["apis"][0]["description"] == (
                "Company workflows with budget and concurrency controls"
            )
            accepted = await post(client, signer)
            assert accepted.status_code == 202, accepted.text
            job_id = accepted.json()["job_id"]
            await app.state.service.drain()
            status_path = PATH + "/" + job_id
            job = (await client.get(status_path, headers=signer.headers("GET", status_path))).json()
            assert job["state"] == "completed" and job["charged_units"] == 12
            assert (await post(client, signer)).json()["job_id"] == job_id
            assert (await post(client, signer, payload={**PAYLOAD, "bags": 0})).status_code == 409
            assert (
                await client.get(status_path, headers=other.headers("GET", status_path))
            ).status_code == 404
            denied = await post(client, signer, "two")
            await app.state.service.drain()
            assert app.state.store.job(denied.json()["job_id"])["state"] == "budget_rejected"
            report = snapshot(config, job_id)
            assert report["budget"]["verified_charges"] == 12
            assert report["budget"]["reserved"] == 0
            assert len(report["operations"]) == 3
            assert app.state.service.backend.api.meter()["executed_queries"] == 3
            denied_report = snapshot(config, denied.json()["job_id"])
            assert not denied_report["operations"] and denied_report["decisions"]
            assert (await client.get("/healthz")).status_code == 200
    # Completed job IDs, budget history and receipts survive restart.
    app = create_app(config)
    async with app.router.lifespan_context(app):
        assert app.state.service.engine.ledger.spent("provider") == 12
        assert snapshot(config)["job_counts"] == {"budget_rejected": 1, "completed": 1}


async def test_rate_limit_and_idempotent_retry(config):
    change(config, rate_per_minute=1)
    app, signer = create_app(config), Signer.load(config.parent)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            assert (await post(client, signer)).status_code == 202
            assert (await post(client, signer)).status_code == 202
            response = await post(client, signer, "second")
            assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0
            await app.state.service.drain()


async def test_cancelled_backend_stops_readiness_and_admission_without_refunding(config):
    class CancelledBackend(DemoBackend):
        async def execute(self, action, params, key):
            raise asyncio.CancelledError

    (config.parent / "state").mkdir()
    backend = CancelledBackend(LocalAirlineAPI(config.parent / "state" / "provider.sqlite"))
    app, signer = create_app(config, backend=backend), Signer.load(config.parent)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            accepted = await post(client, signer)
            assert accepted.status_code == 202
            await asyncio.gather(*list(app.state.service.workers.values()), return_exceptions=True)
            assert app.state.service.engine.fatal
            assert app.state.service.fatal is None  # Engine failure alone makes us unavailable.
            health = await client.get("/healthz")
            assert health.status_code == 503 and health.json() == {"ready": False}
            assert (await post(client, signer, "next")).status_code == 503
            assert snapshot(config)["budget"]["reserved"] == 15


@pytest.mark.parametrize(
    "local,flags,expected",
    [
        (False, [], 2),
        (False, ["--ssl-certfile", "cert.pem"], 2),
        (False, ["--ssl-keyfile", "key.pem"], 2),
        (True, ["--ssl-certfile", "cert.pem"], 2),
        (True, [], 0),
        (False, ["--ssl-certfile", "cert.pem", "--ssl-keyfile", "key.pem"], 0),
    ],
)
def test_serve_tls_guards_and_single_process_launch(config, monkeypatch, local, flags, expected):
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append(kwargs))
    change(config, local_http=local)
    assert main(["serve", "--config", str(config), *flags]) == expected
    assert not (config.parent / "state").exists()
    if expected:
        assert not calls
    else:
        assert len(calls) == 1
        assert calls[0]["workers"] == 1
        assert calls[0]["proxy_headers"] is False
        assert calls[0]["ssl_certfile"] == (None if local else "cert.pem")
        assert calls[0]["ssl_keyfile"] == (None if local else "key.pem")


async def test_unknown_charge_and_config_redaction(config):
    change(config, backend_options={"fault": "unavailable"})
    app, signer = create_app(config), Signer.load(config.parent)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            accepted = await post(client, signer)
            await app.state.service.drain()
            report = snapshot(config, accepted.json()["job_id"])
            assert report["job"]["state"] == "uncertain"
            assert report["budget"]["provisional_charges"] == 5
            assert report["budget"]["verified_charges"] == 0
            assert report["budget"]["reserved"] == 0
    change(config, backend_options={"secret": "never-display"}, capacity=2)
    report = snapshot(config)
    assert "never-display" not in json.dumps(report)
    assert report["config_changed_since_start"]
    app = create_app(config)
    with pytest.raises(ValueError, match="contract changed"):
        async with app.router.lifespan_context(app):
            pass


async def test_interrupted_request_resumes_from_provider_receipt(config):
    entered = asyncio.Event()
    (config.parent / "state").mkdir()
    api = LocalAirlineAPI(config.parent / "state" / "provider.sqlite")

    class Interrupted(DemoBackend):
        async def execute(self, action, params, key):
            await super().execute(action, params, key)
            entered.set()
            await asyncio.Event().wait()

    app, signer = create_app(config, backend=Interrupted(api)), Signer.load(config.parent)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            accepted = await post(client, signer)
            job_id = accepted.json()["job_id"]
            await entered.wait()
            report = snapshot(config, job_id)
            assert report["budget"]["reserved"] == 15
            assert report["budget"]["settled_including_provisional"] == 0
    assert snapshot(config)["budget"]["reserved"] == 15
    app = create_app(config)
    async with app.router.lifespan_context(app):
        await app.state.service.drain()
        assert app.state.store.job(job_id)["state"] == "completed"
        assert app.state.service.backend.api.meter()["executed_queries"] == 3
        assert snapshot(config)["budget"]["verified_charges"] == 12
        assert snapshot(config)["budget"]["reserved"] == 0


async def test_global_company_cap_shared_across_users(config):
    change(config, company_budget_units=15)
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            first = await post(client, Signer.load(config.parent, user="a"))
            await app.state.service.drain()
            second = await post(client, Signer.load(config.parent, user="b"))
            await app.state.service.drain()
            assert app.state.store.job(first.json()["job_id"])["state"] == "completed"
            assert app.state.store.job(second.json()["job_id"])["state"] == "budget_rejected"
            assert snapshot(config)["budget"]["verified_charges"] == 12


async def test_owner_cannot_be_supplied_and_ended_session_rejected(config):
    app, signer = create_app(config), Signer.load(config.parent)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            assert (
                await post(client, signer, payload={**PAYLOAD, "owner": "victim"})
            ).status_code == 400
            route = "/api/session/end"
            assert (
                await client.post(route, headers=signer.headers("POST", route))
            ).status_code == 204
            assert (await post(client, signer)).status_code == 400


async def test_session_end_blocks_next_tool_and_releases_unused_hold(config):
    entered, release = asyncio.Event(), asyncio.Event()
    (config.parent / "state").mkdir()
    api = LocalAirlineAPI(config.parent / "state" / "provider.sqlite")

    class Pausing(DemoBackend):
        async def execute(self, action, params, key):
            receipt = await super().execute(action, params, key)
            entered.set()
            await release.wait()
            return receipt

    app, signer = create_app(config, backend=Pausing(api)), Signer.load(config.parent)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            accepted = await post(client, signer)
            await entered.wait()
            route = "/api/session/end"
            assert (
                await client.post(route, headers=signer.headers("POST", route))
            ).status_code == 204
            release.set()
            await app.state.service.drain()
            job = app.state.store.job(accepted.json()["job_id"])
            assert job["state"] == "rejected" and job["charge"] == 4
            assert api.meter()["executed_queries"] == 1
            assert snapshot(config)["budget"]["reserved"] == 0


async def test_full_inbox_returns_retry_after_without_duplicate_rejection(config):
    change(config, max_pending=1)
    entered, release = asyncio.Event(), asyncio.Event()
    (config.parent / "state").mkdir()
    api = LocalAirlineAPI(config.parent / "state" / "provider.sqlite")

    class Pausing(DemoBackend):
        async def execute(self, action, params, key):
            entered.set()
            await release.wait()
            return await super().execute(action, params, key)

    app, signer = create_app(config, backend=Pausing(api)), Signer.load(config.parent)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            accepted = await post(client, signer)
            await entered.wait()
            assert (await post(client, signer)).json() == accepted.json()
            response = await post(client, signer, "other")
            assert response.status_code == 503 and response.headers["Retry-After"] == "1"
            release.set()
            await app.state.service.drain()


@pytest.mark.parametrize("admission", ["tool", "workflow"])
async def test_replaceable_company_workflow(config, monkeypatch, admission):
    import sys
    import types

    module = types.ModuleType("example_company")

    class Workflow:
        input_schema = {"type": "object", "properties": {"route": {"type": "string"}}}

        def validate(self, task):
            if set(task) != {"id", "owner", "route"}:
                raise ValueError("Unexpected fields")

        async def execute(self, task, call):
            return {"matches": await call("custom-step", "search", {"route": task["route"]})}

    module.build = lambda options: Workflow()
    monkeypatch.setitem(sys.modules, "example_company", module)
    change(config, workflow_factory="example_company:build", admission=admission)
    assert check(config)["valid"]
    app, signer = create_app(config), Signer.load(config.parent)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            accepted = await post(client, signer, payload={"route": "R0"})
            await app.state.service.drain()
            job = app.state.store.job(accepted.json()["job_id"])
            assert job["state"] == "completed" and job["charge"] == 4
            assert len(json.loads(job["result"])["matches"]) == 3
            report = snapshot(config, job["id"])
            assert len(report["operations"]) == 1
            assert report["budget"]["verified_charges"] == 4


def test_bundled_integration_guide_matches_source(config):
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "docs" / "execution-controls.md"
    assert (config.parent / "INTEGRATION.md").read_text() == source.read_text()


def test_missing_plugin_error_points_to_the_company_environment(config, capsys):
    change(config, backend_factory="absent_company_plugin:build")
    assert main(["check", "--config", str(config)]) == 2
    error = capsys.readouterr().err
    assert "company plugin" in error and "same Python environment" in error
    assert "Install the wheel with [controls]" not in error
    assert not (config.parent / "state").exists()


def test_check_rejects_incomplete_workflow_without_creating_state(config, monkeypatch):
    import sys
    import types

    module = types.ModuleType("incomplete_workflow")
    module.build = lambda options: types.SimpleNamespace(input_schema={})
    monkeypatch.setitem(sys.modules, module.__name__, module)
    change(config, workflow_factory="incomplete_workflow:build")
    with pytest.raises(ValueError, match="validate"):
        check(config)
    assert not (config.parent / "state").exists()


async def test_check_and_inspect_detect_contract_change_without_loading_backend(config):
    app = create_app(config)
    async with app.router.lifespan_context(app):
        pass
    change(config, backend_options={"private_option": "never-display"})
    with pytest.raises(ValueError, match="contract changed"):
        check(config)
    report = snapshot(config)
    assert report["execution_contract_changed"] and report["config_changed_since_start"]
    assert "never-display" not in json.dumps(report)


@pytest.mark.parametrize("scheduler", ["fifo"])
async def test_selected_job_shows_its_users_budget_and_separates_other_users(config, scheduler):
    change(config, scheduler=scheduler)
    app = create_app(config)
    alice, bob = Signer.load(config.parent, user="alice"), Signer.load(config.parent, user="bob")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            first = await post(client, alice)
            await app.state.service.drain()
            await post(client, bob)
            await app.state.service.drain()
            report = snapshot(config, first.json()["job_id"])
            assert report["budget"]["verified_charges"] == 24
            assert report["user_budget"]["settled_including_provisional"] == 12
            assert report["user_budget"]["remaining"] == 3


def test_submit_signs_custom_payload_and_polls_with_fresh_proofs(config, monkeypatch):
    import jwt

    from poppy_controls.cli import submit

    requests = []
    real_client = httpx.Client

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            assert json.loads(request.content) == {"order_id": "ORDER-100"}
            assert request.headers["Idempotency-Key"] == "stable-id"
            return httpx.Response(202, json={"job_id": "custom-job"})
        assert request.url.path == "/api/workflows/custom-job"
        return httpx.Response(200, json={"state": "completed", "job_id": "custom-job"})

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    assert submit(config, {"order_id": "ORDER-100"}, "stable-id")["state"] == "completed"
    claims = [
        jwt.decode(r.headers["Poppy-Session"], options={"verify_signature": False})
        for r in requests
    ]
    assert len({c["jti"] for c in claims}) == 2
    assert {c["sub"] for c in claims} == {"demo-user"}
    assert claims[1]["htu"] == "https://airline.example/api/workflows/custom-job"
    assert {r.url.host for r in requests} == {"127.0.0.1"}


@pytest.mark.parametrize(
    "status,code,advice",
    [
        (400, "invalid_request", "openapi.json"),
        (409, "request_id_conflict", "original payload"),
        (429, "rate_limited", "Retry-After: 7"),
        (503, "overloaded", "same request ID"),
    ],
)
def test_submit_errors_include_recovery_advice(config, monkeypatch, status, code, advice):
    from poppy_controls.cli import submit

    real_client = httpx.Client
    transport = httpx.MockTransport(
        lambda _: httpx.Response(status, json={"error": code}, headers={"Retry-After": "7"})
    )
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: real_client(**kwargs, transport=transport)
    )
    with pytest.raises(RuntimeError) as error:
        submit(config, {"order_id": "ORDER-100"}, "stable-id")
    assert code in str(error.value) and advice in str(error.value)


def test_submit_rejects_remote_http_exception_and_payload_identity(config):
    from poppy_controls.cli import submit

    with pytest.raises(ValueError, match="without id or owner"):
        submit(config, {"owner": "somebody"})
    change(config, local_http=False)
    with pytest.raises(ValueError, match="loopback"):
        submit(config, {})


def test_submit_no_wait_and_strict_payload_file(config, monkeypatch, capsys):
    from poppy_controls.cli import submit

    real_client = httpx.Client
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(202, json={"job_id": "pending"})

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    assert submit(config, {"query": "example"}, wait=False) == {"job_id": "pending"}
    assert len(requests) == 1
    bad_payload = config.parent / "bad.json"
    bad_payload.write_text('{"query": "one", "query": "two"}')
    assert main(["submit", "--config", str(config), "--payload", str(bad_payload)]) == 2
    assert "Duplicate" in capsys.readouterr().err
    assert len(requests) == 1
