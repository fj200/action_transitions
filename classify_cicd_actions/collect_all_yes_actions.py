import csv
import sys
from pathlib import Path
csv.field_size_limit(sys.maxsize)

CLASSIFIED_YES_FILE = Path("classified_yes.csv")
VERIFIED_FILE        = Path("classified_maybe_verified.csv")
OUTPUT_FILE          = Path("all_cicd_actions.csv")

yes_rows = list(csv.DictReader(open(CLASSIFIED_YES_FILE, newline="", encoding="utf-8"))) \
    if CLASSIFIED_YES_FILE.exists() else []

verified_rows = list(csv.DictReader(open(VERIFIED_FILE, newline="", encoding="utf-8"))) \
    if VERIFIED_FILE.exists() else []
verified_yes_rows = [r for r in verified_rows if r.get("final_verdict", "").strip().lower() == "y"]

seen = set()
combined = []
for row in yes_rows + verified_yes_rows:
    action = row.get("action_name", "").strip()
    if not action or action in seen:
        continue
    seen.add(action)
    combined.append(row)

cols = list(dict.fromkeys(
    (list(yes_rows[0].keys()) if yes_rows else []) +
    (list(verified_yes_rows[0].keys()) if verified_yes_rows else [])
))

with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore", restval="")
    writer.writeheader()
    writer.writerows(combined)

print(
    f"classified_yes.csv               : {len(yes_rows):>5}\n"
    f"classified_maybe_verified (yes)  : {len(verified_yes_rows):>5}\n"
    f"all_cicd_actions.csv (deduped)   : {len(combined):>5}"
)