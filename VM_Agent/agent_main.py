# VM_Agent/agent_main.py
from __future__ import annotations

import json
import threading
import queue
import concurrent.futures

from pathlib import Path
from typing import Any, Dict, Union, Optional, List

import rpyc
from rpyc.utils.server import ThreadedServer
from rpyc.utils.classic import obtain

# services
from services.browser_service import BrowserService
from services.discord_service import DiscordService
from services.telegram_service import TelegramService

# snapshot executor (VM)
import os
import tempfile
from services.snapshot.snapper import Snapper

# wire schemas
from shared.wire_schemas import (
    ActionRequest,
    ActionResult,
    ArtifactPointer,
    SnapshotPolicy,
    SnapshotResult,
)

DEFAULT_PORT = 18861


def load_vm_config(path: Path) -> Dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


class PrZMAService(rpyc.Service):
    """
    VM-side RPC service.
    - execute_action: run automation actions (browser/discord/telegram)
    - snapshot_collect: execute logical snapshot collection via Snapper

    NOTE:
    ThreadedServer will dispatch RPC calls on different threads.
    Playwright(sync) and related UI automation must run on a single, consistent thread.
    Hence, we serialize UI actions through an ActionWorker thread.
    """

    _action_lock = threading.RLock()    # kept for compatibility
    #_snapshot_lock = threading.RLock()

    # singleton-like service objects
    _browser = BrowserService()
    _discord = DiscordService(_browser)
    _telegram = TelegramService(_browser)

    # Snapper created lazily in __init__ to allow config injection
    _snap: Optional[Snapper] = None

    # ActionWorker (UI actions must run on a single thread)
    _worker_boot_lock = threading.Lock()
    _action_q: "queue.Queue[tuple[str, Any, concurrent.futures.Future]]" = queue.Queue()
    _worker_thread: Optional[threading.Thread] = None

    @classmethod
    def _ensure_action_worker(cls) -> None:
        with cls._worker_boot_lock:
            if cls._worker_thread is not None and cls._worker_thread.is_alive():
                return
            cls._worker_thread = threading.Thread(
                target=cls._action_worker_loop,
                name="PrZMA-ActionWorker",
                daemon=True,
            )
            cls._worker_thread.start()

    @classmethod
    def _action_worker_loop(cls) -> None:
        while True:
            kind, payload, fut = cls._action_q.get()
            try:
                if kind == "close_agent":
                    ok = cls._close_agent_impl(str(payload))
                    fut.set_result(ok)
                    continue

                if kind == "execute_action":
                    req: ActionRequest = payload
                    fut.set_result(cls._execute_action_impl(req))
                    continue

                if kind == "capture_page_state":
                    agent_id, out_dir = payload
                    try:
                        r = cls._browser.capture_page_state(agent_id, out_dir)
                        fut.set_result(r)
                    except Exception as e:
                        fut.set_result({"error": str(e)})
                    continue

                fut.set_result({"ok": False, "error": f"Unknown kind: {kind}"})

            except Exception as e:
                # Ensure caller never hangs
                try:
                    fut.set_result({"ok": False, "error": str(e)})
                except Exception:
                    pass

    @classmethod
    def _close_agent_impl(cls, agent_id: str) -> bool:
        try:
            cls._browser.close(agent_id)
            return True
        except Exception:
            return False

    @classmethod
    def _execute_action_impl(cls, req: ActionRequest) -> Dict[str, Any]:
        """
        Previous exposed_execute_action logic, executed ONLY on ActionWorker thread.
        """
        try:
            name = req.name
            params = dict(req.params or {})

            # auto-launch if needed (except explicit browser.launch)
            if name.startswith(("browser.", "web.", "discord.", "telegram.")) and name != "browser.launch":
                try:
                    # Relies on the internal implementation of BrowserService, but at least it is used to check if the page is open
                    cls._browser._page(req.agent_id)  # type: ignore[attr-defined]
                
                except Exception:
                    raise RuntimeError(
                    f"Browser not launched for agent_id={req.agent_id}. "
                    f"Call browser.launch first (channel/user_data_dir/timezone). "
                    f"Requested action={name}"
                    )

            # dispatch
            if name.startswith("browser."):
                out = cls._browser.execute(req.agent_id, name, params)

            elif name.startswith("web."):
                # alias: web.* -> browser.*
                mapped = "browser." + name.split(".", 1)[1]
                out = cls._browser.execute(req.agent_id, mapped, params)

            elif name.startswith("discord."):
                out = cls._discord.execute(req.agent_id, name, params)

            elif name.startswith("telegram."):
                out = cls._telegram.execute(req.agent_id, name, params)

            else:
                raise ValueError(f"Unsupported action namespace: {name}")

            # artifacts (optional)
            artifacts: List[Dict[str, Any]] = []
            if isinstance(out, dict) and out.get("screenshot_path"):
                artifacts.append(
                    ArtifactPointer(kind="file", vm_path=out["screenshot_path"]).to_dict()
                )

            res = ActionResult(
                run_id=req.run_id,
                agent_id=req.agent_id,
                action_id=req.action_id,
                ok=True,
                error=None,
                outputs=out if isinstance(out, dict) else {"value": out},
                artifacts=artifacts,
            )
            return res.to_dict()

        except Exception as e:
            return ActionResult(
                run_id=req.run_id,
                agent_id=req.agent_id,
                action_id=req.action_id,
                ok=False,
                error=str(e),
                outputs={},
                artifacts=[],
            ).to_dict()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Create Snapper once per service instance (but stored in class var to reuse)
        if self._snap is None:
            # try to read config for snapshot root
            cfg_path = Path(__file__).resolve().parent / "vm_agent_config.json"
            cfg = load_vm_config(cfg_path)
            snapshot_root = cfg.get("snapshot_root")  # optional

            # Be resilient to different Snapper constructor signatures
            try:
                if snapshot_root:
                    self._snap = Snapper(staging_root=snapshot_root)   # type: ignore
                else:
                    self._snap = Snapper()  # type: ignore
            except TypeError:
                # fallback if snapper.py uses different param name
                try:
                    self._snap = Snapper(snapshot_root)  # type: ignore
                except TypeError:
                    self._snap = Snapper()  # type: ignore

    # Basic
    def exposed_ping(self) -> str:
        return "pong"

    def exposed_close_agent(self, agent_id: str) -> bool:
        # MUST run on ActionWorker thread (same thread as Playwright)
        self._ensure_action_worker()
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._action_q.put(("close_agent", agent_id, fut))
        return bool(fut.result())

    # Parsing helpers
    def _parse_action_req(self, x: Union[str, Dict[str, Any]]) -> ActionRequest:
        if isinstance(x, str):
            return ActionRequest.from_json(x)
        return ActionRequest.from_dict(x)

    def _parse_snapshot_policy(self, x: Union[str, Dict[str, Any]]) -> SnapshotPolicy:
        if isinstance(x, str):
            return SnapshotPolicy.from_json(x)
        return SnapshotPolicy.from_dict(x)

    # Action execution
    def exposed_execute_action(self, req_payload: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
        # resolve netrefs from remote side
        req_payload = obtain(req_payload)
        req = self._parse_action_req(req_payload)

        # Enqueue to ActionWorker so UI automation is always on a single thread
        self._ensure_action_worker()
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._action_q.put(("execute_action", req, fut))
        return fut.result()

    # Snapshot collection (VM)
    def exposed_snapshot_collect(self, policy_payload: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
        policy_payload = obtain(policy_payload)
        policy = self._parse_snapshot_policy(policy_payload)

        # Snapshot collection can run concurrently with UI actions
        # with self._snapshot_lock:
        try:
            if self._snap is None:
                raise RuntimeError("Snapper not initialized")

            # Snapper is only responsible for “running the collection + generating the zip/manifest”
            policy_dict = policy.to_dict()

            # Optional: capture web state (HTML, DOM, screenshot, IndexedDB schema) for schema tracking
            capture_web_state = policy_dict.get("capture_web_state") is True
            if capture_web_state:
                self._ensure_action_worker()
                tmpdir = tempfile.mkdtemp(prefix="przma_web_state_")
                fut: concurrent.futures.Future = concurrent.futures.Future()
                self._action_q.put(("capture_page_state", (policy.agent_id, tmpdir), fut))
                try:
                    cap = fut.result(timeout=60)
                    if isinstance(cap, dict) and "error" not in cap:
                        paths = [cap.get("html_path"), cap.get("dom_path"), cap.get("screenshot_path"), cap.get("schema_path")]
                        paths = [p for p in paths if p]
                        if paths:
                            layers = list(policy_dict.get("layers") or []) + ["web_state"]
                            policy_dict["layers"] = layers
                            lp = policy_dict.get("layer_policies") or {}
                            lp["web_state"] = {
                                "enabled": True,
                                "include_paths": paths,
                                "max_file_mb": 50,
                                "max_total_mb": 100,
                                "meta": {"capture": "html_dom_screenshot_schema"},
                            }
                            policy_dict["layer_policies"] = lp
                except Exception:
                    pass

            # Inject runtime profile root into browser layer meta (so {CHROME_ROOT}/{PROFILE} resolve correctly)
            try:
                sess = type(self)._browser._sessions.get(policy.agent_id)  # BrowserSession or None
                lp_all = policy_dict.get("layer_policies") or {}
                # Host sends "browser_artifacts" (artifact_catalog layer name); support both for compatibility
                browser_lp = lp_all.get("browser_artifacts") or lp_all.get("browser")
                if isinstance(browser_lp, dict):
                    meta = browser_lp.get("meta") or {}
                    if not isinstance(meta, dict):
                        meta = {}
                    if sess and sess.user_data_dir:
                        prof = sess.profile_name or meta.get("PROFILE") or meta.get("profile") or "Default"
                        meta["PROFILE"] = prof
                        meta["profile"] = prof
                        meta["CHROME_ROOT"] = sess.user_data_dir
                        meta["EDGE_ROOT"] = sess.user_data_dir
                    else:
                        # No session (e.g. snapshot after browser.close): use same path layout as browser_service
                        drive = os.environ.get("SYSTEMDRIVE") or "C:"
                        fallback_root = os.path.join(drive + os.sep, "PrZMA", "profiles", policy.agent_id, "chrome")
                        meta["PROFILE"] = meta.get("PROFILE") or meta.get("profile") or "Default"
                        meta["profile"] = meta["PROFILE"]
                        meta["CHROME_ROOT"] = fallback_root
                        meta["EDGE_ROOT"] = fallback_root
                    browser_lp["meta"] = meta
                    layer_key = "browser_artifacts" if "browser_artifacts" in lp_all else "browser"
                    lp_all[layer_key] = browser_lp
                    policy_dict["layer_policies"] = lp_all
            except Exception:
                pass

            result = self._snap.collect(policy_dict)


            # collect() can return a SnapshotResult or a dict, so absorb it
            if hasattr(result, "to_dict"):
                return result.to_dict()  # type: ignore
            if isinstance(result, dict):
                return result
            raise TypeError(f"Unexpected snapper result type: {type(result)}")

        except Exception as e:
            return SnapshotResult(
                run_id=policy.run_id,
                snapshot_id=policy.snapshot_id,
                agent_id=policy.agent_id,
                ok=False,
                error=str(e),
                manifest=None,
                zip_bytes=None,
            ).to_dict()


def main():
    cfg_path = Path(__file__).resolve().parent / "vm_agent_config.json"
    cfg = load_vm_config(cfg_path)

    host = cfg.get("host", "0.0.0.0")
    port = int(cfg.get("port", DEFAULT_PORT))

    print(f"[PrZMA VM_Agent] starting RPyC server on {host}:{port}")

    protocol_config = rpyc.core.protocol.DEFAULT_CONFIG.copy()
    protocol_config.update(
        {
            "allow_public_attrs": True,
            "allow_all_attrs": True,
            "allow_pickle": True,
            "sync_request_timeout": 600,
        }
    )

    server = ThreadedServer(
        PrZMAService,
        hostname=host,
        port=port,
        protocol_config=protocol_config,
    )
    server.start()


if __name__ == "__main__":
    main()
