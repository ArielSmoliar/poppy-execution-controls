"""Independent billing, quality, concurrency and crash checks for FIFO execution."""

import asyncio
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from poppy_controls.metered.backend import AirlineBackend, LocalAirlineAPI, Receipt, build
from poppy_controls.metered.engine import Engine, Journal, UncertainError
from poppy_controls.metered.ledger import AtomicLedger, DeniedError
from tests.helpers import tasks_for

CONTROLLERS = ("fifo",)


@pytest.mark.parametrize("ledger_type", [AtomicLedger])
def test_reservations_are_atomic_across_threads_and_scopes(ledger_type):
    ledger = ledger_type()

    def reserve(_):
        try:
            return ledger.reserve({"user:a": 100, "provider": 10}, 3)
        except DeniedError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = [t for t in pool.map(reserve, range(30)) if t is not None]
    assert len(tokens) == 3
    assert ledger.reserved("provider") == ledger.reserved("user:a") == 9
    assert ledger.spent("provider") == 0
    for token in tokens:
        ledger.settle(token, 2)
        ledger.settle(token, 2)
    assert ledger.spent("provider") == ledger.spent("user:a") == 6
    assert ledger.reserved("provider") == 0
    with pytest.raises(DeniedError):
        ledger.reserve({"user:b": 100, "provider": 6}, 1)
    assert ledger.reserved("user:b") == 0  # No partial user reservation leaked.
    with pytest.raises(ValueError):
        ledger.reserve({"user:c": 10, "provider": -1}, 3)
    assert ledger.reserved("user:c") == 0
    token = ledger.reserve({"user:c": 10}, 3)
    ledger.cancel(token)
    ledger.cancel(token)
    assert ledger.reserved("user:c") == 0
    with pytest.raises(ValueError):
        ledger_type().settle(token, 0)


@pytest.mark.parametrize("ledger_type", [AtomicLedger])
def test_underestimate_records_actual_and_blocks_future_work(ledger_type):
    ledger = ledger_type()
    token = ledger.reserve({"user": 5}, 3)
    ledger.settle(token, 7)
    assert ledger.spent("user") == 7
    with pytest.raises(DeniedError):
        ledger.reserve({"user": 5}, 1)


@pytest.mark.parametrize("controller", CONTROLLERS)
async def test_concurrent_duplicates_and_restart_preserve_measured_cost(tmp_path, controller):
    api = LocalAirlineAPI(tmp_path / "api.sqlite")
    task = tasks_for(901)[0]
    path = tmp_path / "journal.sqlite"
    engine = Engine(path, build(api), controller)
    await engine.start()
    try:
        replies = await asyncio.gather(*(engine.run(task) for _ in range(15)))
        assert all(r == replies[0] for r in replies)
        assert replies[0]["options"] == api.expected(task)
        meter = api.meter()
        assert meter["executed_queries"] == 3
        assert engine.ledger.spent("provider") == meter["actual_units"]
        with pytest.raises(ValueError, match="Request ID"):
            await engine.run({**task, "bags": (task["bags"] + 1) % 3})
    finally:
        engine.close()
    restarted = Engine(path, build(api), controller)
    await restarted.start()
    try:
        assert await restarted.run(task) == replies[0]
        assert api.meter() == meter
        assert restarted.ledger.spent("provider") == meter["actual_units"]
    finally:
        restarted.close()
        api.close()


@pytest.mark.parametrize("controller", CONTROLLERS)
@pytest.mark.parametrize("fault", ["lost_response", "transient"])
async def test_retry_uses_provider_idempotency_not_double_billing(tmp_path, controller, fault):
    api = LocalAirlineAPI(tmp_path / "api.sqlite", fault)
    engine = Engine(tmp_path / "journal.sqlite", build(api), controller)
    await engine.start()
    try:
        task = tasks_for(901)[0]
        result = await engine.run(task)
        assert result["options"] == api.expected(task)
        assert api.meter()["executed_queries"] == 3
        assert api.meter()["transport_attempts"] == 6
        assert engine.retried == 3
        assert engine.ledger.spent("provider") == api.meter()["actual_units"]
    finally:
        engine.close()
        api.close()


@pytest.mark.parametrize("controller", CONTROLLERS)
async def test_missing_receipt_retains_provisional_charge(tmp_path, controller):
    api = LocalAirlineAPI(tmp_path / "api.sqlite", "unavailable")
    engine = Engine(tmp_path / "journal.sqlite", build(api), controller, user_budget=5)
    await engine.start()
    try:
        task = tasks_for(901)[0]
        assert (await engine.run(task))["state"] == "uncertain"
        assert api.meter()["actual_units"] == 0
        assert engine.ledger.spent("provider") == 5
        assert (await engine.run(task))["state"] == "uncertain"
        assert engine.ledger.spent("provider") == 5
        assert (await engine.run({**task, "id": "new"}))["state"] == "budget_rejected"
    finally:
        engine.close()
        api.close()


@pytest.mark.parametrize("controller", CONTROLLERS)
async def test_process_death_after_provider_commit_recovers_receipt(tmp_path, controller):
    code = """
import asyncio, os, sys
from pathlib import Path
from poppy_controls.metered.backend import LocalAirlineAPI, build, fingerprint
from poppy_controls.metered.engine import Engine
from tests.helpers import tasks_for
async def main():
    directory = Path(sys.argv[1])
    api = LocalAirlineAPI(directory / "api.sqlite")
    engine = Engine(directory / "journal.sqlite", build(api), sys.argv[2])
    await engine.start()
    task = tasks_for(901)[0]
    engine.journal.request(task["owner"], task)
    key = fingerprint([task["owner"], task["id"], "search"])
    params = {"route": task["route"]}
    engine.journal.begin(key, task["owner"], "search", params, 5)
    await engine.backend.execute("search", params, key)
    os._exit(17)
asyncio.run(main())
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), controller],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 17, child.stderr
    api = LocalAirlineAPI(tmp_path / "api.sqlite")
    assert api.meter()["executed_queries"] == 1
    engine = Engine(tmp_path / "journal.sqlite", build(api), controller)
    await engine.start()
    try:
        task = tasks_for(901)[0]
        assert (await engine.run(task))["options"] == api.expected(task)
        assert api.meter()["executed_queries"] == 3
        assert engine.ledger.spent("provider") == api.meter()["actual_units"]
    finally:
        engine.close()
        api.close()


@pytest.mark.parametrize("controller", CONTROLLERS)
async def test_cancel_queued_call_does_not_leak_capacity_or_stale_task(tmp_path, controller):
    api = LocalAirlineAPI(tmp_path / "api.sqlite")
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingBackend(AirlineBackend):
        async def execute(self, action, params, key):
            entered.set()
            await release.wait()
            return await super().execute(action, params, key)

    engine = Engine(tmp_path / "journal.sqlite", BlockingBackend(api), controller, capacity=1)
    await engine.start()
    first = asyncio.create_task(engine.call("user", "one", "search", "search", {"route": "R0"}))
    await entered.wait()
    waiting = asyncio.create_task(engine.call("user", "two", "search", "search", {"route": "R1"}))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    await first
    await engine.call("user", "three", "search", "search", {"route": "R2"})
    assert api.meter()["executed_queries"] == 2
    assert engine.dispatch.active == 0
    assert not engine.dispatch.waiters
    engine.close()
    api.close()


@pytest.mark.parametrize("controller", CONTROLLERS)
async def test_bad_receipt_is_not_trusted(tmp_path, controller):
    api = LocalAirlineAPI(tmp_path / "api.sqlite")

    class BadBackend(AirlineBackend):
        async def execute(self, action, params, key):
            receipt = await super().execute(action, params, key)
            return Receipt(
                "someone-elses-receipt", receipt.data, receipt.actual_units, receipt.usage
            )

    engine = Engine(tmp_path / "journal.sqlite", BadBackend(api), controller)
    await engine.start()
    with pytest.raises(UncertainError):
        await engine.call("user", "one", "search", "search", {"route": "R0"})
    assert engine.ledger.spent("provider") == 5
    engine.close()
    # Startup can reconcile against a valid independently persisted provider receipt.
    engine = Engine(tmp_path / "journal.sqlite", build(api), controller)
    await engine.start()
    assert engine.ledger.spent("provider") == 4
    engine.close()
    api.close()


def test_single_writer_guard(tmp_path):
    first = Journal(tmp_path / "journal.sqlite")
    try:
        with pytest.raises(RuntimeError, match="one process"):
            Journal(tmp_path / "journal.sqlite")
    finally:
        first.close()


@pytest.mark.parametrize("controller", CONTROLLERS)
async def test_failed_persistence_retains_hold_and_reconciles_on_restart(tmp_path, controller):
    api = LocalAirlineAPI(tmp_path / "api.sqlite")
    path = tmp_path / "journal.sqlite"
    engine = Engine(path, build(api), controller)
    await engine.start()

    def fail(*args):
        raise OSError("Injected journal failure")

    engine.journal.finish = fail
    with pytest.raises(OSError):
        await engine.call("user", "one", "search", "search", {"route": "R0"})
    assert engine.ledger.reserved("provider") == 5
    assert api.meter()["actual_units"] == 4
    with pytest.raises(RuntimeError):
        await engine.call("user", "two", "search", "search", {"route": "R0"})
    engine.close()
    engine = Engine(path, build(api), controller)
    await engine.start()
    assert engine.ledger.spent("provider") == 4
    assert engine.ledger.reserved("provider") == 0
    engine.close()
    api.close()


@pytest.mark.parametrize("controller", CONTROLLERS)
async def test_running_cancellation_reconciles_committed_provider_charge(tmp_path, controller):
    api = LocalAirlineAPI(tmp_path / "api.sqlite")
    committed = asyncio.Event()

    class BlockingResponse(AirlineBackend):
        async def execute(self, action, params, key):
            result = await super().execute(action, params, key)
            committed.set()
            await asyncio.Event().wait()
            return result

    path = tmp_path / "journal.sqlite"
    engine = Engine(path, BlockingResponse(api), controller)
    await engine.start()
    call = asyncio.create_task(engine.call("user", "one", "search", "search", {"route": "R0"}))
    await committed.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert engine.ledger.reserved("provider") == 5
    engine.close()
    engine = Engine(path, build(api), controller)
    await engine.start()
    assert engine.ledger.spent("provider") == 4
    assert await engine.call("user", "one", "search", "search", {"route": "R0"})
    assert api.meter()["executed_queries"] == 1
    engine.close()
    api.close()
