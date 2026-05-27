# VM_Agent/services/snapshot/snapper.py
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shared.wire_schemas import (
    SnapshotPolicy,
    SnapshotManifest,
    CollectedArtifact,
    sha256_file,
    utc_now_iso,
)

def _resolve_path_placeholders(p: str, meta: Dict[str, Any] | None = None) -> str:
    meta = meta or {}

    p = os.path.expandvars(p)

    env = os.environ
    mapping: Dict[str, Optional[str]] = {
        "LOCALAPPDATA": env.get("LOCALAPPDATA"),
        "APPDATA": env.get("APPDATA"),
        "USERPROFILE": env.get("USERPROFILE"),
        "WINDIR": env.get("WINDIR"),
        "SYSTEMDRIVE": env.get("SYSTEMDRIVE"),
        "PROGRAMDATA": env.get("PROGRAMDATA"),
        "PROFILE": meta.get("profile") or meta.get("PROFILE") or "Default",

        # Allow runtime override (critical)
        "CHROME_ROOT": (
            meta.get("CHROME_ROOT")
            or meta.get("chrome_root")
            or meta.get("BROWSER_ROOT")
            or meta.get("browser_root")
            or os.environ.get("PRZMA_CHROME_ROOT")
        ),
        "EDGE_ROOT": (
            meta.get("EDGE_ROOT")
            or meta.get("edge_root")
            or os.environ.get("PRZMA_EDGE_ROOT")
        ),
    }

    if mapping.get("LOCALAPPDATA"):
        local = mapping["LOCALAPPDATA"]
        if not mapping.get("CHROME_ROOT"):
            mapping["CHROME_ROOT"] = os.path.join(local, "Google", "Chrome", "User Data")
        if not mapping.get("EDGE_ROOT"):
            mapping["EDGE_ROOT"] = os.path.join(local, "Microsoft", "Edge", "User Data")

    out = p
    for k, v in mapping.items():
        if v:
            out = out.replace("{" + k + "}", v)

    return out

def _expand_glob(p: str) -> List[str]:
    if "*" in p or "?" in p:
        import glob
        return glob.glob(p, recursive=True)
    return [p]

def _safe_relpath(p: Path, base: Path) -> str:
    try:
        return str(p.relative_to(base))
    except Exception:
        return p.name


def _read_file_bytes_win32_shared(path: Path) -> Optional[bytes]:
    """Windows: read a file opened by another process using FILE_SHARE_READ (useful for cache index files)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x1
        OPEN_EXISTING = 3
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
        path_str = str(path.resolve())
        handle = kernel32.CreateFileW(
            path_str,
            GENERIC_READ,
            FILE_SHARE_READ,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE:
            return None
        try:
            size = path.stat().st_size
            if size <= 0:
                return b""
            buf = (ctypes.c_byte * size)()
            n = wintypes.DWORD()
            ok = kernel32.ReadFile(handle, buf, size, ctypes.byref(n), None)
            if not ok:
                return None
            return bytes(buf[: n.value])
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _read_file_bytes_win32_robocopy_backup(path: Path) -> Optional[bytes]:
    """Windows: copy a locked file via robocopy /B (backup mode) then read it."""
    if sys.platform != "win32":
        return None
    try:
        import subprocess
        import tempfile
        path = path.resolve()
        if not path.is_file():
            return None
        parent = path.parent
        name = path.name
        with tempfile.TemporaryDirectory(prefix="przma_robocopy_") as tmp:
            dest = Path(tmp)
            r = subprocess.run(
                ["robocopy", str(parent), str(dest), name, "/B", "/R:0", "/W:0"],
                capture_output=True,
                timeout=30,
                text=True,
            )
            if dest.joinpath(name).exists():
                return dest.joinpath(name).read_bytes()
    except Exception:
        pass
    return None


def _read_file_bytes(path: Path) -> Optional[bytes]:
    """Read file bytes. On Windows, retry with (1) shared read then (2) robocopy backup mode."""
    try:
        return path.read_bytes()
    except Exception:
        pass
    if sys.platform == "win32":
        out = _read_file_bytes_win32_shared(path)
        if out is not None:
            return out
        if "Cache_Data" in path.parts or (path.name in ("index", "index-dir") or path.name.startswith("data_")):
            return _read_file_bytes_win32_robocopy_backup(path)
    return None


class Snapper:
    def __init__(self, staging_root: str = "snapshots_staging"):
        self.staging_root = Path(staging_root)
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def collect(self, policy_dict: Dict[str, Any]) -> Dict[str, Any]:
        policy = SnapshotPolicy.from_dict(policy_dict)

        stage_dir = self.staging_root / f"{policy.run_id}_{policy.snapshot_id}"
        stage_dir.mkdir(parents=True, exist_ok=True)

        collected: List[Dict[str, Any]] = []
        total_bytes = 0

        layers = list(getattr(policy, "layers", []) or [])
        if not layers:
            layers = list((policy.layer_policies or {}).keys())

        for layer in layers:
            lp = (policy.layer_policies or {}).get(layer, {})
            include_paths = lp.get("include_paths", []) if isinstance(lp, dict) else []
            meta = (lp.get("meta") if isinstance(lp, dict) else None) or {}

            max_mb = (lp.get("max_total_mb") if isinstance(lp, dict) else None) or 1024
            max_total = int(max_mb) * 1024 * 1024
            layer_bytes = 0

            for raw in include_paths:
                artifact_key = None
                raw_path = raw

                if isinstance(raw, str) and "||" in raw:
                    artifact_key, raw_path = raw.split("||", 1)
                    artifact_key = (artifact_key or "").strip() or None
                    raw_path = (raw_path or "").strip()

                resolved = _resolve_path_placeholders(
                    raw_path,
                    lp.get("meta") if isinstance(lp, dict) else None
                )

                for one in _expand_glob(resolved):
                    src = Path(one)
                    if not src.exists():
                        continue

                    if src.is_file():
                        copied, size = self._copy_file(src, stage_dir, layer, artifact_key=artifact_key)
                        if copied:
                            total_bytes += size
                            layer_bytes += size
                            collected.append(copied)
                    else:
                        # directory
                        for f in src.rglob("*"):
                            if not f.is_file():
                                continue
                            copied, size = self._copy_file(f, stage_dir, layer, artifact_key=artifact_key, base_dir=src)
                            if copied:
                                total_bytes += size
                                layer_bytes += size
                                collected.append(copied)
                                if layer_bytes >= max_total:
                                    break

                    if layer_bytes >= max_total:
                        break

                if layer_bytes >= max_total:
                    break

        # zip
        zip_path = stage_dir.with_suffix(".zip")
        self._make_zip(stage_dir, zip_path)

        # manifest
        manifest = SnapshotManifest(
            run_id=policy.run_id,
            snapshot_id=policy.snapshot_id,
            agent_id=policy.agent_id,
            created_at=utc_now_iso(),
            trigger=policy.trigger or {},
            artifacts=collected,
            summary={
                "artifact_count": len(collected),
                "total_bytes": total_bytes,
                "zip_sha256": sha256_file(str(zip_path)) if zip_path.exists() else None,
            },
            environment={},
            repro={},
            notes="",
        )

        zip_bytes = zip_path.read_bytes() if zip_path.exists() else b""

        return {
            "ok": True,
            "run_id": policy.run_id,
            "snapshot_id": policy.snapshot_id,
            "agent_id": policy.agent_id,
            "manifest": manifest.to_dict(),
            "zip_bytes": zip_bytes,
        }

    def _copy_file(
        self,
        src: Path,
        stage_dir: Path,
        layer: str,
        artifact_key: Optional[str] = None,
        base_dir: Optional[Path] = None
    ) -> Tuple[Optional[Dict[str, Any]], int]:

        try:
            size = src.stat().st_size

            safe_key = (artifact_key or "unnamed").replace("\\", "_").replace("/", "_").replace(":", "_")
            layer_dir = stage_dir / "artifacts" / layer / safe_key
            layer_dir.mkdir(parents=True, exist_ok=True)

            if base_dir is not None:
                rel = _safe_relpath(src, base_dir)
                dst = layer_dir / Path(rel)
                dst.parent.mkdir(parents=True, exist_ok=True)
            else:
                dst = layer_dir / src.name

            data = _read_file_bytes(src)
            if data is None:
                return (None, 0)
            dst.write_bytes(data)

            return (
                CollectedArtifact(
                    layer=layer,
                    source_path=str(src),
                    stored_path=str(dst),
                    size=size,
                    sha256=sha256_file(str(dst)),
                    meta={"artifact_key": artifact_key or "unnamed"},
                ).to_dict(),
                size,
            )
        except Exception:
            return (None, 0)

    def _make_zip(self, folder: Path, zip_path: Path) -> None:
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in folder.rglob("*"):
                if p.is_file():
                    # Use forward slashes so cache dump prefix matching works cross-platform
                    arcname = p.relative_to(folder).as_posix()
                    zf.write(p, arcname=arcname)
