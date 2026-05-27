# Tool_Manual_Interpreter/tmi_main.py
from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, List, Set, Tuple

from dotenv import load_dotenv

from Tool_Manual_Interpreter.ingest import ingest_manual
from Tool_Manual_Interpreter import prompts as prompts_module
from Tool_Manual_Interpreter.tmi_core import build_tool_plan


# allow running as a script
_repo_root = Path(__file__).resolve().parents[1]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# Utils
def now_utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def read_json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_repo_root(start: Path) -> Path:
    """
    Find PrZMA repo root by walking up.
    """
    cur = start.resolve()
    for _ in range(10):
        if (cur / "Automation_Agent").exists() and (cur / "Snapshot_Engine").exists():
            return cur
        cur = cur.parent
    return start.resolve()


def env_get(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


def _clamp_int(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(x)))


# Compile outputs (Snapshot rules)
def compile_tmi_rules(
    *,
    tool_plan: Dict[str, Any],
    default_agent_id: str,
    template_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build SnapshotEngine-compatible rules (same shape as current rules.json / config.snapshot).
    Conservative:
      - time_trigger: inherit from template if exists, else disabled
      - event_trigger.on_actions: tool_plan.trigger_actions
      - collection_plan.artifacts: tool_plan.required_artifacts
      - collection_plan.options/limits: inherit if exists
    """
    tp = tool_plan.get("tool_plan") or tool_plan
    trigger_actions = tp.get("trigger_actions") or []
    required_artifacts = tp.get("required_artifacts") or []

    # defaults
    time_trigger = {"enabled": False, "interval": "00:10", "cooldown_sec": 0}
    event_trigger = {
        "enabled": True,
        "on_actions": list(trigger_actions),
        "cooldown_sec": 0,
        "conditions": [{"left": "agent_id", "op": "eq", "right": default_agent_id}],
    }
    collection_plan = {
        "agent_id": default_agent_id,
        "artifacts": list(required_artifacts),
        "limits": {"max_file_mb": 400, "max_total_mb": 2048},
        "options": {"browser": "chrome", "profile": "Default"},
    }

    # inherit from template snapshot if provided
    if isinstance(template_snapshot, dict):
        tt = template_snapshot.get("time_trigger")
        et = template_snapshot.get("event_trigger")
        cp = template_snapshot.get("collection_plan")

        if isinstance(tt, dict):
            time_trigger.update({k: tt[k] for k in ("enabled", "interval", "cooldown_sec") if k in tt})

        if isinstance(et, dict):
            # preserve our on_actions/conditions, inherit cooldown if present
            if "cooldown_sec" in et:
                event_trigger["cooldown_sec"] = et["cooldown_sec"]

        if isinstance(cp, dict):
            if isinstance(cp.get("options"), dict):
                collection_plan["options"].update(cp["options"])
            if isinstance(cp.get("limits"), dict):
                collection_plan["limits"].update(cp["limits"])

    if not event_trigger["on_actions"]:
        event_trigger["enabled"] = False

    return {
        "time_trigger": time_trigger,
        "event_trigger": event_trigger,
        "collection_plan": collection_plan,
    }


# Tool-testing agent synthesis

# action name prefix(namespace) -> config.platform key mapping
# extend when new platforms added
_PLATFORM_NS_MAP: Dict[str, str] = {
    "discord": "discord_web",
    "telegram": "telegram_web",     
    # "slack": "slack_web",
    # "teams": "teams_web",
}


def _infer_needed_platforms(required_actions: List[Dict[str, Any]]) -> Set[str]:
    """
    Look at required_actions[*].name prefixes and infer which platform blocks should be enabled.
    Example:
      - "discord.send_message" -> "discord_web"
      - "telegram.send_message" -> "telegram_web" (if mapped)
    """
    names = [a.get("name", "") for a in (required_actions or []) if isinstance(a, dict)]
    need: Set[str] = set()

    for n in names:
        if not isinstance(n, str) or "." not in n:
            continue
        ns = n.split(".", 1)[0]
        plat = _PLATFORM_NS_MAP.get(ns)
        if plat:
            need.add(plat)

    return need


def _infer_agent_count(required_actions: List[Dict[str, Any]]) -> int:
    """
    Minimum rule:
    - If interaction-generating actions exist (send_message/upload_file), prefer 2 agents
    - Else 1 agent
    """
    names = [a.get("name", "") for a in (required_actions or []) if isinstance(a, dict)]
    # keep this rule conservative
    if any(n.endswith(".send_message") or n.endswith(".upload_file") for n in names if isinstance(n, str)):
        return 2
    return 1


def _build_tooltesting_persona(
    tool_name: str,
    tool_version: str,
    agent_id: str,
    is_primary: bool,
    required_actions: List[Dict[str, Any]],
    forbidden_actions: List[Dict[str, Any]],
    required_artifacts: List[str],
    action_intent_guidance: str = "", 
) -> str:
    ra = [x.get("name") for x in (required_actions or []) if isinstance(x, dict) and x.get("name")]
    fa = [x.get("name") for x in (forbidden_actions or []) if isinstance(x, dict) and x.get("name")]

    ra_s = ", ".join([str(x) for x in ra[:24]]) if ra else "(none)"
    fa_s = ", ".join([str(x) for x in fa[:24]]) if fa else "(none)"
    art_s = ", ".join([str(x) for x in (required_artifacts or [])[:24]]) if required_artifacts else "(none)"

    artifact_first_guidance = (
        "Artifact-first execution rules:\n"
        "- Choose actions to intentionally generate the required_artifacts (not merely to follow required_actions).\n"
        "- Interpret required_actions as allowed primitives; you still must pick parameters that cause artifact creation.\n"
    )

    goto_validity_guidance = ""
    if any(a == "browser.goto" for a in ra):
        goto_validity_guidance = (
            "Navigation validity rules (browser.goto):\n"
            "- Choose a URL that is likely reachable and returns a normal page.\n"
            "- After execution, check success (ok=true). If ok=false, switch to a different URL rather than retrying.\n"
            "- Prefer URLs that match the artifact goal implied by required_artifacts.\n"
        )
    reason_guidance = (
        "Reason-writing rules:\n"
        "- Write one short sentence that links (artifact goal) + (intent) + (expected effect).\n"
        "- Avoid repeating the exact same reason text across steps.\n"
    )

    intent_block = ""
    if action_intent_guidance and action_intent_guidance.strip():
        intent_block = (
            "Action intent guidance (derived from tool plan):\n"
            f"- {action_intent_guidance.strip()}\n"
        )

    role = "primary executor" if is_primary else "supporting counterpart"
    return (
        f"You are a tool-testing bot ({role}) validating the forensic tool '{tool_name}' (version: {tool_version}).\n"
        f"Your only goal is to generate the required forensic artifacts so the tool can be tested.\n\n"
        f"Operating rules:\n"
        f"- Prioritize executing required_actions that lead to required_artifacts.\n"
        f"- Keep actions minimal and directly tied to artifact generation.\n"
        f"- Avoid irrelevant chat; only communicate if an action requires it (e.g., *.send_message).\n"
        f"- Never perform forbidden_actions.\n\n"
        f"{artifact_first_guidance}"
        f"{goto_validity_guidance}"
        f"{intent_block}"
        f"{reason_guidance}\n"
        f"Plan context:\n"
        f"- required_actions: {ra_s}\n"
        f"- forbidden_actions: {fa_s}\n"
        f"- required_artifacts: {art_s}\n"
    )



def _derive_run_limits(
    template_config: Dict[str, Any],
    *,
    required_actions: List[Dict[str, Any]],
    required_artifacts: List[str],
    n_agents: int,
    needed_platforms: Set[str],
) -> Dict[str, int]:
    """
    Decide config.run_limits for tool_testing interpreted config.

    Priority:
      1) template_config.run_limits if valid (baseline)
      2) otherwise defaults

    Then apply tool-testing heuristic to recommend max_actions_total.
    - includes bootstrap cost (per agent)
    - includes core cost from required_actions
    - adds buffer for retries/exploration and artifact confirmation
    """
    # defaults
    max_actions_total = 30
    max_minutes = 15

    # baseline from template (TOP-LEVEL run_limits)
    if isinstance(template_config, dict):
        rl = template_config.get("run_limits") or {}
        if isinstance(rl, dict):
            mt = rl.get("max_actions_total")
            mm = rl.get("max_minutes")
            if isinstance(mt, int) and mt > 0:
                max_actions_total = mt
            if isinstance(mm, int) and mm > 0:
                max_minutes = mm

    # heuristic for tool_testing max_actions_total
    ra_n = len(required_actions or [])
    art_n = len(required_artifacts or [])

    # bootstrap per agent:
    # browser.launch + (platform open/login/goto) ~ 3~5
    # keep conservative but non-trivial
    bootstrap_per_agent = 3
    if "discord_web" in needed_platforms:
        bootstrap_per_agent += 2  # open + goto (login may happen)
    if "telegram_web" in needed_platforms or "slack_web" in needed_platforms:
        bootstrap_per_agent += 2

    bootstrap_cost = n_agents * bootstrap_per_agent

    # core actions: required_actions tend to expand into multiple concrete actions
    # (navigate, wait, confirm, retry, etc.)
    core_cost = max(ra_n * 3, 10)

    # artifact confirmation / retries buffer
    buffer_cost = 8 + min(art_n * 2, 20)

    est = bootstrap_cost + core_cost + buffer_cost

    # clamp: allow bigger ranges than before (automation_agent hard stop)
    max_actions_total = _clamp_int(est, 15, 120)

    # optionally scale minutes with action budget (light touch)
    # keep template minutes as baseline, but if action budget big, bump a bit
    if max_actions_total >= 80 and max_minutes < 25:
        max_minutes = 25
    elif max_actions_total >= 50 and max_minutes < 20:
        max_minutes = 20

    return {"max_actions_total": max_actions_total, "max_minutes": max_minutes}


def compile_interpreted_przma_config(
    *,
    run_id: str,
    tool_plan: Dict[str, Any],
    template_config: Dict[str, Any],
    default_agent_id: str = "A1",
) -> Dict[str, Any]:
    """
    Build interpreted_przma_config.json:
    - Uses template_config as base (keeps vm_boot/discovery etc.)
    - For tool_testing: overrides agents/persona/rendezvous/global_prompt/scenario/snapshot + run_limits
    """
    cfg = deepcopy(template_config) if isinstance(template_config, dict) else {}

    tp = tool_plan.get("tool_plan") or tool_plan
    tool = tp.get("tool") or {}
    tool_name = str(tool.get("name") or "unknown")
    tool_version = str(tool.get("version") or "unknown")
    objective = str(tp.get("objective") or "").strip() or f"Tool testing plan for {tool_name}"

    cfg["schema_version"] = cfg.get("schema_version") or "1.0.0"
    cfg["purpose"] = "tool_testing"

    required_actions = tp.get("required_actions") or []
    forbidden_actions = tp.get("forbidden_actions") or []
    required_artifacts = tp.get("required_artifacts") or []
    trigger_actions = tp.get("trigger_actions") or []
    completion = tp.get("completion") or {}
    action_intent_guidance = str(tp.get("action_intent_guidance") or "").strip()

    # available agent IDs bounded by vmx_paths
    vmx_paths = (((cfg.get("vm_boot") or {}).get("vmx_paths")) or {})
    available_agent_ids = [k for k in vmx_paths.keys() if isinstance(k, str)]
    if not available_agent_ids:
        available_agent_ids = [default_agent_id]

    # choose agent count
    n_agents = _infer_agent_count(required_actions)
    chosen_agent_ids = available_agent_ids[: max(1, min(n_agents, len(available_agent_ids)))]

    # needed platforms (from required_actions)
    needed_platforms = _infer_needed_platforms(required_actions)
    # target_application: when set (e.g. full_trigger on Discord), ensure bootstrap + full_trigger + main loop run on that platform
    target_application = (tool_plan.get("target_application") or tp.get("target_application")) or None
    if target_application and target_application in ("discord_web", "telegram_web"):
        needed_platforms = set(needed_platforms) | {target_application}
        cfg["target_application"] = target_application

    # build agents override
    agents: Dict[str, Any] = {}
    for idx, aid in enumerate(chosen_agent_ids):
        is_primary = (idx == 0)
        agent_obj: Dict[str, Any] = {
            "display_name": f"Agent {aid}",
            "persona": _build_tooltesting_persona(
                tool_name=tool_name,
                tool_version=tool_version,
                agent_id=aid,
                is_primary=is_primary,
                required_actions=required_actions,
                forbidden_actions=forbidden_actions,
                required_artifacts=required_artifacts,
                action_intent_guidance=action_intent_guidance,
            ),
            "platforms": {},
            "browser": {"engine": "chromium", "channel": "chrome"},
        }

        # enable inferred platforms
        if "discord_web" in needed_platforms:
            agent_obj["platforms"]["discord_web"] = {
                "enabled": True,
                "login_required": True,
                "credential_ref": f"DISCORD_{aid}",
            }

        if "telegram_web" in needed_platforms:
            # NOTE: align with your env/credential conventions
            agent_obj["platforms"]["telegram_web"] = {
                "enabled": True,
                "login_required": True,
                "credential_ref": f"TELEGRAM_{aid}",
            }

        if "slack_web" in needed_platforms:
            agent_obj["platforms"]["slack_web"] = {
                "enabled": True,
                "login_required": True,
                "credential_ref": f"SLACK_{aid}",
            }

        agents[aid] = agent_obj

    cfg["agents"] = agents

    # rendezvous (so bootstrap opens that app; when target_application is set, prefer it so full_trigger runs there)
    rendezvous_platform = ""
    if target_application and target_application in needed_platforms:
        rendezvous_platform = target_application
    elif "discord_web" in needed_platforms:
        rendezvous_platform = "discord_web"
    elif len(needed_platforms) == 1:
        rendezvous_platform = list(needed_platforms)[0]
    cfg["rendezvous"] = {"platform": rendezvous_platform}

    # scenario
    cfg["scenario"] = {
        "objective": objective[:900],
        "notes": {
            "tool": {"name": tool_name, "version": tool_version},
            "run_id": run_id,
        },
    }

    # global_prompt: tool-testing oriented (forbidden reasons included)
    forbidden_names: List[str] = []
    forbidden_why: Dict[str, str] = {}
    for x in forbidden_actions:
        if isinstance(x, dict) and x.get("name"):
            name = str(x["name"])
            forbidden_names.append(name)
            if x.get("why"):
                forbidden_why[name] = str(x["why"])[:240]

    cfg["global_prompt"] = {
        "interaction_style": [
            "TOOL-TESTING MODE: prioritize generating required artifacts over conversation.",
            "Be concise; do not roleplay an investigation case.",
            "Only communicate if a required action needs it (e.g., *.send_message).",
            "When logging reasons, state which artifact the action is meant to generate.",
        ],
        "hard_constraints": [
            "Do not execute forbidden actions.",
            {"forbidden_actions": forbidden_names},
            {"forbidden_action_reasons": forbidden_why},
        ],
        "completion_criteria": [
            completion if isinstance(completion, dict) else {"type": "min_required_actions", "detail": ""},
        ],
    }

    # IMPORTANT: run_limits must be TOP-LEVEL (automation_agent reads config.run_limits)
    run_limits = _derive_run_limits(
        template_config=template_config,
        required_actions=required_actions,
        required_artifacts=required_artifacts,
        n_agents=len(chosen_agent_ids),
        needed_platforms=needed_platforms,
    )
    cfg["run_limits"] = run_limits

    # snapshot: derived from tool_plan (disable time trigger by default)
    primary_agent = chosen_agent_ids[0] if chosen_agent_ids else default_agent_id
    trigger_names = [x for x in trigger_actions if isinstance(x, str)]

    # full_trigger (tool testing): when enabled, add browser.full_trigger_click so each click triggers snapshot
    full_trigger = (tool_plan.get("full_trigger") or tp.get("full_trigger")) is True
    if full_trigger:
        if "browser.full_trigger_click" not in trigger_names:
            trigger_names = list(trigger_names) + ["browser.full_trigger_click"]
        cfg["run_full_trigger"] = True
        cfg["full_trigger_agent"] = primary_agent
        ft_max_clicks = tp.get("full_trigger_max_clicks") or 30
        cfg["full_trigger_max_clicks"] = ft_max_clicks
        cfg["full_trigger_wait_after_click_sec"] = tp.get("full_trigger_wait_after_click_sec") or 2.0
        # Reserve action budget for full_trigger so LLM loop still has room for required_actions
        run_limits["max_actions_total"] = run_limits.get("max_actions_total", 30) + int(ft_max_clicks)

    cfg["snapshot"] = cfg.get("snapshot") or {}
    cfg["snapshot"]["time_trigger"] = {"enabled": False, "interval": "00:10", "cooldown_sec": 0}
    cfg["snapshot"]["event_trigger"] = {
        "enabled": True if trigger_names else False,
        "on_actions": trigger_names,
        "cooldown_sec": 0,
        "conditions": [{"left": "agent_id", "op": "eq", "right": primary_agent}],
    }
    collection_plan: Dict[str, Any] = {
        "agent_id": primary_agent,
        "artifacts": list(required_artifacts),
        "limits": {"max_file_mb": 400, "max_total_mb": 2048},
        "options": {"browser": "chrome", "profile": "Default"},
    }
    if full_trigger:
        collection_plan["capture_web_state"] = True
    cfg["snapshot"]["collection_plan"] = collection_plan

    return cfg

# Main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool-name", help="Tool name (or TMI_TOOL_NAME)")
    ap.add_argument("--tool-version", default="", help="Tool version (optional)")

    ap.add_argument("--manual-url", default="", help="Tool manual URL (or TMI_TOOL_MANUAL_URL)")
    ap.add_argument("--manual-path", default="", help="Tool manual path .txt/.pdf (or TMI_TOOL_MANUAL_PATH)")

    ap.add_argument("--actions", default="", help="Path to actions.json (or TMI_ACTIONS_JSON)")
    ap.add_argument("--catalog", default="", help="Path to artifact_catalog.json (or TMI_ARTIFACT_CATALOG_JSON)")

    ap.add_argument("--template-config", default="", help="Optional template config. If empty, uses repo_root/przma_config.json")
    ap.add_argument("--out-dir", default="", help="Output directory (or TMI_OUT_DIR)")
    ap.add_argument("--run-id", default="", help="Run id (or PRZMA_RUN_ID). If empty, auto-generated.")
    ap.add_argument("--default-agent", default="A1", help="Default agent_id for snapshot collection plan")
    ap.add_argument("--full-trigger", action="store_true", help="Enable full trigger (tool testing): enumerate clickables, click each, trigger snapshot per click")
    ap.add_argument("--full-trigger-target", choices=["discord_web", "telegram_web"], default=None, help="Target application for full trigger (and bootstrap): run full_trigger and main loop on this platform (e.g. Discord)")
    ap.add_argument("--full-trigger-max-clicks", type=int, default=0, help="Max clicks for full trigger (default from config, typically 30)")

    args = ap.parse_args()

    repo_root = ensure_repo_root(Path(__file__).resolve())
    load_dotenv(repo_root / ".env")

    api_key = env_get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: Missing env var OPENAI_API_KEY", file=sys.stderr)
        return 2
    model = env_get("OPENAI_MODEL", "gpt-5.2-thinking")

    tool_name = (args.tool_name or env_get("TMI_TOOL_NAME") or "").strip()
    if not tool_name:
        print("ERROR: tool name is required. Set --tool-name or TMI_TOOL_NAME.", file=sys.stderr)
        return 2
    tool_version = (args.tool_version or env_get("TMI_TOOL_VERSION") or "").strip()

    manual_url = (args.manual_url or env_get("TMI_TOOL_MANUAL_URL") or "").strip()
    manual_path = (args.manual_path or env_get("TMI_TOOL_MANUAL_PATH") or "").strip()
    if not manual_url and not manual_path:
        print(
            "ERROR: Provide tool manual via --manual-url/--manual-path "
            "or env TMI_TOOL_MANUAL_URL / TMI_TOOL_MANUAL_PATH",
            file=sys.stderr,
        )
        return 2

    purpose = "tool_testing"

    # resolve actions/catalog paths
    actions_path_s = (args.actions or env_get("TMI_ACTIONS_JSON") or "").strip()
    catalog_path_s = (args.catalog or env_get("TMI_ARTIFACT_CATALOG_JSON") or "").strip()
    if not actions_path_s:
        actions_path_s = str(repo_root / "shared" / "actions.json")
    if not catalog_path_s:
        catalog_path_s = str(repo_root / "Snapshot_Engine" / "artifact_catalog.json")

    actions_path = Path(actions_path_s)
    catalog_path = Path(catalog_path_s)
    if not actions_path.exists():
        print(f"ERROR: actions.json not found: {actions_path}", file=sys.stderr)
        return 2
    if not catalog_path.exists():
        print(f"ERROR: artifact_catalog.json not found: {catalog_path}", file=sys.stderr)
        return 2

    # run_id
    run_id = (args.run_id or env_get("PRZMA_RUN_ID") or "").strip()
    if not run_id:
        run_id = f"tooltest_{tool_name}_{now_utc_compact()}".replace(" ", "_")

    # output directory
    out_dir_s = (args.out_dir or env_get("TMI_OUT_DIR") or "").strip()
    if not out_dir_s:
        out_dir_s = str(repo_root / "runs" / run_id / "tmi")
    out_dir = Path(out_dir_s)

    # ingest manual -> text
    manual_text, manual_meta = ingest_manual(manual_url or None, manual_path or None)

    actions_json = read_json(actions_path)
    catalog_json = read_json(catalog_path)

    # build tool_plan
    tool_plan = build_tool_plan(
        model=model,
        api_key=api_key,
        tool_name=tool_name,
        tool_version=tool_version,
        purpose=purpose,
        manual_text=manual_text,
        actions_json=actions_json,
        artifact_catalog_json=catalog_json,
        prompts_module=prompts_module,
    )

    if getattr(args, "full_trigger", False):
        tool_plan["full_trigger"] = True
        tp_inner = tool_plan.get("tool_plan")
        if isinstance(tp_inner, dict):
            tp_inner["full_trigger"] = True
            if getattr(args, "full_trigger_max_clicks", 0) > 0:
                tp_inner["full_trigger_max_clicks"] = args.full_trigger_max_clicks
        if getattr(args, "full_trigger_target", None):
            tool_plan["target_application"] = args.full_trigger_target
            if isinstance(tool_plan.get("tool_plan"), dict):
                tool_plan["tool_plan"]["target_application"] = args.full_trigger_target

    # template config (default to repo_root/przma_config.json)
    template_cfg: Optional[Dict[str, Any]] = None
    if args.template_config:
        tp = Path(args.template_config)
        if tp.exists():
            template_cfg = read_json(tp)

    if template_cfg is None:
        default_template_path = repo_root / "przma_config.json"
        if not default_template_path.exists():
            print(f"ERROR: default template not found: {default_template_path}", file=sys.stderr)
            return 2
        template_cfg = read_json(default_template_path)

    interpreted_cfg = compile_interpreted_przma_config(
        run_id=run_id,
        tool_plan=tool_plan,
        template_config=template_cfg,
        default_agent_id=args.default_agent,
    )

    tmi_rules = compile_tmi_rules(
        tool_plan=tool_plan,
        default_agent_id=args.default_agent,
        template_snapshot=(template_cfg.get("snapshot") if isinstance(template_cfg, dict) else None),
    )

    # write outputs
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "tool_plan.json", tool_plan)
    write_json(out_dir / "interpreted_przma_config.json", interpreted_cfg)
    write_json(out_dir / "tmi_rules.json", tmi_rules)

    manifest = {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tool": tool_plan.get("tool_plan", {}).get("tool", {"name": tool_name, "version": tool_version or "unknown"}),
        "run_id": run_id,
        "purpose": purpose,
        "manual_meta": manual_meta,
        "inputs": {
            "actions_json": str(actions_path),
            "artifact_catalog_json": str(catalog_path),
            "model": model,
        },
        "outputs": {
            "tool_plan": str(out_dir / "tool_plan.json"),
            "config": str(out_dir / "interpreted_przma_config.json"),
            "rules": str(out_dir / "tmi_rules.json"),
        },
        "notes": {
            "default_agent_id": args.default_agent,
            "template_config_used": True,
            "platform_ns_map": _PLATFORM_NS_MAP,
        },
    }
    write_json(out_dir / "tmi_manifest.json", manifest)

    print(f"[TMI] OK: wrote outputs to: {out_dir}")
    print(f"  - {out_dir / 'tool_plan.json'}")
    print(f"  - {out_dir / 'interpreted_przma_config.json'}")
    print(f"  - {out_dir / 'tmi_rules.json'}")
    print(f"  - {out_dir / 'tmi_manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
