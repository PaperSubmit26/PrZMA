# shared/wire_schemas.py
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Literal
import json
import uuid
import hashlib
import base64
from datetime import datetime, timezone

SCHEMA_VERSION = "1.0.0"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


@dataclass
class WireModel:
    schema_version: str = field(default=SCHEMA_VERSION, init=False)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]):
        d = dict(d)
        d.pop("schema_version", None)
        return cls(**d)

    @classmethod
    def from_json(cls, s: str):
        return cls.from_dict(json.loads(s))

# Action (Host <-> VM)
ActionName = str


@dataclass
class ActionRequest(WireModel):
    run_id: str
    agent_id: str
    action_id: str
    name: ActionName
    params: Dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=utc_now_iso)
    rationale: Optional[str] = None


@dataclass
class ArtifactPointer(WireModel):
    kind: Literal["file", "dir", "bytes", "text", "json"] = "file"
    vm_path: Optional[str] = None
    host_path: Optional[str] = None
    size: Optional[int] = None
    sha256: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionResult(WireModel):
    run_id: str
    agent_id: str
    action_id: str
    ok: bool
    ts: str = field(default_factory=utc_now_iso)
    error: Optional[str] = None
    outputs: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[ArtifactPointer] = field(default_factory=list)


# Snapshot (Host <-> VM)
LayerName = Literal[
    "memory_runtime",
    "system_artifacts",
    "browser_artifacts",
    "app_artifacts",
    "cloud_reflected",
]
TriggerType = Literal["event", "time", "manual"]


@dataclass
class SnapshotTrigger(WireModel):
    type: TriggerType
    reason: str = ""
    action_id: Optional[str] = None
    action_name: Optional[str] = None
    agent_id: Optional[str] = None
    scheduled_at: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LayerPolicy(WireModel):
    enabled: bool = True
    include_paths: List[str] = field(default_factory=list)
    exclude_paths: List[str] = field(default_factory=list)
    include_globs: List[str] = field(default_factory=list)
    exclude_globs: List[str] = field(default_factory=list)
    max_file_mb: Optional[int] = 200
    max_total_mb: Optional[int] = 1024
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SnapshotPolicy(WireModel):
    run_id: str
    snapshot_id: str
    agent_id: str
    trigger: Dict[str, Any]
    layers: List[LayerName] = field(default_factory=lambda: ["browser_artifacts"])
    layer_policies: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # layer -> LayerPolicy.to_dict()
    platform: Optional[Dict[str, Any]] = None
    ts: str = field(default_factory=utc_now_iso)
    # When True, VM captures web state (HTML, DOM, screenshot, IndexedDB schema) into snapshot
    capture_web_state: bool = False


# Currently, shared/schemas.py is based on CollectedFile, but since snapper often uses CollectedArtifact, compatibility is provided
@dataclass
class CollectedFile(WireModel):
    rel_path: str
    size: int
    sha256: str
    source_vm_path: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


# Backward/compat alias (depending on the snapper implementation)
@dataclass
class CollectedArtifact(WireModel):
    layer: str
    source_path: str
    stored_path: str
    size: int
    sha256: str
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SnapshotManifest(WireModel):
    run_id: str
    snapshot_id: str
    agent_id: str
    created_at: str = field(default_factory=utc_now_iso)
    trigger: Dict[str, Any] = field(default_factory=dict)

    # Open to contain either form
    layers: List[str] = field(default_factory=list)
    files: List[Dict[str, Any]] = field(default_factory=list)       # CollectedFile dicts
    artifacts: List[Dict[str, Any]] = field(default_factory=list)   # CollectedArtifact dicts (optional)

    summary: Dict[str, Any] = field(default_factory=dict)
    environment: Dict[str, Any] = field(default_factory=dict)
    repro: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""


@dataclass
class SnapshotResult(WireModel):
    run_id: str
    snapshot_id: str
    agent_id: str
    ok: bool
    ts: str = field(default_factory=utc_now_iso)
    error: Optional[str] = None
    manifest: Optional[Dict[str, Any]] = None
    zip_bytes: Optional[bytes] = None

    # JSON/log storage default excludes payload
    def to_dict(self, include_payload: bool = False) -> Dict[str, Any]:
        d = asdict(self)
        if not include_payload:
            d.pop("zip_bytes", None)
        else:
            if d.get("zip_bytes") is not None:
                d["zip_b64"] = base64.b64encode(d["zip_bytes"]).decode("ascii")
                d.pop("zip_bytes", None)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]):
        d = dict(d)
        d.pop("schema_version", None)
        zip_b64 = d.pop("zip_b64", None)
        if zip_b64 is not None and "zip_bytes" not in d:
            d["zip_bytes"] = base64.b64decode(zip_b64)
        return cls(**d)
