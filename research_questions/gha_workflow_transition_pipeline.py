import zipfile, logging, traceback, sys, os, re, ast, yaml, random, io
import pandas as pd
from pathlib import Path
from collections import defaultdict, Counter
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_PATH      = '../Proj_repos.zip'
RESULT_PATH       = './results'
ANALYSIS_PATH     = './analysis'
CICD_ACTIONS_FILE = '../classify_cicd_actions/cicd_actions.csv'
INDEX             = 0    # ← set per server (0, 1, 2 ...) to avoid overwriting

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f'run_{INDEX}.log', mode='w'),
    ]
)
log = logging.getLogger(__name__)

SHA_RE   = re.compile(r'^[0-9a-f]{6,40}$', re.IGNORECASE)
HTTPS_RE = re.compile(r'https?://[^/]+/([^/\s]+/[^/\s@]+)', re.IGNORECASE)
SEMVER_RE = re.compile(r'v?(\d+)(?:\.(\d+))?(?:\.(\d+))?', re.IGNORECASE)

Path(RESULT_PATH).mkdir(parents=True, exist_ok=True)
Path(ANALYSIS_PATH).mkdir(parents=True, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────
def normalize_action(raw):
    """Extract owner/repo from raw string — handles https:// links."""
    raw = str(raw).strip()
    if raw.startswith('http'):
        m = HTTPS_RE.match(raw)
        return m.group(1).lower() if m else raw
    return raw

def bare(action):
    """owner/repo without @version, normalizing https links."""
    return normalize_action(action.split('@')[0] if '@' in action else action)

def vtag(action):
    parts = str(action).split('@')
    return parts[1] if len(parts) > 1 else ''

def is_sha(t):
    return bool(SHA_RE.match(str(t).strip()))

def fmt_date(ts):
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return f"{dt.month}/{dt.day}/{dt.year}"
    except Exception:
        return str(ts)

def to_month(d):
    try:
        p = str(d).strip().split('/')
        return f"{p[2]}-{int(p[0]):02d}"
    except Exception:
        return 'unknown'

def semver(tag):
    m = SEMVER_RE.match(str(tag).strip())
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)) if m else None

def ver_dir(old, new):
    a, b = semver(old), semver(new)
    if not (a and b): return 'other'
    return 'upgrade' if b > a else ('downgrade' if b < a else 'other')

def pinning_change(old_v, new_v):
    """Detect SHA pin/unpin between two version strings."""
    old_sha, new_sha = is_sha(old_v), is_sha(new_v)
    if new_sha: return 'pinned'
    if old_sha and not new_sha: return 'unpinned'
    return ''


# ── Load allowed actions + marketplace map ────────────────────────────────────
def load_allowed_actions(path):
    """
    Returns (allowed_set or None, marketplace_map dict).
    CSV must have columns: action_name (or first col), is_marketplace.
    """
    log.info(f"Loading allowed actions from '{path}'")
    if path is None:
        return None, {}
    p = Path(path)
    if not p.exists():
        log.warning(f"'{path}' not found — no filtering applied")
        return None, {}

    df  = pd.read_csv(p)
    col = 'action' if 'action' in df.columns else df.columns[0]

    marketplace_col = 'is_marketplace' if 'is_marketplace' in df.columns else None

    marketplace_map = {}
    allowed = set()

    for _, row in df.iterrows():
        name = str(row[col]).strip()
        if not name:
            continue
        key = '/'.join(name.lower().split('/')[-2:])   # ensure owner/repo
        allowed.add(key)
        if marketplace_col:
            val = row[marketplace_col]
            marketplace_map[key] = bool(val) if pd.notna(val) else False

    log.info(f"Loaded {len(allowed)} allowed actions | marketplace flags: {len(marketplace_map)}")
    return allowed, marketplace_map

def is_allowed(bare_action, allowed):
    return allowed is None or bare_action.lower() in allowed


# ── Data classes ──────────────────────────────────────────────────────────────
class Version:
    def __init__(self, _id=-1, diff_changes=None, content_yaml=None, date=0):
        self.id               = _id
        self.diff_changes     = diff_changes or []
        self.content_yaml     = content_yaml or {}
        self.committed_date   = date
        self.actions_added        = set()
        self.actions_removed      = set()
        self.actions_updated      = set()
        self.actions_transitioned = set()
        self.actions_moved        = set()
        self.changes              = set()
        self.action_positions: dict[tuple, set[int]] = {}
        self.current_actions = set()

class File:
    def __init__(self, name):
        self.name     = name
        self.versions = [Version()]

class Project:
    def __init__(self, name, files):
        self.name  = name
        self.files = files


# ── Load & parse ──────────────────────────────────────────────────────────────
def get_random_projects(projects, k):
    random.seed(42)
    return random.sample(projects, min(k, len(projects)))

def filter_projects(zip_path, index, total=1):
    log.info(f"Filtering projects from '{zip_path}' (partition {index}/{total})")
    with zipfile.ZipFile(zip_path, 'r') as zf:
        projects = sorted({
            name.split('/')[1]
            for name in zf.namelist()
            if name.startswith('Proj_repos/')
            and name.count('/') == 2
            and name.split('/')[1]
        })
    log.info(f"Found {len(projects)} total projects, partition {index}: {len(projects[index::total])} projects")
    return projects[index::total]

def get_project_files(zip_path, projects):
    log.info(f"Reading files for {len(projects)} projects")
    project_files = defaultdict(list)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        for i, project in enumerate(projects):
            prefix = f'Proj_repos/{project}/'
            for name in zf.namelist():
                if not name.startswith(prefix): continue
                fname = name.split('/')[-1]
                if not fname.endswith('.csv'): continue
                try:
                    project_files[project].append((fname, zf.read(name)))
                except Exception as e:
                    log.error(f"  [{project}] Failed to read '{fname}': {e}")
            if (i + 1) % 500 == 0 or i == 0 or i == len(projects) - 1:
                log.info(f"  [{i+1}/{len(projects)}] {project}: {len(project_files[project])} files")
    log.info(f"Loaded files for {len(project_files)} projects")
    return project_files

def parse_projects(project_files):
    log.info(f"Parsing {len(project_files)} projects")
    projects = {}
    for i, (proj, file_list) in enumerate(project_files.items()):
        try:
            files = []
            for fname, raw_bytes in file_list:
                try:
                    f  = File(fname)
                    df = pd.read_csv(io.BytesIO(raw_bytes))
                    for idx, row in df.iterrows():
                        if not row['valid_yaml']: continue
                        try:    content_yaml = yaml.safe_load(row['file_content'])
                        except: content_yaml = {}
                        try:    diff_changes = ast.literal_eval(row['diff_changes'])
                        except: diff_changes = []
                        f.versions.append(Version(idx, diff_changes, content_yaml, row['committed_date']))
                    files.append(f)
                except Exception as e:
                    log.error(f"  [{proj}] Failed to parse '{fname}': {e}")
            projects[proj] = Project(proj, files)
            if (i + 1) % 500 == 0 or i == 0 or i == len(project_files) - 1:
                log.info(f"  [{i+1}/{len(project_files)}] {proj}: {len(files)} files")
        except Exception as e:
            log.error(f"  [{proj}] Failed: {e}")
    log.info(f"Parsed {len(projects)} projects")
    return projects


# ── Diff processing ───────────────────────────────────────────────────────────
JOB_STEP_RE = re.compile(r"^jobs\.([A-Za-z0-9_\-]+)\.steps\[(\d+)\]$")
JOB_ONLY_RE = re.compile(r"^jobs\.([A-Za-z0-9_\-]+)$")
USES_RE     = re.compile(r"^jobs\.([A-Za-z0-9_\-]+)\.steps\[(\d+)\]\.uses$")

def steps_in_path(path, value):
    path = str(path)
    m = JOB_STEP_RE.match(path)
    if m:
        yield value, m.group(1), int(m.group(2)); return
    m = JOB_ONLY_RE.match(path)
    if m:
        steps = value.get('steps', []) if isinstance(value, dict) else []
        for i, step in enumerate(steps):
            yield step, m.group(1), i
    m = USES_RE.match(path)
    if m:
        yield {'uses': value}, m.group(1), int(m.group(2));
        return

def actions_in_job(content_yaml, job_name):
    result = defaultdict(set)
    try:
        steps = (content_yaml or {}).get('jobs', {}).get(job_name, {}).get('steps', []) or []
        for i, step in enumerate(steps):
            if isinstance(step, dict) and 'uses' in step:
                result[step['uses']].add(i)
    except Exception:
        pass
    return result

def process_added(new_path, new_value, prev_ver, cur_ver):
    for step, job, step_idx in steps_in_path(new_path, new_value):
        if not isinstance(step, dict) or 'uses' not in step: continue
        action = step['uses']
        if not is_allowed(bare(action), allowed): continue
        prev_indices = actions_in_job(prev_ver.content_yaml, job).get(action, set())
        cur_indices  = actions_in_job(cur_ver.content_yaml,  job).get(action, set())
        if step_idx in prev_indices or not cur_indices: continue
        cur_ver.actions_added.add((action, job, step_idx))
        cur_ver.changes.add(('addition', (action, job, step_idx, action)))
        cur_ver.action_positions[(action, job)] = cur_indices

def process_removed(old_path, old_value, prev_ver, cur_ver):
    for step, job, step_idx in steps_in_path(old_path, old_value):
        if not isinstance(step, dict) or 'uses' not in step: continue
        action = step['uses']
        if not is_allowed(bare(action), allowed): continue
        cur_indices = actions_in_job(cur_ver.content_yaml, job).get(action, set())
        if step_idx in cur_indices: continue
        # if not cur_indices:
        cur_ver.actions_removed.add((action, job, step_idx))
        cur_ver.changes.add(('removal', (action, job, step_idx, 'X')))
        cur_ver.action_positions[(action, job)] = cur_indices

def process_changed(old_path, old_value, new_value, cur_ver):
    if not isinstance(old_path, str) or 'uses' not in old_path: return
    m        = USES_RE.match(str(old_path))
    job      = m.group(1) if m else 'unknown'
    step_idx = int(m.group(2)) if m else -1

    old_bare, new_bare = bare(old_value), bare(new_value)
    is_update = old_bare == new_bare

    if is_update:
        # Same action, version changed → update (may include pinning/unpinning)
        if not is_allowed(old_bare, allowed): return
        cur_ver.changes.add(('update', (old_value, new_value, job, step_idx)))
        cur_ver.actions_updated.add((old_value, new_value, job, step_idx))
    else:
        # Different action → transition
        # if not is_allowed(old_bare, allowed): return  # old must be CI/CD
        is_allowed_old, is_allowed_new = is_allowed(old_bare, allowed), is_allowed(new_bare, allowed)
        if is_allowed_old and is_allowed_new:
            # Both CI/CD → replacement
            cur_ver.changes.add(('transition', (old_value, new_value, job, step_idx)))
            cur_ver.actions_transitioned.add((old_value, new_value, job, step_idx))
        elif is_allowed_old:
            # New action is NOT CI/CD → treat as removal of old only
            cur_ver.actions_removed.add((old_value, job, step_idx))
            cur_ver.changes.add(('removal', (old_value, job, step_idx, 'X')))
        elif is_allowed_new:
            cur_ver.actions_added.add((new_value, job, step_idx))
            cur_ver.changes.add(('addition', (new_value, job, step_idx, new_value)))


    old_key, new_key = (old_value, job), (new_value, job)
    cur_ver.action_positions[old_key] = actions_in_job(cur_ver.content_yaml, job).get(old_value, set())
    cur_ver.action_positions[new_key] = actions_in_job(cur_ver.content_yaml, job).get(new_value, set())

def process_moved(old_path, new_path, value, cur_ver):
    m_old = JOB_STEP_RE.match(str(old_path))
    m_new = JOB_STEP_RE.match(str(new_path))
    if not (m_old and m_new): return
    job = m_new.group(1)
    if not isinstance(value, dict) or 'uses' not in value: return
    action = value['uses']
    if not is_allowed(bare(action), allowed): return
    key = (action, job)
    cur_ver.action_positions[key] = actions_in_job(cur_ver.content_yaml, job).get(action, set())

def process_diff(change, prev_ver, cur_ver):
    kind, old_path, old_value, new_path, new_value = change
    if kind == 'added':    process_added(new_path, new_value, prev_ver, cur_ver)
    elif kind == 'removed': process_removed(old_path, old_value, prev_ver, cur_ver)
    elif kind == 'changed': process_changed(old_path, old_value, new_value, cur_ver)
    elif kind == 'moved':   process_moved(old_path, new_path, new_value, cur_ver)

def propagate_actions(prev_ver, cur_ver):
    current = set(prev_ver.current_actions)
    current |= cur_ver.actions_added
    current = {a for a in current if a not in cur_ver.actions_removed}
    for old_full, new_full, job, step_idx in cur_ver.actions_updated:
        current.discard((old_full, job, step_idx))
        current.add((new_full, job, step_idx))
    for old_full, new_full, job, step_idx in cur_ver.actions_transitioned:
        current.discard((old_full, job, step_idx))
        current.add((new_full, job, step_idx))
    cur_ver.current_actions = current

def propagate_positions(prev_ver, cur_ver):
    positions = {k: set(v) for k, v in prev_ver.action_positions.items()}
    for key, indices in cur_ver.action_positions.items():
        if indices: positions[key] = set(indices)
        else:       positions.pop(key, None)
    # for old_full, new_full, job, step_idx in cur_ver.actions_transitioned | cur_ver.actions_updated:
    #     old_k, new_k = (old_full, job), (new_full, job)
    #     if old_k in positions:
    #         positions.setdefault(new_k, set()).update(positions.pop(old_k))
    for key, old_idx, new_idx in cur_ver.actions_moved:
        positions[key].discard(old_idx)
    cur_ver.action_positions = positions

def process_all(projects):
    log.info(f"Processing {len(projects)} projects")
    for i, (key, project) in enumerate(sorted(projects.items())):
        try:
            for file in project.files:
                try:
                    for j in range(1, len(file.versions)):
                        prev, cur = file.versions[j - 1], file.versions[j]
                        for change in cur.diff_changes:
                            try:    process_diff(change, prev, cur)
                            except Exception as e:
                                log.error(f"  [{key}/{file.name}] v{j} diff error: {e}")
                        propagate_actions(prev, cur)
                        propagate_positions(prev, cur)
                except Exception as e:
                    log.error(f"  [{key}] Failed file '{file.name}': {e}")
            if (i + 1) % 500 == 0 or i == 0 or i == len(projects) - 1:
                log.info(f"  [{i+1}/{len(projects)}] {key}")
        except Exception as e:
            log.error(f"  [{key}] Failed project: {e}")
    log.info("process_all complete")


# ── Per-project CSV export (unchanged) ───────────────────────────────────────
def export_file_csv(project_name, file, allowed):
    out_dir = Path(RESULT_PATH) / project_name
    out_dir.mkdir(parents=True, exist_ok=True)

    versions       = [v for v in file.versions[1:] if v.committed_date]
    release_labels = [f"r{i+1} ({fmt_date(v.committed_date)})" for i, v in enumerate(versions)]

    ver_timeline: dict[tuple, dict] = defaultdict(dict)
    pos_timeline: dict[tuple, dict] = defaultdict(dict)
    counts = Counter()

    for label, version in zip(release_labels, versions):
        for kind, _ in version.changes:
            counts[kind] += 1
        current: dict[tuple, list] = defaultdict(list)
        for (full_action, job), indices in version.action_positions.items():
            if indices:
                current[(bare(full_action), job)].append((full_action, sorted(indices)))

        all_keys = set(ver_timeline) | set(current)
        for key in all_keys:
            if key in current:
                ver_timeline[key][label] = ';'.join(
                    vtag(fa) or fa for fa, _ in current[key])
                pos_timeline[key][label] = '|'.join(
                    ';'.join(f"{vtag(fa) or fa}@step{i+1}" for i in idxs)
                    for fa, idxs in current[key]
                )
            else:
                last = list(ver_timeline[key].values())[-1] if ver_timeline[key] else None
                if last and last != 'X':
                    ver_timeline[key][label] = 'X'
                    pos_timeline[key][label] = 'X'

    def build_df(timeline):
        rows = []
        for (bare_name, job), release_vals in sorted(timeline.items()):
            if not is_allowed(bare_name, allowed) or not release_vals: continue
            rows.append({'action': bare_name, 'job': job, **release_vals})
        if not rows: return pd.DataFrame()
        df      = pd.DataFrame(rows)
        rel_cols = [l for l in release_labels if l in df.columns]
        return df[['action', 'job'] + rel_cols]

    stem = file.name.replace('.csv', '')
    if ver_timeline:
        build_df(ver_timeline).to_csv(out_dir / f"{stem}_action_versions.csv",  index=False)
        build_df(pos_timeline).to_csv(out_dir / f"{stem}_action_positions.csv", index=False)

    return {'project': project_name, 'file': file.name,
            'additions': counts['addition'], 'removals': counts['removal'],
            'updates': counts['update'], 'transitions': counts['transition']}

def export_all(projects, chunk_idx=1):
    log.info(f"Exporting per-project CSVs for {len(projects)} projects")
    metadata_rows, totals = [], Counter()
    for i, (proj_name, project) in enumerate(sorted(projects.items())):
        try:
            for file in project.files:
                try:
                    meta = export_file_csv(proj_name, file, allowed)
                    # metadata_rows.append(meta)
                    for k in ('additions', 'removals', 'updates', 'transitions'):
                        totals[k] += meta[k]
                except Exception as e:
                    log.error(f"  [{proj_name}] Failed export '{file.name}': {e}")
            if (i + 1) % 500 == 0 or i == 0 or i == len(projects) - 1:
                log.info(f"  [{i+1}/{len(projects)}] {proj_name}")
        except Exception as e:
            log.error(f"  [{proj_name}] Failed: {e}")

    # total_sum = sum(totals.values()) or 1
    # metadata_rows.append({'project': 'ALL', 'file': 'ALL', **dict(totals)})
    # metadata_rows.append({'project': 'ALL_PERCENT', 'file': 'ALL',
    #     **{k: round(totals[k] / total_sum * 100, 2) for k in totals}})
    # pd.DataFrame(metadata_rows).to_csv(
    #     Path(RESULT_PATH) / f'metadata_{INDEX}_{chunk_idx}.csv', index=False)
    log.info(f"export_all done | totals: {dict(totals)}")


# ── Transition event emission ─────────────────────────────────────────────────
def emit_transition_events(projects, marketplace_map):
    """
    Generate transition events directly from Version objects.
    No CSV read-back needed.
    Columns:
      project, workflow, action, job, event, date, release_col,
      old_version, new_version, direction, steps, step_count,
      pinning_change, is_marketplace
    """
    log.info(f"emit_transition_events — {len(projects)} projects")
    events = []
    total  = len(projects)

    for i, (proj_name, project) in enumerate(sorted(projects.items())):
        if (i + 1) % 500 == 0 or i == 0 or i == total - 1:
            log.info(f"  [{i+1}/{total}] {proj_name} | events_so_far={len(events)}")

        for file in project.files:
            stem     = file.name.replace('.csv', '')
            versions = file.versions[1:]

            for v_idx, version in enumerate(versions):
                if not version.committed_date: continue

                date_str = fmt_date(version.committed_date)
                label    = f"r{v_idx+1} ({date_str})"

                def mk(action, job, evt, replaces = '', old_v='', replaced_by = '', new_v='', steps='', sc=1, direction='', pin=''):
                    return {
                        'project'        : proj_name,
                        'workflow'       : stem,
                        'action'         : action,
                        'job'            : job,
                        'event'          : evt,
                        'date'           : date_str,
                        'release_col'    : label,
                        'replaces': replaces,
                        'old_version'    : old_v,
                        'replaced_by' : replaced_by,
                        'new_version'    : new_v,
                        'direction'      : direction,
                        'steps'          : steps,
                        'step_count'     : sc,
                        'pinning_change' : pin,
                        'is_marketplace' : marketplace_map.get(action, False),
                    }

                # --- Group additions by (bare_action, job, version) ---
                add_groups = defaultdict(list)
                for (full_action, job, step_idx) in version.actions_added:
                    b, v = bare(full_action), vtag(full_action)
                    add_groups[(b, job, v)].append(step_idx)
                for (b, job, v), idxs in add_groups.items():
                    idxs.sort()
                    events.append(mk(b, job, 'addition',
                        new_v    = v,
                        steps    = ';'.join(f"{v}@step{i + 1}" for i in idxs),
                        sc       = len(idxs),
                        pin      = 'pinned' if is_sha(v) else ''))

                # --- Group removals by (bare_action, job, version) ---
                rem_groups = defaultdict(list)
                for (full_action, job, step_idx) in version.actions_removed:
                    b, v = bare(full_action), vtag(full_action)
                    rem_groups[(b, job, v)].append(step_idx)
                for (b, job, v), idxs in rem_groups.items():
                    idxs.sort()
                    events.append(mk(b, job, 'removal',
                        old_v = v, new_v = 'X',
                        steps = ';'.join(f"{v}@step{i + 1}" for i in idxs),
                        sc    = len(idxs)))

                # --- Updates (same bare name, version changed) ---
                for (old_full, new_full, job, step_idx) in version.actions_updated:
                    b        = bare(old_full)
                    ov, nv   = vtag(old_full), vtag(new_full)
                    pin      = pinning_change(ov, nv)
                    direction = '' if (is_sha(ov) or is_sha(nv)) else ver_dir(ov, nv)
                    events.append(mk(b, job, 'update',
                        old_v     = ov, new_v = nv,
                        steps     = f"{ov}@step{step_idx + 1}->{nv}@step{step_idx + 1}",
                        direction = direction,
                        pin       = pin))

                # --- Transitions (different bare name) ---
                for (old_full, new_full, job, step_idx) in version.actions_transitioned:
                    old_b, new_b = bare(old_full), bare(new_full)
                    ov, nv       = vtag(old_full), vtag(new_full)

                    if is_allowed(new_b, allowed):
                        # Both CI/CD → replacement pair
                        events.append(mk(old_b, job, 'replacement_removal',
                            old_v = ov, replaced_by = new_b, new_v = 'X',
                            steps = f"{ov}@step{step_idx + 1}"))
                        events.append(mk(new_b, job, 'replacement_addition',
                            replaces = old_b,
                            new_v = nv,
                            steps = f"{nv}@step{step_idx + 1}",
                            pin   = 'pinned' if is_sha(nv) else ''))
                    else:
                        # New action not CI/CD → removal of old only
                        events.append(mk(old_b, job, 'removal',
                            old_v = ov, new_v = 'X',
                            steps = f"{ov}@step{step_idx + 1}"))

    if not events:
        log.warning("No transition events generated")
        return pd.DataFrame()

    trans_df = pd.DataFrame(events)
    trans_df['date_parsed'] = pd.to_datetime(trans_df['date'], dayfirst=False, errors='coerce')
    trans_df = (trans_df
        .sort_values(['project', 'workflow', 'job', 'date_parsed'])
        .drop(columns=['date_parsed'])
        .reset_index(drop=True))

    out_path = f'{ANALYSIS_PATH}/transitions_{INDEX}.csv'
    trans_df.to_csv(out_path, index=False)

    counts = trans_df['event'].value_counts().to_dict()
    log.info(f"transitions_{INDEX}.csv — {len(trans_df)} events")
    log.info(f"  event breakdown: {counts}")
    log.info(f"  pinning events : {(trans_df['pinning_change'] != '').sum()}")
    log.info(f"  marketplace    : {trans_df['is_marketplace'].sum()} events on marketplace actions")

    return trans_df


# ── Main ──────────────────────────────────────────────────────────────────────
try:
    log.info("=== Starting pipeline ===")

    allowed, marketplace_map = load_allowed_actions(CICD_ACTIONS_FILE)

    all_projects = filter_projects(PROJECT_PATH, INDEX)
    log.info(f"Total projects in partition: {len(all_projects)}")

    CHUNK_SIZE   = 500
    total_chunks = (len(all_projects) + CHUNK_SIZE - 1) // CHUNK_SIZE
    all_trans    = []   # accumulate transition events across chunks

    for chunk_idx, chunk_start in enumerate(range(0, len(all_projects), CHUNK_SIZE)):
        chunk = all_projects[chunk_start:chunk_start + CHUNK_SIZE]
        log.info(f"=== Chunk {chunk_idx+1}/{total_chunks} | "
                 f"projects {chunk_start+1}-{chunk_start+len(chunk)}/{len(all_projects)} ===")

        project_files = get_project_files(PROJECT_PATH, chunk)
        projects      = parse_projects(project_files)
        process_all(projects)

        # Per-project CSVs
        export_all(projects, chunk_idx + 1)

        # Transition events (in-memory, no CSV read-back)
        chunk_trans = emit_transition_events(projects, marketplace_map)
        if not chunk_trans.empty:
            all_trans.append(chunk_trans)

        del project_files, projects
        log.info(f"  Chunk {chunk_idx+1} done — memory freed")

    # ── Merge all chunks into one transitions file ────────────────────────────
    if all_trans:
        trans_df = pd.concat(all_trans, ignore_index=True)
        trans_df['date_parsed'] = pd.to_datetime(trans_df['date'], dayfirst=False, errors='coerce')
        trans_df = (trans_df
            .sort_values(['project', 'workflow', 'job', 'date_parsed'])
            .drop(columns=['date_parsed'])
            .reset_index(drop=True))
        out_path = f'{ANALYSIS_PATH}/final_transitions.csv'
        trans_df.to_csv(out_path, index=False)
        log.info(f"Final transitions_{INDEX}.csv — {len(trans_df)} events across all chunks")

    log.info("=== Pipeline finished successfully ===")

except Exception as e:
    log.error(f"Pipeline failed: {e}")
    log.error(traceback.format_exc())
    sys.exit(1)
