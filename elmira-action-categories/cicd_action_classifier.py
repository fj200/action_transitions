"""
cicd_action_classifier.py
------------------
Walks a /dataset directory recursively, finds all CSV files, extracts
specific columns, and splits rows into two groups:

  - CI/CD     : CSV file lives anywhere inside a 'Continuous_integration'
                subdirectory (at any depth)
  - Non-CI/CD : everything else

Output (written to the dataset root):
  - cicd_actions.csv
  - non_cicd_actions.csv

Bad rows (any selected column is empty/null) are silently skipped.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONFIGURE HERE — edit these two values, nothing else needs to change.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

# Columns to extract from every CSV (must match header names exactly).
# Rows missing ANY of these columns in the file, or having an empty/null
# value in any of them, are dropped.
COLUMNS_TO_BEPRESENT: list[str] = [
    'action_name_used',
]
COLUMNS_TO_EXTRACT: list[str] = [
    'Action Name',
    'action_name_used',
    'category',
    'short description',
    'Link to action page on GHM',
    'link to action repo',
    'Long_des',
    'downlod_link',
    'download_rep_link',
    'action_file_descriptions',
    'action_yml_list'

    # "repo_name",
    # "action_name",
    # "version",
    # "commit_sha",
    # ADD YOUR COLUMN NAMES HERE
]

# The subdirectory name that marks a file as CI/CD.
# Any CSV whose path contains this directory name (case-sensitive) at any
# depth is treated as CI/CD.
CICD_DIRECTORY_NAMES: set[str] = {
    "Continuous_integration",
    'Deployment'
    # "Another_cicd_dir",
    # add more as needed
}

# ── You should not need to edit below this line ──────────────────────────────

import csv
import sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)


CICD_OUTPUT     = "cicd_actions.csv"
NON_CICD_OUTPUT = "non_cicd_actions.csv"


def is_cicd_file(path: Path) -> bool:
    """Return True if any part of the path matches any of the CICD_DIRECTORY_NAMES."""
    return bool(set(path.parts) & CICD_DIRECTORY_NAMES)


def is_bad_row(row: dict, columns: list[str]) -> bool:
    """Return True if any required column is missing or empty in this row."""
    for col in columns:
        val = row.get(col, "")
        if val is None or str(val).strip() == "":
            return True
    return False


def extract_row(row: dict, columns: list[str]) -> dict:
    """Return a new dict with only the requested columns."""
    return {col: row.get(col, '') for col in columns}


def process_dataset(dataset_root: str = "dataset") -> None:
    root = Path(dataset_root).resolve()

    if not root.is_dir():
        print(f"ERROR: '{root}' is not a directory.")
        sys.exit(1)

    if not COLUMNS_TO_EXTRACT:
        print("ERROR: COLUMNS_TO_EXTRACT is empty. Edit the config at the top of the script.")
        sys.exit(1)

    # Discover every CSV under the root, at any depth
    all_csvs = sorted(root.rglob("*.csv"))

    # Skip the output files themselves if re-running
    output_names = {CICD_OUTPUT, NON_CICD_OUTPUT}
    all_csvs = [p for p in all_csvs if p.name not in output_names]

    if not all_csvs:
        print(f"No CSV files found under '{root}'.")
        sys.exit(0)

    print(f"Found {len(all_csvs)} CSV file(s) under '{root}'")
    print(f"Extracting columns: {COLUMNS_TO_EXTRACT}\n")

    cicd_rows:     list[dict] = []
    non_cicd_rows: list[dict] = []
    skipped_bad   = 0
    skipped_cols  = 0

    for csv_path in all_csvs:
        relative = csv_path.relative_to(root)
        cicd = is_cicd_file(relative)
        label = "CI/CD" if cicd else "non-CI/CD"

        try:
            with open(csv_path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                fieldnames = reader.fieldnames or []

                # Check which requested columns are actually present
                # missing = [c for c in COLUMNS_TO_EXTRACT if c not in fieldnames]
                if "action_name_used" not in fieldnames:
                    print(f"  [{label}] SKIP {relative} — missing columns: action_name_used")
                    skipped_cols += 1
                    continue

                file_good = file_bad = 0
                for row in reader:
                    if is_bad_row(row, COLUMNS_TO_BEPRESENT):
                        file_bad += 1
                        skipped_bad += 1
                        continue
                    extracted = extract_row(row, COLUMNS_TO_EXTRACT)
                    if cicd:
                        cicd_rows.append(extracted)
                    else:
                        non_cicd_rows.append(extracted)
                    file_good += 1

                print(f"  [{label}] {relative}  →  {file_good} rows kept, {file_bad} bad rows skipped")

        except Exception as exc:
            print(f"  ERROR reading {relative}: {exc}")

    # Write outputs
    _write_csv(root / CICD_OUTPUT,     cicd_rows,     COLUMNS_TO_EXTRACT)
    _write_csv(root / NON_CICD_OUTPUT, non_cicd_rows, COLUMNS_TO_EXTRACT)

    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Summary
  CI/CD rows      : {len(cicd_rows):>6}  →  {CICD_OUTPUT}
  Non-CI/CD rows  : {len(non_cicd_rows):>6}  →  {NON_CICD_OUTPUT}
  Bad rows skipped: {skipped_bad:>6}
  Files skipped (missing columns): {skipped_cols}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━""")


def _write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    root_arg = sys.argv[1] if len(sys.argv) > 1 else "datasets"
    process_dataset(dataset_root=root_arg)