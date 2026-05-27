# PrZMA/Automation_Agent/agent_generator.py
from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

import rpyc
from dotenv import load_dotenv
dotenv_path = Path(__file__).resolve().parents[1] / ".env"  # Automation_Agent/.. = PrZMA
load_dotenv(dotenv_path)


def read_json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_vm_agent(host: str, port: int, timeout: int = 3) -> bool:
    try:
        c = rpyc.connect(
            host,
            port,
            config={
                "sync_request_timeout": timeout,
                "allow_public_attrs": True,
                "allow_all_attrs": True,
                "allow_pickle": True,
            },
        )
        ok = (c.root.ping() == "pong")
        c.close()
        return ok
    except Exception:
        return False


def discover_vm_agents_in_target(target_cidr: str, port: int, need: int) -> List[Tuple[str, int]]:
    net = ipaddress.ip_network(target_cidr, strict=False)
    found: List[Tuple[str, int]] = []

    for ip in net.hosts():
        host = str(ip)
        if not port_open(host, port):
            continue
        if is_vm_agent(host, port):
            found.append((host, port))
            if len(found) >= need:
                break

    found.sort(key=lambda x: int(ipaddress.ip_address(x[0])))
    return found


def _run_vmrun(cmd: List[str], *, password: Optional[str] = None) -> subprocess.CompletedProcess:
    """
    If a password exists, it can also be injected via stdin. 
    (This corresponds to the case where vmrun displays the 'Encrypted virtual machine password:' prompt.)
    """
    if password is None:
        return subprocess.run(cmd, check=False, capture_output=True, text=True)
    return subprocess.run(cmd, check=False, capture_output=True, text=True, input=password + "\n")


def vmrun_start(vmrun_path: str, vmx_path: str, gui: bool = True, vm_password: Optional[str] = None) -> subprocess.CompletedProcess:
    """
    Specifying -T ws is more secure on VMware Workstation.
    If the VM is encrypted:
    1) Try the -vp <pw> option.
    2) If it fails/is not supported, retry using stdin injection.
    """
    mode = "gui" if gui else "nogui"
    base = [vmrun_path, "-T", "ws"]

    if vm_password:
        cmd = base + ["-vp", vm_password, "start", vmx_path, mode]
        res = _run_vmrun(cmd)
        if res.returncode == 0:
            return res

        combined = (res.stdout or "") + "\n" + (res.stderr or "")

        if ("Unknown option" in combined) or ("unrecognized option" in combined) or ("Encrypted virtual machine password" in combined):
            cmd2 = base + ["start", vmx_path, mode]
            return _run_vmrun(cmd2, password=vm_password)

        return res

    cmd = base + ["start", vmx_path, mode]
    return _run_vmrun(cmd)


def boot_vms_from_config(vm_boot_cfg: Dict[str, Any], agent_ids: List[str]) -> None:
    if not vm_boot_cfg.get("enabled", False):
        return

    provider = vm_boot_cfg.get("provider", "vmware")
    if provider != "vmware":
        raise RuntimeError(f"Unsupported vm_boot.provider: {provider}")

    vmrun_path = vm_boot_cfg.get("vmrun_path")
    if not vmrun_path:
        raise RuntimeError("vm_boot.enabled=true but vm_boot.vmrun_path is missing")

    gui = bool(vm_boot_cfg.get("gui", True))

    # Password env key(default: VM_PASSWORD)
    pw_env = vm_boot_cfg.get("vm_password_env", "VM_PASSWORD")
    vm_password = os.getenv(pw_env)  

    vmx_paths = vm_boot_cfg.get("vmx_paths")

    # (A) dict format: {"A1": "...vmx", "A2": "...vmx"}
    if isinstance(vmx_paths, dict):
        for aid in agent_ids:
            vmx = vmx_paths.get(aid)
            if not vmx:
                raise RuntimeError(f"vm_boot.vmx_paths missing vmx for agent_id={aid}")
            import sys
            print(f"[vm_boot] Starting VM for agent {aid}: {vmx}", file=sys.stderr)
            res = vmrun_start(vmrun_path, vmx, gui=gui, vm_password=vm_password)

            if res.returncode != 0:
                combined = (res.stdout or "") + "\n" + (res.stderr or "")
                raise RuntimeError(f"vmrun start failed for {aid}: {combined.strip()}")
        return

    # (B) list format: ["...A1.vmx", "...A2.vmx"]
    if isinstance(vmx_paths, list) and vmx_paths:
        for vmx in vmx_paths:
            res = vmrun_start(vmrun_path, vmx, gui=gui, vm_password=vm_password)
            if res.returncode != 0:
                combined = (res.stdout or "") + "\n" + (res.stderr or "")
                raise RuntimeError(f"vmrun start failed: {combined.strip()}")
        return

    raise RuntimeError("vm_boot.enabled=true but vm_boot.vmx_paths is empty/invalid")


def build_vm_endpoints(agent_ids: List[str], discovered: List[Tuple[str, int]]) -> Dict[str, Any]:
    if len(discovered) < len(agent_ids):
        raise RuntimeError(f"Not enough VM_Agents discovered: need={len(agent_ids)} got={len(discovered)}")

    endpoints = []
    for aid, (host, port) in zip(agent_ids, discovered[: len(agent_ids)]):
        endpoints.append(
            {
                "agent_id": aid,
                "host": host,
                "port": port,
                "meta": {"notes": "VM_Agent running agent_main.py"},
            }
        )
    return {"schema_version": "1.0.0", "endpoints": endpoints}


@dataclass
class GenerateResult:
    endpoints_path: Path
    endpoints: Dict[str, Any]
    discovered: List[Tuple[str, int]]


def generate_vm_endpoints(
    config: Dict[str, Any],
    agent_ids: List[str],
    endpoints_path: Path,
    *,
    boot_first: bool = True,
    wait_timeout_sec: int = 180,
    poll_interval_sec: float = 2.0,
) -> GenerateResult:
    # .env load (Automation_Agent/.. = PrZMA)
    dotenv_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(dotenv_path)

    import sys
    if boot_first:
        boot_vms_from_config(config.get("vm_boot") or {}, agent_ids)
        print("[discovery] VMs started, scanning for agents...", file=sys.stderr, flush=True)

    discovery = config.get("discovery") or {}

    targets = discovery.get("scan_targets")
    if not targets:
        subnet = discovery.get("subnet")
        if subnet:
            targets = [subnet]
        else:
            raise RuntimeError("Missing discovery.scan_targets (or discovery.subnet)")

    port = int(discovery.get("rpyc_port", discovery.get("port", 18861)))
    print(f"[discovery] Looking for {len(agent_ids)} agent(s) on {targets}, port {port} (timeout {wait_timeout_sec}s)", file=sys.stderr, flush=True)

    deadline = time.time() + wait_timeout_sec
    discovered: List[Tuple[str, int]] = []
    last_log = 0.0

    while time.time() < deadline:
        tmp: List[Tuple[str, int]] = []
        for t in targets:
            need = len(agent_ids) - len(tmp)
            if need <= 0:
                break
            tmp.extend(discover_vm_agents_in_target(t, port, need=need))

        uniq = {(h, p) for (h, p) in tmp}
        discovered = sorted(list(uniq), key=lambda x: int(ipaddress.ip_address(x[0])))

        now = time.time()
        if now - last_log >= 10.0:
            print(f"[discovery] Found {len(discovered)}/{len(agent_ids)} agents, waiting...", file=sys.stderr, flush=True)
            last_log = now

        if len(discovered) >= len(agent_ids):
            print(f"[discovery] All {len(agent_ids)} agent(s) found.", file=sys.stderr, flush=True)
            break
        time.sleep(poll_interval_sec)

    if len(discovered) < len(agent_ids):
        raise RuntimeError(
            f"Discovery timeout: need={len(agent_ids)} got={len(discovered)} "
            f"(targets={targets}, port={port})"
        )

    endpoints = build_vm_endpoints(agent_ids, discovered)
    write_json(endpoints_path, endpoints)
    return GenerateResult(endpoints_path=endpoints_path, endpoints=endpoints, discovered=discovered)
