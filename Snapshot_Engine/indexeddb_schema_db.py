# Snapshot_Engine/indexeddb_schema_db.py
"""
Run-level IndexedDB schema tracking DB.
- DB path: run_dir / "schema_tracking_{run_id}.db" (run directory; filename includes run_id)
- Each snapshot: append schema from web_state_indexeddb_schema.json (and optionally
  ccl_chromium_reader from LevelDB in zip); compute diff from previous snapshot.
- IndexedDB schema is appended per snapshot; cache dump is typically appended near run end.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import sqlite3
import tempfile
import zipfile
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Optional: ccl_chromium_reader for LevelDB-backed IndexedDB interpretation and cache
try:
    from ccl_chromium_reader import ccl_chromium_indexeddb
    HAS_CCL = True
except ImportError:
    HAS_CCL = False
try:
    from ccl_chromium_reader import ccl_chromium_cache
    HAS_CCL_CACHE = True
except ImportError:
    HAS_CCL_CACHE = False

DB_FILENAME = "schema_tracking.db"
OLD_DB_FILENAME = "indexeddb_schema_tracking.db"

SCHEMA_VERSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    trigger_type TEXT,
    action_name TEXT,
    captured_at TEXT,
    zip_path TEXT NOT NULL,
    schema_json TEXT NOT NULL,
    source TEXT DEFAULT 'web_state',
    UNIQUE(run_id, snapshot_id)
);
"""

SCHEMA_DIFFS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_diffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    left_version_id INTEGER NOT NULL,
    right_version_id INTEGER NOT NULL,
    diff_summary TEXT NOT NULL,
    FOREIGN KEY (left_version_id) REFERENCES schema_versions(id),
    FOREIGN KEY (right_version_id) REFERENCES schema_versions(id)
);
"""

# Full schema structure: one row per (version, db_name, object_store) so we can observe what exists at each snapshot
SCHEMA_FULL_TABLE = """
CREATE TABLE IF NOT EXISTS schema_full (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    snapshot_id TEXT NOT NULL,
    db_name TEXT NOT NULL,
    object_store_name TEXT NOT NULL,
    origin TEXT DEFAULT '',
    FOREIGN KEY (version_id) REFERENCES schema_versions(id)
);
CREATE INDEX IF NOT EXISTS idx_schema_full_version ON schema_full(version_id);
CREATE INDEX IF NOT EXISTS idx_schema_full_snapshot ON schema_full(snapshot_id);
"""

# Cache dump (ccl Chromium cache): per-snapshot cache entries for channel/chat observation
CACHE_DUMP_TABLE = """
CREATE TABLE IF NOT EXISTS cache_dump (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    url TEXT,
    key TEXT,
    entry_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_cache_dump_snapshot ON cache_dump(snapshot_id);
"""

# Discord channel schema tracking: store observed message field sets per snapshot/action.
DISCORD_CHANNEL_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS discord_channel_schema (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    version_id INTEGER,
    channel_id TEXT NOT NULL,
    schema_json TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    action_name TEXT,
    FOREIGN KEY (version_id) REFERENCES schema_versions(id)
);
CREATE INDEX IF NOT EXISTS idx_discord_channel_schema_snapshot ON discord_channel_schema(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_discord_channel_schema_version ON discord_channel_schema(version_id);
"""

# Field-level object store schema: key_path, indexes, and value-derived field sets (per snapshot/version).
OBJECT_STORE_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS object_store_schema (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    snapshot_id TEXT NOT NULL,
    db_name TEXT NOT NULL,
    object_store_name TEXT NOT NULL,
    origin TEXT DEFAULT '',
    key_path_json TEXT,
    auto_increment INTEGER,
    index_names_json TEXT,
    value_fields_json TEXT NOT NULL,
    sample_count INTEGER DEFAULT 0,
    FOREIGN KEY (version_id) REFERENCES schema_versions(id)
);
CREATE INDEX IF NOT EXISTS idx_object_store_schema_version ON object_store_schema(version_id);
CREATE INDEX IF NOT EXISTS idx_object_store_schema_snapshot ON object_store_schema(snapshot_id);
"""

# IndexedDB value sampling: store a small sample of key/value pairs per snapshot/DB/object store.
IDB_VALUE_SAMPLE_BATCH_TABLE = """
CREATE TABLE IF NOT EXISTS idb_value_sample_batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    db_name TEXT NOT NULL,
    origin TEXT,
    object_store_name TEXT NOT NULL,
    sample_strategy TEXT NOT NULL,
    limit_per_store INTEGER NOT NULL,
    collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idb_val_batch_run_snap
    ON idb_value_sample_batch(run_id, snapshot_id, db_name, object_store_name);
"""

IDB_VALUE_SAMPLES_TABLE = """
CREATE TABLE IF NOT EXISTS idb_value_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES idb_value_sample_batch(id) ON DELETE CASCADE,
    key_json TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    value_json TEXT,
    value_hash TEXT,
    is_partial INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_idb_val_samples_batch_key
    ON idb_value_samples(batch_id, key_hash);
"""

# Optional table for caching value diffs (currently computed on demand).
IDB_VALUE_DIFFS_TABLE = """
CREATE TABLE IF NOT EXISTS idb_value_diffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    left_snapshot_id TEXT NOT NULL,
    right_snapshot_id TEXT NOT NULL,
    db_name TEXT NOT NULL,
    origin TEXT,
    object_store_name TEXT NOT NULL,
    key_json TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    change_type TEXT NOT NULL,  -- 'added', 'removed', 'modified'
    left_value_json TEXT,
    right_value_json TEXT,
    left_value_hash TEXT,
    right_value_hash TEXT,
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idb_val_diffs_run_snap_store
    ON idb_value_diffs(run_id, left_snapshot_id, right_snapshot_id, db_name, object_store_name);
"""


# Convenience views (run_id immediately visible without manual joins)
VIEWS_SQL = """
CREATE VIEW IF NOT EXISTS v_schema_versions AS
SELECT
  id AS version_id,
  run_id,
  snapshot_id,
  agent_id,
  trigger_type,
  action_name,
  captured_at,
  zip_path,
  source
FROM schema_versions;

CREATE VIEW IF NOT EXISTS v_schema_full AS
SELECT
  sv.run_id,
  sv.snapshot_id,
  sv.agent_id,
  sv.trigger_type,
  sv.action_name,
  sv.captured_at,
  sv.source,
  sf.version_id,
  sf.db_name,
  sf.object_store_name,
  sf.origin
FROM schema_full sf
JOIN schema_versions sv ON sv.id = sf.version_id;

CREATE VIEW IF NOT EXISTS v_object_store_schema AS
SELECT
  sv.run_id,
  sv.snapshot_id,
  sv.agent_id,
  sv.trigger_type,
  sv.action_name,
  sv.captured_at,
  sv.source,
  os.version_id,
  os.db_name,
  os.object_store_name,
  os.origin,
  os.key_path_json,
  os.auto_increment,
  os.index_names_json,
  os.value_fields_json,
  os.sample_count
FROM object_store_schema os
JOIN schema_versions sv ON sv.id = os.version_id;

CREATE VIEW IF NOT EXISTS v_idb_value_sample_batch AS
SELECT
  id AS batch_id,
  run_id,
  snapshot_id,
  db_name,
  origin,
  object_store_name,
  sample_strategy,
  limit_per_store,
  collected_at
FROM idb_value_sample_batch;

CREATE VIEW IF NOT EXISTS v_idb_value_samples AS
SELECT
  b.run_id,
  b.snapshot_id,
  b.db_name,
  b.origin,
  b.object_store_name,
  s.batch_id,
  s.key_json,
  s.key_hash,
  s.value_json,
  s.value_hash,
  s.is_partial
FROM idb_value_samples s
JOIN idb_value_sample_batch b ON b.id = s.batch_id;

CREATE VIEW IF NOT EXISTS v_cache_dump AS
SELECT
  run_id,
  snapshot_id,
  url,
  key,
  entry_json
FROM cache_dump;

CREATE VIEW IF NOT EXISTS v_discord_channel_schema AS
SELECT
  run_id,
  snapshot_id,
  version_id,
  channel_id,
  schema_json,
  message_count,
  action_name
FROM discord_channel_schema;
"""


def get_db_path(run_dir: Path) -> Path:
    """Return schema_tracking_{run_id}.db path under run_dir.

    Filename includes run_id: runs/run_{run_id}/schema_tracking_{run_id}.db
    """
    run_dir = Path(run_dir)
    # Extract run_id from directory name (run_{run_id} -> run_id)
    run_id = run_dir.name.replace("run_", "") if run_dir.name.startswith("run_") else run_dir.name
    db_filename = f"schema_tracking_{run_id}.db"
    new_path = run_dir / db_filename
    return new_path


def get_cumulative_db_path(run_dir: Path) -> Path:
    """Return cumulative schema_tracking.db path at runs/ level.

    Cumulative DB across all runs: runs/schema_tracking.db
    """
    run_dir = Path(run_dir)
    # If run_dir is runs/run_{run_id}, go up to runs/
    if run_dir.name.startswith("run_"):
        runs_dir = run_dir.parent
    else:
        # If run_dir is already runs/, keep it
        runs_dir = run_dir
    cumulative_db_path = runs_dir / "schema_tracking.db"
    return cumulative_db_path


def init_db(db_path: Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(SCHEMA_VERSIONS_TABLE)
        conn.executescript(SCHEMA_DIFFS_TABLE)
        conn.executescript(SCHEMA_FULL_TABLE)
        conn.executescript(CACHE_DUMP_TABLE)
        conn.executescript(DISCORD_CHANNEL_SCHEMA_TABLE)
        conn.executescript(OBJECT_STORE_SCHEMA_TABLE)
        conn.executescript(IDB_VALUE_SAMPLE_BATCH_TABLE)
        conn.executescript(IDB_VALUE_SAMPLES_TABLE)
        conn.executescript(IDB_VALUE_DIFFS_TABLE)
        conn.executescript(VIEWS_SQL)


# Artifact key used in collection_plan so Cache is included in snapshot zip; dump looks for this in zip.
CHROMIUM_CACHE_ARTIFACT_PREFIX = "artifacts/browser_artifacts/chromium.cache.cache_data/"
# Service Worker CacheStorage (may contain Discord messages)
CACHE_STORAGE_SUBPATHS = ("Service Worker/CacheStorage", "chromium.storage.cachestorage")
# Discord message JSON pattern (see analyze_CacheStorage.py)
DISCORD_MSG_PATTERN = re.compile(r'{"id":"\d+","type":\d+,"content":".+?","channel_id":"\d+"')

# CCL script path via PRZMA_CCL_CHROMIUM_CACHE (optionally loaded from .env)
def _get_ccl_cache_script_path() -> Optional[str]:
    path = os.environ.get("PRZMA_CCL_CHROMIUM_CACHE") or None
    if path:
        return path
    try:
        from pathlib import Path as _P
        from dotenv import load_dotenv
        for _d in [_P(__file__).resolve().parents[1], _P(__file__).resolve().parents[2]]:
            _e = _d / ".env"
            if _e.exists():
                load_dotenv(_e)
                path = os.environ.get("PRZMA_CCL_CHROMIUM_CACHE") or None
                if path:
                    return path
    except Exception:
        pass
    return None


def _is_discord_message_json(data: Any) -> bool:
    """Return True if payload looks like a Discord message list (CacheStorage single message allowed)."""
    if not isinstance(data, list) or len(data) == 0:
        return False
    sample = data[0]
    if not isinstance(sample, dict):
        return False
    # Full message payload: type, timestamp, id, channel_id, author
    if all(k in sample for k in ("type", "timestamp", "id", "channel_id", "author")):
        return True
    # Regex-extracted CacheStorage single message: accept minimal fields for schema tracking.
    return "channel_id" in sample and ("id" in sample or "type" in sample)


def _normalize_discord_json(data: Any) -> Optional[List[Dict[str, Any]]]:
    """Normalize list[str] -> list[dict]; keep list[dict] as-is."""
    if not isinstance(data, list) or len(data) == 0:
        return None
    if isinstance(data[0], dict):
        return data
    if isinstance(data[0], str):
        out = []
        for item in data:
            try:
                out.append(json.loads(item))
            except Exception:
                return None
        return out
    return None


def _discord_schema_keys(msgs: List[Dict[str, Any]]) -> List[str]:
    """Return sorted set of all keys seen across messages (schema tracking)."""
    keys: set = set()
    for m in msgs:
        if isinstance(m, dict):
            keys.update(m.keys())
    return sorted(keys)


def _extract_discord_from_cache_storage_zip(zip_path: Path) -> List[Dict[str, Any]]:
    """Extract Discord message patterns from Service Worker/CacheStorage files inside zip."""
    entries: List[Dict[str, Any]] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                lower = name.lower()
                if "cachestorage" not in lower and "cache storage" not in lower:
                    continue
                if "service worker" not in lower and "chromium.storage.cachestorage" not in lower:
                    continue
                try:
                    raw = zf.read(name).decode("utf-8", errors="ignore")
                except Exception:
                    continue
                for match in DISCORD_MSG_PATTERN.finditer(raw):
                    try:
                        # Pattern may be incomplete; try adding a closing brace.
                        s = match.group(0) + "}"
                        obj = json.loads(s)
                        if isinstance(obj, dict) and obj.get("channel_id"):
                            entries.append({
                                "url": "cachestorage",
                                "key": name,
                                "entry_json": json.dumps([obj], ensure_ascii=False),
                            })
                    except Exception:
                        continue
    except Exception:
        pass
    return entries


def _parse_discord_from_cache_files(extract_base: Path) -> List[Dict[str, Any]]:
    """Fallback: scan extracted Cache_Data f_* files for Discord message JSON when CCL fails."""
    entries: List[Dict[str, Any]] = []
    extract_base = Path(extract_base)
    if not extract_base.is_dir():
        return entries
    import gzip
    import zlib
    for path in sorted(extract_base.iterdir()):
        if path.is_dir() or path.name.startswith("index"):
            continue
        try:
            raw = path.read_bytes()
        except Exception:
            continue
        if len(raw) < 20:
            continue
        for _ in range(2):
            try:
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                elif raw[:1] in (b"\x78", b"\x9c", b"\xda"):
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                else:
                    break
            except Exception:
                break
        try:
            text = raw.decode("utf-8", errors="ignore")
        except Exception:
            continue
        text = text.strip()
        if not text.startswith(("[", "{")):
            # Chromium Simple Cache is a binary format; search for embedded JSON text.
            for match in DISCORD_MSG_PATTERN.finditer(text):
                try:
                    start = match.start()
                    brace = text.rfind("[", 0, start)
                    if brace == -1:
                        brace = text.rfind("{", 0, start)
                    if brace == -1:
                        continue
                    end = text.find("]", brace + 1)
                    if end == -1:
                        end = text.find("}", brace + 1)
                    if end == -1:
                        continue
                    chunk = text[brace : end + 1]
                    data = json.loads(chunk)
                    if isinstance(data, list) and data:
                        normalized = _normalize_discord_json(data)
                        if normalized and _is_discord_message_json(normalized):
                            entries.append({"url": "cache_fallback", "key": path.name, "entry_json": json.dumps(normalized, ensure_ascii=False)})
                            break
                    elif isinstance(data, dict) and data.get("channel_id"):
                        normalized = _normalize_discord_json([data])
                        if normalized and _is_discord_message_json(normalized):
                            entries.append({"url": "cache_fallback", "key": path.name, "entry_json": json.dumps(normalized, ensure_ascii=False)})
                            break
                except Exception:
                    continue
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        if isinstance(data, list) and data and isinstance(data[0], dict):
            normalized = _normalize_discord_json(data)
            if normalized and _is_discord_message_json(normalized):
                entries.append({"url": "cache_fallback", "key": path.name, "entry_json": json.dumps(normalized, ensure_ascii=False)})
        elif isinstance(data, dict):
            for key in ("messages", "message_list", "body"):
                arr = data.get(key)
                if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                    normalized = _normalize_discord_json(arr)
                    if normalized and _is_discord_message_json(normalized):
                        entries.append({"url": "cache_fallback", "key": path.name, "entry_json": json.dumps(normalized, ensure_ascii=False)})
                        break
    return entries


def _read_json_from_path(path: Path) -> Optional[Any]:
    """Read a file and parse as JSON, with gzip/zlib fallback."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if text.strip().startswith(("[", "{")):
            return json.loads(text)
    except Exception:
        pass
    try:
        raw = path.read_bytes()
        if len(raw) < 4:
            return None
        if raw[:2] == b"\x1f\x8b":
            import gzip
            raw = gzip.decompress(raw)
        elif raw[:1] in (b"\x78", b"\x9c", b"\xda"):
            import zlib
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        text = raw.decode("utf-8", errors="ignore")
        if text.strip().startswith(("[", "{")):
            return json.loads(text)
    except Exception:
        pass
    return None


def _run_ccl_cache_subprocess(extract_base: Path, out_dir: Path) -> List[Dict[str, Any]]:
    """Run external ccl_chromium_cache.py and parse cache_report.csv + cache_files/; filter Discord message payloads."""
    script = _get_ccl_cache_script_path()
    if not script or not Path(script).exists():
        return []
    out_dir = Path(out_dir)
    entries: List[Dict[str, Any]] = []
    try:
        subprocess.run(
            [sys.executable, script, str(extract_base), str(out_dir)],
            check=True,
            timeout=120,
            capture_output=True,
        )
    except Exception:
        return []
    cache_files_dir = out_dir / "cache_files"
    if not cache_files_dir.is_dir():
        return entries

    # Scan all JSON files in cache_files/ and keep only Discord message payloads.
    def _to_discord_list(data: Any) -> Optional[List[Dict[str, Any]]]:
        if isinstance(data, list):
            return _normalize_discord_json(data)
        if isinstance(data, dict):
            for k in ("messages", "message_list", "body"):
                arr = data.get(k)
                if isinstance(arr, list) and arr:
                    out = _normalize_discord_json(arr)
                    if out and _is_discord_message_json(out):
                        return out
        return None

    for path in cache_files_dir.glob("*.json"):
        try:
            data = _read_json_from_path(path)
            if data is None:
                continue
            normalized = _to_discord_list(data)
            if not normalized or not _is_discord_message_json(normalized):
                continue
            key = path.stem
            entries.append({
                "url": "ccl_cache_files",
                "key": key,
                "entry_json": json.dumps(normalized, ensure_ascii=False),
            })
        except Exception:
            continue

    # Also collect entries mapped via cache_report.csv (may overlap with JSON scan).
    csv_path = out_dir / "cache_report.csv"
    if csv_path.exists():
        try:
            import csv as _csv
            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in _csv.DictReader(f):
                    key = row.get("key") or ""
                    file_hash = row.get("file_hash") or ""
                    if not file_hash or file_hash == "<No cache file data>":
                        continue
                    for cpath in cache_files_dir.glob(file_hash + "*"):
                        data = _read_json_from_path(cpath)
                        normalized = _to_discord_list(data) if data is not None else None
                        if normalized and _is_discord_message_json(normalized):
                            entries.append({"url": key, "key": key, "entry_json": json.dumps(normalized, ensure_ascii=False)})
                        break
        except Exception:
            pass
    return entries


def _decode_discord_from_entries(
    conn: sqlite3.Connection,
    run_id: str,
    snapshot_id: str,
    version_id: Optional[int],
    entries: List[Dict[str, Any]],
    action_name: str = "",
) -> None:
    """From cache_dump entries, decode Discord message JSON and store per-channel schema."""
    for e in entries:
        ej = e.get("entry_json")
        if not ej:
            continue
        try:
            data = json.loads(ej)
        except Exception:
            continue
        data = _normalize_discord_json(data)
        if not data or not _is_discord_message_json(data):
            continue
        channel_id = str(data[0].get("channel_id", ""))
        if not channel_id:
            continue
        schema_keys = _discord_schema_keys(data)
        schema_json = json.dumps(schema_keys, ensure_ascii=False)
        conn.execute(
            """INSERT INTO discord_channel_schema (run_id, snapshot_id, version_id, channel_id, schema_json, message_count, action_name)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, snapshot_id, version_id, channel_id, schema_json, len(data), action_name or ""),
        )


def _run_cache_dump_if_available(
    run_dir: Path,
    run_id: str,
    snapshot_id: str,
    zip_path: Path,
    db_path: Path,
    version_id: Optional[int] = None,
    action_name: str = "",
) -> None:
    """Dump Chromium cache/CacheStorage, store JSON in DB, and extract Discord channel schema when possible."""
    run_dir = Path(run_dir)
    entries: List[Dict[str, Any]] = []

    # 1) Extract Discord messages from CacheStorage (zip-only)
    entries.extend(_extract_discord_from_cache_storage_zip(zip_path))

    # 2) Dump chromium.cache.cache_data via in-process CCL or external script
    # Zip member names may use backslash on Windows; normalize to / for prefix matching.
    def _norm(name: str) -> str:
        return name.replace("\\", "/")

    cache_prefix = None
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            for name in names:
                n = _norm(name)
                if n.startswith(CHROMIUM_CACHE_ARTIFACT_PREFIX) and len(n) > len(CHROMIUM_CACHE_ARTIFACT_PREFIX):
                    cache_prefix = CHROMIUM_CACHE_ARTIFACT_PREFIX
                    break
            if not cache_prefix:
                for name in names:
                    n = _norm(name)
                    if "/Cache/" in n and ("browser_artifacts" in n or "chromium" in n.lower()):
                        parts = n.split("/")
                        for i, p in enumerate(parts):
                            if p == "Cache" and i > 0:
                                cache_prefix = "/".join(parts[: i + 1]) + ("/" if not n.endswith("/") else "")
                                break
                        if cache_prefix:
                            break
    except Exception:
        cache_prefix = None

    if cache_prefix:
        try:
            with tempfile.TemporaryDirectory(prefix="przma_cache_") as tmp:
                extract_base = Path(tmp) / "cache"
                extract_base.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for name in zf.namelist():
                        n = _norm(name)
                        if not n.startswith(cache_prefix) or n.endswith("/"):
                            continue
                        rel = n[len(cache_prefix):].lstrip("/").replace("/", os.sep)
                        dest = extract_base / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(zf.read(name))
                # 2a) In-process CCL (requires index file)
                if HAS_CCL_CACHE and hasattr(ccl_chromium_cache, "guess_cache_class"):
                    try:
                        cache_type = ccl_chromium_cache.guess_cache_class(extract_base)
                        if cache_type is not None:
                            with cache_type(extract_base) as cache:
                                for key in getattr(cache, "keys", lambda: [])():
                                    try:
                                        metas = getattr(cache, "get_metadata", lambda k: [None])(key)
                                        datas = getattr(cache, "get_cachefile", lambda k: [None])(key)
                                        url = key
                                        if metas and metas[0] is not None and getattr(metas[0], "url", None):
                                            url = getattr(metas[0], "url", key)
                                        entry = {"url": url, "key": key, "entry_json": ""}
                                        if datas and datas[0]:
                                            import gzip
                                            raw = bytes(datas[0])
                                            for _ in range(2):
                                                try:
                                                    if raw[:2] == b"\x1f\x8b":
                                                        raw = gzip.decompress(raw)
                                                    elif raw[:1] in (b"\x78", b"\x9c"):
                                                        import zlib
                                                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                                                    text = raw.decode("utf-8", errors="ignore")
                                                    if text.strip().startswith(("[", "{")):
                                                        entry["entry_json"] = text
                                                    break
                                                except Exception:
                                                    break
                                        entries.append(entry)
                                    except Exception:
                                        pass
                    except Exception:
                        pass
                # 2b) CCL subprocess (requires index + f_* files; out_dir must not exist)
                if not entries:
                    out_dir = Path(tmp) / "ccl_out"
                    entries.extend(_run_ccl_cache_subprocess(extract_base, out_dir))
                # 2c) Fallback: scan f_* files for Discord message JSON when CCL fails
                if not entries:
                    entries.extend(_parse_discord_from_cache_files(extract_base))
        except Exception:
            pass

    if not entries:
        return
    out_json = run_dir / f"cache_dump_{snapshot_id}.json"
    try:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=0)
    except Exception:
        pass
    init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        for e in entries[:500]:
            conn.execute(
                "INSERT INTO cache_dump (run_id, snapshot_id, url, key, entry_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, snapshot_id, e.get("url") or "", e.get("key") or "", (e.get("entry_json") or "")[: 1024 * 1024]),
            )
        _decode_discord_from_entries(conn, run_id, snapshot_id, version_id, entries, action_name)


def _read_web_state_schema_from_zip(zip_path: Path) -> Optional[List[Dict[str, Any]]]:
    """Read web_state_indexeddb_schema.json from snapshot zip."""
    zip_path = Path(zip_path)
    if not zip_path.exists():
        return None
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if "indexeddb_schema" in name.lower() and name.endswith(".json"):
                    with zf.open(name) as f:
                        data = json.load(f)
                    return data if isinstance(data, list) else [data]
    except Exception:
        pass
    return None


def _decode_idb_key_path_bytes(data: bytes) -> Optional[Any]:
    """Decode Chromium IndexedDB LevelDB IDBKeyPath bytes."""
    if not data:
        return None
    try:
        if len(data) < 3 or data[0] != 0 or data[1] != 0:
            return data.decode("utf-16-be", errors="ignore").strip() or None
        kind = data[2]
        pos = 3
        if kind == 0:
            return None
        if kind == 1:
            # String: varint (length in UTF-16 code units) then utf-16-be
            n = 0
            shift = 0
            while pos < len(data):
                b = data[pos]
                pos += 1
                n |= (b & 0x7F) << shift
                if (b & 0x80) == 0:
                    break
                shift += 7
            if pos + n * 2 <= len(data):
                return data[pos : pos + n * 2].decode("utf-16-be", errors="ignore")
            return None
        if kind == 2:
            # Array: varint count, then each StringWithLength
            cnt = 0
            shift = 0
            while pos < len(data):
                b = data[pos]
                pos += 1
                cnt |= (b & 0x7F) << shift
                if (b & 0x80) == 0:
                    break
                shift += 7
            out = []
            for _ in range(cnt):
                if pos >= len(data):
                    break
                n = 0
                shift = 0
                while pos < len(data):
                    b = data[pos]
                    pos += 1
                    n |= (b & 0x7F) << shift
                    if (b & 0x80) == 0:
                        break
                    shift += 7
                if pos + n * 2 <= len(data):
                    out.append(data[pos : pos + n * 2].decode("utf-16-be", errors="ignore"))
                    pos += n * 2
            return out if out else None
    except Exception:
        pass
    return None


def _normalize_idb_key_for_json(key: Any) -> Any:
    """Normalize an IndexedDB key into a JSON-serializable representation."""
    if isinstance(key, (str, int, float, bool)) or key is None:
        return key
    if isinstance(key, (list, tuple)):
        return [_normalize_idb_key_for_json(x) for x in key]
    if isinstance(key, dict):
        return {str(k): _normalize_idb_key_for_json(v) for k, v in key.items()}
    # Unknown types: stringify
    try:
        return repr(key)
    except Exception:
        return str(key)


def _normalize_idb_value_for_json(value: Any) -> Tuple[Any, bool]:
    """Normalize a value into a JSON-serializable representation.

    Returns: (normalized_value, is_partial). For non-serializable types, store metadata only.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value, False
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        partial = False
        for k, v in value.items():
            nv, p = _normalize_idb_value_for_json(v)
            out[str(k)] = nv
            partial = partial or p
        return out, partial
    if isinstance(value, (list, tuple)):
        out_list: List[Any] = []
        partial = False
        for v in value:
            nv, p = _normalize_idb_value_for_json(v)
            out_list.append(nv)
            partial = partial or p
        return out_list, partial
    # Other types (e.g., Blob): store metadata only
    type_name = type(value).__name__
    meta = {"__type__": type_name}
    size = getattr(value, "size", None)
    if isinstance(size, int):
        meta["size"] = size
    return meta, True


def _schema_from_ccl_leveldb(leveldb_path: Path, blob_path: Optional[Path] = None) -> Optional[List[Dict[str, Any]]]:
    """Use ccl_chromium_reader to get IndexedDB schema (DB names + object stores) from LevelDB dir."""
    if not HAS_CCL:
        return None
    blob_path = blob_path or (leveldb_path.parent / (leveldb_path.name.replace(".leveldb", ".blob")))
    if not blob_path.is_dir():
        blob_path = None
    try:
        wrapper = ccl_chromium_indexeddb.WrappedIndexDB(
            str(leveldb_path),
            str(blob_path) if blob_path else "",
        )
        out: List[Dict[str, Any]] = []
        for db in wrapper.database_ids:
            try:
                db_obj = wrapper[db]
                name = getattr(db_obj, "name", None) or str(db)
                stores = list(getattr(db_obj, "object_store_names", []) or [])
                out.append({"name": name, "objectStores": stores, "source": "ccl_chromium_reader"})
            except Exception:
                continue
        return out if out else None
    except Exception:
        return None


def _schema_detail_from_ccl_leveldb(
    leveldb_path: Path,
    blob_path: Optional[Path],
    origin: str,
    max_records_per_store: int = 50,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Collect field-level schema and lightweight value samples per object store.

    Returns:
        - details: rows for object_store_schema
        - value_samples_meta: [{db_name, object_store_name, origin, samples: [{key_json, value_json, is_partial}]}]
    """
    if not HAS_CCL:
        return [], []
    blob_path = blob_path or (leveldb_path.parent / (leveldb_path.name.replace(".leveldb", ".blob")))
    if not blob_path.is_dir():
        blob_path = None
    details: List[Dict[str, Any]] = []
    value_samples_meta: List[Dict[str, Any]] = []
    try:
        wrapper = ccl_chromium_indexeddb.WrappedIndexDB(
            str(leveldb_path),
            str(blob_path) if blob_path else "",
        )
        ObjectStoreMetadataType = getattr(
            ccl_chromium_indexeddb, "ObjectStoreMetadataType", None
        )
        for db in wrapper.database_ids:
            try:
                db_obj = wrapper[db]
                db_name = getattr(db_obj, "name", None) or str(db)
                db_origin = origin or getattr(db_obj, "origin", "")
                for store in db_obj:
                    store_name = getattr(store, "name", None) or ""
                    if not store_name:
                        continue
                    key_path_json: Optional[str] = None
                    auto_increment: Optional[int] = None
                    index_names_json = "[]"
                    value_fields: set = set()
                    sample_count = 0
                    samples_for_store: List[Dict[str, Any]] = []
                    # KeyPath: ccl_chromium_reader may not expose KeyPath meta; decode raw bytes when available.
                    if ObjectStoreMetadataType is not None:
                        try:
                            raw_db = getattr(wrapper, "_raw_db", None)
                            if raw_db is not None:
                                db_id = getattr(db_obj, "db_number", None)
                                store_id = getattr(store, "object_store_id", None)
                                if db_id is not None and store_id is not None:
                                    meta = getattr(raw_db, "object_store_meta", None)
                                    metas = getattr(meta, "_metas", None) if meta else None
                                    if metas is not None:
                                        record = metas.get((db_id, store_id, ObjectStoreMetadataType.KeyPath))
                                        if record is not None:
                                            raw_val = getattr(record, "value", None)
                                            if raw_val:
                                                kp = _decode_idb_key_path_bytes(bytes(raw_val))
                                                if kp is not None:
                                                    key_path_json = json.dumps(kp, ensure_ascii=False)
                        except Exception:
                            pass
                    try:
                        # Try live_only=True first, then fallback to live_only=False if no records found.
                        # bad_deserializer_data_handler: skip records that CCL can't deserialize (e.g. "version tag" error)
                        # so we can still collect value samples from other records in the same store.
                        def _skip_bad_record(_key, _raw):  # noqa: B008
                            pass
                        records_iterated = 0
                        for rec in store.iterate_records(live_only=True, bad_deserializer_data_handler=_skip_bad_record):
                            if sample_count >= max_records_per_store:
                                break
                            val = getattr(rec, "value", None)
                            if val is None:
                                continue
                            records_iterated += 1
                            sample_count += 1
                            # Collect value field keys for value_fields_json
                            if isinstance(val, dict):
                                value_fields.update(val.keys())
                            elif isinstance(val, (list, tuple)) and val and isinstance(val[0], dict):
                                value_fields.update(val[0].keys())
                            # Store a lightweight key/value sample (JSON-serializable)
                            key_raw = getattr(rec, "key", None)
                            try:
                                key_norm = _normalize_idb_key_for_json(key_raw)
                                key_json = json.dumps(key_norm, ensure_ascii=False, sort_keys=True)
                            except Exception:
                                # Skip record if key cannot be serialized
                                continue
                            val_norm, is_partial = _normalize_idb_value_for_json(val)
                            try:
                                value_json = json.dumps(val_norm, ensure_ascii=False, sort_keys=True)
                            except Exception:
                                value_json = json.dumps({"__error__": "serialization_failed"}, ensure_ascii=False)
                                is_partial = True
                            samples_for_store.append(
                                {
                                    "key_json": key_json,
                                    "value_json": value_json,
                                    "is_partial": 1 if is_partial else 0,
                                }
                            )
                        
                        # If no records found with live_only=True, try live_only=False (includes deleted records)
                        if records_iterated == 0:
                            try:
                                for rec in store.iterate_records(live_only=False, bad_deserializer_data_handler=_skip_bad_record):
                                    if sample_count >= max_records_per_store:
                                        break
                                    val = getattr(rec, "value", None)
                                    if val is None:
                                        continue
                                    records_iterated += 1
                                    sample_count += 1
                                    # Collect value field keys for value_fields_json
                                    if isinstance(val, dict):
                                        value_fields.update(val.keys())
                                    elif isinstance(val, (list, tuple)) and val and isinstance(val[0], dict):
                                        value_fields.update(val[0].keys())
                                    # Store a lightweight key/value sample
                                    key_raw = getattr(rec, "key", None)
                                    try:
                                        key_norm = _normalize_idb_key_for_json(key_raw)
                                        key_json = json.dumps(key_norm, ensure_ascii=False, sort_keys=True)
                                    except Exception:
                                        continue
                                    val_norm, is_partial = _normalize_idb_value_for_json(val)
                                    try:
                                        value_json = json.dumps(val_norm, ensure_ascii=False, sort_keys=True)
                                    except Exception:
                                        value_json = json.dumps({"__error__": "serialization_failed"}, ensure_ascii=False)
                                        is_partial = True
                                    samples_for_store.append(
                                        {
                                            "key_json": key_json,
                                            "value_json": value_json,
                                            "is_partial": 1 if is_partial else 0,
                                        }
                                    )
                            except Exception:
                                pass
                    except Exception as e:
                        # CCL can't deserialize some stores (e.g. Telegram tt-data); skip record iteration, keep schema
                        import sys
                        print("Warning: IndexedDB store %s.%s: could not iterate records (%s)" % (db_name, store_name, e), file=sys.stderr, flush=True)
                        pass
                    details.append({
                        "db_name": db_name,
                        "object_store_name": store_name,
                        "origin": db_origin,
                        "key_path_json": key_path_json,
                        "auto_increment": auto_increment,
                        "index_names_json": index_names_json,
                        "value_fields_json": json.dumps(sorted(value_fields, key=str), ensure_ascii=False),
                        "sample_count": sample_count,
                    })
                    if samples_for_store:
                        value_samples_meta.append(
                            {
                                "db_name": db_name,
                                "object_store_name": store_name,
                                "origin": db_origin,
                                "samples": samples_for_store,
                            }
                        )
            except Exception as e:
                # Skip this DB (e.g. tweb-account-1 if iteration fails); log for debugging
                import sys
                db_id_str = getattr(db, "name", str(db))
                print("Warning: IndexedDB db %s skipped: %s" % (db_id_str, e), file=sys.stderr, flush=True)
                continue
    except Exception:
        pass
    return details, value_samples_meta


def _find_indexeddb_leveldb_in_zip(zip_path: Path) -> List[str]:
    """Return list of zip member prefixes that are .../origin.indexeddb.leveldb/ dirs.
    Sorted so that Discord (discord.com) and Telegram (telegram.org) are prioritized."""
    out: List[str] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if ".indexeddb.leveldb" in name and (name.endswith("/") or "/" in name):
                    prefix = name.split("/")[:-1]
                    prefix_str = "/".join(prefix) + "/" if prefix else ""
                    if prefix_str and prefix_str not in out:
                        out.append(prefix_str)
        # Prioritize Discord and Telegram so they are always parsed when present
        def _key(p: str) -> tuple:
            if "discord.com" in p.lower():
                return (0, p)  # Discord first
            elif "telegram.org" in p.lower():
                return (1, p)  # Telegram second
            else:
                return (2, p)  # Others last
        out.sort(key=_key)
    except Exception:
        pass
    return out


def _merge_schema_lists(web_state: Optional[List[Dict[str, Any]]], ccl: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Merge web_state schema with ccl-derived schema; prefer ccl for same DB name, add DBs from both."""
    by_name: Dict[str, Dict[str, Any]] = {}
    for item in (ccl or []):
        if isinstance(item, dict) and item.get("name"):
            by_name[item["name"]] = {**item, "source": item.get("source", "ccl_chromium_reader")}
    for item in (web_state or []):
        if isinstance(item, dict) and item.get("name"):
            if item["name"] not in by_name:
                by_name[item["name"]] = {**item, "source": "web_state"}
    return list(by_name.values())


def _schema_summary(schema_list: Optional[List[Dict[str, Any]]]) -> Dict[str, List[str]]:
    """DB name -> sorted object store names."""
    if not schema_list:
        return {}
    out: Dict[str, List[str]] = {}
    for db in schema_list:
        if not isinstance(db, dict):
            continue
        name = db.get("name") or "?"
        stores = db.get("objectStores") or []
        out[name] = sorted(stores) if isinstance(stores, list) else list(stores)
    return out


def _compute_diff(left_schema: Optional[List[Dict[str, Any]]], right_schema: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    left_sum = _schema_summary(left_schema)
    right_sum = _schema_summary(right_schema)
    left_dbs = set(left_sum.keys())
    right_dbs = set(right_sum.keys())
    added_dbs = list(right_dbs - left_dbs)
    removed_dbs = list(left_dbs - right_dbs)
    store_diffs: Dict[str, Dict[str, Any]] = {}
    for db in left_dbs & right_dbs:
        l_stores = set(left_sum.get(db, []))
        r_stores = set(right_sum.get(db, []))
        if l_stores != r_stores:
            store_diffs[db] = {
                "added_stores": list(r_stores - l_stores),
                "removed_stores": list(l_stores - r_stores),
            }
    return {
        "added_databases": added_dbs,
        "removed_databases": removed_dbs,
        "object_store_changes": store_diffs,
    }


def append_snapshot(
    run_dir: Path,
    snapshot_id: str,
    zip_path: Path,
    agent_id: str,
    trigger_dict: Optional[Dict[str, Any]] = None,
    manifest: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Append one snapshot's IndexedDB schema to the run-level DB.
    - Reads web_state_indexeddb_schema.json from zip.
    - If ccl_chromium_reader is available and zip contains chromium IndexedDB LevelDB dirs, parses them and merges.
    - Inserts into schema_versions; computes diff from previous row and inserts into schema_diffs.
    Returns True if at least one schema source was found and appended.
    """
    run_dir = Path(run_dir)
    zip_path = Path(zip_path)
    if not zip_path.exists():
        return False

    web_state_schema = _read_web_state_schema_from_zip(zip_path)
    ccl_schema: Optional[List[Dict[str, Any]]] = None
    object_store_details: List[Dict[str, Any]] = []
    value_samples_meta_all: List[Dict[str, Any]] = []

    if HAS_CCL:
        leveldb_prefixes = _find_indexeddb_leveldb_in_zip(zip_path)  # Discord first, Telegram second, then others
        _max_origins = 10  # parse Discord + Telegram + up to 8 others so Discord and Telegram are never skipped
        for prefix in leveldb_prefixes[:_max_origins]:
            try:
                with tempfile.TemporaryDirectory(prefix="przma_idb_") as tmp:
                    extract_base = Path(tmp) / "leveldb"
                    extract_base.mkdir(parents=True, exist_ok=True)
                    # Blob dir: CCL needs it to decode values stored in .blob (e.g. Telegram)
                    blob_prefix = prefix.replace(".indexeddb.leveldb", ".indexeddb.blob")
                    extract_blob_base = Path(tmp) / "leveldb.blob"
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        for name in zf.namelist():
                            if name.endswith("/"):
                                continue
                            if name.startswith(prefix):
                                rel = name[len(prefix):].lstrip("/").replace("/", os.sep)
                                dest = extract_base / rel
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                dest.write_bytes(zf.read(name))
                            elif blob_prefix and name.startswith(blob_prefix):
                                rel = name[len(blob_prefix):].lstrip("/").replace("/", os.sep)
                                dest = extract_blob_base / rel
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                dest.write_bytes(zf.read(name))
                    if (extract_base / "CURRENT").exists():
                        leveldb_dir = extract_base
                    else:
                        leveldb_dir = None
                        for p in extract_base.iterdir():
                            if p.is_dir() and (p / "CURRENT").exists():
                                leveldb_dir = p
                                break
                    if leveldb_dir and (leveldb_dir / "CURRENT").exists():
                        origin_tag = ""
                        if "/" in prefix:
                            part = prefix.rstrip("/").split("/")[-1]
                            if ".indexeddb.leveldb" in part:
                                origin_tag = part.replace(".indexeddb.leveldb", "").replace("_0", "").replace("_", ".")
                        # Blob dir (from zip or sibling); required for blob-backed values, e.g. Telegram
                        blob_dir = leveldb_dir.parent / (leveldb_dir.name.replace(".leveldb", ".blob"))
                        if not blob_dir.is_dir():
                            blob_dir = None
                        parsed = _schema_from_ccl_leveldb(leveldb_dir, blob_dir)
                        if parsed:
                            for item in parsed:
                                if isinstance(item, dict) and origin_tag:
                                    item["origin"] = origin_tag
                            ccl_schema = (ccl_schema or []) + parsed
                        try:
                            details, value_samples_meta = _schema_detail_from_ccl_leveldb(
                                leveldb_dir, blob_dir, origin_tag, max_records_per_store=50
                            )
                            object_store_details.extend(details)
                            value_samples_meta_all.extend(value_samples_meta)
                            # Debug: log if we got samples for Telegram dialogs/messages
                            if "telegram" in origin_tag.lower():
                                for meta in value_samples_meta:
                                    if meta.get("object_store_name") in ("dialogs", "messages"):
                                        sample_count = len(meta.get("samples", []))
                                        if sample_count > 0:
                                            import sys
                                            print(f"DEBUG: Found {sample_count} samples for {meta.get('db_name')}.{meta.get('object_store_name')} from {origin_tag}", file=sys.stderr)
                        except Exception as e:
                            import sys
                            import traceback
                            print(f"Warning: Failed to get schema details for {origin_tag}: {e}\n{traceback.format_exc()}", file=sys.stderr)
                            # Continue with other origins even if this one fails
            except Exception:
                continue

    schema_list = _merge_schema_lists(web_state_schema, ccl_schema)
    if not schema_list and web_state_schema:
        schema_list = web_state_schema

    run_id = run_dir.name.replace("run_", "") if run_dir.name.startswith("run_") else run_dir.name
    trigger_type = (trigger_dict or {}).get("type") or (trigger_dict or {}).get("trigger_type") or ""
    action_name = (trigger_dict or {}).get("action_name") or (trigger_dict or {}).get("name") or ""
    captured_at = (manifest or {}).get("created_at") or ""

    # DB + row: create whenever we have a snapshot zip so get_versions() and cache dump work even when schema is empty
    source = "merged" if (web_state_schema and ccl_schema) else ("ccl_chromium_reader" if ccl_schema else "web_state")
    schema_json = json.dumps(schema_list, ensure_ascii=False) if schema_list else "[]"
    if not schema_list:
        source = "empty"

    db_path = get_db_path(run_dir)
    init_db(db_path)
    cumulative_db_path = get_cumulative_db_path(run_dir)
    init_db(cumulative_db_path)

    # Helper function to insert data into a DB connection
    def _insert_snapshot_data(conn: sqlite3.Connection, is_cumulative: bool = False) -> Optional[int]:
        """Insert snapshot data into DB. Returns new_id if successful."""
        conn.execute(
            """
            INSERT OR REPLACE INTO schema_versions
            (run_id, snapshot_id, agent_id, trigger_type, action_name, captured_at, zip_path, schema_json, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, snapshot_id, agent_id or "", trigger_type, action_name, captured_at, str(zip_path.resolve()), schema_json, source),
        )
        row = conn.execute(
            "SELECT id FROM schema_versions WHERE run_id = ? AND snapshot_id = ?",
            (run_id, snapshot_id),
        ).fetchone()
        new_id = row[0] if row else None
        if not new_id:
            return None
        
        # Compute diff only for run-level DB (not cumulative)
        if not is_cumulative:
            prev_row = conn.execute(
                "SELECT id, schema_json FROM schema_versions WHERE run_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
                (run_id, new_id),
            ).fetchone()
            if prev_row:
                prev_id, prev_json = prev_row[0], prev_row[1]
                try:
                    prev_schema = json.loads(prev_json)
                except Exception:
                    prev_schema = None
                diff = _compute_diff(prev_schema, schema_list)
                conn.execute(
                    "INSERT INTO schema_diffs (left_version_id, right_version_id, diff_summary) VALUES (?, ?, ?)",
                    (prev_id, new_id, json.dumps(diff, ensure_ascii=False)),
                )
        
        # Store full schema structure
        for db_item in schema_list:
            if not isinstance(db_item, dict):
                continue
            db_name = (db_item.get("name") or "").strip() or "?"
            origin = (db_item.get("origin") or "").strip()
            for store_name in db_item.get("objectStores") or []:
                store_name = (store_name or "?").strip() if isinstance(store_name, str) else "?"
                conn.execute(
                    "INSERT INTO schema_full (version_id, snapshot_id, db_name, object_store_name, origin) VALUES (?, ?, ?, ?, ?)",
                    (new_id, snapshot_id, db_name, store_name, origin),
                )
        
        # Field-level schema: object_store_schema
        for d in object_store_details:
            conn.execute(
                """INSERT INTO object_store_schema
                   (version_id, snapshot_id, db_name, object_store_name, origin, key_path_json, auto_increment, index_names_json, value_fields_json, sample_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id,
                    snapshot_id,
                    d.get("db_name") or "?",
                    d.get("object_store_name") or "?",
                    d.get("origin") or "",
                    d.get("key_path_json"),
                    d.get("auto_increment"),
                    d.get("index_names_json") or "[]",
                    d.get("value_fields_json") or "[]",
                    d.get("sample_count") or 0,
                ),
            )
        
        # Value sampling: idb_value_sample_batch / idb_value_samples
        max_values_per_store = 100
        now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        for meta in value_samples_meta_all:
            samples = meta.get("samples") or []
            if not samples:
                continue
            db_name = (meta.get("db_name") or "?").strip() or "?"
            store_name = (meta.get("object_store_name") or "?").strip() or "?"
            origin = (meta.get("origin") or "").strip()
            limit = min(len(samples), max_values_per_store)
            sample_strategy = "first_n"
            cur = conn.execute(
                """INSERT INTO idb_value_sample_batch
                   (run_id, snapshot_id, db_name, origin, object_store_name, sample_strategy, limit_per_store, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, snapshot_id, db_name, origin, store_name, sample_strategy, limit, now_iso),
            )
            batch_id = cur.lastrowid
            if not batch_id:
                continue
            for s in samples[:limit]:
                key_json = s.get("key_json")
                if not isinstance(key_json, str):
                    continue
                value_json = s.get("value_json")
                is_partial = 1 if s.get("is_partial") else 0
                try:
                    key_hash = hashlib.sha256(key_json.encode("utf-8")).hexdigest()
                except Exception:
                    continue
                value_hash = None
                if isinstance(value_json, str):
                    try:
                        value_hash = hashlib.sha256(value_json.encode("utf-8")).hexdigest()
                    except Exception:
                        value_hash = None
                conn.execute(
                    """INSERT INTO idb_value_samples
                       (batch_id, key_json, key_hash, value_json, value_hash, is_partial)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (batch_id, key_json, key_hash, value_json, value_hash, is_partial),
                )
        
        return new_id

    # Insert into run-level DB
    with sqlite3.connect(str(db_path)) as conn:
        new_id = _insert_snapshot_data(conn, is_cumulative=False)
        if not new_id:
            return True
    
    # Insert into cumulative DB at runs/ level
    try:
        with sqlite3.connect(str(cumulative_db_path)) as conn:
            _insert_snapshot_data(conn, is_cumulative=True)
    except Exception:
        # If cumulative DB write fails, log but don't fail the whole operation
        pass
    
    return True


def get_versions(run_dir: Path, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """List schema versions in run-level DB."""
    db_path = get_db_path(run_dir)
    if not db_path.exists():
        return []
    init_db(db_path)
    q = "SELECT id, run_id, snapshot_id, agent_id, trigger_type, action_name, captured_at, source FROM schema_versions WHERE 1=1"
    params: List[Any] = []
    if run_id:
        q += " AND run_id = ?"
        params.append(run_id)
    q += " ORDER BY id"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_diff(run_dir: Path, left_id: int, right_id: int) -> Dict[str, Any]:
    """Get stored diff between two version IDs."""
    db_path = get_db_path(run_dir)
    if not db_path.exists():
        return {"error": "DB not found"}
    init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT diff_summary FROM schema_diffs WHERE left_version_id = ? AND right_version_id = ?",
            (left_id, right_id),
        ).fetchone()
    if not row:
        return {"error": "diff not found"}
    try:
        return json.loads(row[0])
    except Exception:
        return {"error": "invalid diff_summary", "raw": row[0][:200]}


def run_cache_dump_for_snapshot(
    run_dir: Path,
    snapshot_id: str,
) -> bool:
    """Run cache dump / Discord channel schema extraction for a single snapshot.zip."""
    run_dir = Path(run_dir)
    snap_dir = run_dir / "snapshots" / snapshot_id
    zip_path = snap_dir / "snapshot.zip"
    if not zip_path.exists():
        return False
    db_path = get_db_path(run_dir)
    init_db(db_path)
    run_id = run_dir.name.replace("run_", "") if run_dir.name.startswith("run_") else run_dir.name
    version_id: Optional[int] = None
    action_name = ""
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, action_name FROM schema_versions WHERE run_id = ? AND snapshot_id = ? ORDER BY id DESC LIMIT 1",
                (run_id, snapshot_id),
            ).fetchone()
            if row:
                version_id = int(row["id"])
                action_name = (row["action_name"] or "").strip()
    except Exception:
        version_id = None
        action_name = ""
    _run_cache_dump_if_available(
        run_dir,
        run_id,
        snapshot_id,
        zip_path,
        db_path,
        version_id=version_id,
        action_name=action_name,
    )
    return True


def get_value_diff(
    run_dir: Path,
    left_snapshot_id: str,
    right_snapshot_id: str,
    db_name: str,
    object_store_name: str,
) -> Dict[str, Any]:
    """Compute a diff of sampled IndexedDB values between two snapshots."""
    run_dir = Path(run_dir)
    db_path = get_db_path(run_dir)
    if not db_path.exists():
        return {"error": "DB not found"}
    init_db(db_path)
    run_id = run_dir.name.replace("run_", "") if run_dir.name.startswith("run_") else run_dir.name
    db_name = (db_name or "?").strip() or "?"
    object_store_name = (object_store_name or "?").strip() or "?"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        # Use the most recent batch per snapshot
        def _latest_batch_id(snapshot_id: str) -> Optional[int]:
            row = conn.execute(
                """
                SELECT id FROM idb_value_sample_batch
                WHERE run_id = ? AND snapshot_id = ? AND db_name = ? AND object_store_name = ?
                ORDER BY id DESC LIMIT 1
                """,
                (run_id, snapshot_id, db_name, object_store_name),
            ).fetchone()
            return int(row["id"]) if row else None

        left_batch = _latest_batch_id(left_snapshot_id)
        right_batch = _latest_batch_id(right_snapshot_id)
        if not left_batch or not right_batch:
            return {"error": "no_samples", "left_batch": left_batch, "right_batch": right_batch}

        def _load_batch(batch_id: int) -> Dict[str, Dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT key_hash, key_json, value_hash, value_json
                FROM idb_value_samples
                WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchall()
            out: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                kh = r["key_hash"]
                out[kh] = {
                    "key_json": r["key_json"],
                    "value_json": r["value_json"],
                    "value_hash": r["value_hash"],
                }
            return out

        left_map = _load_batch(left_batch)
        right_map = _load_batch(right_batch)

        left_keys = set(left_map.keys())
        right_keys = set(right_map.keys())

        added: List[Dict[str, Any]] = []
        removed: List[Dict[str, Any]] = []
        modified: List[Dict[str, Any]] = []

        for kh in sorted(right_keys - left_keys):
            r = right_map[kh]
            added.append(
                {
                    "key_json": r["key_json"],
                    "right_value_json": r["value_json"],
                }
            )
        for kh in sorted(left_keys - right_keys):
            l = left_map[kh]
            removed.append(
                {
                    "key_json": l["key_json"],
                    "left_value_json": l["value_json"],
                }
            )
        for kh in sorted(left_keys & right_keys):
            l = left_map[kh]
            r = right_map[kh]
            if l.get("value_hash") != r.get("value_hash"):
                modified.append(
                    {
                        "key_json": l["key_json"],
                        "left_value_json": l["value_json"],
                        "right_value_json": r["value_json"],
                    }
                )
    return {
        "run_id": run_id,
        "db_name": db_name,
        "object_store_name": object_store_name,
        "left_snapshot_id": left_snapshot_id,
        "right_snapshot_id": right_snapshot_id,
        "added": added,
        "removed": removed,
        "modified": modified,
    }
