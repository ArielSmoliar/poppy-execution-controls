"""Strict, versioned JSON configuration; relative paths resolve beside the file."""

import importlib
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = {
    "version": 1,
    "company": "https://airline.example",
    "state_dir": "state",
    "trust_file": "trust.json",
    "host": "127.0.0.1",
    "port": 8765,
    "local_http": True,
    "scheduler": "fifo",
    "admission": "workflow",
    "capacity": 4,
    "user_budget_units": 15,
    "company_budget_units": 120,
    "max_pending": 100,
    "rate_per_minute": 100,
    "timeout_seconds": 2,
    "retries": 1,
    "backend_factory": "poppy_controls.demo:build",
    "backend_options": {},
    "workflow_factory": "poppy_controls.metered.workflow:build",
    "workflow_options": {},
    "contract_version": "local-airline-v1",
}


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate configuration key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("Non-finite JSON number")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def load(path):
    path = Path(path).resolve()
    raw = strict_json(path.read_text())
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("Configuration must be an object containing only documented keys")
    cfg = {**DEFAULTS, **raw}
    if type(cfg["version"]) is not int or cfg["version"] != 1:
        raise ValueError("Unsupported configuration version")
    for key in ("company", "state_dir", "trust_file", "host", "contract_version"):
        if not isinstance(cfg[key], str) or not cfg[key].strip():
            raise ValueError(f"{key} must be a nonempty string")
    origin = urlsplit(cfg["company"])
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.path
        or origin.query
        or origin.fragment
        or origin.username
        or origin.password
    ):
        raise ValueError("company must be an HTTPS origin without trailing slash")
    _ = origin.port  # Reject malformed port numbers.
    limits = {
        "port": (1, 65535),
        "capacity": (1, 1000),
        "max_pending": (1, 1000),
        "rate_per_minute": (1, 1000000),
        "retries": (0, 3),
        "user_budget_units": (0, 10**9),
        "company_budget_units": (0, 10**9),
    }
    for key, (low, high) in limits.items():
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}")
    value = cfg["timeout_seconds"]
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 3600:
        raise ValueError("timeout_seconds must be greater than 0 and at most 3600")
    if cfg["scheduler"] != "fifo" or cfg["admission"] not in ("workflow", "tool"):
        raise ValueError("Use scheduler fifo and admission workflow/tool")
    if type(cfg["local_http"]) is not bool:
        raise ValueError("local_http must be boolean")
    if cfg["local_http"] and cfg["host"] not in ("127.0.0.1", "::1"):
        raise ValueError("local_http requires a loopback host")
    for kind in ("backend", "workflow"):
        name = cfg[kind + "_factory"]
        if not isinstance(name, str) or not re.fullmatch(r"[\w.]+:[A-Za-z_]\w*", name):
            raise ValueError(f"Invalid {kind}_factory; use module:function")
        if not isinstance(cfg[kind + "_options"], dict):
            raise ValueError(f"{kind}_options must be an object")
    for key in ("state_dir", "trust_file"):
        cfg[key] = str((path.parent / cfg[key]).resolve())
    return cfg


def public_config(cfg):
    """Operator view: do not print adapter options, which may contain credentials."""
    return {k: v for k, v in cfg.items() if not k.endswith("_options")}


def trust(cfg):
    try:
        manifest = strict_json(Path(cfg["trust_file"]).read_text())
        if manifest.get("company") != cfg["company"] or not isinstance(
            manifest.get("clients"), dict
        ):
            raise ValueError("Trust manifest must match company and contain clients")
        from poppy_controls.protocol.auth import SessionVerifier

        return SessionVerifier(cfg["company"], manifest["clients"]).clients
    except (KeyError, TypeError, AttributeError):
        raise ValueError("Invalid trust manifest structure") from None


def factory(name):
    module, function = name.split(":")
    try:
        result = getattr(importlib.import_module(module), function)
    except ImportError as error:
        raise ValueError(
            f"Cannot load factory {name}: missing module {error.name}. "
            "Install the company plugin and its dependencies in the same Python environment "
            "as poppy-controls, then run check again."
        ) from None
    except AttributeError:
        raise ValueError(
            f"Factory {name} does not exist. Check the module:function spelling in controls.json."
        ) from None
    if not callable(result):
        raise ValueError(f"Factory {name} must be callable")
    return result


def contract_id(cfg):
    from poppy_controls.metered.backend import fingerprint

    return fingerprint(
        {
            k: cfg[k]
            for k in (
                "backend_factory",
                "backend_options",
                "workflow_factory",
                "workflow_options",
                "contract_version",
            )
        }
    )


def validate_workflow(workflow):
    for name in ("validate", "execute"):
        if not callable(getattr(workflow, name, None)):
            raise ValueError(
                f"Workflow must implement {name}(task" + (", call)" if name == "execute" else ")")
            )
    if not isinstance(getattr(workflow, "input_schema", None), dict):
        raise ValueError("Workflow must provide an input_schema object")


def check_state_contract(cfg):
    """Read existing metadata without creating state or constructing a backend."""
    import sqlite3

    path = Path(cfg["state_dir"]) / "inbox.sqlite"
    if not path.exists():
        return
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        metadata = dict(db.execute("SELECT key,value FROM metadata"))
        if metadata.get("company") != cfg["company"]:
            raise ValueError("State belongs to another company; select its correct state_dir")
        if metadata.get("contract", contract_id(cfg)) != contract_id(cfg):
            raise ValueError(
                "Execution contract changed. Restore the original factories, options and version "
                "or migrate the state. For a separate local experiment, use a new state_dir; "
                "do not reset a live budget."
            )
    finally:
        db.close()
