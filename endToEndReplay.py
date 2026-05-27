# PrZMA/endToEndReplay.py
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import rpyc
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Automation_Agent.agent_generator import generate_vm_endpoints


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def to_jsonable(x: Any, depth: int = 0, max_depth: int = 8) -> Any:
    if x is None or isinstance(x, (str, int, float, bool)):
        return x
    if isinstance(x, (bytes, bytearray)):
        return x.decode("utf-8", errors="replace")
    if depth >= max_depth:
        return str(x)
    if isinstance(x, dict):
        return {str(k): to_jsonable(v, depth + 1, max_depth) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [to_jsonable(v, depth + 1, max_depth) for v in x]
    return str(x)


def parse_iso_ts(v: Any) -> Optional[datetime]:
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        s = v.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def connect_vm(host: str, port: int, timeout: int = 60) -> rpyc.Connection:
    return rpyc.connect(
        host,
        port,
        config={
            "sync_request_timeout": timeout,
            "allow_public_attrs": True,
            "allow_all_attrs": True,
            "allow_pickle": True,
        },
    )


def vm_execute_action(conn: rpyc.Connection, req: Dict[str, Any]) -> Dict[str, Any]:
    payload = json.dumps(req, ensure_ascii=False)
    res = conn.root.execute_action(payload)
    return to_jsonable(res)


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for idx, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                obj["_line_no"] = idx
                out.append(obj)
        except Exception:
            continue
    return out


def resolve_discord_login_params(
    config: Dict[str, Any],
    agent_id: str,
    original_params: Dict[str, Any],
) -> Dict[str, Any]:
    agents = config.get("agents") or {}
    agent_cfg = agents.get(agent_id) or {}
    platforms = agent_cfg.get("platforms") or {}
    dcfg = platforms.get("discord_web") or {}
    cred_ref = dcfg.get("credential_ref")
    if not cred_ref:
        return original_params

    email = os.getenv(f"{cred_ref}_EMAIL")
    password = os.getenv(f"{cred_ref}_PASSWORD")
    if not email or not password:
        return original_params

    params = dict(original_params or {})
    if params.get("email") in (None, "", "***"):
        params["email"] = email
    if params.get("password") in (None, "", "***"):
        params["password"] = password
    return params


def params_for_action_log(name: Optional[str], params: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(params or {})
    if name == "discord.login":
        if "email" in p:
            p["email"] = "***"
        if "password" in p:
            p["password"] = "***"
    return p


def extract_replay_actions(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for e in entries:
        action = e.get("action")
        if not isinstance(action, dict):
            continue
        name = action.get("name")
        params = action.get("params")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(params, dict):
            continue
        if name.startswith("full_trigger."):
            continue
        out.append(e)
    return out


def load_endpoints_map(endpoints_path: Path) -> Dict[str, Tuple[str, int]]:
    data = read_json(endpoints_path)
    ep_map: Dict[str, Tuple[str, int]] = {}
    for e in data.get("endpoints", []):
        aid = e.get("agent_id")
        host = e.get("host")
        port = e.get("port")
        if isinstance(aid, str) and isinstance(host, str):
            ep_map[aid] = (host, int(port))
    return ep_map


def start_engine(
    *,
    run_id: str,
    rules_path: Path,
    catalog_path: Path,
    endpoints_path: Path,
    out_dir: Path,
    action_log_path: Path,
) -> subprocess.Popen:
    engine_py = ROOT / "Snapshot_Engine" / "engine.py"
    cmd = [
        sys.executable,
        str(engine_py),
        "--run-id",
        run_id,
        "--rules",
        str(rules_path),
        "--catalog",
        str(catalog_path),
        "--endpoints",
        str(endpoints_path),
        "--out",
        str(out_dir),
        "--action-log",
        str(action_log_path),
        "--drain-timeout-sec",
        "1200",
    ]
    return subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)


def stop_engine(engine_p: subprocess.Popen) -> None:
    try:
        engine_p.send_signal(signal.CTRL_BREAK_EVENT)
        engine_p.wait(timeout=1500)
        return
    except Exception:
        pass

    try:
        engine_p.terminate()
        engine_p.wait(timeout=300)
        return
    except Exception:
        pass

    try:
        if engine_p.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(engine_p.pid), "/T", "/F"],
                capture_output=True,
                text=True,
            )
    except Exception:
        pass


def replay_actions(
    *,
    entries: List[Dict[str, Any]],
    replay_run_id: str,
    config: Dict[str, Any],
    conns: Dict[str, rpyc.Connection],
    replay_log_path: Path,
) -> Dict[str, Any]:
    executed = 0
    skipped = 0
    failed = 0
    prev_ts: Optional[datetime] = None

    for idx, e in enumerate(entries, start=1):
        action = e.get("action") or {}
        name = action.get("name")
        params = dict(action.get("params") or {})
        reason = str(action.get("reason") or "")
        src_ts = parse_iso_ts(e.get("ts"))
        src_kind = str(e.get("kind") or "")
        agent_id = e.get("agent_id")

        if not isinstance(agent_id, str) or agent_id not in conns:
            skipped += 1
            append_jsonl(
                replay_log_path,
                {
                    "ts": now_iso(),
                    "run_id": replay_run_id,
                    "agent_id": str(agent_id),
                    "kind": "replay_skip",
                    "action": {
                        "name": name,
                        "params": params_for_action_log(name if isinstance(name, str) else None, params),
                        "reason": reason,
                    },
                    "result": {
                        "ok": False,
                        "error": f"agent_id not connected: {agent_id}",
                        "source_kind": src_kind,
                        "source_index": idx,
                    },
                },
            )
            prev_ts = src_ts or prev_ts
            continue

        if prev_ts and src_ts:
            delay = (src_ts - prev_ts).total_seconds()
            if delay > 0:
                time.sleep(delay)
        if src_ts:
            prev_ts = src_ts

        if name == "discord.login":
            params = resolve_discord_login_params(config, agent_id, params)

        req = {
            "schema_version": "1.0.0",
            "run_id": replay_run_id,
            "agent_id": agent_id,
            "action_id": f"replay_{agent_id}_{int(time.time()*1000)}_{idx}",
            "name": name,
            "params": params,
        }

        try:
            res = vm_execute_action(conns[agent_id], req)
            ok = bool(res.get("ok") is True) if isinstance(res, dict) else False
            if ok:
                executed += 1
            else:
                failed += 1
            append_jsonl(
                replay_log_path,
                {
                    "ts": now_iso(),
                    "run_id": replay_run_id,
                    "agent_id": agent_id,
                    "kind": src_kind or "replay",
                    "action": {
                        "name": name,
                        "params": params_for_action_log(name if isinstance(name, str) else None, params),
                        "reason": reason,
                    },
                    "result": to_jsonable(res),
                    "replay_meta": {
                        "source_ts": e.get("ts"),
                        "source_kind": src_kind,
                        "source_line_no": e.get("_line_no"),
                        "source_run_id": e.get("run_id"),
                    },
                },
            )
        except Exception as ex:
            failed += 1
            append_jsonl(
                replay_log_path,
                {
                    "ts": now_iso(),
                    "run_id": replay_run_id,
                    "agent_id": agent_id,
                    "kind": "error",
                    "action": {
                        "name": name,
                        "params": params_for_action_log(name if isinstance(name, str) else None, params),
                        "reason": reason,
                    },
                    "result": {
                        "ok": False,
                        "error": f"replay execute_action failed: {type(ex).__name__}: {ex}",
                    },
                    "replay_meta": {
                        "source_ts": e.get("ts"),
                        "source_kind": src_kind,
                        "source_line_no": e.get("_line_no"),
                        "source_run_id": e.get("run_id"),
                    },
                },
            )

    return {"executed": executed, "skipped": skipped, "failed": failed}


def main() -> None:
    ap = argparse.ArgumentParser(description="PrZMA end-to-end replay (no LLM)")
    ap.add_argument("--actions-log", required=True, help="Source actions.jsonl path")
    ap.add_argument("--config", required=True, help="Config used in original run")
    ap.add_argument("--run-id", default="", help="Replay run_id (default: auto)")
    ap.add_argument("--catalog", default=str(ROOT / "Snapshot_Engine" / "artifact_catalog.json"))
    ap.add_argument("--rules", default=str(ROOT / "Snapshot_Engine" / "rules.json"))
    ap.add_argument("--out-dir", default=str(ROOT / "runs"))
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")

    source_log_path = Path(args.actions_log)
    config_path = Path(args.config)
    out_dir = Path(args.out_dir)
    rules_path = Path(args.rules)
    catalog_path = Path(args.catalog)

    if not source_log_path.exists():
        raise RuntimeError(f"actions-log not found: {source_log_path}")
    if not config_path.exists():
        raise RuntimeError(f"config not found: {config_path}")
    if not catalog_path.exists():
        raise RuntimeError(f"catalog not found: {catalog_path}")

    config: Dict[str, Any] = read_json(config_path)
    agents = config.get("agents") or {}
    agent_ids = list(agents.keys())
    if not agent_ids:
        raise RuntimeError("No agents in config")

    run_id = args.run_id.strip() or f"Replay_{now_compact()}"
    run_dir = out_dir / f"run_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    replay_log_path = run_dir / "replayed_actions.jsonl"

    snapshot_rules = config.get("snapshot")
    if not isinstance(snapshot_rules, dict):
        raise RuntimeError("config.snapshot missing or not a dict")
    write_json(rules_path, snapshot_rules)

    endpoints_path = ROOT / "Snapshot_Engine" / "vm_endpoints.json"
    print("[replay] Booting VMs and discovering agents...", flush=True)
    generate_vm_endpoints(
        config=config,
        agent_ids=agent_ids,
        endpoints_path=endpoints_path,
        boot_first=True,
        wait_timeout_sec=int((config.get("vm_boot") or {}).get("wait_timeout_sec", 180)),
        poll_interval_sec=float((config.get("vm_boot") or {}).get("poll_interval_sec", 2.0)),
    )

    engine_p = start_engine(
        run_id=run_id,
        rules_path=rules_path,
        catalog_path=catalog_path,
        endpoints_path=endpoints_path,
        out_dir=out_dir,
        action_log_path=replay_log_path,
    )

    conns: Dict[str, rpyc.Connection] = {}
    try:
        ep_map = load_endpoints_map(endpoints_path)
        for aid in agent_ids:
            if aid not in ep_map:
                raise RuntimeError(f"vm_endpoints missing mapping for agent_id={aid}")
            host, port = ep_map[aid]
            conn = connect_vm(host, port, timeout=120)
            if conn.root.ping() != "pong":
                raise RuntimeError(f"VM_Agent not responding: {aid} {host}:{port}")
            conns[aid] = conn

        raw_entries = load_jsonl(source_log_path)
        replay_entries = extract_replay_actions(raw_entries)
        print(
            f"[replay] Source lines={len(raw_entries)}, replayable actions={len(replay_entries)}",
            flush=True,
        )

        stats = replay_actions(
            entries=replay_entries,
            replay_run_id=run_id,
            config=config,
            conns=conns,
            replay_log_path=replay_log_path,
        )
        print(
            f"[replay] finished: executed={stats['executed']} skipped={stats['skipped']} failed={stats['failed']}",
            flush=True,
        )
    finally:
        for c in conns.values():
            try:
                c.close()
            except Exception:
                pass

        stop_engine(engine_p)

        try:
            from Snapshot_Engine import indexeddb_schema_db as sdb

            versions = sdb.get_versions(run_dir)
            if versions:
                last_snap = versions[-1]
                sdb.run_cache_dump_for_snapshot(run_dir, last_snap["snapshot_id"])
        except Exception:
            pass


if __name__ == "__main__":
    main()
