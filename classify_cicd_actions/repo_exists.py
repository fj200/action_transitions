import csv, time, requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

FILE = Path("classified_maybe_verified.csv")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"}
MAX_WORKERS = 8

rows = list(csv.DictReader(open(FILE, newline="", encoding="utf-8")))


def check_row(row: dict) -> dict:
    action = "/".join(row.get("action_name", "").strip().split("/")[:2])
    try:
        exists = requests.head(f"https://github.com/{action}", timeout=10,
                            headers=HEADERS, allow_redirects=True).status_code == 200
    except Exception:
        row["_exists"] = "Unknown"
        return row

    time.sleep(0.2)
    if not exists:
        row["http_verdict"] = "n"
        row["final_verdict"] = ""
    row["_exists"] = exists
    return row


with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
    for i, row in enumerate(pool.map(check_row, rows), 1):
        print(f"[{i}/{len(rows)}] {row.get('action_name',''):40s} exists={row.pop('_exists')}")

with open(FILE, "w", newline="", encoding="utf-8") as fh:
    writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)