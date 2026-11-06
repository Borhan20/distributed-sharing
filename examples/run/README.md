# `pool run` examples

Each folder holds a program and its `Dockerfile`. Nothing else is needed.
pool builds the image on your computer and runs it on a computer of your
group. See the [user guide, §8](../../docs/user-guide.md#8-run-your-program-from-your-terminal-pool-run).

## Run one

1. Go to the example's folder: `cd examples/run/NAME`
2. Try it on your own computer first, with the rules of a member's computer: `pool run --local`
3. Run it on the group's computers: `pool run`
4. Find its files in `./pool-out`.

Until the CLI is packaged, type `uv run pool run` instead of `pool run`.

## The examples

| Example | Kind of work | Language | What comes back in `pool-out` |
|---|---|---|---|
| [`python-train`](python-train) | Training a model (gradient descent) | Python | `model.json`, `result.json` |
| [`sklearn-classifier`](sklearn-classifier) | Machine learning with a library: tries several settings, keeps the best | Python, scikit-learn | `result.json`, `scores.csv`, `confusion_matrix.csv` |
| [`python-images`](python-images) | Image processing: thumbnails and edge pictures | Python, Pillow | `thumb-*.png`, `edges-*.png`, `pictures.csv`, `result.json` |
| [`node-report`](node-report) | Data processing: a report from a dataset | JavaScript (Node.js) | `result.json`, `by_region.csv` |
| [`node-tests`](node-tests) | A test suite, with a test report | JavaScript (Node.js) | `junit.xml` |
| [`java-wordcount`](java-wordcount) | Text processing | Java | `top_words.csv`, `result.json` |
| [`r-stats`](r-stats) | Statistics: bootstrap confidence intervals | R | `intervals.csv`, `result.json` |
| [`go-primes`](go-primes) | A CPU-heavy computation on several cores | Go | `result.json` |
| [`cpp-heat`](cpp-heat) | A simulation (heat diffusion) | C++ | `heat.csv` |

## What they have in common

- **No settings needed.** pool chooses the sizes from your earlier runs. With
  `--local` they run with 1 CPU, 1 GiB memory and 1 hour, which is enough.
  Only `go-primes` has a `pool.yaml`, to show how to save a setting (`cpu: 4`).
- **Everything is in the image.** Packages are installed in the `Dockerfile`,
  because jobs have no network while they run. The data is made up and is
  created in the code or while the image is built.
- **Results go to `/out`.** A `/out/result.json` becomes one row of the job's
  results table.
- **They can be split.** Most of them read `POOL_TASK_INDEX` and
  `POOL_TASK_COUNT` and take only their share of the work. With one task (the
  default), that is all of it. `node-tests` splits its test files with Node's
  `--test-shard`.

## Change the program's options

The words after `--` go to the program, as with `docker run IMAGE ARG...`:

```bash
cd examples/run/python-train && pool run -- --epochs 300 --lr 0.1
cd examples/run/r-stats && pool run -- 50000      # resamples
```
