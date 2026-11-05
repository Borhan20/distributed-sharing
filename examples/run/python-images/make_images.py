"""Draw 12 made-up sample pictures (colour gradients with circles) into the folder given."""
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw

folder = Path(sys.argv[1])
folder.mkdir(parents=True, exist_ok=True)
random.seed(1)
for n in range(12):
    img = Image.new("RGB", (800, 600))
    draw = ImageDraw.Draw(img)
    for y in range(600):
        draw.line([(0, y), (800, y)], fill=(y * 255 // 600, 80 + n * 10, 255 - y * 255 // 600))
    for _ in range(15):
        x, y, r = random.randint(0, 800), random.randint(0, 600), random.randint(10, 80)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=tuple(random.randint(0, 255) for _ in range(3)))
    img.save(folder / f"picture-{n:02d}.png")
print(f"made 12 pictures in {folder}")
