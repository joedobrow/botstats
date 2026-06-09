import csv
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

draft_mmr = []
windrun_mmr = []

with open(DATA_DIR / "mmr_comparison.csv", "r") as f:
    reader = csv.DictReader(f)
    for row in reader:
        draft_mmr.append(float(row["draft_mmr"]))
        windrun_mmr.append(float(row["windrun_mmr"]))

x = np.array(draft_mmr)
y = np.array(windrun_mmr)

slope, intercept = np.polyfit(x, y, 1)
r = np.corrcoef(x, y)[0, 1]

print(f"windrun_mmr = {slope:.4f} * draft_mmr + {intercept:.2f}")
print(f"R² = {r**2:.4f}")
