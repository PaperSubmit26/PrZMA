# Snapshot_Engine/engine.py
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import time
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import rpyc
import sys
from pathlib import Path

# add PrZMA root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]  # .../PrZMA
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Host/VM wire schemas
from shared.wire_schemas import (
    utc_now_iso,
    new_id,
    sha256_bytes,
    SnapshotPolicy,
    SnapshotTrigger,
    LayerPolicy,
)

LOG = logging.getLogger("przma.snapshot_engine")

# helpers
def _safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _dump_json(path: Path, obj: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _get_by_path(obj: Any, path: str) -> Any:
    """
    Supports "result.ok", "action.name", or flat "ok" etc.
    """
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _cmp(op: str, left: Any, right: Any) -> bool:
    try:
        if op == "eq":
            return left == right
        if op == "ne":
            return left != right
        if op == "in":
            if right is None:
                return False
            if isinstance(right, (list, tuple, set)):
                return left in right
            return False
        if op == "contains":
            if left is None:
                return False
            if isinstance(left, (list, tuple, set)):
                return right in left
            return str(right) in str(left)
        if op == "startswith":
            return str(left).startswith(str(right))
        if op == "endswith":
            return str(left).endswith(str(right))
        if op == "regex":
            return re.search(str(right), str(left) if left is not None else "") is not None

        lf = float(left)
        rf = float(right)
        if op == "gt":
            return lf > rf
        if op == "gte":
            return lf >= rf
        if op == "lt":
            return lf < rf
        if op == "lte":
            return lf <= rf
    except Exception:
        return False
    return False


def _parse_hhmm_interval(s: str) -> Optional[int]:
    # "HH:MM"
    m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", (s or "").strip())
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    total = hh * 3600 + mm * 60
    return total if total > 0 else None


def _default_placeholder_values(platform_cfg: Dict[str, Any] | None) -> Dict[str, str]:
    """
    By default, the Host does NOT resolve any placeholders.
    Substitution values are provided ONLY when explicitly defined in platform_cfg.
    (i.e., environment-specific resolution is handled on the VM side by the Snapper)
    """
    platform_cfg = platform_cfg or {}

    localapp = platform_cfg.get("LOCALAPPDATA")
    appdata  = platform_cfg.get("APPDATA")
    userprof = platform_cfg.get("USERPROFILE")
    windir   = platform_cfg.get("WINDIR")
    sysdrv   = platform_cfg.get("SYSTEMDRIVE")
    progdata = platform_cfg.get("PROGRAMDATA")

    m: Dict[str, str] = {}
    if localapp: m["{LOCALAPPDATA}"] = localapp
    if appdata:  m["{APPDATA}"] = appdata
    if userprof: m["{USERPROFILE}"] = userprof
    if windir:   m["{WINDIR}"] = windir
    if sysdrv:   m["{SYSTEMDRIVE}"] = sysdrv
    if progdata: m["{PROGRAMDATA}"] = progdata

    if localapp:
        m["{CHROME_ROOT}"] = str(Path(localapp) / "Google" / "Chrome" / "User Data")
        m["{EDGE_ROOT}"]   = str(Path(localapp) / "Microsoft" / "Edge" / "User Data")

    return m

def _apply_placeholders(path_tmpl: str, values: Dict[str, str]) -> str:
    out = path_tmpl
    for k, v in values.items():
        out = out.replace(k, v)
    return out

def _load_endpoints(path: str) -> Dict[str, Dict[str, Any]]:
    raw = _load_json(Path(path))
    eps = raw.get("endpoints") if isinstance(raw, dict) else raw
    m: Dict[str, Dict[str, Any]] = {}
    for e in eps:
        agent_id = e["agent_id"]
        m[agent_id] = {"host": e["host"], "port": int(e["port"]), "meta": e.get("meta", {})}
    return m


class SnapshotEngine:
    def __init__(
        self,
        *,
        run_id: str,
        rules_path: str,
        catalog_path: str,
        out_dir: str,
        vm_endpoints: Dict[str, Dict[str, Any]],
        platform_by_agent: Optional[Dict[str, Dict[str, Any]]] = None,
        # drain timeout & rpc timeout baseline
        drain_timeout_sec: int = 300,
    ):
        self.run_id = run_id
        self.rules_path = Path(rules_path)
        self.catalog_path = Path(catalog_path)
        self.out_dir = Path(out_dir)
        self.vm_endpoints = vm_endpoints
        self.platform_by_agent = platform_by_agent or {}

        self._stop_evt = threading.Event()
        self._time_thread: Optional[threading.Thread] = None
        self._tail_thread: Optional[threading.Thread] = None

        self._rules: Dict[str, Any] = {}
        self._catalog: Dict[str, Any] = {}

        self._last_event_fired: float = 0.0
        self._next_time_run: float = 0.0

        _safe_mkdir(self.out_dir)

        # in-flight snapshot tracking (drain)
        self._drain_timeout_sec = int(drain_timeout_sec or 0)
        self._inflight_lock = threading.Lock()
        self._inflight = 0
        self._inflight_zero = threading.Event()
        self._inflight_zero.set()  # initially 0

    def load(self) -> None:
        self._rules = _load_json(self.rules_path)
        self._catalog = _load_json(self.catalog_path)
        LOG.info("Loaded rules: %s", self.rules_path)
        LOG.info("Loaded catalog: %s", self.catalog_path)

        now = time.time()
        interval = self._get_time_interval_sec()
        if interval:
            self._next_time_run = now + interval

    def start(self, watch_action_log_path: Optional[str] = None) -> None:
        self.load()
        self._stop_evt.clear()

        self._time_thread = threading.Thread(target=self._time_loop, name="SnapshotEngineTime", daemon=False)
        self._time_thread.start()

        if watch_action_log_path:
            self._tail_thread = threading.Thread(
                target=self._tail_loop,
                args=(watch_action_log_path,),
                name="SnapshotEngineTail",
                daemon=False,
            )
            self._tail_thread.start()

        LOG.info("SnapshotEngine started (run_id=%s)", self.run_id)

    # stop with drain
    def stop(self, drain_timeout_sec: Optional[int] = None) -> None:
        self._stop_evt.set()

        timeout = self._drain_timeout_sec if drain_timeout_sec is None else int(drain_timeout_sec or 0)

        # Stop producing new snapshots quickly (tail/time loop will exit)
        # But wait for in-flight snapshot workers to finish
        if timeout > 0:
            deadline = time.time() + timeout
            while time.time() < deadline:
                with self._inflight_lock:
                    if self._inflight == 0:
                        break
                time.sleep(0.2)

        # Join threads 
        join_timeout = 10
        if timeout > 0:
            remaining = max(0, (time.time() + 0) - (time.time() - 0))  # no-op safe
            join_timeout = max(10, timeout)

        if self._time_thread and self._time_thread.is_alive():
            self._time_thread.join(timeout=join_timeout)
        if self._tail_thread and self._tail_thread.is_alive():
            self._tail_thread.join(timeout=join_timeout)

        with self._inflight_lock:
            inflight = self._inflight
        if inflight != 0:
            LOG.warning("SnapshotEngine stopped with inflight=%d (drain timeout hit)", inflight)
        else:
            LOG.info("SnapshotEngine stopped (run_id=%s)", self.run_id)

    def _get_time_interval_sec(self) -> Optional[int]:
        t = self._rules.get("time_trigger") or {}
        if not t.get("enabled", False):
            return None
        interval = t.get("interval")
        if isinstance(interval, str):
            return _parse_hhmm_interval(interval)
        return None

    def _get_time_cooldown(self) -> int:
        t = self._rules.get("time_trigger") or {}
        return int(t.get("cooldown_sec", 0) or 0)

    def _get_event_enabled(self) -> bool:
        e = self._rules.get("event_trigger") or {}
        return bool(e.get("enabled", False))

    def _get_event_cooldown(self) -> int:
        e = self._rules.get("event_trigger") or {}
        return int(e.get("cooldown_sec", 0) or 0)

    def _get_event_actions(self) -> List[str]:
        e = self._rules.get("event_trigger") or {}
        xs = e.get("on_actions") or []
        return list(xs) if isinstance(xs, list) else []

    def _get_event_conditions(self) -> List[Dict[str, Any]]:
        e = self._rules.get("event_trigger") or {}
        xs = e.get("conditions") or []
        return list(xs) if isinstance(xs, list) else []

    def _get_collection_plan(self) -> Dict[str, Any]:
        return self._rules.get("collection_plan") or {}

    # inflight helpers
    def _inflight_inc(self) -> None:
        with self._inflight_lock:
            self._inflight += 1
            if self._inflight > 0:
                self._inflight_zero.clear()

    def _inflight_dec(self) -> None:
        with self._inflight_lock:
            self._inflight = max(0, self._inflight - 1)
            if self._inflight == 0:
                self._inflight_zero.set()

    # public
    def notify_action(self, action_log_entry: Dict[str, Any]) -> None:
        if not self._get_event_enabled():
            return

        cooldown = self._get_event_cooldown()
        if cooldown > 0 and (time.time() - self._last_event_fired) < cooldown:
            return

        act_name = (
            _get_by_path(action_log_entry, "action.name")
            or _get_by_path(action_log_entry, "name")
            or _get_by_path(action_log_entry, "action_name")
        )
        if act_name is None:
            return

        if act_name not in self._get_event_actions():
            return

        for c in self._get_event_conditions():
            left = c.get("left", "")
            op = c.get("op", "eq")
            right = c.get("right")
            lv = _get_by_path(action_log_entry, left) if left else None
            if not _cmp(op, lv, right):
                return

        agent_id = (
            _get_by_path(action_log_entry, "agent_id")
            or _get_by_path(action_log_entry, "action.agent_id")
            or _get_by_path(action_log_entry, "agent")
        )
        if not agent_id:
            LOG.warning("event trigger matched but agent_id missing")
            return

        self._last_event_fired = time.time()

        # Run snapshot in a worker thread so tail loop never blocks and so we can drain properly.
        def _worker():
            self._inflight_inc()
            try:
                self._run_snapshot(
                    agent_id=str(agent_id),
                    trigger_type="event",
                    reason=f"event:{act_name}",
                    action_entry=action_log_entry,
                )
            except Exception as e:
                LOG.exception("snapshot worker failed: %s", e)
            finally:
                self._inflight_dec()

        t = threading.Thread(target=_worker, name=f"SnapshotWorker-{agent_id}-{int(time.time())}", daemon=False)
        t.start()

    def _catalog_platform(self) -> str:
        return "windows"

    def _get_catalog_entry(self, key: str) -> Optional[Dict[str, Any]]:
        plat = self._catalog.get("platforms", {}).get(self._catalog_platform(), {})
        e = plat.get(key)
        return e if isinstance(e, dict) else None

    def _maybe_remap_key_by_browser(self, key: str, browser: str, include_edge_equivalents: bool) -> str:
        if browser == "edge":
            if key.startswith("chromium."):
                candidate = "edge." + key[len("chromium.") :]
                if self._get_catalog_entry(candidate):
                    return candidate
            if ".chromium." in key:
                candidate = key.replace(".chromium.", ".edge.")
                if self._get_catalog_entry(candidate):
                    return candidate
            if key.startswith("edge.") or ".edge." in key:
                return key
        return key

    def _resolve_artifact_keys_to_layer_policies(
        self,
        agent_id: str,
        artifact_keys: List[str],
        layers_hint: Optional[List[str]],
        limits: Dict[str, Any],
        options: Dict[str, Any],
    ) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
        browser = str(options.get("browser", "chrome")).lower()
        profile = str(options.get("profile", "Default"))
        include_edge_equivalents = bool(options.get("include_edge_equivalents", False))

        platform_cfg = self.platform_by_agent.get(agent_id) or {}
        ph = _default_placeholder_values(platform_cfg)
        ph["{PROFILE}"] = profile

        paths_by_layer: Dict[str, List[str]] = {}

        for k0 in artifact_keys:
            k = self._maybe_remap_key_by_browser(k0, browser, include_edge_equivalents)
            entry = self._get_catalog_entry(k)
            if not entry:
                LOG.warning("catalog key not found: %s (original=%s)", k, k0)
                continue

            layer = entry.get("layer", "browser_artifacts")
            paths = entry.get("paths", [])
            if not isinstance(paths, list):
                continue

            for p in paths:
                if not isinstance(p, str):
                    continue
                resolved = _apply_placeholders(p, ph)
                paths_by_layer.setdefault(layer, []).append(f"{k}||{resolved}")

        layers: List[str] = list(layers_hint) if layers_hint else sorted(paths_by_layer.keys())

        max_file_mb = int(limits.get("max_file_mb", 200) or 200)
        max_total_mb = int(limits.get("max_total_mb", 1024) or 1024)

        layer_policies: Dict[str, Dict[str, Any]] = {}
        for layer in layers:
            include_paths = paths_by_layer.get(layer, [])
            lp = LayerPolicy(
                enabled=True,
                include_paths=include_paths,
                exclude_paths=[],
                include_globs=[],
                exclude_globs=[],
                max_file_mb=max_file_mb,
                max_total_mb=max_total_mb,
                meta={"resolved_from": "artifact_catalog", "browser": browser, "profile": profile},
            )
            layer_policies[layer] = lp.to_dict()

        return layers, layer_policies

    def _run_snapshot(
        self,
        *,
        agent_id: str,
        trigger_type: str,
        reason: str,
        action_entry: Optional[Dict[str, Any]] = None,
        scheduled_at: Optional[str] = None,
    ) -> None:
        plan = self._get_collection_plan()
        artifact_keys = plan.get("artifacts") or []
        if not isinstance(artifact_keys, list) or not artifact_keys:
            LOG.warning("collection_plan.artifacts is empty; skip snapshot")
            return

        layers_hint = plan.get("layers")
        layers_hint = layers_hint if isinstance(layers_hint, list) else None
        limits = plan.get("limits") or {}
        limits = limits if isinstance(limits, dict) else {}
        options = plan.get("options") or {}
        options = options if isinstance(options, dict) else {}

        snapshot_id = new_id("snap")

        trigger = SnapshotTrigger(
            type=trigger_type,
            reason=reason,
            action_id=_get_by_path(action_entry, "action.action_id") if action_entry else None,
            action_name=_get_by_path(action_entry, "action.name") if action_entry else None,
            agent_id=agent_id,
            scheduled_at=scheduled_at,
            meta={"rules_path": str(self.rules_path), "catalog_path": str(self.catalog_path)},
        )

        layers, layer_policies = self._resolve_artifact_keys_to_layer_policies(
            agent_id=agent_id,
            artifact_keys=[str(x) for x in artifact_keys],
            layers_hint=[str(x) for x in layers_hint] if layers_hint else None,
            limits=limits,
            options=options,
        )

        policy = SnapshotPolicy(
            run_id=self.run_id,
            snapshot_id=snapshot_id,
            agent_id=agent_id,
            trigger=trigger.to_dict(),
            layers=layers,
            layer_policies=layer_policies,
            platform=self.platform_by_agent.get(agent_id),
        )

        LOG.info("Trigger snapshot: agent=%s snapshot_id=%s type=%s", agent_id, snapshot_id, trigger_type)
        import sys
        print("[PrZMA] Snapshot triggered: agent=%s snapshot_id=%s" % (agent_id, snapshot_id), file=sys.stderr, flush=True)
        policy_dict = policy.to_dict()
        policy_dict["capture_web_state"] = plan.get("capture_web_state")
        vm_result = self._call_vm_snapshot_collect(agent_id, policy_dict)
        self._persist_snapshot_result(agent_id, snapshot_id, trigger.to_dict(), policy.to_dict(), vm_result)

    def _call_vm_snapshot_collect(self, agent_id: str, policy_dict: Dict[str, Any]) -> Dict[str, Any]:
        ep = self.vm_endpoints.get(agent_id)
        if not ep:
            return {"ok": False, "error": f"VM endpoint not found for agent_id={agent_id}", "manifest": None, "zip_bytes": None}

        host = ep["host"]
        port = int(ep["port"])

        # make RPC timeout at least drain timeout
        rpc_timeout = max(600, int(self._drain_timeout_sec or 0))

        try:
            conn = rpyc.connect(host, port, config={"sync_request_timeout": rpc_timeout, "allow_pickle": True})
            fn = getattr(conn.root, "snapshot_collect", None)
            if fn is None:
                raise AttributeError("VM_Agent has no exposed snapshot_collect()")
            result = fn(policy_dict)
            if isinstance(result, dict):
                return result
            try:
                return dict(result)
            except Exception:
                return {"ok": False, "error": f"Unexpected snapshot_collect return type: {type(result)}", "manifest": None, "zip_bytes": None}
        except Exception as e:
            return {"ok": False, "error": str(e), "manifest": None, "zip_bytes": None}

    def _persist_snapshot_result(
        self,
        agent_id: str,
        snapshot_id: str,
        trigger_dict: Dict[str, Any],
        policy_dict: Dict[str, Any],
        vm_result: Dict[str, Any],
    ) -> None:
        run_dir = self.out_dir / f"run_{self.run_id}"
        snap_dir = run_dir / "snapshots" / snapshot_id
        _safe_mkdir(snap_dir)

        _dump_json(snap_dir / "policy.json", policy_dict)

        vm_result_slim = dict(vm_result)
        zip_bytes = vm_result_slim.pop("zip_bytes", None)
        _dump_json(snap_dir / "vm_result.json", vm_result_slim)

        manifest = vm_result.get("manifest")
        if manifest:
            _dump_json(snap_dir / "manifest.json", manifest)

        if isinstance(zip_bytes, (bytes, bytearray)):
            zpath = snap_dir / "snapshot.zip"
            with zpath.open("wb") as f:
                f.write(zip_bytes)
            _dump_json(
                snap_dir / "zip_meta.json",
                {"size": len(zip_bytes), "sha256": sha256_bytes(bytes(zip_bytes)), "path": str(zpath)},
            )

        _dump_json(
            snap_dir / "engine_record.json",
            {
                "run_id": self.run_id,
                "snapshot_id": snapshot_id,
                "agent_id": agent_id,
                "created_at": utc_now_iso(),
                "trigger": trigger_dict,
                "ok": bool(vm_result.get("ok")),
                "error": vm_result.get("error"),
            },
        )

        # Run-level IndexedDB schema tracking: append web_state_indexeddb_schema (and optional ccl_chromium_reader) to run_dir DB
        zpath = snap_dir / "snapshot.zip"
        if zpath.exists():
            try:
                from Snapshot_Engine import indexeddb_schema_db
                if indexeddb_schema_db.append_snapshot(
                    run_dir,
                    snapshot_id,
                    zpath,
                    agent_id,
                    trigger_dict=trigger_dict,
                    manifest=manifest,
                ):
                    LOG.debug("IndexedDB schema appended for snapshot %s", snapshot_id)
            except Exception as e:
                LOG.debug("IndexedDB schema append skipped or failed: %s", e)

    def _time_loop(self) -> None:
        while not self._stop_evt.is_set():
            interval = self._get_time_interval_sec()
            if not interval:
                self._stop_evt.wait(0.5)
                continue

            now = time.time()

            if self._next_time_run <= 0:
                self._next_time_run = now + interval

            if now >= self._next_time_run:
                agent_id = (self._get_collection_plan().get("agent_id") or "").strip()
                if not agent_id:
                    LOG.warning("time trigger enabled but collection_plan.agent_id missing; skip")
                else:
                    def _worker():
                        self._inflight_inc()
                        try:
                            self._run_snapshot(
                                agent_id=agent_id,
                                trigger_type="time",
                                reason="time_trigger",
                                action_entry=None,
                                scheduled_at=utc_now_iso(),
                            )
                        finally:
                            self._inflight_dec()

                    threading.Thread(target=_worker, name=f"SnapshotWorker-time-{int(time.time())}", daemon=False).start()

                self._next_time_run = now + interval

            self._stop_evt.wait(0.5)

    def _tail_loop(self, action_log_path: str) -> None:
        path = Path(action_log_path)
        LOG.info("Tailing action log: %s", path)
        _safe_mkdir(path.parent)

        pos = 0
        while not self._stop_evt.is_set():
            if not path.exists():
                self._stop_evt.wait(0.5)
                continue

            try:
                with path.open("r", encoding="utf-8") as f:
                    f.seek(pos)
                    while not self._stop_evt.is_set():
                        line = f.readline()
                        if not line:
                            pos = f.tell()
                            break
                        line = line.strip()
                        # Skip empty lines
                        if not line:
                            continue
                        # Skip lines that don't start with '{' (likely incomplete JSON from concurrent writes)
                        if not line.startswith("{"):
                            continue
                        try:
                            entry = json.loads(line)
                            self.notify_action(entry)
                        except json.JSONDecodeError as e:
                            # Only log if it's a real JSON error, not just incomplete line
                            # Incomplete lines (from concurrent writes) are common and should be silently skipped
                            if e.pos > 0 or len(line) > 10:  # Only log if we parsed some of it or it's substantial
                                LOG.debug("Skipping incomplete/invalid JSON line (likely from concurrent write): %s...", line[:100] if len(line) > 100 else line)
                            # Don't update pos for incomplete lines - we'll retry on next iteration
                            continue
                        except Exception as e:
                            # Other errors (non-JSON) - log but continue
                            LOG.warning("Failed to parse action log line: %s (line preview: %s...)", e, line[:100] if len(line) > 100 else line)
                    # Only update pos if we successfully read to end of file
                    pos = f.tell()
                self._stop_evt.wait(0.2)
            except Exception as e:
                LOG.warning("Tail loop error: %s", e)
                self._stop_evt.wait(1.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--rules", required=True, help="rules.json path")
    ap.add_argument("--catalog", required=True, help="artifact_catalog.json path")
    ap.add_argument("--endpoints", required=True, help="vm_endpoints.json path")
    ap.add_argument("--out", default="runs", help="output directory")
    ap.add_argument("--action-log", default=None, help="actions.jsonl path (tail mode)")
    ap.add_argument(
        "--drain-timeout-sec",
        type=int,
        default=180,
        help="Seconds to wait for in-flight snapshot collection to finish on shutdown",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    endpoints = _load_endpoints(args.endpoints)

    engine = SnapshotEngine(
        run_id=args.run_id,
        rules_path=args.rules,
        catalog_path=args.catalog,
        out_dir=args.out,
        vm_endpoints=endpoints,
        drain_timeout_sec=args.drain_timeout_sec,
    )
    engine.start(watch_action_log_path=args.action_log)

    # shutdown handlers
    def _graceful_shutdown(signum, frame):
        try:
            LOG.info("Received signal %s; stopping SnapshotEngine...", signum)
            engine.stop(drain_timeout_sec=args.drain_timeout_sec)
        except Exception as e:
            LOG.exception("Error during graceful shutdown: %s", e)

    # SIGINT, SIGTERM
    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    # CTRL_BREAK_EVENT
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _graceful_shutdown)

    # Main loop
    try:
        while not engine._stop_evt.is_set():
            time.sleep(0.5)
    finally:
        engine.stop(drain_timeout_sec=args.drain_timeout_sec)


if __name__ == "__main__":
    main()
