"""Signed protocol boundary and session/account isolation for company workflows."""

import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from poppy_controls.app import create_app
from poppy_controls.config import DEFAULTS
from poppy_controls.protocol.auth import SessionVerifier, thumbprint
from poppy_controls.protocol.client import CLIENT, COMPANY, Signer, public_jwk
from poppy_controls.protocol.store import Store

PATH = "/api/workflows"
PAYLOAD = {"route": "R0", "bags": 1, "max_fare_cents": 50000}


@asynccontextmanager
async def running(directory, credentials, **kwargs):
    root, clients = credentials
    directory.mkdir(exist_ok=True)
    config = directory / "controls.json"
    config.write_text(json.dumps({**DEFAULTS, "local_http": False, **kwargs}))
    (directory / "trust.json").write_text(json.dumps({"company": COMPANY, "clients": clients}))
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=COMPANY
        ) as client:
            yield app, client, Signer(root)


async def submit(client, signer, key=None, payload=None):
    return await client.post(
        PATH,
        json=PAYLOAD if payload is None else payload,
        headers={**signer.headers("POST", PATH), "Idempotency-Key": key or uuid4().hex},
    )
@pytest.fixture
def credentials():
    root = ec.generate_private_key(ec.SECP256R1())
    key = public_jwk(root)
    key.update(kid=thumbprint(key), alg="ES256")
    clients = {
        CLIENT: {
            "metadata": {"client_id": CLIENT, "jwks_uri": "https://agent.example/jwks.json"},
            "jwks": {"keys": [key]},
        }
    }
    return root, clients



@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "version",
        "wrong_method",
        "wrong_url",
        "wrong_aud",
        "old_iat",
        "no_delegation",
        "wrong_thumbprint",
        "expired_delegation",
        "ath",
        "access_token",
        "forged",
    ],
)
async def test_invalid_identity_cannot_enqueue(tmp_path, credentials, mutation):
    async with running(tmp_path / "db", credentials) as (app, c, s):
        headers = s.headers("POST", PATH)
        token = headers["Poppy-Session"]
        claims = jwt.decode(token, options={"verify_signature": False})
        th = jwt.get_unverified_header(token)
        if mutation == "missing":
            headers.pop("Poppy-Session")
        elif mutation == "version":
            headers["Poppy-Version"] = "2.0"
        elif mutation == "access_token":
            headers["Authorization"] = "Bearer unsupported"
        else:
            if mutation == "wrong_method":
                claims["htm"] = "GET"
            elif mutation == "wrong_url":
                claims["htu"] = COMPANY + "/other"
            elif mutation == "wrong_aud":
                claims["aud"] = "https://another.example"
            elif mutation == "old_iat":
                claims["iat"] = int(time.time()) - 120
            elif mutation == "no_delegation":
                th.pop("poppy_dlg")
            elif mutation == "wrong_thumbprint":
                th["kid"] = "wrong"
            elif mutation == "expired_delegation":
                delegation = jwt.decode(th["poppy_dlg"], options={"verify_signature": False})
                delegation.update(iat=int(time.time()) - 200, exp=int(time.time()) - 100)
                th["poppy_dlg"] = jwt.encode(
                    delegation,
                    s.root,
                    algorithm="ES256",
                    headers={"typ": "poppy-delegation+jwt", "kid": s.root_kid},
                )
            elif mutation == "ath":
                claims["ath"] = "unexpected"
            signing_key = ec.generate_private_key(ec.SECP256R1()) if mutation == "forged" else s.key
            headers["Poppy-Session"] = jwt.encode(
                claims, signing_key, algorithm="ES256", headers=th
            )
        headers["Idempotency-Key"] = "request"
        response = await c.post(
            PATH, headers=headers, json=PAYLOAD
        )
        assert response.status_code in (400, 401)
        assert not list(app.state.store.db.execute("SELECT * FROM jobs"))



async def test_jwt_replay_is_not_application_retry(tmp_path, credentials):
    async with running(tmp_path / "db", credentials) as (app, c, s):
        headers = {**s.headers("POST", PATH), "Idempotency-Key": "same"}
        payload = PAYLOAD
        first = await c.post(PATH, headers=headers, json=payload)
        replay = await c.post(PATH, headers=headers, json=payload)
        assert first.status_code == 202
        assert replay.json()["error"] == "invalid_session"
        retry = await submit(c, s, key="same")
        assert retry.json() == first.json()
        await app.state.service.drain()



def test_ps384_delegations_and_public_manifest_enforcement(credentials):
    root = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(root.public_key()))
    key.update(kid="rsa-root", alg="PS384")
    clients = copy.deepcopy(credentials[1])
    clients[CLIENT]["jwks"]["keys"] = [key]
    signer = Signer(root, root_algorithm="PS384", root_kid="rsa-root")
    verifier = SessionVerifier(COMPANY, clients)
    identity, _ = verifier.verify(
        signer.headers("GET", "/api/budget")["Poppy-Session"], "GET", COMPANY + "/api/budget", "1.0"
    )
    assert identity.user == signer.user
    clients[CLIENT]["jwks"]["keys"][0]["d"] = "private-material"
    with pytest.raises(ValueError):
        SessionVerifier(COMPANY, clients)



def test_single_process_database_guard(tmp_path):
    first = Store(tmp_path / "db", COMPANY)
    try:
        with pytest.raises(RuntimeError):
            Store(tmp_path / "db", COMPANY)
    finally:
        first.close()



async def test_new_sessions_do_not_reset_user_or_company_budget(tmp_path, credentials):
    async with running(
        tmp_path / "company", credentials, user_budget_units=15, company_budget_units=27
    ) as (app, client, signer):
        first = await submit(client, signer)
        await app.state.service.drain()
        assert app.state.store.job(first.json()["job_id"])["state"] == "completed"
        renewed = await submit(client, Signer(credentials[0], user=signer.user))
        second = await submit(client, Signer(credentials[0], user="second-user"))
        await app.state.service.drain()
        third = await submit(client, Signer(credentials[0], user="third-user"))
        await app.state.service.drain()
        assert app.state.store.job(renewed.json()["job_id"])["state"] == "budget_rejected"
        assert app.state.store.job(second.json()["job_id"])["state"] == "completed"
        assert app.state.store.job(third.json()["job_id"])["state"] == "budget_rejected"
        assert sum(r[0] for r in app.state.store.db.execute("SELECT charge FROM jobs")) == 24


async def test_concurrent_duplicate_http_requests_execute_once(tmp_path, credentials):
    async with running(tmp_path / "company", credentials) as (app, client, signer):
        replies = await asyncio.gather(*(submit(client, signer, key="same") for _ in range(12)))
        assert {r.status_code for r in replies} == {202}
        assert len({r.json()["job_id"] for r in replies}) == 1
        await app.state.service.drain()
        assert app.state.service.backend.api.meter()["executed_queries"] == 3
        assert app.state.service.engine.ledger.spent("provider") == 12
        assert app.state.service.engine.ledger.reserved("provider") == 0


@pytest.mark.parametrize(
    "payload",
    [
        {**PAYLOAD, "bags": True},
        {**PAYLOAD, "estimated_units": 0},
        {**PAYLOAD, "max_fare_cents": None},
        {**PAYLOAD, "owner": "victim"},
        [],
    ],
)
async def test_invalid_payload_cannot_control_pricing(tmp_path, credentials, payload):
    async with running(tmp_path / "company", credentials) as (app, client, signer):
        response = await submit(client, signer, payload=payload)
        assert response.status_code == 400
        assert app.state.store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert app.state.service.backend.api.meter()["executed_queries"] == 0


async def test_plain_http_is_rejected_without_local_exception(tmp_path, credentials):
    async with running(tmp_path / "company", credentials) as (app, client, signer):
        response = await client.post(
            "http://airline.example" + PATH, json=PAYLOAD,
            headers={**signer.headers("POST", PATH), "Idempotency-Key": "http"},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "https_required"
        assert not list(app.state.store.db.execute("SELECT * FROM jobs"))
