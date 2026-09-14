"""
process_gawd_actions.py
-----------------------
Scans a top-level `projects/` directory structured as:

    projects/
        project_a/
            file1.csv
            file2.csv
        project_b/
            ...

Each CSV has one or more columns. We only read the `diff_content` column.
Each cell in that column is a Python list repr of GAWD 5-tuples, e.g.:

    [('added', None, None, 'jobs.build.steps[0]', {'name': 'Checkout', 'uses': 'actions/checkout@v3'}),
     ('changed', 'jobs.build.steps[0].uses', 'actions/checkout@v2', 'jobs.build.steps[0].uses', 'actions/checkout@v3')]

We care about:
  - 'added'   → new_value is a step dict containing 'uses', or a job dict
                whose 'steps' list contains step dicts with 'uses'
  - 'changed' → old_path contains 'uses' → new_value is the raw action string

Output:
  - projects/<project>/actions_<project>.csv   — unique action names for that project
  - projects/extracted_uses.csv                — union of all projects

Uses ThreadPoolExecutor (3 workers) — I/O-bound CSV reading benefits from
threads rather than processes, keeping it laptop-friendly.
"""

import ast
import csv
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_action_name(action_string: str) -> str | None:
    """Strip version/tag/sha from an action string, return bare name.

    'actions/checkout@v3'       -> 'actions/checkout'
    'actions/checkout@main'     -> 'actions/checkout'
    'docker://alpine:3.14'      -> 'docker://alpine'  (kept as-is up to colon for docker refs)
    './.github/actions/my-act'  -> './.github/actions/my-act'  (local, kept whole)
    """
    if not isinstance(action_string, str):
        return None
    action_string = action_string.strip()
    if not action_string:
        return None

    if action_string.startswith("docker://"):
        # Docker refs use :tag not @ref — strip after the last colon,
        # but only if there is actually a tag (i.e. a colon exists after "docker://")
        body = action_string[len("docker://"):]  # e.g. "alpine:3.14" or "ghcr.io/owner/image:tag"
        name = body.split(":")[0].strip()  # drop ":tag"
        return f"docker://{name}" if name else None

    # All other forms (public actions, local actions, reusable workflows)
    # use @ref as the version separator
    name = action_string.split("@")[0].strip()
    return name if name else None


def parse_diff_content(raw: str) -> list:
    """
    Parse one diff_content cell — a Python list repr of GAWD 5-tuples.
    Returns a (possibly empty) list of valid 5-tuples.

    Expected cell shape:
        [('added', None, None, 'jobs.x.steps[0]', {...}),
         ('changed', 'jobs.x.steps[1].uses', 'old@v1', '...', 'new@v2')]
    """
    raw = raw.strip()
    if not raw:
        return []
    try:
        parsed = ast.literal_eval(raw)
        if not isinstance(parsed, list):
            return []
        return [t for t in parsed if isinstance(t, tuple) and len(t) == 5]
    except Exception:
        return []


def extract_actions_from_row(row_tuple) -> set[str]:
    """
    Given a parsed GAWD 5-tuple, return the set of action names we care about.

    Rules (matching the logic in your provided code):
      - 'added':   new_value may be a step dict with 'uses', OR a job dict
                   whose 'steps' list contains step dicts with 'uses'.
      - 'changed': if old_path (string) contains 'uses', new_value is the
                   action string after the change.
    """
    kind, old_path, old_value, new_path, new_value = row_tuple
    names: set[str] = set()

    if kind == 'added':
        # new_value could be a single step dict or a job dict with a steps list
        steps = []
        if isinstance(new_value, dict):
            if 'uses' in new_value:
                steps = [new_value]
            elif 'steps' in new_value:
                steps = new_value.get('steps', [])

        for step in steps:
            if not isinstance(step, dict) or 'uses' not in step:
                continue
            name = extract_action_name(step['uses'])
            if name:
                names.add(name)

    elif kind == 'changed':
        # old_path tells us what field changed; we only care about 'uses' fields
        path_str = str(old_path) if old_path is not None else ''
        if 'uses' in path_str:
            name = extract_action_name(str(new_value))
            if name:
                names.add(name)

    return names


# ---------------------------------------------------------------------------
# Per-project processing (runs in a thread)
# ---------------------------------------------------------------------------

def process_project(project_dir: Path) -> tuple[str, set[str]]:
    """
    Read all CSV files in `project_dir`, extract unique action names, write
    a per-project CSV, and return (project_name, set_of_names).
    """
    project_name = project_dir.name
    all_actions: set[str] = set()
    csv_files = sorted(project_dir.glob('*.csv'))

    if not csv_files:
        print(f"  [{project_name}] No CSV files found, skipping.")
        return project_name, all_actions

    for csv_path in csv_files:
        try:
            with open(csv_path, newline='', encoding='utf-8') as fh:
                reader = csv.DictReader(fh)
                if 'diff_changes' not in (reader.fieldnames or []):
                    print(f"  [{project_name}] WARNING: no diff_changes column in {csv_path.name}, skipping.")
                    continue
                for row in reader:
                    raw = row.get('diff_changes', '')
                    tuples = parse_diff_content(raw)
                    for gawd_tuple in tuples:
                        actions = extract_actions_from_row(gawd_tuple)
                        all_actions.update(actions)
        except Exception as exc:
            print(f"  [{project_name}] ERROR reading {csv_path.name}: {exc}")

    # Write per-project output CSV
    out_path = project_dir / f'actions_{project_name}.csv'
    _write_actions_csv(out_path, all_actions, header='action_name')
    print(f"  [{project_name}] {len(all_actions)} unique actions → {out_path.name}")

    return project_name, all_actions


def _write_actions_csv(path: Path, actions: set[str], header: str = 'action_name') -> None:
    """Write a set of action names to a single-column CSV (sorted)."""
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh)
        writer.writerow([header])
        for name in sorted(actions):
            writer.writerow([name])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(projects_root: str = 'projects', num_workers: int = 3) -> None:
    root = Path(projects_root).resolve()
    if not root.is_dir():
        print(f"ERROR: '{root}' is not a directory.")
        sys.exit(1)

    # Discover project sub-directories (one level deep)
    project_dirs = [p for p in sorted(root.iterdir()) if p.is_dir()]
    if not project_dirs:
        print(f"No sub-directories found inside '{root}'.")
        sys.exit(0)

    print(f"Found {len(project_dirs)} project(s) under '{root}'")
    print(f"Processing with {num_workers} threads...\n")

    master_actions: set[str] = set()

    # --- Threads are ideal here: the work is I/O-bound (CSV reads) ----------
    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        futures = {pool.submit(process_project, d): d for d in project_dirs}
        for future in as_completed(futures):
            project_dir = futures[future]
            try:
                _, actions = future.result()
                master_actions.update(actions)
            except Exception as exc:
                print(f"  [{project_dir.name}] Unhandled error: {exc}")

    # Write master file
    master_path = root / 'master_actions.csv'
    _write_actions_csv(master_path, master_actions, header='action_name')
    print(f"\nDone. {len(master_actions)} unique actions across all projects → {master_path}")


if __name__ == '__main__':
    # Optionally pass the projects root as the first CLI argument
    root_arg = 'Proj_repos'
    main(projects_root=root_arg, num_workers=3)