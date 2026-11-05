"""Make a thumbnail and a grey edge picture of every picture in /data, and write them to /out.

Each part takes every n-th picture (POOL_TASK_INDEX of POOL_TASK_COUNT), so the parts share the
work without overlap; one part does all of them. Writes /out/result.json and /out/pictures.csv.
"""
import csv
import json
import os
import time
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

part = int(os.environ.get("POOL_TASK_INDEX", "0"))
parts = int(os.environ.get("POOL_TASK_COUNT", "1"))
out = Path("/out")
pictures = sorted(Path("/data").glob("*.png"))[part::parts]

start = time.time()
rows = []
for path in pictures:
    with Image.open(path) as img:
        thumb = img.copy()
        thumb.thumbnail((160, 160))
        thumb.save(out / f"thumb-{path.name}")
        grey = ImageOps.grayscale(img).filter(ImageFilter.FIND_EDGES)
        grey.save(out / f"edges-{path.name}")
        rows.append({"picture": path.name, "width": img.width, "height": img.height,
                     "thumb_width": thumb.width, "thumb_height": thumb.height})
    print(f"{path.name}: done", flush=True)

with open(out / "pictures.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["picture", "width", "height", "thumb_width", "thumb_height"])
    w.writeheader()
    w.writerows(rows)
result = {"part": part, "pictures": len(rows), "seconds": round(time.time() - start, 2)}
(out / "result.json").write_text(json.dumps(result))
print(f"done: {len(rows)} pictures in {result['seconds']} s")
