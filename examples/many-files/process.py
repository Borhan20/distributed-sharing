"""Count lines and words of every file in /data, split over the job's parts.

Part i of n takes every n-th file, starting at file i, so the parts share the work without overlap.
Each part writes /out/summary.csv with the same columns; the job's "Download all" zip merges them
into combined/summary.csv.
"""
import csv
import os
import pathlib

part = int(os.environ["POOL_TASK_INDEX"])
parts = int(os.environ["POOL_TASK_COUNT"])
files = sorted(pathlib.Path("/data").glob("*.txt"))
mine = files[part::parts]

with open("/out/summary.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["file", "lines", "words"])
    for path in mine:
        text = path.read_text()
        w.writerow([path.name, len(text.splitlines()), len(text.split())])
print(f"part {part} of {parts}: {len(mine)} of {len(files)} files")
