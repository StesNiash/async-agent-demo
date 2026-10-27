# Benchmark

The benchmark is intentionally small and local. It measures the scheduler-level
effect that motivated the project:

> A long tool call must not serialize unrelated runs behind it.

It is not a production throughput benchmark and does not include HTTP, network
or real model latency.

## Reproduce

```powershell
python bench/bench_async_actions.py --runs 20 --tool-seconds 5
```

The script compares two scenarios using the real run engine:

- **sync baseline** — the worker is occupied by a simulated blocking tool for
  5 seconds, then processes 20 short `echo` runs.
- **async** — a `manual` action is started and left running; the same worker
  processes 20 short `echo` runs while the action is still `running`, then the
  action is completed.

The async scenario asserts that the long action is still `waiting` after all 20
short runs complete.

## Result

Local run on Python 3.13 / Windows:

| Scenario | Short runs done | Elapsed | Long action |
|---|---:|---:|---|
| Sync baseline | 20 | 7486 ms | blocked the worker |
| Async action | 20 | 2489 ms | still `running` |
| Speedup | 20 | **3.01x** | — |

The important result is not the exact multiplier: it is that **all 20 short
runs completed while the long action was still active**. In the blocking model,
none could start until the long tool returned.

### Metrics explained

- **Elapsed** — wall-clock time from the start of the long operation to the last
  short run reaching `done`.
- **Speedup** — sync elapsed / async elapsed.
- **Long action** — in async mode, verified as `waiting` after the short runs
  finished; in sync mode, the simulated worker is unavailable by construction.

The mock model sleeps 50 ms per generation step, so the short-run work itself is
visible in both scenarios. With a faster model the async advantage grows; with a
slower tool it also grows.

## What this does and does not show

Shows:

- unrelated runs are not blocked by an in-flight action;
- a waiting run can be woken by heartbeat/notification without completing the
  action;
- the action lifecycle is independent from the run's wait timeout.

Does not show:

- production throughput or tail latency;
- distributed scheduling or multi-worker leases;
- real shell / GPU tool execution;
- token-level preemption.
