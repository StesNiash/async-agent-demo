"""Small scheduler-level benchmark for async agent actions.

The async scenario keeps one long action running while the worker serves short
runs. The sync baseline simulates the same worker being occupied by a blocking
tool call. This is intentionally a local micro-benchmark, not a production
throughput claim.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine import RunEngine
from app.model import MockModel
from app.store import Store


async def wait_until(fn, timeout: float = 10.0, interval: float = 0.005):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = fn()
        if value:
            return value
        await asyncio.sleep(interval)
    raise TimeoutError("benchmark condition was not met")


def make_engine() -> tuple[RunEngine, Store]:
    store = Store(":memory:")
    engine = RunEngine(store, MockModel(), heartbeat_interval=30.0, poll_interval=0.005)
    return engine, store


async def async_scenario(runs: int, tool_seconds: float) -> dict:
    engine, store = make_engine()
    await engine.start()
    try:
        manual_id = await engine.create_run("manual", heartbeat_interval=30)
        await wait_until(lambda: store.get_run(manual_id)["status"] == "waiting")
        action = store.get_actions(manual_id)[0]
        assert action["status"] == "running"

        started = time.perf_counter()
        short_ids = [await engine.create_run("echo bench", heartbeat_interval=30) for _ in range(runs)]
        await wait_until(
            lambda: all(store.get_run(run_id)["status"] == "done" for run_id in short_ids)
        )
        elapsed = time.perf_counter() - started
        still_running = store.get_run(manual_id)["status"] == "waiting"
        assert still_running

        await engine.complete_action(action["id"], result={"manual": "bench"})
        await engine.wait_for_run(manual_id, timeout=10)
        return {
            "short_runs_completed": runs,
            "elapsed_ms": round(elapsed * 1000, 1),
            "long_action_still_running": still_running,
            "long_action_seconds": tool_seconds,
        }
    finally:
        await engine.stop()
        store.close()


async def sync_scenario(runs: int, tool_seconds: float) -> dict:
    engine, store = make_engine()
    await engine.start()
    try:
        # Simulates one worker blocked inside a synchronous tool call.
        started = time.perf_counter()
        await asyncio.sleep(tool_seconds)
        short_ids = [await engine.create_run("echo bench", heartbeat_interval=30) for _ in range(runs)]
        await wait_until(
            lambda: all(store.get_run(run_id)["status"] == "done" for run_id in short_ids)
        )
        elapsed = time.perf_counter() - started
        return {
            "short_runs_completed": runs,
            "elapsed_ms": round(elapsed * 1000, 1),
            "long_action_seconds": tool_seconds,
        }
    finally:
        await engine.stop()
        store.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--tool-seconds", type=float, default=2.0)
    args = parser.parse_args()

    sync = await sync_scenario(args.runs, args.tool_seconds)
    asynchronous = await async_scenario(args.runs, args.tool_seconds)
    speedup = sync["elapsed_ms"] / asynchronous["elapsed_ms"]
    print(json.dumps({
        "benchmark": "scheduler-level async action",
        "runs": args.runs,
        "tool_seconds": args.tool_seconds,
        "sync": sync,
        "async": asynchronous,
        "speedup": round(speedup, 2),
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
