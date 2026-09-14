"""
classify_by_membership.py
--------------------------
Classifies every action in extracted_uses.csv into three buckets:

  yes   — action found in the cicd set
  no    — local action (built in-repo, never published to marketplace)
          OR action found in the non-cicd set
  maybe — marketplace but matched neither cicd nor non-cicd

1. Local action  (./ .github/ / ${{ docker://)  -> no
2. In cicd set                                   -> yes
3. In non-cicd set                               -> no
4. Neither                                       -> maybe
"""

MASTER_FILE   = "extracted_uses.csv"
CICD_FILE     = "elmira-action-categories/cicd_actions.csv"
NON_CICD_FILE = "elmira-action-categories/non_cicd_actions.csv"
OUTPUT_DIR = "."

# ---------------------------------------------------------------------------
import csv, re, sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)

GITHUB_URL_RE = re.compile(
    r'https?://github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$'
)


def normalize(name: str) -> str:
    return name.strip().lower().split("@")[0].rstrip("/")


def is_local(norm: str) -> bool:
    return (
            not norm
            or norm.startswith(".")
            or norm.startswith("$")
            or norm.startswith("docker://")
            or norm.startswith("/")
            or "/" not in norm
    )


def owner_repo_from_master(norm: str) -> str | None:
    if is_local(norm):
        return None
    parts = [p for p in norm.split("/") if p]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def build_cicd_lookup(path: Path) -> dict[str, dict]:
    """
    Returns a dict mapping each candidate key (owner/repo and owner/slug)
    to the full metadata row from the cicd CSV.

    If two different rows produce the same key the last one wins —
    in practice this is rare and the metadata is equivalent.
    """
    lookup: dict[str, dict] = {}

    if not path.exists():
        print(f"  WARNING: '{path}' not found.")
        return lookup
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                url = row.get("link to action repo", "").strip()
                slug = row.get("action_name_used", "").strip().lower()
                m = GITHUB_URL_RE.match(url)
                if not m:
                    continue
                owner, repo = m.group(1).lower(), m.group(2).lower()

                for candidate in {f"{owner}/{repo}", f"{owner}/{slug}"}:
                    if candidate:
                        lookup[candidate] = row
    except Exception as exc:
        print(f"  ERROR reading '{path}': {exc}")
    return lookup


def build_action_set(path: Path) -> set[str]:
    """Returns a set of candidate keys (owner/repo and owner/slug) for non-cicd."""
    actions: set[str] = set()
    if not path.exists():
        print(f"  WARNING: '{path}' not found.")
        return actions
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                url = row.get("link to action repo", "").strip()
                slug = row.get("action_name_used", "").strip().lower()
                m = GITHUB_URL_RE.match(url)
                if not m:
                    continue
                owner, repo = m.group(1).lower(), m.group(2).lower()
                actions.add(f"{owner}/{repo}")
                if slug:
                    actions.add(f"{owner}/{slug}")
    except Exception as exc:
        print(f"  ERROR reading '{path}': {exc}")
    return actions


def read_master(path: Path) -> list[str]:
    names: list[str] = []
    if not path.exists():
        print(f"  ERROR: '{path}' not found.")
        sys.exit(1)
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            val = row.get("action_name", "").strip().lower()
            if val:
                names.append(val)
    return names


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    out_dir = Path(OUTPUT_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Reading '{MASTER_FILE}'...")
    master_raw = read_master(Path(MASTER_FILE))

    print(f"Building cicd lookup from '{CICD_FILE}'...")
    cicd_lookup = build_cicd_lookup(Path(CICD_FILE))

    print(f"Building non-cicd set from '{NON_CICD_FILE}'...")
    non_cicd_set = build_action_set(Path(NON_CICD_FILE))

    # Infer cicd metadata columns from first lookup entry
    cicd_meta_cols = []
    if cicd_lookup:
        cicd_meta_cols = list(next(iter(cicd_lookup.values())).keys())

    print(
        f"\nLookup sizes — cicd: {len(cicd_lookup)}, "
        f"non-cicd: {len(non_cicd_set)}\n"
    )

    seen: set[str] = set()
    yes_rows: list[dict] = []
    no_rows: list[dict] = []
    maybe_rows: list[dict] = []

    for raw in sorted(set(master_raw)):
        norm = normalize(raw)
        if not norm or norm in seen:
            continue
        seen.add(norm)

        or_ = owner_repo_from_master(norm)

        if is_local(norm):
            no_rows.append({"action_name": raw, "matched_key": "—", "bucket": "no"})

        elif or_ and or_ in cicd_lookup:
            # Attach full cicd metadata to the yes row
            meta = cicd_lookup[or_]
            yes_rows.append({"action_name": raw, "matched_key": or_, **meta})

        elif or_ and or_ in non_cicd_set:
            no_rows.append({"action_name": raw, "matched_key": or_, "bucket": "no"})

        else:
            maybe_rows.append({"action_name": raw, "matched_key": or_ or "—", "bucket": "maybe"})

    # yes: master action_name + matched_key + all cicd metadata columns
    yes_fields = ["action_name", "matched_key"] + cicd_meta_cols
    # no / maybe: just the base columns
    other_fields = ["action_name", "matched_key", "bucket"]

    write_csv(out_dir / "classified_yes.csv", yes_rows, yes_fields)
    write_csv(out_dir / "classified_no.csv", no_rows, other_fields)
    write_csv(out_dir / "classified_maybe.csv", maybe_rows, other_fields)

    no_local = sum(1 for r in no_rows if r["matched_key"] == "—")
    no_matched = len(no_rows) - no_local
    dupes = len(master_raw) - len(set(master_raw))
    norm_dupes = len(set(master_raw)) - len(seen)

    print(
        f"===========================================\n"
        f"  classified_yes.csv   : {len(yes_rows):>6}  (cicd match + full metadata)\n"
        f"  classified_no.csv    : {len(no_rows):>6}  "
        f"({no_local} local + {no_matched} non-cicd match)\n"
        f"  classified_maybe.csv : {len(maybe_rows):>6}  (no match)\n"
        f"  Duplicates collapsed : {dupes} exact + {norm_dupes} normalisation\n"
        f"==========================================="
    )


if __name__ == "__main__":
    main()
