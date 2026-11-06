# Job files: many tasks from one description

A job file is a short YAML file. Submit it with `pool submit job.yaml`. Use it
when the same program must run many times: parts, seeds, parameter sweeps,
many files, many test files.

## Contents

1. Fields
2. Split into parts
3. Sweep over values
4. Placeholders
5. YAML traps
6. Examples

## 1. Fields

| Field | What it does | Default |
|---|---|---|
| `image` | The container image. A tag is fixed to its exact version (digest) at submit time. | required |
| `command` | A string, split like a shell does, or a list. Pipes and `&&` need `sh -c '...'`. Replaces the image's command. | the image's command |
| `args` | Arguments for the image's own command, like `docker run IMAGE ARG...` | none |
| `workdir` | Working directory in the container | the image's |
| `parts` | Run this many copies, or `auto` | run once |
| `sweep` | One task per combination of values | – |
| `cpu`, `memory`, `disk` | Per task. Cores may be fractional. Sizes like `512Mi`, `4Gi`. | pool decides from earlier runs |
| `gpu` | GPUs per task | `0` |
| `time_limit` | Per task, e.g. `30m`, `2h` | pool decides from earlier runs |
| `retries` | Tries after a failure, 0–20. A task that fails on 2 different computers is marked failed. | `3` |
| `name`, `group` | The job's name and group (or `--group`) | from the image, from `pool login --group` |
| `env` | Environment variables. Not for secrets. | – |

An unknown field is refused, and the message lists the allowed fields.

## 2. Split into parts

`parts: 8` runs 8 copies of the command. `parts: auto` makes as many as the
group's available computers can run at once.

Each copy reads its number and does its share:

```python
import os
part = int(os.environ["POOL_TASK_INDEX"])
parts = int(os.environ["POOL_TASK_COUNT"])
my_items = all_items[part::parts]      # every n-th item, no overlap
```

A program that cannot do part of the work must run once, on a computer that is
big enough. pool cannot split it.

## 3. Sweep over values

```yaml
sweep:
  seed: 0..4                 # 0, 1, 2, 3, 4
  lr: [0.01, 0.1]            # or: lr: 0.01, 0.1
```

This makes 5 × 2 = 10 tasks, at most 10,000. Try more values without editing
the file: `pool sweep job.yaml --set seed=0..19 --dry-run` shows the count,
then run it without `--dry-run`.

## 4. Placeholders

| In `command` | Becomes |
|---|---|
| `{{index}}` | the task's number |
| `{{count}}` | the number of tasks |
| `{{seed}}` (any sweep name) | that task's value |

An unknown placeholder is refused: add the name to `sweep`, or use `{{index}}`.

## 5. YAML traps

A command that contains `: ` or ` #` must go after `>-` on the next line:

```yaml
command: >-
  sh -c 'echo "{\"part\": {{index}}}" > /out/result.json'
```

## 6. Examples

**Monte Carlo with a public image, split automatically:**

```yaml
name: monte-carlo-pi
image: python:3.12-slim
command: |
  python -c "
  import json, os, random
  part = int(os.environ['POOL_TASK_INDEX'])
  random.seed(part)
  n = 2_000_000
  inside = sum(random.random() ** 2 + random.random() ** 2 <= 1 for _ in range(n))
  json.dump({'part': part, 'pi': 4 * inside / n}, open('/out/result.json', 'w'))
  "
parts: auto
cpu: 1
memory: 256Mi
time_limit: 15m
```

**A parameter sweep of your own image:**

```yaml
name: pi-sweep
image: ghcr.io/your-org/pi-sweep:1
command: python /app/pi.py --seed {{seed}} --samples {{samples}}
sweep:
  seed: 0..4
  samples: [1000000, 10000000]
memory: 512Mi
time_limit: 30m
```

**A test suite split over 3 computers:**

```yaml
name: tests
image: ghcr.io/your-org/app-tests:1
command: >-
  sh -c 'files=$(ls tests/test_*.py | awk -v n="$POOL_TASK_COUNT" -v i="$POOL_TASK_INDEX" "(NR - 1) % n == i");
  [ -n "$files" ] || exit 0;
  pytest -q -p no:cacheprovider --junitxml=/out/junit.xml $files'
parts: 3
retries: 0
```

`-p no:cacheprovider` because the file system is read-only. `retries: 0` so a
failing test is reported at once.

After the job ends: `pool results JOB-ID -o results.csv` for the table, and
`pool download JOB-ID --all -o out/` for every task's files.
