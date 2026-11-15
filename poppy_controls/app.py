"""ASGI factory for signed Poppy requests and metered execution."""

from pathlib import Path

from poppy_controls.config import load, trust
from poppy_controls.protocol.app import create_app as poppy_app
from poppy_controls.protocol.app import openapi
from poppy_controls.service import PATH, Service


def create_app(config_path, *, backend=None):
    config = load(config_path)
    clients = trust(config)

    def schema(company):
        document = openapi(company)
        document["info"]["title"] = "Poppy execution controls reference"
        paths = document["paths"]
        submit = paths.pop("/api/searches")
        submit["post"]["summary"] = "Submit a workflow under company execution controls"
        submit["post"]["requestBody"]["content"]["application/json"]["schema"] = (
            app.state.service.workflow.input_schema
        )
        paths[PATH] = submit
        paths[PATH + "/{job_id}"] = paths.pop("/api/searches/{job_id}")
        return document

    app = poppy_app(
        Path(config["state_dir"]) / "inbox.sqlite",
        config["company"],
        clients,
        allow_http_loopback=config["local_http"],
        backend=backend,
        service_factory=Service,
        schema_factory=schema,
        resource_path=PATH,
        organization_name=config["company"],
        api_description="Company workflows with budget and concurrency controls",
        config=config,
    )
    return app
