"""Train a support vector classifier on the handwritten digits that ship with scikit-learn (no download).

It tries several values of gamma and keeps the best one. Each part tries every n-th value
(POOL_TASK_INDEX of POOL_TASK_COUNT); one part tries them all. Writes /out/result.json (one row of
the results table), /out/scores.csv and /out/confusion_matrix.csv for the best model.
"""
import csv
import json
import os
import time

from sklearn.datasets import load_digits
from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.model_selection import train_test_split
from sklearn.svm import SVC

part = int(os.environ.get("POOL_TASK_INDEX", "0"))
parts = int(os.environ.get("POOL_TASK_COUNT", "1"))
gammas = [0.0001, 0.0003, 0.001, 0.003, 0.01, 0.03][part::parts]

X, y = load_digits(return_X_y=True)
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=0, stratify=y)

start = time.time()
scores = []
best = None
for gamma in gammas:
    model = SVC(gamma=gamma).fit(X_train, y_train)
    acc = accuracy_score(y_test, model.predict(X_test))
    scores.append({"gamma": gamma, "accuracy": round(acc, 4)})
    print(f"gamma {gamma:<7} accuracy {acc:.4f}", flush=True)
    if best is None or acc > best[1]:
        best = (gamma, acc, model)

with open("/out/scores.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["gamma", "accuracy"])
    w.writeheader()
    w.writerows(scores)
if best is not None:
    gamma, acc, model = best
    with open("/out/confusion_matrix.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["true\\predicted", *range(10)])
        for digit, row in enumerate(confusion_matrix(y_test, model.predict(X_test))):
            w.writerow([digit, *row])
    result = {"part": part, "best_gamma": gamma, "accuracy": round(acc, 4), "tried": len(scores),
              "train_size": len(y_train), "test_size": len(y_test), "seconds": round(time.time() - start, 2)}
    json.dump(result, open("/out/result.json", "w"))
    print(f"done: best gamma {gamma}, accuracy {acc:.4f}")
else:
    print("done: no values for this part")
