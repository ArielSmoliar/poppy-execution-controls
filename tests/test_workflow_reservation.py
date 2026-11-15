"""Whole-workflow envelopes: admission, settlement, recovery and interruption safety."""

import asyncio
import subprocess
import sys

import pytest

from poppy_controls.metered.backend import AirlineBackend, LocalAirlineAPI, build
from poppy_controls.metered.engine import Engine
from tests.helpers import tasks_for

TASK = {"id": "one", "owner": "alice", "route": "R0", "bags": 1, "max_fare_cents": 50000}


@pytest.mark.parametrize("controller", ["fifo"])
async def test_reserve_all_steps_before_first_call_and_settle_once(tmp_path, controller):
    api = LocalAirlineAPI(tmp_path / "provider.sqlite")
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingBackend(AirlineBackend):
        async def execute(self, action, params, key):
            entered.set()
            await release.wait()
            return await super().execute(action, params, key)

    engine = Engine(
        tmp_path / "journal.sqlite",
        BlockingBackend(api),
        controller,
        admission="workflow",
        provider_budget=15,
        capacity=2,
    )
    await engine.start()
    first = asyncio.create_task(engine.run(TASK))
    await entered.wait()
    assert engine.ledger.reserved("provider") == 15  # Not just the first tool's 5.
    assert api.meter()["actual_units"] == 0
    denied = await engine.run({**TASK, "owner": "bob"})
    assert denied["state"] == "budget_rejected"
    assert api.meter()["executed_queries"] == 0
    release.set()
    outcome = await first
    assert outcome["options"] == api.expected(TASK)
    assert engine.ledger.spent("provider") == api.meter()["actual_units"] == 12
    assert engine.ledger.reserved("provider") == 0
    assert not engine.journal.holds(open_only=True)
    assert await engine.run(TASK) == outcome
    assert api.meter()["executed_queries"] == 3
    engine.close()
    api.close()


@pytest.mark.parametrize("controller", ["fifo"])
async def test_budget_rejected_workflows_do_no_billable_work(tmp_path, controller):
    api = LocalAirlineAPI(tmp_path / "provider.sqlite")
    engine = Engine(
        tmp_path / "journal.sqlite", build(api), controller,
        user_budget=18, admission="workflow",
    )
    await engine.start()
    try:
        tasks = tasks_for(901)
        results = await asyncio.gather(*(engine.run(task) for task in tasks))
        assert sum(r["state"] == "completed" for r in results) == 8
        denied = [task for task, result in zip(tasks, results)
                  if result["state"] == "budget_rejected"]
        assert len(denied) == 8
        assert all(engine.accounted(task["owner"], task["id"]) == 0 for task in denied)
        assert api.meter()["executed_queries"] == 24
        assert engine.ledger.spent("provider") == api.meter()["actual_units"]
        assert engine.ledger.reserved("provider") == 0
    finally:
        engine.close()
        api.close()



@pytest.mark.parametrize("controller", ["fifo"])
async def test_unknown_call_keeps_its_estimate_but_releases_future_steps(tmp_path, controller):
    api = LocalAirlineAPI(tmp_path / "provider.sqlite", "unavailable")
    engine = Engine(tmp_path / "journal.sqlite", build(api), controller, admission="workflow")
    await engine.start()
    assert (await engine.run(TASK))["state"] == "uncertain"
    assert engine.ledger.spent("provider") == 5
    assert engine.ledger.reserved("provider") == 0
    assert api.meter()["actual_units"] == 0
    assert not engine.journal.holds(open_only=True)
    engine.close()
    api.close()


@pytest.mark.parametrize("controller", ["fifo"])
async def test_changed_price_cannot_start_a_tool_outside_the_envelope(tmp_path, controller):
    api = LocalAirlineAPI(tmp_path / "provider.sqlite")

    class ChangedTariff(AirlineBackend):
        async def execute(self, action, params, key):
            result = await super().execute(action, params, key)
            if action == "search":
                self.api.pricing = {"query_units": 2, "row_units": 2}
            return result

    engine = Engine(
        tmp_path / "journal.sqlite",
        ChangedTariff(api),
        controller,
        admission="workflow",
        user_budget=15,
        provider_budget=15,
    )
    await engine.start()
    assert (await engine.run(TASK))["state"] == "budget_rejected"
    assert api.meter()["executed_queries"] == 2  # Never starts the now-unfunded status call.
    assert engine.ledger.spent("provider") == api.meter()["actual_units"] == 12
    assert engine.ledger.reserved("provider") == 0
    engine.close()
    # A later funded retry must not re-bill cached steps or use the closed envelope again.
    engine = Engine(
        tmp_path / "journal.sqlite",
        build(api),
        controller,
        admission="workflow",
        user_budget=100,
        provider_budget=100,
    )
    await engine.start()
    assert (await engine.run(TASK))["state"] == "completed"
    assert api.meter()["executed_queries"] == 3
    assert engine.ledger.spent("provider") == api.meter()["actual_units"] == 20
    assert len(engine.journal.holds()) == 2
    engine.close()
    api.close()


@pytest.mark.parametrize("controller", ["fifo"])
async def test_hard_crash_restores_future_commitment_without_double_counting(tmp_path, controller):
    code = """
import asyncio, os, sys
from pathlib import Path
from poppy_controls.metered.backend import LocalAirlineAPI, AirlineBackend
from poppy_controls.metered.engine import Engine
class CrashAfterCommit(AirlineBackend):
    async def execute(self, action, params, key):
        await super().execute(action, params, key)
        os._exit(17)
async def main():
    path = Path(sys.argv[1])
    api = LocalAirlineAPI(path / "provider.sqlite")
    engine = Engine(path / "journal.sqlite", CrashAfterCommit(api), sys.argv[2],
                    admission="workflow", user_budget=15, provider_budget=15)
    await engine.start()
    await engine.run({"id":"one","owner":"alice","route":"R0","bags":1,"max_fare_cents":50000})
asyncio.run(main())
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), controller],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 17, child.stderr
    api = LocalAirlineAPI(tmp_path / "provider.sqlite")
    assert api.meter()["actual_units"] == 4
    wrong_mode = Engine(tmp_path / "journal.sqlite", build(api), controller)
    with pytest.raises(ValueError, match="workflow admission"):
        await wrong_mode.start()
    wrong_mode.close()
    engine = Engine(
        tmp_path / "journal.sqlite",
        build(api),
        controller,
        admission="workflow",
        user_budget=15,
        provider_budget=15,
    )
    await engine.start()
    # Paid search is included INSIDE the restored 15-unit commitment, not added again.
    assert engine.ledger.spent("provider") == 0
    assert engine.ledger.reserved("provider") == 15
    assert (await engine.run({**TASK, "owner": "bob"}))["state"] == "budget_rejected"
    assert (await engine.run(TASK))["options"] == api.expected(TASK)
    assert api.meter()["executed_queries"] == 3
    assert engine.ledger.spent("provider") == 12
    assert engine.ledger.reserved("provider") == 0
    engine.close()
    api.close()


@pytest.mark.parametrize("controller", ["fifo"])
async def test_old_per_tool_journal_can_resume_without_double_charging(tmp_path, controller):
    api = LocalAirlineAPI(tmp_path / "provider.sqlite")
    engine = Engine(tmp_path / "journal.sqlite", build(api), controller)
    await engine.start()
    engine.journal.request(TASK["owner"], TASK)
    await engine.call(TASK["owner"], TASK["id"], "search", "search", {"route": "R0"})
    engine.close()
    engine = Engine(
        tmp_path / "journal.sqlite",
        build(api),
        controller,
        admission="workflow",
        user_budget=15,
        provider_budget=15,
    )
    await engine.start()
    assert (await engine.run(TASK))["state"] == "completed"
    assert engine.ledger.spent("provider") == api.meter()["actual_units"] == 12
    assert api.meter()["executed_queries"] == 3
    engine.close()
    api.close()


@pytest.mark.parametrize("controller", ["fifo"])
async def test_unrecoverable_crash_aborts_future_steps_but_retains_unknown_charge(
    tmp_path, controller
):
    api = LocalAirlineAPI(tmp_path / "provider.sqlite")
    engine = Engine(tmp_path / "journal.sqlite", build(api), controller, admission="workflow")
    await engine.start()
    engine.journal.request(TASK["owner"], TASK)
    flow = engine.prepare_workflow(TASK)
    engine.admit_workflow(flow)
    engine.journal.begin(
        "uncertain-operation", TASK["owner"], "search", {"route": "R0"}, 5, workflow_key=flow.key
    )
    engine.close()  # Models a crash after starting an operation; no receipt is available.
    engine = Engine(tmp_path / "journal.sqlite", build(api), controller, admission="workflow")
    await engine.start()
    assert engine.ledger.spent("provider") == 5
    assert engine.ledger.reserved("provider") == 0
    assert not engine.journal.holds(open_only=True)
    assert api.meter()["executed_queries"] == 0
    engine.close()
    api.close()




@pytest.mark.parametrize("controller", ["fifo"])
@pytest.mark.parametrize("failure", ["cancellation", "journal_write"])
async def test_interrupted_workflow_keeps_full_envelope_until_recovery(
    tmp_path, controller, failure
):
    api = LocalAirlineAPI(tmp_path / "provider.sqlite")
    committed = asyncio.Event()

    class InterruptedBackend(AirlineBackend):
        async def execute(self, action, params, key):
            receipt = await super().execute(action, params, key)
            committed.set()
            if failure == "cancellation":
                await asyncio.Event().wait()
            return receipt

    path = tmp_path / "journal.sqlite"
    engine = Engine(
        path,
        InterruptedBackend(api),
        controller,
        admission="workflow",
        user_budget=15,
        provider_budget=15,
    )
    await engine.start()
    if failure == "journal_write":

        def fail(*args):
            raise OSError("Injected settlement persistence failure")

        engine.journal.finish = fail
    pending = asyncio.create_task(engine.run(TASK))
    await committed.wait()
    if failure == "cancellation":
        pending.cancel()
        expected = asyncio.CancelledError
    else:
        expected = OSError
    with pytest.raises(expected):
        await pending
    assert engine.fatal
    assert engine.ledger.reserved("provider") == 15
    assert len(engine.journal.holds(open_only=True)) == 1
    engine.close()
    engine = Engine(
        path, build(api), controller, admission="workflow", user_budget=15, provider_budget=15
    )
    await engine.start()
    assert engine.ledger.spent("provider") == 0
    assert engine.ledger.reserved("provider") == 15
    assert (await engine.run(TASK))["state"] == "completed"
    assert engine.ledger.spent("provider") == api.meter()["actual_units"] == 12
    assert engine.ledger.reserved("provider") == 0
    assert api.meter()["executed_queries"] == 3
    engine.close()
    api.close()
