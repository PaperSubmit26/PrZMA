# shared/schemas.py
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Literal, Union, Tuple
import json
import uuid
import hashlib
import base64
from datetime import datetime, timezone

# helpers
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

# Base wire models: JSON-friendly types for Host <-> VM exchange
@dataclass
class WireModel:
    # Keep this out of __init__ to avoid field order issues
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


# # Identity & environment modeling (mapped from Interaction Modeling results)
Purpose = Literal["education", "tool_testing"]

OSName = Literal["windows", "macos", "linux", "android"]
BrowserType = Literal[
    "chromium",       # generic chromium engine
    "chrome",
    "msedge",
    "firefox",
    "webkit",         # safari-like engine (windows safari is not supported)
]

ServiceType = Literal[
    "web",            # generic web browsing
    "discord",
    "telegram",
    "whatsapp",
    "gmail",
    "teams",
    "slack",
    "custom",
]

ExecutionMode = Literal["vm", "baremetal"]


@dataclass
class AgentProfile(WireModel):
    """# Profile for a single agent (one account / one execution environment)"""
    agent_id: str                 # e.g., "A1"
    persona: str                  # free text
    os: OSName = "windows"
    mode: ExecutionMode = "vm"
    notes: str = ""


@dataclass
class AccountConfig(WireModel):
    """ Account / credential refs (secrets in .env; store key names only)"""
    service: ServiceType
    username_ref: str                       # e.g., "DISCORD_A1_USER"
    password_ref: str                       # e.g., "DISCORD_A1_PASS"
    otp_secret_ref: Optional[str] = None    # e.g., "DISCORD_A1_TOTP"


@dataclass
class BrowserConfig(WireModel):
    """# Browser execution settings (e.g., Edge, Chrome)"""
    browser: BrowserType
    channel: Optional[str] = None          # playwright channel name if needed
    user_data_dir: Optional[str] = None    # explicit profile dir override
    profile_name: Optional[str] = None     # "Default", "Profile 1", ...
    headless: bool = False
    locale: Optional[str] = "en-US"
    timezone: Optional[str] = "UTC"
    extra_args: List[str] = field(default_factory=list)


@dataclass
class PlatformConfig(WireModel):
    """# Platforms used by the agent (browser and/or service)"""
    browser: Optional[BrowserConfig] = None
    services: List[ServiceType] = field(default_factory=list)
    accounts: List[AccountConfig] = field(default_factory=list)


# Tool Manual Interpreter output model (target structure of interpreted.json)
@dataclass
class ToolCapability(WireModel):
    """
    Core structure extracted by the Tool Manual Interpreter
    Captures supported services, target artifacts, execution scope, and constraints in a structured form
    """
    tool_name: str
    tool_type: Literal["commercial", "opensource", "utility", "library", "unknown"] = "unknown"
    supported_os: List[OSName] = field(default_factory=lambda: ["windows"])
    supported_services: List[ServiceType] = field(default_factory=list)
    supported_artifact_types: List[str] = field(default_factory=list)     # Free-text interpretation + mapping to artifact catalog
    extraction_scope: List[str] = field(default_factory=list)             # e.g., "browser_cache", "registry"
    version_constraints: Dict[str, str] = field(default_factory=dict)     # e.g., {"windows": ">=10"}
    notes: str = ""


@dataclass
class InterpretedSpec(WireModel):
    """
    Automation Agent input for the tool-testing pipeline
    purpose must be tool_testing
    """
    purpose: Purpose
    capability: ToolCapability
    # Tool-testing objective: which artifacts to generate and which actions should trigger them
    target_artifacts: List[str] = field(default_factory=list)
    # Candidate action boundaries derived from tool specifications
    allowed_action_prefixes: List[str] = field(default_factory=list)  # e.g., ["browser.", "discord."]
    # Spec-derived snapshot rule candidates
    suggested_rules: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)


# Action Space (core of Prompt-Guided Action Execution)

ActionName = str
# e.g.,
# - "browser.launch", "browser.goto", "browser.search_google"
# - "discord.login", "discord.send_message", "discord.upload_file"
# - "telegram.login", "telegram.send_message", "telegram.upload_file"
# - "web.browse", "web.scroll", "web.open_result"
#
# Design principles:
# - Actions are defined as namespace.verb
# - New services/browsers can be added by registering names only

# Interaction Modeling (Action Boundary)
@dataclass
class ActionBoundary(WireModel):
    """
    Execution boundaries for the Automation Agent.
    - required: actions that must be executed (or key actions that must appear)
    - forbidden: actions that must never be executed
    - allowed_*: explicitly allowed scope (by prefix or by exact action name)
    - constraints: additional execution constraints such as
    disallowing file uploads or blocking access to specific domains
    (free-form structure, but key conventions must be documented)

    """
    allowed_action_prefixes: List[str] = field(default_factory=list)     # e.g., ["browser.", "discord."]
    allowed_actions: List[str] = field(default_factory=list)             # e.g., ["browser.goto", "discord.send_message"]
    required_actions: List[str] = field(default_factory=list)
    forbidden_actions: List[str] = field(default_factory=list)
    constraints: Dict[str, Any] = field(default_factory=dict)
    rationale: str = ""

@dataclass
class InteractionModel(WireModel):
    """
    interaction_modeling.json (shared output for Education and Tool Testing)
    - Generated by the Tool Manual Interpreter or the Automation Agent
    - Serves as the top-level plan and boundary document
    referenced by the Snapshot Engine and evaluation modules
    """
    interaction_id: str = field(default_factory=lambda: new_id("im"))
    run_id: str = ""
    purpose: Purpose = "education"                                 # "education" | "tool_testing"
    scenario_id: Optional[str] = None   
    document_id: Optional[str] = None                              # tool spec id / doc number
    created_at: str = field(default_factory=utc_now_iso)
    created_by: str = ""                                           # "tool_manual_interpreter" | "automation_agent"
    plan: Dict[str, Any] = field(default_factory=dict)             # high-level plan steps / notes
    boundary: ActionBoundary = field(default_factory=ActionBoundary)
    notes: str = ""


@dataclass
class ActionSpec(WireModel):
    """
    Metadata describing the supported Action Space.
    Required for documentation, LLM-driven planning, and runtime validation.
    Can be provided by the Host via action_catalog.json or defined in code.
    """
    name: ActionName
    description: str
    namespace: str                                                  # "browser", "discord", "telegram", "web", ...
    params_schema: Dict[str, Any] = field(default_factory=dict)     # json-schema style
    requires: List[str] = field(default_factory=list)               # preconditions tags
    produces: List[str] = field(default_factory=list)               # artifact/event tags
    risk: Literal["low", "medium", "high"] = "low"


@dataclass
class ActionRequest(WireModel):
    run_id: str
    agent_id: str
    action_id: str                 # Assigned by the host for tracking and reproducibility
    name: ActionName               # Extensible string identifier
    params: Dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=utc_now_iso)
    # Intent generated by the LLM/planner (for explanation and traceability)
    rationale: Optional[str] = None


@dataclass
class ArtifactPointer(WireModel):
    """
    Common pointer to files/data produced by actions or snapshots.
    - vm_path: path inside the VM
    - host_path: storage path on the host (filled by the host)
    """
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
    outputs: Dict[str, Any] = field(default_factory=dict)           # Lightweight results (e.g., current_url)
    artifacts: List[ArtifactPointer] = field(default_factory=list)  # Binary outputs such as screenshots


@dataclass
class ActionLogEntry(WireModel):
    """
    Backbone of the ground truth.
    Executed actions + outcomes + timestamps + optional normalized labels
    """
    run_id: str
    agent_id: str
    seq: int
    action: Dict[str, Any]          # ActionRequest.to_dict()
    result: Dict[str, Any]          # ActionResult.to_dict()
    ts: str = field(default_factory=utc_now_iso)
    labels: Dict[str, Any] = field(default_factory=dict)  # e.g., {"phase":"during","channel":"#hobby"}


# Snapshot (core of the logical snapshot mechanism)
LayerName = Literal[
    "memory_runtime",      # Full VM memory / process memory (optional)
    "system_artifacts",    # event logs, registry, prefetch, jumplist, ...
    "browser_artifacts",   # chromium profile, cache, indexeddb, localstorage...
    "app_artifacts",       # app-level dbs (discord/telegram local, etc.)
    "cloud_reflected",     # not “server data”, local manifestation
]

TriggerType = Literal["event", "time", "manual"]


@dataclass
class SnapshotTrigger(WireModel):
    type: TriggerType
    reason: str = ""
    # event-based: by which action?
    action_id: Optional[str] = None
    action_name: Optional[str] = None
    agent_id: Optional[str] = None
    # time-based
    scheduled_at: Optional[str] = None
    # extra
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LayerPolicy(WireModel):
    """
    Layer-specific collection configuration.
    Extension point for include/exclude patterns, glob rules, size limits, etc.
    """
    enabled: bool = True
    include_paths: List[str] = field(default_factory=list)   # resolved on VM
    exclude_paths: List[str] = field(default_factory=list)
    include_globs: List[str] = field(default_factory=list)
    exclude_globs: List[str] = field(default_factory=list)
    max_file_mb: Optional[int] = 200
    max_total_mb: Optional[int] = 1024
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SnapshotPolicy(WireModel):
    """
    Snapshot instruction passed from the Host (Snapshot_Engine) to the VM.
    """
    run_id: str
    snapshot_id: str
    agent_id: str
    trigger: Dict[str, Any]                     # SnapshotTrigger.to_dict()
    layers: List[LayerName] = field(default_factory=lambda: ["browser_artifacts"])
    layer_policies: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # layer -> LayerPolicy.to_dict()
     # Context required on the VM side for path resolution (browser type, pr
    platform: Optional[Dict[str, Any]] = None   # PlatformConfig.to_dict()
    ts: str = field(default_factory=utc_now_iso)


@dataclass
class CollectedFile(WireModel):
    rel_path: str
    size: int
    sha256: str
    source_vm_path: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SnapshotManifest(WireModel):
    """
    manifest.json inside the snapshot ZIP (minimal metadata for eval/repro/tool-testing).
    """
    run_id: str
    snapshot_id: str
    agent_id: str
    created_at: str = field(default_factory=utc_now_iso)
    trigger: Dict[str, Any] = field(default_factory=dict)
    layers: List[str] = field(default_factory=list)
    files: List[Dict[str, Any]] = field(default_factory=list)      # CollectedFile.to_dict() list
    summary: Dict[str, Any] = field(default_factory=dict)          # Per-layer statistics (file count, total size, etc.)
    environment: Dict[str, Any] = field(default_factory=dict)      # OS, browser version, etc.
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


# Rules (Snapshot Rule Configuration)
Comparator = Literal["eq", "ne", "contains", "startswith", "endswith", "regex", "gt", "gte", "lt", "lte"]


@dataclass
class Condition(WireModel):
    """
    rule condition:
    - left: log field path (e.g., "action.name", "result.ok", "labels.channel")
    - op: comparator
    - right: compare value
    """
    left: str
    op: Comparator
    right: Any


@dataclass
class RuleAction(WireModel):
    """
    Action executed on rule match (snapshot for now, extensible by design)
    """
    kind: Literal["snapshot", "notify", "mark"] = "snapshot"
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SnapshotRule(WireModel):
    rule_id: str
    enabled: bool = True
    trigger_type: TriggerType = "event"
    description: str = ""
    conditions: List[Dict[str, Any]] = field(default_factory=list)   # Condition.to_dict() list
    actions: List[Dict[str, Any]] = field(default_factory=list)      # RuleAction.to_dict() list
    cooldown_sec: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)



# Evaluation schemas
@dataclass
class ComplianceMetrics(WireModel):
    scenario_id: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    precision: float = 0.0
    recall: float = 0.0
    forbidden_rate: float = 0.0

@dataclass
class ReproMeta(WireModel):
    code_commit: str = ""                 
    code_dirty: Optional[bool] = None     
    host_os: str = ""                    
    python_version: str = ""
    playwright_version: str = ""
    vm_image_id: str = ""                                       # VM image hash/tag
    vm_snapshot_id: str = ""                                    # VM snapshot tag/id
    app_versions: Dict[str, str] = field(default_factory=dict)     
    tool_versions: Dict[str, str] = field(default_factory=dict)   
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RunMeta(WireModel):
    run_id: str
    purpose: Purpose
    started_at: str = field(default_factory=utc_now_iso)
    ended_at: Optional[str] = None
    agents: List[Dict[str, Any]] = field(default_factory=list)         
    platform_by_agent: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # agent_id -> PlatformConfig
    repro: Dict[str, Any] = field(default_factory=dict)                        
    notes: str = ""

# VM Endpoint config
@dataclass
class VMAgentEndpoint(WireModel):
    agent_id: str
    host: str
    port: int
    label: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
