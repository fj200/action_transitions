"""
verify_checkpoint.py
---------------------
1. Pull tags from the marketplace_url and source_repo_url (if present)
   and check them against CICD_TAGS -> http_verdict
2. claude_verdict=y & http_verdict=y -> final_verdict=y
3. claude_verdict=y & http_verdict=n -> written to needs_manual_review.csv
4. all final_verdict=y rows are merged into classified_yes.csv
   (deduped by action_name) -> classified_yes_merged.csv
"""

CHECKPOINT_FILE      = "classified_maybe.csv"
CLASSIFIED_YES_FILE  = "classified_yes.csv"
OUTPUT_DIR           = "."

CICD_TAGS = {"continuous-integration", "continuous-deployment", "container-ci"}

import csv, sys, time
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run: pip install requests beautifulsoup4")
    sys.exit(1)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"}
SLEEP_BETWEEN = 1


def get_tags(url: str) -> set:
    """
    Fetch a page and return its tags.
    - marketplace pages (github.com/marketplace/actions/...) use ?category= links
    - plain repo pages (github.com/owner/repo) use /topics/ links instead
    """
    if not url:
        return set()
    try:
        resp = requests.get(url, timeout=10, headers=HEADERS)
        if resp.status_code != 200:
            return set()
        soup = BeautifulSoup(resp.text, "html.parser")

        is_marketplace = "/marketplace/" in url
        href_match = (lambda h: h and "category=" in h) if is_marketplace \
                else (lambda h: h and "/topics/" in h)

        return {
            a.get_text(strip=True).lower()
            for a in soup.find_all("a", href=href_match)
        }
    except Exception:
        return set()


def write_csv(path: Path, rows: list, cols: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore", restval="")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    out_dir = Path(OUTPUT_DIR)
    ckpt = Path(CHECKPOINT_FILE)
    if not ckpt.exists():
        print(f"ERROR: '{ckpt}' not found.")
        sys.exit(1)

    with open(ckpt, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    in_cols = list(rows[0].keys()) if rows else []
    out_cols = in_cols + ["http_verdict", "final_verdict"]

    review_rows = []
    yes_rows = []

    for i, row in enumerate(rows, start=1):
        action = row.get("action_name", "")
        claude_verdict = row.get("claude_verdict", "n").strip().lower()
        marketplace_url = f"https://github.com/marketplace/actions/{action.split('/')[-1]}"
        source_repo_url =  f"https://github.com/{action.strip()}"
        http_verdict = "y" if get_tags(marketplace_url) & CICD_TAGS else "n"
        if http_verdict == "n":
            http_verdict = "y" if get_tags(source_repo_url) & CICD_TAGS else "n"
        final_verdict = "y" if http_verdict == "y" else ""
        row["http_verdict"] = http_verdict
        row["final_verdict"] = final_verdict

        if claude_verdict == "y" and http_verdict == "n":
            review_rows.append(row)
        if final_verdict == "y":
            yes_rows.append(row)

        print(f"  [{i:>5}/{len(rows)}] {action:40s} claude={claude_verdict} http={http_verdict}")
        time.sleep(SLEEP_BETWEEN)

    write_csv(out_dir / "classified_maybe_verified.csv", rows, out_cols)
    write_csv(out_dir / "needs_manual_review.csv", review_rows, out_cols)

    # merge agreed-yes rows into the existing classified_yes file, dedup by action_name
    existing_path = Path(CLASSIFIED_YES_FILE)
    existing_rows = []
    if existing_path.exists():
        with open(existing_path, newline="", encoding="utf-8") as fh:
            existing_rows = list(csv.DictReader(fh))

    seen = set()
    merged_rows = []
    for r in existing_rows + yes_rows:
        key = r.get("action_name", "")
        if key in seen:
            continue
        seen.add(key)
        merged_rows.append(r)

    existing_cols = list(existing_rows[0].keys()) if existing_rows else []
    merged_cols = list(dict.fromkeys(existing_cols + out_cols))
    write_csv(out_dir / "cicd_actions.csv", merged_rows, merged_cols)

    print(
        f"\n===========================================\n"
        f"  classified_maybe_verified.csv : {len(rows):>5} total\n"
        f"  needs_manual_review.csv       : {len(review_rows):>5}  (claude=y, http=n)\n"
        f"  cicd_actions.csv (merged with yes             : {len(merged_rows):>5}  "
        f"({len(existing_rows)} existing + {len(yes_rows)} new)\n"
        f"==========================================="
    )


if __name__ == "__main__":
    main()