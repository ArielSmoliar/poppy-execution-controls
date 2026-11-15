"""A signed-out Poppy OpenAPI service; no OAuth/account access or conversations."""

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from poppy_controls.protocol.auth import ID, ProtocolError, SessionVerifier, https_url
from poppy_controls.protocol.store import Store


def openapi(company):
    version = {
        "name": "Poppy-Version",
        "in": "header",
        "required": True,
        "schema": {"type": "string", "example": "1.0"},
    }
    job_id = {"name": "job_id", "in": "path", "required": True, "schema": {"type": "string"}}

    def operation(summary, parameters=None):
        return {
            "summary": summary,
            "parameters": [version, *(parameters or [])],
            "security": [{"PoppySession": []}],
            "responses": {
                str(code): {"description": description}
                for code, description in (
                    (200, "Result"),
                    (400, "Invalid request, version or Session"),
                    (401, "Account tokens are unsupported"),
                    (404, "Unknown job or another owner's job"),
                    (409, "Request ID reused with different content"),
                    (429, "Rate limited; wait Retry-After seconds"),
                    (503, "Capacity unavailable; retry later"),
                )
            },
        }

    submit = operation(
        "Start a synthetic read-only airline lookup",
        [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,256}$"},
            }
        ],
    )
    submit["responses"]["202"] = {
        "description": "Durably accepted; identical retries return the same job",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "status_url": {"type": "string", "format": "uri"},
                    },
                    "required": ["job_id", "status_url"],
                }
            }
        },
    }
    submit["requestBody"] = {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["action", "query"],
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["fare_search", "flight_status", "upgrade_enquiry"],
                        },
                        "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    },
                }
            }
        },
    }
    end = operation("End this signed-out Session; budgets remain cumulative")
    end["responses"]["204"] = {"description": "Session ended"}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Poppy execution controls reference", "version": "0.1.0"},
        "servers": [{"url": company}],
        "components": {
            "securitySchemes": {
                "PoppySession": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "Poppy-Session",
                    "description": "Delegated Poppy Session JWT; not a static API key",
                }
            }
        },
        "paths": {
            "/api/searches": {"post": submit},
            "/api/searches/{job_id}": {"get": operation("Read your job", [job_id])},
            "/api/budget": {"get": operation("Read your execution-unit budget")},
            "/api/session/end": {"post": end},
        },
    }


def create_app(
    database,
    company,
    clients,
    *,
    allow_http_loopback=False,
    backend=None,
    service_factory,
    schema_factory=openapi,
    resource_path="/api/searches",
    organization_name="Fictional airline demo",
    api_description="Synthetic airline lookups with execution budgets",
    **settings,
):
    parsed = https_url(company)
    if parsed.path not in ("", "/") or company.endswith("/"):
        raise ValueError("Company identifier must be an HTTPS origin without trailing slash")
    verifier = SessionVerifier(company, clients)

    @asynccontextmanager
    async def lifespan(app):
        store = Store(database, company)
        try:
            service = service_factory(store, verifier.clients, backend=backend, **settings)
        except BaseException:
            store.close()
            raise
        app.state.store, app.state.service = store, service
        try:
            await service.start()
            yield
        finally:
            await service.close()
            store.close()

    async def error_handler(request, error):
        return JSONResponse(
            {"error": error.code},
            error.status,
            headers={"Cache-Control": "no-store", **error.headers},
        )

    def identify(request):
        # Canonical origin comes from operator config, never Host/X-Forwarded-*.
        if request.url.scheme != "https":
            address = request.client.host if request.client else ""
            if not allow_http_loopback or address not in ("127.0.0.1", "::1", "testclient"):
                raise ProtocolError("https_required", 400)
        for name in ("Poppy-Session", "Poppy-Version", "Poppy-Extensions", "Authorization"):
            if len(request.headers.getlist(name)) > 1:
                raise ProtocolError("invalid_request")
        raw_path = request.scope.get("raw_path", request.url.path.encode()).decode("ascii")
        identity, jti = verifier.verify(
            request.headers.get("Poppy-Session"),
            request.method,
            company + raw_path,
            request.headers.get("Poppy-Version"),
            request.headers.get("Poppy-Extensions", ""),
            request.headers.get("Authorization"),
        )
        request.app.state.store.authenticate(identity, jti)
        return identity

    async def discovery(request):
        return JSONResponse(
            {
                "protocol_version": "1.0",
                "organization": {
                    "name": organization_name,
                    "domain": urlsplit(company).netloc,
                },
                "apis": [
                    {
                        "type": "openapi",
                        "url": company + "/openapi.json",
                        "description": api_description,
                    }
                ],
                "web": {"disabled": True},
            }
        )

    async def schema(request):
        return JSONResponse(schema_factory(company))

    async def submit(request: Request):
        identity = identify(request)
        ids = request.headers.getlist("Idempotency-Key")
        if len(ids) != 1 or not ID.fullmatch(ids[0]):
            raise ProtocolError("invalid_request")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise ProtocolError("invalid_request", 415)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                raise ProtocolError("request_too_large", 413)
        import json

        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ProtocolError("invalid_request") from None
        job = await request.app.state.service.submit(identity, ids[0], payload)
        return JSONResponse(
            {"job_id": job["id"], "status_url": company + resource_path + "/" + job["id"]},
            202,
            headers={"Cache-Control": "no-store"},
        )

    async def get_job(request):
        identity = identify(request)
        job = request.app.state.store.job(request.path_params["job_id"])
        if not job or job["owner"] != identity.owner:
            raise ProtocolError("job_not_found", 404)
        import json

        return JSONResponse(
            {
                "job_id": job["id"],
                "state": job["state"],
                "estimated_units": job["estimate"],
                "charged_units": job["charge"],
                "result": json.loads(job["result"]) if job["result"] else None,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def budget(request):
        identity = identify(request)
        return JSONResponse(
            request.app.state.service.balances(identity), headers={"Cache-Control": "no-store"}
        )

    async def end_session(request):
        request.app.state.store.end_session(identify(request))
        return Response(status_code=204)

    async def health(request):
        service = request.app.state.service
        ready = service.available
        return JSONResponse({"ready": ready}, status_code=200 if ready else 503)

    return Starlette(
        lifespan=lifespan,
        exception_handlers={ProtocolError: error_handler},
        routes=[
            Route("/.well-known/poppy.json", discovery),
            Route("/healthz", health),
            Route("/openapi.json", schema),
            Route(resource_path, submit, methods=["POST"]),
            Route(resource_path + "/{job_id}", get_job),
            Route("/api/budget", budget),
            Route("/api/session/end", end_session, methods=["POST"]),
        ],
    )
