import argparse
import json
import pathlib
import random
import time

parser = argparse.ArgumentParser()
parser.add_argument("--seed", type=int, required=True)
parser.add_argument("--samples", type=int, required=True)
args = parser.parse_args()

random.seed(args.seed)
start = time.time()
inside = sum(1 for _ in range(args.samples) if random.random() ** 2 + random.random() ** 2 <= 1)
result = {"seed": args.seed, "samples": args.samples, "pi": 4 * inside / args.samples,
          "seconds": round(time.time() - start, 2)}

print(json.dumps(result))  # visible in the task's log
out = pathlib.Path("/out")
out.mkdir(exist_ok=True)
(out / "result.json").write_text(json.dumps(result))  # one row of the job's results table
