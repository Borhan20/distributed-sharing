# Spike S6: PostgreSQL as the job queue (`SKIP LOCKED`)

| | |
|---|---|
| **Question** (plan §9 Phase 0) | Is PostgreSQL queue throughput with `SKIP LOCKED` enough at our target scale? |
| **Date** | 2 November 2026 |
| **Status** | Measured on one developer machine. Target scale is still open (plan §12, question 1). |
| **Script** | [`tests/perf/s6_queue_bench.py`](../../tests/perf/s6_queue_bench.py) |

## Answer

**Yes, for the scale a self-hosted group is likely to have.** On one
developer machine, one API-like process completed about **47 full task
lifecycles per second**: claim, accept, start, commit, each in its own
transaction with fencing and audit records. That is roughly 170,000 tasks per
hour. With 20,000 tasks queued, a claim alone took **9 ms (median)**. No task
was ever claimed twice in any run.

The limit is not `SKIP LOCKED`. It is the global lock that keeps the audit
hash chain linear, which every attempt start and end must take. See
"What limits throughput".

## Setup

- **Machine:** 8 vCPU, 15 GB RAM, Linux. The Docker daemon was also running
  other idle containers.
- **Database:** PostgreSQL 17 (`postgres:17-alpine`) in Docker on the same
  host, default settings (not tuned), reached through Docker's port
  forwarding.
- **Workload:** one array job with 20,000 pending tasks, run from a fresh
  database each time.
- **Workers:** M simulated devices calling the real control-plane functions
  (`pool_cp.scheduler.core`), each with its own DB session.
- **What is skipped:** HTTP and WebSocket, so this measures the queue, not
  the network.
- **Caveat:** all workers share **one Python process and event loop**, which
  is how one API process behaves. Several API processes were not measured.
  Each case ran for 15 s.

```
uv run python tests/perf/s6_queue_bench.py --tasks 20000 --workers 1,4,16,32 --seconds 15
```

## Results (final code)

| mode | workers | tasks done | seconds | tasks/s | p50 ms | p99 ms | double-claimed |
|---|---|---|---|---|---|---|---|
| claim only | 1 | 1436 | 15.01 | 95.6 | 9.2 | 28.4 | 0 |
| claim only | 4 | 2800 | 15.02 | 186.4 | 20.2 | 33.0 | 0 |
| claim only | 16 | 2871 | 15.02 | 191.1 | 80.9 | 127.3 | 0 |
| claim only | 32 | 2502 | 15.18 | 164.8 | 190.3 | 344.2 | 0 |
| claim+accept+start+commit | 1 | 337 | 15.02 | 22.4 | 43.5 | 87.9 | 0 |
| claim+accept+start+commit | 4 | 722 | 15.06 | 47.9 | 81.6 | 115.1 | 0 |
| claim+accept+start+commit | 16 | 715 | 15.14 | 47.2 | 330.0 | 522.3 | 0 |
| claim+accept+start+commit | 32 | 704 | 15.3 | 46.0 | 672.4 | 2160.2 | 0 |

The latency columns are for one claim (claim only) or one full lifecycle
(the other rows). Load average was about 2.8 during the run, so treat these
as rough numbers.

## What changed because of this spike

The first version of the claim was much slower. Each change below was
measured, and the tests in `control-plane/tests/test_scheduling.py` still
pass.

| Version | Claim p50, 1 worker | Problem |
|---|---|---|
| 1. Lock 20 candidates `FOR UPDATE SKIP LOCKED`, filter in Python | 43 ms (5k tasks) | Concurrent agents found every candidate locked and got `no_work` while work was waiting. Only 12 of 20 tasks were claimed in `test_concurrent_claims_never_share_a_task`. |
| 2. One statement with every filter, `ORDER BY ... LIMIT 1 FOR UPDATE SKIP LOCKED` | 90 ms (5k tasks) | Correct, but it sorted every pending task on every claim (17 ms of SQL at 5k tasks), so cost grew with queue length. |
| 3. **Rank active jobs first, then lock one task by index** (`ix_tasks_job_pending`) | **9 ms (20k tasks)** | Current version. Cost depends on the number of active jobs, not tasks. |

## What limits throughput

1. **Audit hash chain.** `attempt.start` and `attempt.end` are audit events,
   and the chain is kept linear with one transaction-level advisory lock. With
   4 or more workers the full lifecycle stops scaling at about 47/s. Ways out,
   to decide when the target scale is known:
   - keep per-attempt events in `attempts`, which is already an append-only
     history, and chain only membership, policy, device and job events;
   - one hash chain per group, so groups don't serialize each other;
   - write audit rows in batches from a single writer.
2. **Four transactions per task.** Accept and start could be one message.
3. **One Python event loop.** Run several API processes behind the proxy.
   This needs the cancel-push path to use PostgreSQL `LISTEN/NOTIFY`
   (design doc §3, deviation X4).

## Not measured

- Several API processes, a tuned PostgreSQL, or a database on its own host.
- Many groups and many jobs at the same time. Step 1 of the claim is
  O(active jobs in the device's groups), limited to 10 per claim.
- A queue of a million tasks or more.
- WebSocket and network overhead. The chaos test (`tests/e2e/test_chaos.py`)
  covers correctness, not throughput.

## Decision input

PostgreSQL as both source of truth and queue (D7) holds for Phase 1 and
Phase 2. Revisit if the target scale (plan §12, question 1) is above about
100,000 tasks per hour per server, or if fixing the audit lock is not enough.
