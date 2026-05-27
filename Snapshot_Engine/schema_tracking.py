# Snapshot_Engine/schema_tracking.py
"""
Schema tracking DB for web app DB schema drift (e.g. Discord, Telegram Web).
Record snapshots that include web_state (HTML, DOM, screenshot, IndexedDB schema)
and compare schema versions over time.
"""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCHEMA_VERSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    app_name TEXT,
    captured_at TEXT,
    zip_path TEXT NOT NULL,
    UNIQUE(run_id, snapshot_id)
);
"""

SCHEMA_DIFFS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_diffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    left_version_id INTEGER NOT NULL,
    right_version_id INTEGER NOT NULL,
    diff_summary TEXT,
    FOREIGN KEY (left_version_id) REFERENCES schema_versions(id),
    FOREIGN KEY (right_version_id) REFERENCES schema_versions(id)
);
"""


def init_db(db_path: Path) -> None:
    """Create or ensure schema tracking DB and tables."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(SCHEMA_VERSIONS_TABLE)
        conn.executescript(SCHEMA_DIFFS_TABLE)


def _read_schema_from_zip(zip_path: Path) -> Optional[List[Dict[str, Any]]]:
    """Read IndexedDB schema JSON from a snapshot zip. Looks for *indexeddb_schema*.json inside."""
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


def register_snapshot(
    db_path: Path,
    run_id: str,
    snapshot_id: str,
    agent_id: str,
    zip_path: Path,
    app_name: Optional[str] = None,
    captured_at: Optional[str] = None,
) -> bool:
    """Register a snapshot that contains web_state (schema) in the tracking DB."""
    zip_path = Path(zip_path)
    if not zip_path.exists():
        return False
    schema = _read_schema_from_zip(zip_path)
    if schema is None:
        return False
    init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO schema_versions (run_id, snapshot_id, agent_id, app_name, captured_at, zip_path)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, snapshot_id, agent_id, app_name or "", captured_at or "", str(zip_path.resolve())),
        )
    return True


def register_run(run_dir: Path, db_path: Path, app_name: Optional[str] = None) -> int:
    """
    Scan run_dir for snapshots that contain web_state schema and register them.
    Returns number of snapshots registered.
    """
    run_dir = Path(run_dir)
    snapshots_dir = run_dir / "snapshots"
    if not snapshots_dir.is_dir():
        return 0
    count = 0
    for snap_dir in snapshots_dir.iterdir():
        if not snap_dir.is_dir():
            continue
        snapshot_id = snap_dir.name
        zip_path = snap_dir / "snapshot.zip"
        if not zip_path.exists():
            continue
        manifest_path = snap_dir / "manifest.json"
        run_id = run_dir.name.replace("run_", "") if run_dir.name.startswith("run_") else run_dir.name
        agent_id = ""
        captured_at = None
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                agent_id = manifest.get("agent_id") or ""
                captured_at = manifest.get("created_at")
            except Exception:
                pass
        if register_snapshot(
            db_path, run_id, snapshot_id, agent_id, zip_path, app_name=app_name, captured_at=captured_at
        ):
            count += 1
    return count


def get_versions(
    db_path: Path,
    run_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    app_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List registered schema versions with optional filters."""
    init_db(db_path)
    q = "SELECT id, run_id, snapshot_id, agent_id, app_name, captured_at, zip_path FROM schema_versions WHERE 1=1"
    params: List[Any] = []
    if run_id:
        q += " AND run_id = ?"
        params.append(run_id)
    if agent_id:
        q += " AND agent_id = ?"
        params.append(agent_id)
    if app_name:
        q += " AND app_name = ?"
        params.append(app_name)
    q += " ORDER BY id"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def _schema_summary(schema_list: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Produce a comparable summary: DB names and their object stores."""
    if not schema_list:
        return {}
    out = {}
    for db in schema_list:
        if not isinstance(db, dict):
            continue
        name = db.get("name") or "?"
        stores = db.get("objectStores") or []
        out[name] = sorted(stores) if isinstance(stores, list) else []
    return out


def diff_schemas(
    db_path: Path,
    left_id: int,
    right_id: int,
) -> Dict[str, Any]:
    """
    Compare two schema versions. Returns added/removed DBs and object stores.
    """
    init_db(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        left_row = conn.execute("SELECT zip_path FROM schema_versions WHERE id = ?", (left_id,)).fetchone()
        right_row = conn.execute("SELECT zip_path FROM schema_versions WHERE id = ?", (right_id,)).fetchone()
    if not left_row or not right_row:
        return {"error": "version not found"}
    left_schema = _read_schema_from_zip(Path(left_row[0]))
    right_schema = _read_schema_from_zip(Path(right_row[0]))
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
        "left_id": left_id,
        "right_id": right_id,
        "added_databases": added_dbs,
        "removed_databases": removed_dbs,
        "object_store_changes": store_diffs,
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="PrZMA schema tracking: register snapshots and diff")
    ap.add_argument("--db", default="schema_tracking.db", help="SQLite DB path")
    ap.add_argument("--register-run", metavar="RUN_DIR", help="Register all web_state snapshots in RUN_DIR")
    ap.add_argument("--list", action="store_true", help="List registered versions")
    ap.add_argument("--diff", metavar="LEFT_ID,RIGHT_ID", help="Diff two version IDs")
    args = ap.parse_args()
    db_path = Path(args.db)
    if args.register_run:
        n = register_run(Path(args.register_run), db_path)
        print(f"Registered {n} snapshot(s).")
    if args.list:
        for row in get_versions(db_path):
            print(row)
    if args.diff:
        parts = args.diff.split(",")
        if len(parts) != 2:
            print("--diff requires LEFT_ID,RIGHT_ID")
            return
        result = diff_schemas(db_path, int(parts[0].strip()), int(parts[1].strip()))
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
