"""
build_ratings.py  --  RUN THIS ONCE (locally or in any environment with internet)
to produce ratings.json, then commit ratings.json to your repo.

    python build_ratings.py            # trains last 3 years incl. challengers
    python build_ratings.py 5          # trains last 5 years

Why: training loads Jeff Sackmann's CSVs with pandas, which is the app's biggest
memory user. By baking the result into a small JSON the live server just LOADS,
the running site never imports pandas or touches the CSVs — so it fits in 512 MB
comfortably, starts faster, and keeps 100% of the historical training.

Re-run this whenever you want to refresh ratings (e.g. monthly) and re-commit
ratings.json. Everything else about deploying stays the same.
"""

import datetime as dt
import sys

from predictions import PredictionEngine

years = int(sys.argv[1]) if len(sys.argv) > 1 else 3
this_year = dt.date.today().year

eng = PredictionEngine()
n = eng.train_from_sackmann(range(this_year - years + 1, this_year + 1))
count = eng.export_ratings("ratings.json")
print(f"Trained on {n} matches across {years} years.")
print(f"Exported {count} player ratings -> ratings.json")
print("Now commit ratings.json to your repo and redeploy.")
