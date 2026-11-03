"""A small training run: fit y = 3x + 2 by gradient descent. Writes the model and metrics to /out."""
import argparse
import json
import random

parser = argparse.ArgumentParser()
parser.add_argument("--epochs", type=int, default=200)
parser.add_argument("--lr", type=float, default=0.05)
args = parser.parse_args()

random.seed(0)
data = [(x, 3 * x + 2 + random.gauss(0, 0.1)) for x in (random.uniform(-1, 1) for _ in range(1000))]
w = b = 0.0
for epoch in range(args.epochs):
    gw = sum(2 * (w * x + b - y) * x for x, y in data) / len(data)
    gb = sum(2 * (w * x + b - y) for x, y in data) / len(data)
    w, b = w - args.lr * gw, b - args.lr * gb
    if epoch % 50 == 0:
        loss = sum((w * x + b - y) ** 2 for x, y in data) / len(data)
        print(f"epoch {epoch}: loss {loss:.4f}", flush=True)

loss = sum((w * x + b - y) ** 2 for x, y in data) / len(data)
json.dump({"w": w, "b": b}, open("/out/model.json", "w"))
json.dump({"epochs": args.epochs, "lr": args.lr, "loss": loss}, open("/out/result.json", "w"))
print(f"done: w={w:.3f} b={b:.3f} loss={loss:.4f}")
