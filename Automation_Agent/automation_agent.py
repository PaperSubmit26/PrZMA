# PrZMA/Automation_Agent/automation_agent.py
from __future__ import annotations

import argparse
import json
import os
import time
import ast
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set
from collections.abc import Mapping

import requests
import rpyc
from dotenv import load_dotenv

dotenv_path = Path(__file__).resolve().parents[1] / ".env"  # Automation_Agent/.. = PrZMA
load_dotenv(dotenv_path)

# Utils
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_now_ts() -> float:
    return time.time()


def read_json(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)

    def default(o):
        if isinstance(o, (bytes, bytearray)):
            return o.decode("utf-8", errors="replace")
        return str(o)

    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=default) + "\n")


def to_jsonable(x, _depth=0, _max_depth=8):
    if x is None or isinstance(x, (str, int, float, bool)):
        return x
    if isinstance(x, (bytes, bytearray)):
        return x.decode("utf-8", errors="replace")

    if _depth >= _max_depth:
        return str(x)

    try:
        if hasattr(x, "items"):
            return {str(k): to_jsonable(v, _depth + 1) for k, v in x.items()}
    except Exception:
        pass

    if isinstance(x, (list, tuple, set)):
        return [to_jsonable(v, _depth + 1) for v in x]

    return str(x)


def _coerce_result_dict(res: Any) -> Dict[str, Any]:
    if res is None:
        return {}
    if isinstance(res, dict):
        return res
    if isinstance(res, Mapping):
        return dict(res)
    if isinstance(res, str):
        try:
            v = ast.literal_eval(res)
            if isinstance(v, dict):
                return v
        except Exception:
            return {}
    return {}

# Action Space (actions.json)
@dataclass
class ActionSpec:
    name: str
    summary: str
    params_schema: Dict[str, Any]


def load_action_specs(actions_path: Path) -> Dict[str, ActionSpec]:
    data = read_json(actions_path)
    specs: Dict[str, ActionSpec] = {}
    for a in data.get("actions", []):
        name = a["name"]
        specs[name] = ActionSpec(
            name=name,
            summary=a.get("summary", ""),
            params_schema=a.get("params_schema", {"type": "object", "properties": {}, "required": []}),
        )
    return specs


def load_file_catalog(files_path: Path) -> Dict[str, Any]:
    """Load shared/file.json. Return {} if missing."""
    if not files_path.exists():
        return {}
    data = read_json(files_path)
    return data if isinstance(data, dict) else {}


def resolve_file_path(file_catalog: Dict[str, Any], file_key: Optional[str], file_path_param: Optional[str]) -> Optional[str]:
    """
    Resolve file_key or file_path to VM path for upload actions.
    If file_key is set, look up in catalog and return base_path_vm + path; else return file_path_param as-is.
    """
    if file_path_param and str(file_path_param).strip():
        return str(file_path_param).strip()
    if not file_key or not str(file_key).strip():
        return None
    files = (file_catalog or {}).get("files") or []
    base = (file_catalog or {}).get("base_path_vm") or "C:\\VM_Agent\\files"
    base = base.rstrip("\\/")
    for f in files:
        if isinstance(f, dict) and f.get("id") == file_key:
            p = f.get("path") or f.get("name") or ""
            if p:
                return f"{base}\\{p.replace('/', chr(92))}"
    return None


def normalize_vm_path(p: Optional[str]) -> str:
    """Normalize a VM path string for comparisons (Windows-friendly)."""
    if not p:
        return ""
    s = str(p).strip().replace("/", "\\")
    # Collapse duplicate separators best-effort
    while "\\\\" in s:
        s = s.replace("\\\\", "\\")
    return s.lower()


def validate_params(params: Dict[str, Any], schema: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    if schema.get("type") != "object":
        return errs

    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    additional = schema.get("additionalProperties", True)

    for r in required:
        if r not in params:
            errs.append(f"missing required param: '{r}'")

    if additional is False:
        for k in params.keys():
            if k not in props:
                errs.append(f"unknown param not allowed: '{k}'")

    for k, v in params.items():
        ps = props.get(k)
        if not ps:
            continue
        t = ps.get("type")
        if t == "string" and not isinstance(v, str):
            errs.append(f"param '{k}' must be string")
        elif t == "integer" and not isinstance(v, int):
            errs.append(f"param '{k}' must be integer")
        elif t == "number" and not isinstance(v, (int, float)):
            errs.append(f"param '{k}' must be number")
        elif t == "boolean" and not isinstance(v, bool):
            errs.append(f"param '{k}' must be boolean")
        elif t == "object" and not isinstance(v, dict):
            errs.append(f"param '{k}' must be object")
        elif t == "array" and not isinstance(v, list):
            errs.append(f"param '{k}' must be array")

        if isinstance(v, str):
            if "minLength" in ps and len(v) < int(ps["minLength"]):
                errs.append(f"param '{k}' too short")
            if "maxLength" in ps and len(v) > int(ps["maxLength"]):
                errs.append(f"param '{k}' too long")

    return errs

# RPyC VM calls
def connect_vm(host: str, port: int, timeout: int = 180) -> rpyc.Connection:
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


def vm_snapshot_collect(conn: rpyc.Connection, policy: Dict[str, Any]) -> Dict[str, Any]:
    payload = json.dumps(policy, ensure_ascii=False)
    res = conn.root.snapshot_collect(payload)
    return to_jsonable(res)

# OpenAI call
def openai_chat(model: str, api_key: str, messages: List[Dict[str, str]], temperature: float = 0.2) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"model": model, "messages": messages, "temperature": temperature}
    r = requests.post(url, headers=headers, json=body, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI error {r.status_code}: {r.text[:800]}")
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

# LLM choose one clickable index from compact list (for full_trigger)
def _format_clickables_compact(clickables: List[Dict[str, Any]], max_text: int = 45) -> str:
    """One line per item: i\\ttag\\ttext (minimal for token efficiency)."""
    lines = []
    for i, c in enumerate(clickables):
        if not isinstance(c, dict):
            continue
        tag = (c.get("tag") or "")[:20]
        text = (c.get("text") or c.get("selector") or "")[:max_text].replace("\t", " ").replace("\n", " ")
        lines.append(f"{i}\t{tag}\t{text}")
    return "\n".join(lines) if lines else ""


def llm_choose_clickable_index(
    compact_lines: str,
    depth_label: str,
    model: str,
    api_key: str,
) -> Tuple[Optional[int], str]:
    """Ask LLM to choose one index (0-based) from compact clickables. Returns (None, '') if LLM says done, else (index, reason)."""
    if not compact_lines.strip():
        return (None, "")
    sys_content = (
        "You choose which UI element to click in a Discord channel. GOAL: produce REAL changes (message sent, reaction added, reply sent), not just open/close pickers. "
        "PRIORITISE: (1) Elements that COMPLETE an action: Send / Submit / Apply (e.g. Send message, submit reply, click an emoji to ADD reaction to message). "
        "(2) Inside a picker: click an actual emoji/sticker/GIF to apply it, or Send/Done—not just switch tabs. "
        "AVOID: Repeatedly only opening the same entry points (Add Emoji, Open GIF picker, Open sticker picker, More message options) without then selecting and sending. "
        "If the list has a Send button, reply submit, or an emoji/sticker that applies to a message, prefer those. If you only see picker-openers you have already used, reply {\"done\": true} to finish this chain. "
        "If going deeper is needed to complete an action (e.g. open picker -> select one item -> it applies), choose an index; otherwise {\"done\": true}. "
        "Reply with JSON only: {\"index\": N, \"reason\": \"<one short phrase why, e.g. to send message, to add reaction>\"} or {\"done\": true}. No other text."
    )
    user_content = f"Depth: {depth_label} (prefer: Send/Apply/Submit; avoid only opening same pickers again). Clickables (choose one index or done):\n{compact_lines}"
    messages = [{"role": "system", "content": sys_content}, {"role": "user", "content": user_content}]
    out = openai_chat(model=model, api_key=api_key, messages=messages, temperature=0.1)
    try:
        obj = json.loads(out)
        if obj.get("done") is True:
            return (None, "")
        idx = obj.get("index")
        reason = str(obj.get("reason") or "").strip()[:200]
        if isinstance(idx, int) and idx >= 0:
            return (idx, reason)
    except Exception:
        pass
    return (0, "")  # fallback first


# Action space per agent
def action_space_for_agent(specs: Dict[str, ActionSpec], agent_platforms: Any) -> List[ActionSpec]:
    enabled_namespaces = set()

    if isinstance(agent_platforms, dict):
        for plat, cfg in (agent_platforms or {}).items():
            if isinstance(cfg, dict) and cfg.get("enabled", False):
                ns = plat.split("_", 1)[0]
                enabled_namespaces.add(ns)

    elif isinstance(agent_platforms, list):
        for item in agent_platforms:
            if isinstance(item, str):
                enabled_namespaces.add(item.split("_", 1)[0])

    enabled_namespaces.add("browser")
    enabled_namespaces.add("web")

    out = []
    for s in specs.values():
        ns = s.name.split(".", 1)[0]
        if ns in enabled_namespaces:
            out.append(s)
    out.sort(key=lambda x: x.name)
    return out


def format_action_space(spec_list: List[ActionSpec], max_items: int = 120) -> str:
    items = spec_list[:max_items]
    lines = []
    for s in items:
        lines.append(f"- {s.name}: {s.summary}")
        lines.append(f"  params_schema: {json.dumps(s.params_schema, ensure_ascii=False)}")
    return "\n".join(lines)

# Prompts
def format_file_catalog(file_catalog: Dict[str, Any], max_items: int = 80) -> str:
    """Format file catalog for LLM (id, name, description, scenario_hint)."""
    if not file_catalog:
        return "(No file catalog loaded.)"
    files = (file_catalog.get("files") or [])[:max_items]
    lines = []
    for f in files:
        if not isinstance(f, dict):
            continue
        fid = f.get("id", "")
        name = f.get("name", "")
        desc = f.get("description", "")
        hint = f.get("scenario_hint", "")
        lines.append(f"- id={fid!r} name={name!r} | {desc} | scenario_hint={hint}")
    return "\n".join(lines) if lines else "(No files in catalog.)"


def build_agent_system_prompt(
    config: Dict[str, Any],
    agent_id: str,
    spec_list: List[ActionSpec],
    file_catalog: Optional[Dict[str, Any]] = None,
) -> str:
    scenario = (config.get("scenario") or {}).get("objective", "")
    global_prompt = config.get("global_prompt") or {}
    interaction_style = global_prompt.get("interaction_style", [])
    hard_constraints = global_prompt.get("hard_constraints", [])
    completion_criteria = global_prompt.get("completion_criteria", [])

    agent_cfg = (config.get("agents") or {}).get(agent_id) or {}

    persona_raw = agent_cfg.get("persona")
    persona_text = ""
    tone = ""
    rules = []

    if isinstance(persona_raw, dict):
        tone = str(persona_raw.get("tone", ""))
        rules = persona_raw.get("behavior_rules", []) or []
        persona_text = str(persona_raw.get("text") or persona_raw.get("prompt") or "")
    elif isinstance(persona_raw, str):
        persona_text = persona_raw

    run_limits = config.get("run_limits") or {}
    max_actions_total = run_limits.get("max_actions_total")
    max_minutes = run_limits.get("max_minutes")

    return (
        "You are an automation agent controlling ONE VM-bound persona.\n"
        f"Agent: {agent_id}\n\n"
        "High-level scenario objective:\n"
        f"{scenario}\n\n"
        "Global interaction_style:\n"
        f"{json.dumps(interaction_style, ensure_ascii=False)}\n\n"
        "Hard constraints:\n"
        f"{json.dumps(hard_constraints, ensure_ascii=False)}\n\n"
        "Completion criteria (human text):\n"
        f"{json.dumps(completion_criteria, ensure_ascii=False)}\n\n"
        "Run limits (hard stops):\n"
        f"{json.dumps({'max_actions_total': max_actions_total, 'max_minutes': max_minutes}, ensure_ascii=False)}\n\n"
        "Your persona:\n"
        f"- persona_text: {persona_text}\n"
        f"- tone: {tone}\n"
        f"- behavior_rules: {json.dumps(rules, ensure_ascii=False)}\n\n"
        "You MUST choose your next action ONLY from the allowed Action Space below.\n"
        "When you choose an action, your params MUST match that action's params_schema EXACTLY.\n"
        "Do not invent param names.\n\n"
                + (
                    "CRITICAL (full_trigger mode): Full_trigger runs FIRST and explores chat features sequentially (send, edit, delete, reaction, reply, GIF, sticker, upload, pin, options). It runs until max_clicks is reached or no new clickables. Do NOT perform discord.* or telegram.* actions until full_trigger has truly finished. Return done=true only when full_trigger.done appears with stop_reason indicating completion (e.g. max_clicks reached, no_clickables, all_features_done). If full_trigger.done shows only a few clicks_executed and no stop_reason yet, the phase may still be running—do not return done.\n\n"
                    "After full_trigger.done (with completion stop_reason), do NOT call discord.send_message, discord.react_message, discord.reply_message, or discord.upload_file.\n\n"
                    if config.get("run_full_trigger") else ""
                )
                + (
                    "Target application for this run: {}. Prefer performing required_actions on this platform when generating artifacts (e.g. use discord.* or telegram.* actions on that app).\n\n".format(
                        "Discord" if config.get("target_application") == "discord_web" else "Telegram" if config.get("target_application") == "telegram_web" else ""
                    )
                    if config.get("target_application") in ("discord_web", "telegram_web") else ""
                )
                + (
                    "When on Discord: if a modal, voice panel, or overlay is open and blocking the channel, use browser.press with key 'Escape' to close it first. Prefer triggering channel features that produce artifacts: reply, emoji reaction, add/upload (+), @mention.\n"
                    "IMPORTANT: Do NOT call discord.login—login is handled by bootstrap. If you see login page, use discord.goto_channel to navigate back to the channel. Do NOT pass email/password as literal strings like 'A2_DISCORD_EMAIL'—those are environment variable names, not values.\n"
                    "IMPORTANT: For @mention actions (discord.send_message with @username), you MUST ensure the mention is actually selected from the autocomplete dropdown—type @, type the username, wait for autocomplete, then press ArrowDown+Enter or click the first autocomplete option. Do NOT just type @username as plain text—it must be a real Discord mention.\n"
                    "IMPORTANT: Do NOT repeat the same file upload action multiple times. Across ALL agents, each file_key should be uploaded at most once per run. Check recent_actions to see what was already uploaded. If upload failed due to overlay blocking, dismiss the overlay first (browser.press Escape or browser.click on Close button), then retry ONCE.\n\n"
                    if config.get("target_application") == "discord_web" else ""
                )
                + "For discord.upload_file and telegram.upload_file: use file_key (id from File Catalog) or file_path (VM path). Across ALL agents, do NOT upload the same file_key/file_path more than once in the same run.\n\n"
        "Output MUST be STRICT JSON with this schema:\n"
        "{\n"
        '  "action": {"name": "<string or null>", "params": <object>},\n'
        '  "reason": "<one short sentence: WHY you chose this action, e.g. \\"to send a message for coverage\\", \\"to open emoji picker\\">",\n'
        '  "done": <boolean>\n'
        "}\n"
        "Always set reason to a brief explanation of why you chose this action (or why done). If done=true, set action.name=null and action.params={}.\n\n"
        "File Catalog (use file_key in upload actions):\n"
        f"{format_file_catalog(file_catalog or {})}\n\n"
        "Allowed Action Space:\n"
        f"{format_action_space(spec_list)}\n"
    )


def build_agent_user_prompt(step: int, observations: Dict[str, Any], progress: Dict[str, Any]) -> str:
    safe_obs = to_jsonable(observations)
    safe_prog = to_jsonable(progress)
    return (
        f"Step={step}\n"
        "Progress (JSON):\n"
        f"{json.dumps(safe_prog, ensure_ascii=False, indent=2)}\n\n"
        "Observations (JSON):\n"
        f"{json.dumps(safe_obs, ensure_ascii=False, indent=2)}\n\n"
        "Decide the NEXT single action for your agent.\n"
    )

# LLM decision per agent (validate, re-ask if invalid)
def llm_choose_next_action(
    model: str,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    spec_map: Dict[str, ActionSpec],
    max_repairs: int = 2,
) -> Dict[str, Any]:
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
    last_err = None

    for _ in range(max_repairs + 1):
        out = openai_chat(model=model, api_key=api_key, messages=messages)

        try:
            obj = json.loads(out)
        except Exception:
            last_err = "Output is not valid JSON."
            messages.append({"role": "assistant", "content": out})
            messages.append({"role": "user", "content": f"STRICT JSON only. Error: {last_err}\nRe-output ONLY JSON."})
            continue

        if not isinstance(obj, dict) or "done" not in obj or "action" not in obj or "reason" not in obj:
            last_err = "JSON must include keys: done, action, reason."
            messages.append({"role": "assistant", "content": out})
            messages.append({"role": "user", "content": f"Schema error: {last_err}\nRe-output ONLY JSON."})
            continue

        action = obj.get("action") or {}
        name = action.get("name")
        params = action.get("params") or {}

        if obj.get("done") is True:
            obj["action"] = {"name": None, "params": {}}
            if not isinstance(obj.get("reason"), str):
                obj["reason"] = str(obj.get("reason"))
            return obj

        if not isinstance(name, str) or name not in spec_map:
            last_err = f"Action name must be one of allowed actions. got={name}"
            messages.append({"role": "assistant", "content": out})
            messages.append({"role": "user", "content": f"{last_err}\nChoose ONLY from action space. Re-output ONLY JSON."})
            continue

        if not isinstance(params, dict):
            last_err = "action.params must be an object"
            messages.append({"role": "assistant", "content": out})
            messages.append({"role": "user", "content": f"{last_err}\nRe-output ONLY JSON."})
            continue

        schema = spec_map[name].params_schema
        errs = validate_params(params, schema)
        if errs:
            last_err = "Params do not match schema: " + "; ".join(errs)
            messages.append({"role": "assistant", "content": out})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"{last_err}\n"
                        f"Action '{name}' params_schema is:\n{json.dumps(schema, ensure_ascii=False)}\n"
                        "Re-output ONLY JSON with corrected params."
                    ),
                }
            )
            continue

        if not isinstance(obj.get("reason"), str):
            obj["reason"] = str(obj.get("reason"))

        return obj

    raise RuntimeError(f"LLM failed to produce valid action after repairs. last_err={last_err}")

def _sanitize_result_for_log(result: Dict[str, Any], action_name: Optional[str]) -> Dict[str, Any]:
    """For actions that can produce huge payloads (e.g. browser.get_clickables), replace with a short summary to avoid log bloat."""
    if action_name != "browser.get_clickables" or not isinstance(result, dict):
        return result
    outputs = result.get("outputs")
    if not isinstance(outputs, dict):
        return result
    clickables = outputs.get("clickables")
    if not isinstance(clickables, list):
        return result
    # Keep result structure but replace outputs with a copy that summarizes clickables
    out_copy = dict(outputs)
    sample_size = 5
    sample = [
        {"selector": c.get("selector"), "tag": c.get("tag"), "text": (c.get("text") or "")[:60]}
        for c in clickables[:sample_size]
        if isinstance(c, dict)
    ]
    out_copy["clickables"] = {"count": len(clickables), "sample": sample}
    return {**result, "outputs": out_copy}


# Logging helper (count actions consistently)
def log_action(
    action_log_path: Path,
    *,
    run_id: str,
    agent_id: str,
    name: Optional[str],
    params: Dict[str, Any],
    reason: str,
    result: Dict[str, Any],
    kind: str = "action",
) -> None:
    result_to_log = _sanitize_result_for_log(result, name)
    entry = {
        "ts": now_iso(),
        "run_id": run_id,
        "agent_id": agent_id,
        "kind": kind,  # "bootstrap" | "action" | "done" | "error" (optional use)
        "action": {"name": name, "params": params, "reason": reason},
        "result": to_jsonable(result_to_log),
    }
    append_jsonl(action_log_path, entry)

# Bootstrap actions (counted as actions)
def bootstrap_agent(
    run_id: str,
    agent_id: str,
    agent_cfg: Dict[str, Any],
    rendezvous: Dict[str, Any],
    conn: rpyc.Connection,
    action_specs: Dict[str, ActionSpec],
    action_log_path: Path,
) -> int:
    """
    Minimal boot:
    - browser.launch
    - if discord_web: discord.open, discord.login (optional), discord.goto_channel
    Returns: number of executed actions (for counting).
    """
    executed = 0

    # browser.launch
    if "browser.launch" in action_specs:
        bcfg = agent_cfg.get("browser") or {}
        req = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "agent_id": agent_id,
            "action_id": f"act_{agent_id}_{int(time.time()*1000)}",
            "name": "browser.launch",
            "params": {
                "browser_config": {
                    "browser": (bcfg.get("engine") or "chromium"),
                    "channel": (bcfg.get("channel") or "chrome"),
                    "headless": bool(bcfg.get("headless", False)),
                    "user_data_dir": bcfg.get("user_data_dir"),
                    "profile_name": (bcfg.get("profile_name") or "Default"),
                    "locale": (bcfg.get("locale") or "en-US"),
                    "timezone": (bcfg.get("timezone") or "UTC"),
                    "extra_args": list(bcfg.get("extra_args") or []),
                }
            },
        }
        res = vm_execute_action(conn, req)
        log_action(
            action_log_path,
            run_id=run_id,
            agent_id=agent_id,
            name=req["name"],
            params=req["params"],
            reason="bootstrap: launch browser",
            result=res,
            kind="bootstrap",
        )
        executed += 1
        
        # Wait for browser/page to be ready before proceeding with platform actions
        # This prevents "page/context/browser has been closed" errors
        time.sleep(1.0)

    platforms = agent_cfg.get("platforms") or {}
    rendezvous_platform = (rendezvous or {}).get("platform")

    if rendezvous_platform == "discord_web":
        dcfg = platforms.get("discord_web") or {}
        if isinstance(dcfg, dict) and dcfg.get("enabled", False):
            # discord.open
            if "discord.open" in action_specs:
                req = {
                    "schema_version": "1.0.0",
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "action_id": f"act_{agent_id}_{int(time.time()*1000)}",
                    "name": "discord.open",
                    "params": {},
                }
                res = vm_execute_action(conn, req)
                log_action(
                    action_log_path,
                    run_id=run_id,
                    agent_id=agent_id,
                    name=req["name"],
                    params=req["params"],
                    reason="bootstrap: open discord",
                    result=res,
                    kind="bootstrap",
                )
                executed += 1

            # discord.login
            if dcfg.get("login_required", False) and "discord.login" in action_specs:
                cred_ref = dcfg.get("credential_ref")
                if not cred_ref:
                    raise RuntimeError(f"[{agent_id}] discord_web.login_required=true but credential_ref missing")

                email = os.getenv(f"{cred_ref}_EMAIL")
                pwd = os.getenv(f"{cred_ref}_PASSWORD")
                if not email or not pwd:
                    raise RuntimeError(f"Missing env vars for {cred_ref}: {cred_ref}_EMAIL / {cred_ref}_PASSWORD")

                req = {
                    "schema_version": "1.0.0",
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "action_id": f"act_{agent_id}_{int(time.time()*1000)}",
                    "name": "discord.login",
                    "params": {"email": email, "password": pwd},
                }
                res = vm_execute_action(conn, req)
                log_action(
                    action_log_path,
                    run_id=run_id,
                    agent_id=agent_id,
                    name=req["name"],
                    params={"email": "***", "password": "***"},
                    reason="bootstrap: login discord",
                    result=res,
                    kind="bootstrap",
                )
                executed += 1

            # discord.goto_channel
            if "discord.goto_channel" in action_specs:
                channel_url_var = "DISCORD_MEETING_CHANNEL"
                channel_url = os.getenv(channel_url_var)
                if not channel_url:
                    raise RuntimeError(f"Missing env var: {channel_url_var} (Discord channel URL)")

                req = {
                    "schema_version": "1.0.0",
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "action_id": f"act_{agent_id}_{int(time.time()*1000)}",
                    "name": "discord.goto_channel",
                    "params": {"channel_url": channel_url},
                }
                res = vm_execute_action(conn, req)
                log_action(
                    action_log_path,
                    run_id=run_id,
                    agent_id=agent_id,
                    name=req["name"],
                    params=req["params"],
                    reason="bootstrap: enter rendezvous channel",
                    result=res,
                    kind="bootstrap",
                )
                executed += 1

    if rendezvous_platform == "telegram_web":
        tcfg = platforms.get("telegram_web") or {}
        if isinstance(tcfg, dict) and tcfg.get("enabled", False):
            # telegram.open
            if "telegram.open" in action_specs:
                variant = tcfg.get("variant", "k")  # "k" or "a"
                req = {
                    "schema_version": "1.0.0",
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "action_id": f"act_{agent_id}_{int(time.time()*1000)}",
                    "name": "telegram.open",
                    "params": {"variant": variant},
                }
                res = vm_execute_action(conn, req)
                log_action(
                    action_log_path,
                    run_id=run_id,
                    agent_id=agent_id,
                    name=req["name"],
                    params=req["params"],
                    reason="bootstrap: open telegram",
                    result=res,
                    kind="bootstrap",
                )
                executed += 1

            # telegram.select_chat (select chat)
            chat_name_var = "TELEGRAM_MEETING_CHAT"
            chat_name = os.getenv(chat_name_var)
            if chat_name and "telegram.select_chat" in action_specs:
                variant = tcfg.get("variant", "k")
                req = {
                    "schema_version": "1.0.0",
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "action_id": f"act_{agent_id}_{int(time.time()*1000)}",
                    "name": "telegram.select_chat",
                    "params": {"chat": chat_name, "variant": variant},
                }
                res = vm_execute_action(conn, req)
                log_action(
                    action_log_path,
                    run_id=run_id,
                    agent_id=agent_id,
                    name=req["name"],
                    params={"chat": chat_name},
                    reason="bootstrap: enter rendezvous chat",
                    result=res,
                    kind="bootstrap",
                )
                executed += 1

    return executed

# Fetch latest messages (Discord)

def fetch_latest_messages_if_supported(
    run_id: str,
    agent_id: str,
    conn: rpyc.Connection,
    action_specs: Dict[str, ActionSpec],
    limit: int = 10,
) -> Optional[Dict[str, Any]]:
    if "discord.get_latest_messages" not in action_specs:
        return None

    req = {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "agent_id": agent_id,
        "action_id": f"act_{agent_id}_{int(time.time()*1000)}",
        "name": "discord.get_latest_messages",
        "params": {"limit": limit},
    }
    res = vm_execute_action(conn, req)
    if res.get("ok") is True:
        return res.get("outputs")
    return {"error": res.get("error"), "outputs": res.get("outputs", {})}


# Run limits / termination

def read_run_limits(config: Dict[str, Any]) -> Dict[str, Any]:
    rl = config.get("run_limits") or {}
    # defaults: safe but permissive
    max_actions_total = rl.get("max_actions_total")
    max_minutes = rl.get("max_minutes")

    # normalize
    if max_actions_total is None:
        max_actions_total = 30
    try:
        max_actions_total = int(max_actions_total)
    except Exception:
        max_actions_total = 30

    if max_minutes is None:
        max_minutes = 15
    try:
        max_minutes = int(max_minutes)
    except Exception:
        max_minutes = 15

    return {"max_actions_total": max_actions_total, "max_minutes": max_minutes}


def hard_stop_reached(start_ts: float, max_minutes: int) -> bool:
    if max_minutes <= 0:
        return False
    return (utc_now_ts() - start_ts) >= (max_minutes * 60)

def _clickable_to_feature_key(item: Dict[str, Any]) -> Optional[str]:
    """Map a clickable element to a coarse 'feature' key to reduce repeats."""
    if not isinstance(item, dict):
        return None
    text = ((item.get("text") or "") + " " + (item.get("selector") or "")).lower()
    # Reactions
    if "thumbsup" in text or "👍" in (item.get("text") or ""):
        return "reaction_thumbsup"
    if "thumbsdown" in text or "👎" in (item.get("text") or ""):
        return "reaction_thumbsdown"
    if "click to react" in text or "add reaction" in text:
        return "reaction_add"
    # GIF
    if "gif" in text:
        return "gif"
    # Stickers
    if "sticker" in text:
        return "sticker"
    # Emoji picker entry
    if "add emoji" in text:
        return "add_emoji"
    # Reply
    if "reply" in text and "reply to" not in text:
        return "reply"
    # Message options
    if "more message options" in text:
        return "more_options"
    # Upload/attach
    if "upload" in text or "attach" in text or "open.*file" in text:
        return "upload"
    # Pin
    if "pin" in text:
        return "pin"
    return None


def _is_send_like_button(item: Dict[str, Any]) -> bool:
    """True if the element looks like Send/Submit/Apply (for message/emoji/sticker submit)."""
    if not isinstance(item, dict):
        return False
    text = ((item.get("text") or "") + " " + (item.get("selector") or "")).lower()
    return any(
        x in text
        for x in (
            "send",
            "submit",
            "post",
            "apply",
            "done",
            "aria-label=\"send",
            "aria-label=\"submit",
            "type=\"submit\"",
        )
    )


def _full_trigger_priority(item: Dict[str, Any]) -> int:
    """Higher = try earlier. Prioritize: close, then SEND/SUBMIT/APPLY (artifact-producing), then reply/emoji/+, then picker-openers last."""
    if not isinstance(item, dict):
        return 0
    text = ((item.get("text") or "") + " " + (item.get("selector") or "")).lower()
    if not text:
        return 0
    # Close/dismiss first so we don't get stuck in overlays
    if any(x in text for x in ("close", "x", "esc", "[aria-label=\"close\"]", "dismiss")):
        return 100
    # HIGHEST: Elements that COMPLETE an action (Send, Submit, Apply) -> real artifact change
    if _is_send_like_button(item):
        return 95
    # Reply (often leads to submit) and reaction apply
    if any(x in text for x in ("reply", "react", "add reaction")):
        return 85
    # Chat input / message area (typing then send)
    selector = (item.get("selector") or "").lower()
    is_in_chat_area = any(x in selector for x in [
        "chatcontent", "messagecontent", "form", "channeltextarea",
        "chat_f75fb0", "content_f75fb0", "main.chatcontent", "inner__74017"
    ])
    if is_in_chat_area:
        return 75
    # Picker-openers only: lower priority so we don't cycle Add Emoji / GIF / Sticker forever
    if any(x in text for x in ("open gif picker", "open sticker picker", "add emoji", "more message options")):
        return 35
    # Other chat-related (upload, @, emoji picker tab, sticker list item)
    if any(x in text for x in ("upload", "attach", "@", "mention", "emoji", "sticker", "gif", "+")):
        return 50
    # Voice/panel: deprioritize
    if any(x in text for x in ("voice", "mic", "mute")):
        return 10
    return 0


def _press_escape(conn: rpyc.Connection, run_id: str, agent_id: str) -> None:
    """Send Escape key to dismiss overlays/modals (e.g. Discord voice panel)."""
    try:
        vm_execute_action(
            conn,
            {
                "schema_version": "1.0.0",
                "run_id": run_id,
                "agent_id": agent_id,
                "action_id": f"act_ft_esc_{agent_id}_{int(time.time()*1000)}",
                "name": "browser.press",
                "params": {"key": "Escape"},
            },
        )
    except Exception:
        pass


def _run_full_trigger_llm(
    *,
    run_id: str,
    agent_id: str,
    conn: rpyc.Connection,
    action_log_path: Path,
    base_url_final: str,
    max_clicks: int,
    wait_after_click_sec: float,
    model: str,
    api_key: str,
) -> int:
    """Full trigger: explore UI actions with depth-chaining and feature-level dedupe."""
    executed = 0
    total_clicks = 0  # Count ALL click actions (browser.click, browser.smart_click) for max_clicks limit
    MAX_DEPTH = 6
    clicked_by_depth = [set() for _ in range(MAX_DEPTH + 1)]  # index 1..6
    done_features: set = set()  # feature keys already executed
    stop_reason = "max_clicks"

    for step in range(max_clicks):
        _press_escape(conn, run_id, agent_id)
        time.sleep(0.2)
        # Only navigate if we're not already in the target chat
        # For Telegram: check if current URL matches base_url_final (or is in the same chat)
        try:
            # Get current URL first
            current_url_res = vm_execute_action(conn, {"schema_version": "1.0.0", "run_id": run_id, "agent_id": agent_id, "action_id": f"act_ft_llm_gc_url_{agent_id}_{int(time.time()*1000)}_{step}", "name": "browser.get_clickables", "params": {"timeout_ms": 3000}})
            current_url = ""
            if isinstance(current_url_res, dict):
                outputs = current_url_res.get("outputs", {})
                if isinstance(outputs, dict):
                    current_url = outputs.get("current_url", "").strip()
            
            # For Telegram: if base_url_final has # (chat ID), check if current URL is in the same chat
            # Don't navigate if we're already in the target chat
            should_navigate = True
            if base_url_final and "web.telegram.org" in base_url_final.lower():
                if "/#" in base_url_final:
                    # Extract chat ID from base_url_final (e.g., #8468503735)
                    chat_id = base_url_final.split("/#")[-1] if "/#" in base_url_final else ""
                    if chat_id and current_url and f"/#{chat_id}" in current_url:
                        should_navigate = False  # Already in the target chat
                elif current_url == base_url_final:
                    should_navigate = False  # Already at the same URL
            
            if should_navigate:
                try:
                    vm_execute_action(conn, {"schema_version": "1.0.0", "run_id": run_id, "agent_id": agent_id, "action_id": f"act_ft_llm_{agent_id}_{int(time.time()*1000)}_{step}", "name": "browser.goto", "params": {"url": base_url_final, "timeout_ms": 15000}})
                except Exception:
                    pass
        except Exception:
            # Fallback: navigate anyway if URL check fails
            try:
                vm_execute_action(conn, {"schema_version": "1.0.0", "run_id": run_id, "agent_id": agent_id, "action_id": f"act_ft_llm_{agent_id}_{int(time.time()*1000)}_{step}", "name": "browser.goto", "params": {"url": base_url_final, "timeout_ms": 15000}})
            except Exception:
                pass
        time.sleep(0.2)

        res = vm_execute_action(conn, {"schema_version": "1.0.0", "run_id": run_id, "agent_id": agent_id, "action_id": f"act_ft_llm_gc_{agent_id}_{int(time.time()*1000)}_{step}", "name": "browser.get_clickables", "params": {"timeout_ms": 5000}})
        outputs = (res or {}).get("outputs", {})
        all_clickables = [c for c in (outputs.get("clickables") or []) if isinstance(c, dict) and c.get("selector")]
        
        # Filter: Focus on chat interaction elements (not chat list or menu)
        # For Telegram: prioritize elements in chat area (column-center, message input, emoji/sticker/upload buttons)
        # Exclude: chat list (LeftColumn), menu buttons, "New Message", "Return to chat list", etc.
        list_d1 = []
        for c in all_clickables:
            selector = (c.get("selector") or "").lower()
            text = (c.get("text") or "").lower()
            
            # Skip if already clicked at depth 1
            if c.get("selector") in clicked_by_depth[1]:
                continue
            
            # Skip chat list and menu elements
            skip_patterns = [
                "#leftcolumn", "#column-left", ".leftcolumn", ".chat-list", ".chatlist",
                "new message", "return to chat list", "chat list", "menu",
                "aria-label=\"new", "aria-label=\"return", "newchatbutton",
                ".listitem-button", "chat-item-clickable", "contact-list-item",
            ]
            if any(pattern in selector or pattern in text for pattern in skip_patterns):
                continue
            
            # Prioritize chat interaction elements (message input, emoji, sticker, upload, send, etc.)
            # These are in column-center or are interaction buttons
            is_chat_interaction = any(x in selector or x in text for x in [
                "column-center", "#column-center", ".column-center",
                "contenteditable", "textbox", "message-input", "input-message",
                "emoji", "sticker", "gif", "upload", "attach", "send",
                "reply", "react",
                "button", "input", "textarea",  # General interactive elements
            ])
            
            # If not explicitly a chat interaction element, check if it's likely in chat area
            # Telegram Web structure: LeftColumn (sidebar) vs ColumnCenter (chat area)
            if not is_chat_interaction:
                # Skip if clearly in sidebar (LeftColumn)
                if any(x in selector for x in ["#leftcolumn", ".leftcolumn", "left-column", ".chat-list"]):
                    continue
                # Skip if text contains multiple menu items (indicates sidebar menu)
                menu_keywords = ["saved messages", "contacts", "settings", "more", "add account", "my profile"]
                if text and len([x for x in menu_keywords if x in text]) >= 2:
                    continue
                # Skip generic Transition containers that span full height (likely sidebar)
                if any(x in selector for x in ["transition.full-height", ".full-height"]) and "column-center" not in selector:
                    # But allow if it's in column-center area
                    if "#column-center" not in selector and ".column-center" not in selector:
                        continue
                # Allow buttons, inputs, and clickable elements that are not in sidebar
                if any(x in selector for x in ["button", "input", "textarea", "[role=", "[aria-"]):
                    # Likely an interactive element in chat area
                    is_chat_interaction = True
            
            if is_chat_interaction:
                list_d1.append(c)
        
        # Feature-level dedupe: skip already executed feature keys.
        list_d1 = [c for c in list_d1 if _clickable_to_feature_key(c) not in done_features]
        if not list_d1:
            stop_reason = "no_clickables"
            break
        # Sort so Send/Submit/Apply appear first
        list_d1 = sorted(list_d1, key=lambda x: (-_full_trigger_priority(x), 0))
        compact1 = _format_clickables_compact(list_d1)
        idx1, chain_reason = llm_choose_clickable_index(compact1, "depth1", model, api_key)
        # If LLM returns done, fall back to the top-priority candidate to keep exploring.
        if idx1 is None:
            idx1 = 0
        idx1 = min(max(0, idx1), len(list_d1) - 1)
        item1 = list_d1[idx1]
        selector1 = item1.get("selector")
        if not selector1:
            continue
        try:
            vm_execute_action(conn, {"schema_version": "1.0.0", "run_id": run_id, "agent_id": agent_id, "action_id": f"act_ft_llm_d1_{agent_id}_{int(time.time()*1000)}_{step}", "name": "browser.click", "params": {"selector": selector1, "timeout_ms": 8000}})
            total_clicks += 1  # Count this browser.click
        except Exception as e:
            log_action(action_log_path, run_id=run_id, agent_id=agent_id, name="browser.full_trigger_click", params={"selector": selector1, "error": str(e)}, reason="full_trigger (LLM depth1 click failed)", result={"ok": False, "error": str(e)}, kind="full_trigger")
            executed += 1
            total_clicks += 1  # Count failed click attempt
            time.sleep(wait_after_click_sec)
            continue
        clicked_by_depth[1].add(selector1)
        time.sleep(0.4)

        # Depth 2..6: LLM chooses the next click in the chain.
        # Focus on chat interaction elements (same filtering as depth 1)
        chain_selectors: List[str] = [selector1]
        for depth_level in range(2, MAX_DEPTH + 1):
            res_d = vm_execute_action(conn, {"schema_version": "1.0.0", "run_id": run_id, "agent_id": agent_id, "action_id": f"act_ft_llm_d{depth_level}_gc_{agent_id}_{int(time.time()*1000)}_{step}", "name": "browser.get_clickables", "params": {"timeout_ms": 3000}})
            outputs_d = (res_d or {}).get("outputs", {})
            already_in_chain = set(chain_selectors)
            all_d = [c for c in (outputs_d.get("clickables") or []) if isinstance(c, dict) and c.get("selector")]
            
            # Filter: Focus on chat interaction (same logic as depth 1)
            list_d = []
            for c in all_d:
                selector = (c.get("selector") or "").lower()
                text = (c.get("text") or "").lower()
                
                # Skip if already clicked or in chain
                if c.get("selector") in clicked_by_depth[depth_level] or c.get("selector") in already_in_chain:
                    continue
                
                # Skip chat list and menu elements
                skip_patterns = [
                    "#leftcolumn", "#column-left", ".leftcolumn", ".chat-list", ".chatlist",
                    "new message", "return to chat list", "chat list", "menu",
                    "aria-label=\"new", "aria-label=\"return", "newchatbutton",
                    ".listitem-button", "chat-item-clickable", "contact-list-item",
                ]
                if any(pattern in selector or pattern in text for pattern in skip_patterns):
                    continue
                
                # Prioritize chat interaction elements
                is_chat_interaction = any(x in selector or x in text for x in [
                    "column-center", "#column-center", ".column-center",
                    "contenteditable", "textbox", "message-input", "input-message",
                    "emoji", "sticker", "gif", "upload", "attach", "send",
                    "reply", "react",
                    "button", "input", "textarea",  # General interactive elements
                ])
                
                # If not explicitly a chat interaction element, check if it's likely in chat area
                if not is_chat_interaction:
                    # Skip if clearly in sidebar (LeftColumn)
                    if any(x in selector for x in ["#leftcolumn", ".leftcolumn", "left-column", ".chat-list"]):
                        continue
                    # Skip if text contains multiple menu items (indicates sidebar menu)
                    menu_keywords = ["saved messages", "contacts", "settings", "more", "add account", "my profile"]
                    if text and len([x for x in menu_keywords if x in text]) >= 2:
                        continue
                    # Skip generic Transition containers that span full height (likely sidebar)
                    if any(x in selector for x in ["transition.full-height", ".full-height"]) and "column-center" not in selector:
                        # But allow if it's in column-center area
                        if "#column-center" not in selector and ".column-center" not in selector:
                            continue
                    # Allow buttons, inputs, and clickable elements that are not in sidebar
                    if any(x in selector for x in ["button", "input", "textarea", "[role=", "[aria-"]):
                        # Likely an interactive element in chat area
                        is_chat_interaction = True
                
                if is_chat_interaction:
                    list_d.append(c)
            if not list_d:
                break
            list_d = sorted(list_d, key=lambda x: (-_full_trigger_priority(x), 0))
            compact_d = _format_clickables_compact(list_d)
            idx_d, _ = llm_choose_clickable_index(compact_d, f"depth{depth_level}", model, api_key)
            if idx_d is None:
                break
            idx_d = min(max(0, idx_d), len(list_d) - 1)
            sel_d = list_d[idx_d].get("selector")
            if not sel_d:
                break
            try:
                vm_execute_action(conn, {"schema_version": "1.0.0", "run_id": run_id, "agent_id": agent_id, "action_id": f"act_ft_llm_d{depth_level}_{agent_id}_{int(time.time()*1000)}_{step}", "name": "browser.click", "params": {"selector": sel_d, "timeout_ms": 5000}})
                total_clicks += 1  # Count this browser.click
            except Exception:
                break
            clicked_by_depth[depth_level].add(sel_d)
            chain_selectors.append(sel_d)
            time.sleep(0.3)

        # If we went into a picker (depth > 1), ensure we actually send/apply so artifact is left (emoji in message, sticker, etc.)
        if len(chain_selectors) > 1:
            time.sleep(0.5)  # let picker close / UI update so Send button is visible
            try:
                res_send = vm_execute_action(conn, {"schema_version": "1.0.0", "run_id": run_id, "agent_id": agent_id, "action_id": f"act_ft_llm_send_{agent_id}_{int(time.time()*1000)}_{step}", "name": "browser.get_clickables", "params": {"timeout_ms": 3000}})
                out_send = (res_send or {}).get("outputs", {}) or {}
                cands = [c for c in (out_send.get("clickables") or []) if isinstance(c, dict) and c.get("selector")]
                send_btns = [c for c in cands if _full_trigger_priority(c) >= 95]
                if not send_btns:
                    send_btns = [c for c in cands if _is_send_like_button(c)]
                if send_btns:
                    send_btn = send_btns[0]
                    vm_execute_action(conn, {"schema_version": "1.0.0", "run_id": run_id, "agent_id": agent_id, "action_id": f"act_ft_llm_send_click_{agent_id}_{int(time.time()*1000)}_{step}", "name": "browser.click", "params": {"selector": send_btn.get("selector"), "timeout_ms": 5000}})
                    total_clicks += 1  # Count this browser.click (Send button)
                    time.sleep(0.3)
                else:
                    # No Send button found (e.g. icon-only or different locale): try Enter so message with emoji is sent
                    try:
                        vm_execute_action(conn, {"schema_version": "1.0.0", "run_id": run_id, "agent_id": agent_id, "action_id": f"act_ft_llm_send_enter_{agent_id}_{int(time.time()*1000)}_{step}", "name": "browser.press", "params": {"key": "Enter"}})
                        time.sleep(0.3)
                    except Exception:
                        pass
            except Exception:
                pass

        params_log = {"selector": selector1, "tag": item1.get("tag"), "text": (item1.get("text") or "")[:80], "index": step, "chain_depth": len(chain_selectors)}
        for d in range(2, len(chain_selectors) + 1):
            params_log[f"depth{d}_selector"] = chain_selectors[d - 1]
        result_log = {"ok": True, "selector": selector1, "chain_depth": len(chain_selectors)}
        log_reason = chain_reason if chain_reason else "full_trigger: snapshot after feature execution (LLM-chosen, depth up to 6)"
        log_action(action_log_path, run_id=run_id, agent_id=agent_id, name="browser.full_trigger_click", params=params_log, reason=log_reason, result=result_log, kind="full_trigger")
        executed += 1
        
        # Check if we've reached max_clicks by counting ALL click actions (browser.click, browser.smart_click)
        if total_clicks >= max_clicks:
            stop_reason = "max_clicks"
            break
        # Track executed feature keys for dedupe.
        fk = _clickable_to_feature_key(item1)
        if fk:
            done_features.add(fk)
        for sel in chain_selectors:
            sel_lower = (sel or "").lower()
            if "thumbsup" in sel_lower and "react" in sel_lower:
                done_features.add("reaction_thumbsup")
            elif "thumbsdown" in sel_lower and "react" in sel_lower:
                done_features.add("reaction_thumbsdown")
            elif "gif" in sel_lower:
                done_features.add("gif")
            elif "sticker" in sel_lower:
                done_features.add("sticker")
        time.sleep(0.2)
        # Reset to base_url_final only if we're not already there (same logic as above)
        try:
            current_url_res = vm_execute_action(conn, {"schema_version": "1.0.0", "run_id": run_id, "agent_id": agent_id, "action_id": f"act_ft_llm_reset_url_{agent_id}_{int(time.time()*1000)}_{step}", "name": "browser.get_clickables", "params": {"timeout_ms": 2000}})
            current_url = ""
            if isinstance(current_url_res, dict):
                outputs = current_url_res.get("outputs", {})
                if isinstance(outputs, dict):
                    current_url = outputs.get("current_url", "").strip()
            
            should_reset = True
            if base_url_final and "web.telegram.org" in base_url_final.lower():
                if "/#" in base_url_final:
                    chat_id = base_url_final.split("/#")[-1] if "/#" in base_url_final else ""
                    if chat_id and current_url and f"/#{chat_id}" in current_url:
                        should_reset = False
                elif current_url == base_url_final:
                    should_reset = False
            
            if should_reset:
                try:
                    vm_execute_action(conn, {"schema_version": "1.0.0", "run_id": run_id, "agent_id": agent_id, "action_id": f"act_ft_llm_reset_{agent_id}_{int(time.time()*1000)}_{step}", "name": "browser.goto", "params": {"url": base_url_final, "timeout_ms": 15000}})
                except Exception:
                    pass
        except Exception:
            # Fallback: reset anyway
            try:
                vm_execute_action(conn, {"schema_version": "1.0.0", "run_id": run_id, "agent_id": agent_id, "action_id": f"act_ft_llm_reset_{agent_id}_{int(time.time()*1000)}_{step}", "name": "browser.goto", "params": {"url": base_url_final, "timeout_ms": 15000}})
            except Exception:
                pass
    return (executed, stop_reason, total_clicks)


def run_full_trigger_phase(
    *,
    run_id: str,
    agent_id: str,
    conn: rpyc.Connection,
    action_log_path: Path,
    base_url: Optional[str],
    max_clicks: int,
    wait_after_click_sec: float,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    use_llm: bool = True,
) -> int:
    """
    Full trigger (tool testing): get clickables, then for each step —
    goto(base_url) → click(depth1) → … → click(depthK) → log browser.full_trigger_click.
    If use_llm and model/api_key: LLM chooses at each depth; can go up to depth 5 (e.g. emoji picker → select → send). LLM replies {\"done\": true} to end chain.
    Else: rule-based priority order, depth 3 fixed.
    Returns number of full_trigger_click actions executed.
    """
    # Use browser.get_clickables directly - it already handles Discord page loading and waiting
    # Retry logic: if get_clickables returns 0, retry with longer waits
    max_retries = 3
    res = None
    clickables = []
    url = base_url or ""
    
    for retry_idx in range(max_retries):
        # If base_url is provided, navigate to it first
        # Discord: direct URL navigation
        # Telegram: URL navigation (chat URL like web.telegram.org/k/#-1234567890)
        if base_url:
            url_lower = base_url.lower()
            is_telegram = "web.telegram.org" in url_lower or "t.me" in url_lower
            is_discord = "discord.com" in url_lower
            
            if is_discord or is_telegram:
                try:
                    vm_execute_action(
                        conn,
                        {
                            "schema_version": "1.0.0",
                            "run_id": run_id,
                            "agent_id": agent_id,
                            "action_id": f"act_{agent_id}_{int(time.time()*1000)}_wait_{retry_idx}",
                            "name": "browser.goto",
                            "params": {"url": base_url, "timeout_ms": 20000, "wait": "domcontentloaded"},
                        },
                    )
                    # Brief wait for page load
                    time.sleep(1.0)
                except Exception:
                    pass
        # If base_url is None, we rely on browser.get_clickables to work on current page
        # (which should be Discord channel or Telegram chat after bootstrap)
        
        # Call browser.get_clickables - it handles Discord page loading internally
        # Use the same timeout logic as successful LLM calls (30s default, increase on retry)
        req = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "agent_id": agent_id,
            "action_id": f"act_{agent_id}_{int(time.time()*1000)}_{retry_idx}",
            "name": "browser.get_clickables",
            "params": {"timeout_ms": 30000 + (retry_idx * 10000)},  # 30s, 40s, 50s
        }
        try:
            res = vm_execute_action(conn, req)
            # ActionResult structure: {ok, outputs: {clickables, current_url}, ...}
            outputs = res.get("outputs", {}) if isinstance(res, dict) else {}
            clickables = outputs.get("clickables", []) if isinstance(outputs, dict) else []
            url = (outputs.get("current_url", "") or base_url or "").strip() if isinstance(outputs, dict) else (base_url or "")
            
            # If we got clickables, break out of retry loop
            if len(clickables) > 0:
                if retry_idx > 0:
                    log_action(
                        action_log_path,
                        run_id=run_id,
                        agent_id=agent_id,
                        name="full_trigger.get_clickables_retry_success",
                        params={"retry_count": retry_idx, "clickables_count": len(clickables)},
                        reason=f"Full trigger: get_clickables succeeded after {retry_idx} retries",
                        result={"ok": True, "retry_count": retry_idx, "clickables_count": len(clickables)},
                        kind="full_trigger",
                    )
                break
            
            # If no clickables and not last retry, log and continue
            if retry_idx < max_retries - 1:
                log_action(
                    action_log_path,
                    run_id=run_id,
                    agent_id=agent_id,
                    name="full_trigger.get_clickables_retry",
                    params={"retry_count": retry_idx + 1, "url": url},
                    reason=f"Full trigger: get_clickables returned 0 clickables, retrying ({retry_idx + 1}/{max_retries})",
                    result={"ok": False, "retry_count": retry_idx + 1, "will_retry": True},
                    kind="full_trigger",
                )
                time.sleep(2.0)  # Brief wait before retry
                
        except Exception as e:
            if retry_idx < max_retries - 1:
                log_action(
                    action_log_path,
                    run_id=run_id,
                    agent_id=agent_id,
                    name="full_trigger.get_clickables_retry_error",
                    params={"retry_count": retry_idx + 1, "error": str(e)},
                    reason=f"Full trigger: get_clickables RPC failed, retrying ({retry_idx + 1}/{max_retries})",
                    result={"ok": False, "error": str(e), "will_retry": True},
                    kind="full_trigger",
                )
                time.sleep(2.0)
            else:
                log_action(
                    action_log_path,
                    run_id=run_id,
                    agent_id=agent_id,
                    name="full_trigger.get_clickables_failed",
                    params={"error": str(e), "retry_count": max_retries},
                    reason="Full trigger: get_clickables RPC failed after all retries",
                    result={"ok": False, "error": str(e)},
                    kind="full_trigger",
                )
                return (0, "get_clickables_failed")

    # Extract debug info and error from outputs if available
    outputs = res.get("outputs", {}) if isinstance(res, dict) else {}
    debug_info = outputs.get("debug", {}) if isinstance(outputs, dict) else {}
    error_msg = res.get("error", "") or outputs.get("error", "") if isinstance(res, dict) else ""
    
    if not url:
        # Retry: try to get URL from browser or env
        log_action(
            action_log_path,
            run_id=run_id,
            agent_id=agent_id,
            name="full_trigger.no_base_url_retry",
            params={"res_url": res.get("current_url") if isinstance(res, dict) else None, "base_url": base_url},
            reason="Full trigger: no base URL available, retrying detection",
            result={"ok": False, "error": "No URL", "retrying": True},
            kind="full_trigger",
        )
        # Last resort: try to navigate and get current URL from browser
        # Note: Do NOT use DISCORD_MEETING_CHANNEL here - this function is platform-agnostic
        # The caller (execute_full_trigger_if_needed) should handle platform-specific env vars
        try:
            vm_execute_action(
                conn,
                {
                    "schema_version": "1.0.0",
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "action_id": f"act_{agent_id}_{int(time.time()*1000)}_retry_url",
                    "name": "browser.get_clickables",
                    "params": {"timeout_ms": 30000},
                },
            )
            retry_res = vm_execute_action(
                conn,
                {
                    "schema_version": "1.0.0",
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "action_id": f"act_{agent_id}_{int(time.time()*1000)}_retry_url2",
                    "name": "browser.get_clickables",
                    "params": {"timeout_ms": 30000},
                },
            )
            if isinstance(retry_res, dict):
                retry_url = retry_res.get("current_url", "").strip()
                if retry_url:
                    url = retry_url
                    log_action(
                        action_log_path,
                        run_id=run_id,
                        agent_id=agent_id,
                        name="full_trigger.base_url_retry_browser_success",
                        params={"detected_url": url},
                        reason="Full trigger: base_url detected from browser after retry",
                        result={"ok": True, "base_url": url},
                        kind="full_trigger",
                    )
        except Exception as e:
                log_action(
                    action_log_path,
                    run_id=run_id,
                    agent_id=agent_id,
                    name="full_trigger.base_url_retry_failed",
                    params={"error": str(e)},
                    reason="Full trigger: base_url retry failed",
                    result={"ok": False, "error": str(e)},
                    kind="full_trigger",
                )
        
        # If still no URL after retry, give up
        if not url:
            outputs_final = res.get("outputs", {}) if isinstance(res, dict) else {}
            res_url = outputs_final.get("current_url", "") if isinstance(outputs_final, dict) else None
            log_action(
                action_log_path,
                run_id=run_id,
                agent_id=agent_id,
                name="full_trigger.no_base_url_final",
                params={"res_url": res_url, "base_url": base_url},
                reason="Full trigger: no base URL available after retry",
                result={"ok": False, "error": "No URL after retry"},
                kind="full_trigger",
            )
            return (0, "no_base_url")
    if len(clickables) == 0:
        log_action(
            action_log_path,
            run_id=run_id,
            agent_id=agent_id,
            name="full_trigger.no_clickables",
            params={
                "url": url,
                "debug": debug_info,
                "error": error_msg,
                "response_keys": list(res.keys()) if isinstance(res, dict) else [],
            },
            reason="Full trigger: get_clickables returned 0 clickables",
            result={"ok": False, "error": "No clickables found", "debug": debug_info, "error_detail": error_msg},
            kind="full_trigger",
        )
        return (0, "no_clickables")
    base_url_final = url
    # Dismiss any open overlay (voice panel, modal) so we start from main channel view
    _press_escape(conn, run_id, agent_id)
    time.sleep(0.5)
    # Prioritize: close/X first, then reply/emoji/+/@/upload, then rest; deprioritize voice-detail links
    to_do = sorted(clickables, key=lambda x: (-_full_trigger_priority(x), 0))[: max(1, int(max_clicks))]
    executed = 0

    if use_llm and model and api_key:
        result = _run_full_trigger_llm(
            run_id=run_id,
            agent_id=agent_id,
            conn=conn,
            action_log_path=action_log_path,
            base_url_final=base_url_final,
            max_clicks=max(1, int(max_clicks)),
            wait_after_click_sec=wait_after_click_sec,
            model=model,
            api_key=api_key,
        )
        # _run_full_trigger_llm returns (executed, stop_reason, total_clicks)
        if isinstance(result, tuple) and len(result) == 3:
            executed, stop_reason, total_clicks = result
            return (executed, stop_reason)
        elif isinstance(result, tuple) and len(result) == 2:
            return result
        else:
            return (result, "max_clicks")

    # Rule-based: track already-clicked selectors at depth2/depth3; return (executed, stop_reason)
    clicked_depth2_selectors = set()
    clicked_depth3_selectors = set()
    message_accessory_depth3_count = {}
    for i, item in enumerate(to_do):
        selector = item.get("selector") if isinstance(item, dict) else None
        if not selector:
            continue
        tag = item.get("tag", "") if isinstance(item, dict) else ""
        text = (item.get("text") or "")[:80] if isinstance(item, dict) else ""
        # Dismiss overlay from previous click, then reset to base page
        _press_escape(conn, run_id, agent_id)
        time.sleep(0.3)
        # Reset to base page (in case previous click navigated away)
        try:
            vm_execute_action(
                conn,
                {
                    "schema_version": "1.0.0",
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "action_id": f"act_ft_{agent_id}_{int(time.time()*1000)}_{i}",
                    "name": "browser.goto",
                    "params": {"url": base_url_final, "timeout_ms": 15000},
                },
            )
        except Exception:
            pass
        time.sleep(0.3)
        # Click the element
        try:
            vm_execute_action(
                conn,
                {
                    "schema_version": "1.0.0",
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "action_id": f"act_ft_click_{agent_id}_{int(time.time()*1000)}_{i}",
                    "name": "browser.click",
                    "params": {"selector": selector, "timeout_ms": 8000},
                },
            )
        except Exception as e:
            log_action(
                action_log_path,
                run_id=run_id,
                agent_id=agent_id,
                name="browser.full_trigger_click",
                params={"selector": selector, "tag": tag, "text": text, "error": str(e)},
                reason="full_trigger (click failed)",
                result={"ok": False, "error": str(e)},
                kind="full_trigger",
            )
            executed += 1
            time.sleep(wait_after_click_sec)
            continue
        
        # Wait for the click to take effect (Discord UI update)
        time.sleep(0.7)
        
        # DEPTH 2 CLICKING: After first click, try to click a child element if available
        # This allows us to go deeper (e.g., sticker picker -> select sticker)
        # Only perform depth 2 click if the first click opened a picker/menu (not just any element)
        depth2_clicked = False
        depth2_selector = None
        try:
            # Check if first click opened a picker/menu by looking for specific UI patterns
            # Brief wait for picker/menu to appear
            time.sleep(0.3)
            
            # Get new clickables after first click (might have opened a menu/picker)
            depth2_res = vm_execute_action(
                conn,
                {
                    "schema_version": "1.0.0",
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "action_id": f"act_ft_depth2_{agent_id}_{int(time.time()*1000)}_{i}",
                    "name": "browser.get_clickables",
                    "params": {"timeout_ms": 3000},  # Shorter timeout for depth 2
                },
            )
            if isinstance(depth2_res, dict):
                outputs = depth2_res.get("outputs", {})
                depth2_clickables = outputs.get("clickables", []) if isinstance(outputs, dict) else []
                if depth2_clickables and len(depth2_clickables) > 0:
                    # Filter for interactive elements (buttons, clickable items in pickers/menus)
                    interactive_items = [
                        item for item in depth2_clickables
                        if isinstance(item, dict) and item.get("selector")
                    ]
                    
                    # Check if first click likely opened a picker/menu by examining the selector/text
                    first_click_selector_lower = selector.lower()
                    first_click_text_lower = text.lower()
                    likely_opened_picker = any(x in first_click_selector_lower or x in first_click_text_lower 
                                               for x in ["picker", "emoji", "sticker", "gif", "expression", "button", "menu", "popup", "modal"])
                    
                    # Filter for items that seem to be in a picker/menu (not existing page elements)
                    # Exclude elements that are likely from existing messages (message-accessories, etc.)
                    picker_items = [
                        item for item in interactive_items
                        if isinstance(item, dict) and item.get("selector")
                        and not any(x in item.get("selector", "").lower() 
                                   for x in ["message-accessories", "messagecontent", "chat-messages", "li[data-list-item-id"])
                        and any(x in (item.get("selector", "") + item.get("text", "")).lower() 
                               for x in ["sticker", "emoji", "gif", "button", "item", "option", "select", "picker", "menu", "popup", "grid", "tile"])
                    ]
                    
                    # Only perform depth 2 click if:
                    # 1. First click likely opened a picker/menu, OR
                    # 2. We found picker-specific items (not just general page elements)
                    if likely_opened_picker or picker_items:
                        # Prefer picker items, otherwise use interactive items; exclude already-clicked so we don't repeat same depth2 every iteration
                        candidates = picker_items if picker_items else interactive_items
                        candidates = [c for c in candidates if isinstance(c, dict) and c.get("selector") and c.get("selector") not in clicked_depth2_selectors]
                        target_item = candidates[0] if candidates else None
                        if target_item:
                            depth2_selector = target_item.get("selector")
                            if depth2_selector:
                                vm_execute_action(
                                    conn,
                                    {
                                        "schema_version": "1.0.0",
                                        "run_id": run_id,
                                        "agent_id": agent_id,
                                        "action_id": f"act_ft_depth2_click_{agent_id}_{int(time.time()*1000)}_{i}",
                                        "name": "browser.click",
                                        "params": {"selector": depth2_selector, "timeout_ms": 5000},
                                    },
                                )
                                depth2_clicked = True
                                clicked_depth2_selectors.add(depth2_selector)
                                time.sleep(0.6)  # Wait after depth 2 click
        except Exception as e:
            # Depth 2 click is optional - if it fails, continue with depth 1
            import logging
            logging.getLogger(__name__).debug(f"Depth 2 click failed (optional): {e}")

        # DEPTH 3 CLICKING: After depth2 click, get clickables again and click one more (current page -> click -> click -> click)
        depth3_clicked = False
        depth3_selector = None
        try:
            if depth2_clicked:
                time.sleep(0.3)
                depth3_res = vm_execute_action(
                    conn,
                    {
                        "schema_version": "1.0.0",
                        "run_id": run_id,
                        "agent_id": agent_id,
                        "action_id": f"act_ft_depth3_{agent_id}_{int(time.time()*1000)}_{i}",
                        "name": "browser.get_clickables",
                        "params": {"timeout_ms": 3000},
                    },
                )
                if isinstance(depth3_res, dict):
                    outputs = depth3_res.get("outputs", {})
                    depth3_clickables = outputs.get("clickables", []) if isinstance(outputs, dict) else []
                    if depth3_clickables:
                        import re
                        def _message_accessory_id(sel: str) -> str:
                            m = re.search(r"#message-accessories-(\d+)", sel)
                            return m.group(1) if m else ""
                        # Exclude: already used at depth3, already used at depth2 (any iteration), or current depth2
                        # Also limit to 1 depth3 click per message-accessories id (avoid same message attachments repeatedly)
                        depth3_items = []
                        for c in depth3_clickables:
                            if not isinstance(c, dict) or not c.get("selector"):
                                continue
                            sel = c.get("selector", "")
                            if sel in clicked_depth3_selectors or sel in clicked_depth2_selectors:
                                continue
                            if depth2_selector and sel == depth2_selector:
                                continue
                            mid = _message_accessory_id(sel)
                            if mid and message_accessory_depth3_count.get(mid, 0) >= 1:
                                continue
                            depth3_items.append(c)
                        # Do not fall back to already-clicked; skip depth3 if no new candidate
                        target = depth3_items[0] if depth3_items else None
                        if target:
                            depth3_selector = target.get("selector")
                            if depth3_selector:
                                vm_execute_action(
                                    conn,
                                    {
                                        "schema_version": "1.0.0",
                                        "run_id": run_id,
                                        "agent_id": agent_id,
                                        "action_id": f"act_ft_depth3_click_{agent_id}_{int(time.time()*1000)}_{i}",
                                        "name": "browser.click",
                                        "params": {"selector": depth3_selector, "timeout_ms": 5000},
                                    },
                                )
                                depth3_clicked = True
                                clicked_depth3_selectors.add(depth3_selector)
                                mid = _message_accessory_id(depth3_selector) if depth3_selector else ""
                                if mid:
                                    message_accessory_depth3_count[mid] = message_accessory_depth3_count.get(mid, 0) + 1
                                time.sleep(0.5)
        except Exception as e:
            import logging
            logging.getLogger(__name__).debug(f"Depth 3 click failed (optional): {e}")

        # Verify click actually happened by checking if URL changed or element state changed
        # This helps confirm the click triggered actual Discord functionality
        click_verified = False
        click_effect_detected = False
        try:
            # Get current URL to see if navigation happened
            current_url_res = vm_execute_action(
                conn,
                {
                    "schema_version": "1.0.0",
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "action_id": f"act_ft_verify_{agent_id}_{int(time.time()*1000)}_{i}",
                    "name": "browser.get_clickables",
                    "params": {"timeout_ms": 5000},
                },
            )
            if isinstance(current_url_res, dict):
                outputs = current_url_res.get("outputs", {})
                verified_url = outputs.get("current_url", "") if isinstance(outputs, dict) else ""
                # If URL changed or we're still on Discord, consider it verified
                if verified_url:
                    if "discord.com" in verified_url:
                        click_verified = True
                    if verified_url != base_url_final:
                        click_effect_detected = True  # Navigation happened
        except Exception:
            # If verification fails, assume click worked (don't block progress)
            click_verified = True
        
        # Log so SnapshotEngine event_trigger fires AFTER the feature has executed
        # IMPORTANT: Log BEFORE resetting page, so snapshot captures the state after click
        log_action(
            action_log_path,
            run_id=run_id,
            agent_id=agent_id,
            name="browser.full_trigger_click",
            params={
                "selector": selector,
                "tag": tag,
                "text": text,
                "index": i,
                "verified": click_verified,
                "effect_detected": click_effect_detected,
                "depth2_clicked": depth2_clicked,
                "depth2_selector": depth2_selector,
                "depth3_clicked": depth3_clicked,
                "depth3_selector": depth3_selector,
            },
            reason="full_trigger: snapshot after feature execution (to capture new schema state)",
            result={
                "ok": True,
                "selector": selector,
                "verified": click_verified,
                "effect_detected": click_effect_detected,
                "depth2_clicked": depth2_clicked,
                "depth3_clicked": depth3_clicked,
            },
            kind="full_trigger",
        )
        executed += 1
        
        # AFTER logging (which triggers snapshot), reset to base page for next click
        time.sleep(0.3)  # Brief delay before reset
        try:
            vm_execute_action(
                conn,
                {
                    "schema_version": "1.0.0",
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "action_id": f"act_ft_reset_{agent_id}_{int(time.time()*1000)}_{i}",
                    "name": "browser.goto",
                    "params": {"url": base_url_final, "timeout_ms": 15000},
                },
            )
        except Exception:
            pass
    # Leave browser on base page so Tool Testing main loop (LLM) starts from intended state
    if executed > 0 and base_url_final:
        try:
            vm_execute_action(
                conn,
                {
                    "schema_version": "1.0.0",
                    "run_id": run_id,
                    "agent_id": agent_id,
                    "action_id": f"act_ft_reset_{agent_id}_{int(time.time()*1000)}",
                    "name": "browser.goto",
                    "params": {"url": base_url_final, "timeout_ms": 15000},
                },
            )
        except Exception:
            pass
    return (executed, "max_clicks")


def finalize_run_with_snapshot_trigger(
    *,
    run_id: str,
    agent_ids: List[str],
    conns: Dict[str, rpyc.Connection],
    per_agent_specmap: Dict[str, Dict[str, ActionSpec]],
    action_log_path: Path,
) -> None:
    """
    Always execute a final snapshot-triggering action so SnapshotEngine rules fire
    even when the loop ends by 'done' or run limits.
    Default trigger: browser.close (safe / idempotent).
    """
    for aid in agent_ids:
        # only do if this agent supports browser.close in action space
        if "browser.close" not in per_agent_specmap.get(aid, {}):
            continue

        req = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "agent_id": aid,
            "action_id": f"act_{aid}_{int(time.time()*1000)}",
            "name": "browser.close",
            "params": {},
        }

        try:
            res = vm_execute_action(conns[aid], req)
        except Exception as e:
            res = {"ok": False, "error": f"finalize browser.close failed: {e}"}

        log_action(
            action_log_path,
            run_id=run_id,
            agent_id=aid,
            name="browser.close",
            params={},
            reason="finalize: force snapshot trigger on run termination (browser.close)",
            result=res,
            kind="finalize", 
        )



# Main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="przma_config.json or interpreted_przma_config.json")
    ap.add_argument("--endpoints", required=True, help="Snapshot_Engine/vm_endpoints.json")
    ap.add_argument("--actions", required=True, help="shared/actions.json")
    ap.add_argument("--files", default=None, help="shared/file.json (optional; used for upload file_key resolution and LLM context)")
    ap.add_argument("--action-log", required=True, help="runs/.../actions.jsonl")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--latest-limit", type=int, default=10)
    args = ap.parse_args()

    # env
    load_dotenv(".env")  # Based on repo root
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing env var: OPENAI_API_KEY")

    model = os.getenv("OPENAI_MODEL", "gpt-5.2-thinking")

    config_path = Path(args.config)
    config = read_json(config_path)

    purpose = config.get("purpose")
    if purpose not in ("education", "tool_testing"):
        raise RuntimeError("config.purpose must be 'education' or 'tool_testing'")

    agents = config.get("agents") or {}
    agent_ids = list(agents.keys())
    if not agent_ids:
        raise RuntimeError("No agents in config")

    rendezvous = config.get("rendezvous") or {}
    action_log_path = Path(args.action_log)

    run_limits = read_run_limits(config)
    max_actions_total = run_limits["max_actions_total"]
    max_minutes = run_limits["max_minutes"]

    # action space
    specs_all = load_action_specs(Path(args.actions))

    # file catalog (for upload actions and LLM context)
    files_path = Path(args.files) if getattr(args, "files", None) else config_path.parent / "shared" / "file.json"
    file_catalog = load_file_catalog(files_path)
    used_upload_file_keys: Set[str] = set()
    used_upload_file_paths: Set[str] = set()

    # endpoints
    endpoints_data = read_json(Path(args.endpoints))
    endpoints = endpoints_data.get("endpoints", [])
    ep_map: Dict[str, Tuple[str, int]] = {}
    for e in endpoints:
        ep_map[e["agent_id"]] = (e["host"], int(e["port"]))

    # connect
    conns: Dict[str, rpyc.Connection] = {}
    for aid in agent_ids:
        if aid not in ep_map:
            raise RuntimeError(f"vm_endpoints.json missing mapping for agent_id={aid}")
        host, port = ep_map[aid]
        conns[aid] = connect_vm(host, port, timeout=120)
        if conns[aid].root.ping() != "pong":
            raise RuntimeError(f"VM_Agent not responding: {aid} {host}:{port}")

    # per-agent specs/prompts
    per_agent_specmap: Dict[str, Dict[str, ActionSpec]] = {}
    per_agent_system: Dict[str, str] = {}

    for aid in agent_ids:
        agent_cfg = agents.get(aid) or {}
        plats = agent_cfg.get("platforms") or agent_cfg.get("capabilities") or {}
        spec_list = action_space_for_agent(specs_all, plats)
        per_agent_specmap[aid] = {s.name: s for s in spec_list}
        per_agent_system[aid] = build_agent_system_prompt(config, aid, spec_list, file_catalog)

    # track agent done state
    agent_done: Dict[str, bool] = {aid: False for aid in agent_ids}

    # count total executed actions (bootstrap + main actions)
    actions_total = 0

    # start time
    start_ts = utc_now_ts()

    # Bootstrap (COUNTED)

    for aid in agent_ids:
        agent_cfg = agents.get(aid) or {}
        executed = bootstrap_agent(
            run_id=args.run_id,
            agent_id=aid,
            agent_cfg=agent_cfg,
            rendezvous=rendezvous,
            conn=conns[aid],
            action_specs={k: v for k, v in specs_all.items()},
            action_log_path=action_log_path,
        )
        actions_total += executed

        # hard stops can kick in even during bootstrap
        if actions_total >= max_actions_total:
            break
        if hard_stop_reached(start_ts, max_minutes):
            break

    # Full trigger phase (tool_testing only): click each clickable → trigger snapshot → back to page
    # NEW ORDER: Run LLM loop first to build up conversation, then run full trigger
    run_full_trigger = config.get("run_full_trigger") is True and purpose == "tool_testing"
    full_trigger_executed = False  # Track if full trigger has been executed
    
    # Debug: log why full trigger might not run
    if not run_full_trigger:
        log_action(
            action_log_path,
            run_id=args.run_id,
            agent_id=agent_ids[0] if agent_ids else "unknown",
            name="full_trigger.debug",
            params={
                "run_full_trigger_config": config.get("run_full_trigger"),
                "run_full_trigger_is_true": config.get("run_full_trigger") is True,
                "purpose": purpose,
                "purpose_is_tool_testing": purpose == "tool_testing",
            },
            reason="Debug: Full trigger condition check",
            result={"ok": False, "will_run": False},
            kind="full_trigger",
        )
    
    # Helper function to execute full trigger (will be called from main loop when conditions are met)
    def execute_full_trigger_if_needed():
        """Execute full trigger phase if conditions are met (conversation has built up)"""
        nonlocal full_trigger_executed, actions_total
        
        if full_trigger_executed or not run_full_trigger or not agent_ids:
            return
        
        ft_agent = config.get("full_trigger_agent") or agent_ids[0]
        if ft_agent not in conns or actions_total >= max_actions_total:
            return
        
        # Check if we have enough messages/conversation to react to
        # Count messages from latest fetch
        try:
            latest = fetch_latest_messages_if_supported(args.run_id, ft_agent, conns[ft_agent], per_agent_specmap[ft_agent], limit=10)
            message_count = 0
            if latest and isinstance(latest, dict):
                messages = latest.get("messages", [])
                if isinstance(messages, list):
                    message_count = len(messages)
            
            # Also count actions that indicate conversation has started
            action_count = 0
            if action_log_path.exists():
                try:
                    lines = action_log_path.read_text(encoding="utf-8").splitlines()
                    for ln in lines:
                        try:
                            entry = json.loads(ln)
                            action_name = entry.get("action", {}).get("name", "") if isinstance(entry.get("action"), dict) else ""
                            # Count meaningful actions (send_message, react_message, upload_file, reply_message)
                            if any(x in action_name for x in ["send_message", "react_message", "upload_file", "reply_message"]):
                                action_count += 1
                        except Exception:
                            pass
                except Exception:
                    pass
            
            # Trigger full trigger if we have at least 2 messages OR at least 3 meaningful actions
            # This ensures there's conversation to react to
            # For Telegram: bootstrap completion (telegram.select_chat success) is enough to start full trigger
            target_app = config.get("target_application", "")
            is_telegram = target_app == "telegram_web"
            
            if is_telegram:
                # Telegram: Check if bootstrap is complete (telegram.select_chat was successful)
                # Also extract the chat URL from bootstrap to use as base_url
                bootstrap_complete = False
                bootstrap_chat_url = None
                if action_log_path.exists():
                    try:
                        lines = action_log_path.read_text(encoding="utf-8").splitlines()
                        for ln in lines:
                            try:
                                entry = json.loads(ln)
                                action_name = entry.get("action", {}).get("name", "") if isinstance(entry.get("action"), dict) else ""
                                if action_name == "telegram.select_chat":
                                    result = entry.get("result", {})
                                    if isinstance(result, dict) and result.get("ok") is True:
                                        bootstrap_complete = True
                                        # Extract current_url from result (the chat URL after selecting chat)
                                        outputs = result.get("outputs", {})
                                        if isinstance(outputs, dict):
                                            chat_url = outputs.get("current_url", "").strip()
                                            chat_opened = outputs.get("chat_opened", False)
                                            message_input_visible = outputs.get("message_input_visible", False)
                                            # If chat is opened (URL has # or message input is visible), use it as base_url
                                            # Even if URL is still /a/, if message_input_visible is True, chat is open
                                            if chat_url and (("/#" in chat_url) or (message_input_visible and chat_opened)):
                                                bootstrap_chat_url = chat_url
                                            # If URL doesn't have # but chat_opened is True, try to get URL from browser
                                            elif chat_opened or message_input_visible:
                                                # Chat is open but URL might not be updated yet
                                                # We'll use /a/ as base_url and let full trigger detect the actual chat URL
                                                bootstrap_chat_url = chat_url if chat_url else "https://web.telegram.org/a/"
                                        break
                            except Exception:
                                pass
                    except Exception:
                        pass
                
                # For Telegram, start full trigger if bootstrap is complete (even without messages)
                if not bootstrap_complete:
                    return  # Wait for bootstrap to complete
            else:
                # Discord: Require conversation to be built up
                if message_count < 2 and action_count < 3:
                    return  # Not enough conversation yet, wait for more LLM actions
            
            log_action(
                action_log_path,
                run_id=args.run_id,
                agent_id=ft_agent,
                name="full_trigger.condition_met",
                params={"message_count": message_count, "action_count": action_count},
                reason="Full trigger: conditions met (enough conversation built up)",
                result={"ok": True, "message_count": message_count, "action_count": action_count},
                kind="full_trigger",
            )
        except Exception as e:
            log_action(
                action_log_path,
                run_id=args.run_id,
                agent_id=ft_agent,
                name="full_trigger.condition_check_error",
                params={"error": str(e)},
                reason="Full trigger: error checking conditions, proceeding anyway",
                result={"ok": False, "error": str(e)},
                kind="full_trigger",
            )
            # If check fails, proceed anyway (don't block full trigger)
        
        # Initialize ft_base_url and other parameters AFTER condition check
        ft_max = int(config.get("full_trigger_max_clicks") or 5)
        ft_wait = float(config.get("full_trigger_wait_after_click_sec") or 0.8)
        ft_base_url = config.get("full_trigger_base_url")  # Initialize ft_base_url first (may be None)
        
        # If we have a bootstrap chat URL (from Telegram), use it as base_url (prioritize over config)
        if is_telegram and bootstrap_chat_url and not ft_base_url:
            ft_base_url = bootstrap_chat_url
            log_action(
                action_log_path,
                run_id=args.run_id,
                agent_id=ft_agent,
                name="full_trigger.base_url_bootstrap",
                params={"detected_url": ft_base_url},
                reason="Full trigger: using chat URL from bootstrap (telegram.select_chat)",
                result={"ok": True, "base_url": ft_base_url},
                kind="full_trigger",
            )
        
        # Count existing click actions (browser.full_trigger_click, browser.click, browser.smart_click) from action log
        # This ensures max_clicks counts ALL click actions, not just browser.full_trigger_click
        existing_click_count = 0
        if action_log_path.exists():
            try:
                lines = action_log_path.read_text(encoding="utf-8").splitlines()
                for ln in lines:
                    try:
                        entry = json.loads(ln)
                        action_name = entry.get("action", {}).get("name", "") if isinstance(entry.get("action"), dict) else ""
                        # Count all click-related actions
                        if action_name in ("browser.full_trigger_click", "browser.click", "browser.smart_click"):
                            existing_click_count += 1
                    except Exception:
                        pass
            except Exception:
                pass
        
        # Adjust max_clicks based on existing clicks
        ft_max_adjusted = max(0, ft_max - existing_click_count)
        
        # Log that full trigger is starting
        log_action(
            action_log_path,
            run_id=args.run_id,
            agent_id=ft_agent,
            name="full_trigger.start",
            params={"max_clicks": ft_max, "max_clicks_adjusted": ft_max_adjusted, "existing_click_count": existing_click_count, "base_url": ft_base_url},
            reason="Starting full trigger phase: enumerate clickables and click each",
            result={"ok": True},
            kind="full_trigger",
        )
        
        # If base_url not provided, get current URL from the agent's page
        target_app = config.get("target_application", "")
        
        # First try: use platform-specific env var (Discord or Telegram)
        if not ft_base_url:
            if target_app == "discord_web":
                channel_url = os.getenv("DISCORD_MEETING_CHANNEL", "").strip()
                if channel_url:
                    ft_base_url = channel_url
                    log_action(
                        action_log_path,
                        run_id=args.run_id,
                        agent_id=ft_agent,
                        name="full_trigger.base_url_env",
                        params={"detected_url": ft_base_url},
                        reason="Full trigger: using DISCORD_MEETING_CHANNEL as base_url",
                        result={"ok": True, "base_url": ft_base_url},
                        kind="full_trigger",
                    )
            elif target_app == "telegram_web":
                # Telegram: TELEGRAM_MEETING_CHAT is chat name, not URL
                # Use current URL from get_clickables (after telegram.select_chat)
                pass  # Will be handled in Second try below
        
        # Second try: get current URL by calling get_clickables (works for both Discord and Telegram)
        if not ft_base_url:
            try:
                clickables_res = vm_execute_action(
                    conns[ft_agent],
                    {
                        "schema_version": "1.0.0",
                        "run_id": args.run_id,
                        "agent_id": ft_agent,
                        "action_id": f"act_{ft_agent}_{int(time.time()*1000)}",
                        "name": "browser.get_clickables",
                        "params": {"timeout_ms": 30000},
                    },
                )
                if isinstance(clickables_res, dict):
                    outputs = clickables_res.get("outputs", {}) if isinstance(clickables_res, dict) else {}
                    suggested_url = outputs.get("current_url", "").strip() if isinstance(outputs, dict) else ""
                    if suggested_url:
                        ft_base_url = suggested_url
                        log_action(
                            action_log_path,
                            run_id=args.run_id,
                            agent_id=ft_agent,
                            name="full_trigger.base_url_auto",
                            params={"detected_url": ft_base_url},
                            reason="Full trigger: auto-detected base_url from get_clickables",
                            result={"ok": True, "base_url": ft_base_url},
                            kind="full_trigger",
                        )
            except Exception as e:
                log_action(
                    action_log_path,
                    run_id=args.run_id,
                    agent_id=ft_agent,
                    name="full_trigger.base_url_error",
                    params={"error": str(e)},
                    reason="Full trigger: failed to detect base_url from get_clickables",
                    result={"ok": False, "error": str(e)},
                    kind="full_trigger",
                )
        
        # Validate base_url: ensure we're in a chat/channel (not outside)
        # For Telegram: be more lenient - if bootstrap completed (telegram.select_chat succeeded), allow /a/ or /k/ as valid
        # The actual chat verification will happen during full trigger execution
        if ft_base_url:
            url_lower = ft_base_url.lower()
            is_valid_chat_url = False
            if target_app == "discord_web":
                is_valid_chat_url = "discord.com/channels/" in url_lower
            elif target_app == "telegram_web":
                # Telegram chat URL: web.telegram.org/k/#-1234567890, web.telegram.org/a/#@username, web.telegram.org/a/#-1234567890, or t.me
                # Valid patterns: /k/#..., /a/#..., /k/, /a/
                # For Telegram, if bootstrap completed (telegram.select_chat succeeded), accept /a/ or /k/ as valid
                # Telegram Web sometimes doesn't update URL immediately, so we trust bootstrap completion
                is_valid_chat_url = (
                    ("web.telegram.org" in url_lower and ("/#" in url_lower or url_lower.endswith("/k/") or url_lower.endswith("/a/"))) or 
                    ("t.me" in url_lower)
                )
            else:
                is_valid_chat_url = True  # Unknown platform, allow anyway
            
            if not is_valid_chat_url:
                log_action(
                    action_log_path,
                    run_id=args.run_id,
                    agent_id=ft_agent,
                    name="full_trigger.base_url_invalid",
                    params={"base_url": ft_base_url, "target_app": target_app},
                    reason="Full trigger: base_url does not appear to be inside a chat/channel. Skipping full_trigger to avoid errors.",
                    result={"ok": False, "skipped": True},
                    kind="full_trigger",
                )
                return  # Skip full_trigger if not in chat
        
        # If base_url is still None, retry detection with longer wait
        if not ft_base_url:
            log_action(
                action_log_path,
                run_id=args.run_id,
                agent_id=ft_agent,
                name="full_trigger.base_url_retry",
                params={"attempt": 1},
                reason="Full trigger: base_url is null, retrying detection after page load wait",
                result={"ok": False, "retrying": True},
                kind="full_trigger",
            )
            time.sleep(3.0)
            if target_app == "discord_web":
                channel_url = os.getenv("DISCORD_MEETING_CHANNEL", "").strip()
                if channel_url:
                    try:
                        vm_execute_action(
                            conns[ft_agent],
                            {
                                "schema_version": "1.0.0",
                                "run_id": args.run_id,
                                "agent_id": ft_agent,
                                "action_id": f"act_{ft_agent}_{int(time.time()*1000)}_retry",
                                "name": "browser.goto",
                                "params": {"url": channel_url, "timeout_ms": 20000, "wait": "networkidle"},
                            },
                        )
                        time.sleep(2.0)
                        clickables_res = vm_execute_action(
                            conns[ft_agent],
                            {
                                "schema_version": "1.0.0",
                                "run_id": args.run_id,
                                "agent_id": ft_agent,
                                "action_id": f"act_{ft_agent}_{int(time.time()*1000)}_retry2",
                                "name": "browser.get_clickables",
                                "params": {"timeout_ms": 30000},
                            },
                        )
                        if isinstance(clickables_res, dict):
                            outputs = clickables_res.get("outputs", {}) if isinstance(clickables_res, dict) else {}
                            suggested_url = outputs.get("current_url", "").strip() if isinstance(outputs, dict) else ""
                            if suggested_url:
                                ft_base_url = suggested_url
                                log_action(
                                    action_log_path,
                                    run_id=args.run_id,
                                    agent_id=ft_agent,
                                    name="full_trigger.base_url_retry_success",
                                    params={"detected_url": ft_base_url},
                                    reason="Full trigger: base_url detected after retry",
                                    result={"ok": True, "base_url": ft_base_url},
                                    kind="full_trigger",
                                )
                    except Exception as e:
                        log_action(
                            action_log_path,
                            run_id=args.run_id,
                            agent_id=ft_agent,
                            name="full_trigger.base_url_retry_failed",
                            params={"error": str(e)},
                            reason="Full trigger: base_url retry failed",
                            result={"ok": False, "error": str(e)},
                            kind="full_trigger",
                        )
            elif target_app == "telegram_web":
                # Telegram: retry by ensuring we're in a chat (telegram.select_chat if needed)
                chat_name = os.getenv("TELEGRAM_MEETING_CHAT", "").strip()
                if chat_name:
                    try:
                        # Get variant from config (same as bootstrap)
                        agent_config = config.get("agents", {}).get(ft_agent, {})
                        platforms_config = agent_config.get("platforms", {})
                        telegram_config = platforms_config.get("telegram_web", {})
                        variant = telegram_config.get("variant", "k")  # Default to "k" if not specified
                        
                        # Ensure we're in the chat
                        vm_execute_action(
                            conns[ft_agent],
                            {
                                "schema_version": "1.0.0",
                                "run_id": args.run_id,
                                "agent_id": ft_agent,
                                "action_id": f"act_{ft_agent}_{int(time.time()*1000)}_retry_tg",
                                "name": "telegram.select_chat",
                                "params": {"chat": chat_name, "variant": variant},
                            },
                        )
                        time.sleep(2.0)
                        clickables_res = vm_execute_action(
                            conns[ft_agent],
                            {
                                "schema_version": "1.0.0",
                                "run_id": args.run_id,
                                "agent_id": ft_agent,
                                "action_id": f"act_{ft_agent}_{int(time.time()*1000)}_retry_tg2",
                                "name": "browser.get_clickables",
                                "params": {"timeout_ms": 30000},
                            },
                        )
                        if isinstance(clickables_res, dict):
                            outputs = clickables_res.get("outputs", {}) if isinstance(clickables_res, dict) else {}
                            suggested_url = outputs.get("current_url", "").strip() if isinstance(outputs, dict) else ""
                            if suggested_url and ("web.telegram.org" in suggested_url.lower() or "t.me" in suggested_url.lower()):
                                ft_base_url = suggested_url
                                log_action(
                                    action_log_path,
                                    run_id=args.run_id,
                                    agent_id=ft_agent,
                                    name="full_trigger.base_url_retry_success",
                                    params={"detected_url": ft_base_url},
                                    reason="Full trigger: base_url detected after Telegram chat selection retry",
                                    result={"ok": True, "base_url": ft_base_url},
                                    kind="full_trigger",
                                )
                    except Exception as e:
                        log_action(
                            action_log_path,
                            run_id=args.run_id,
                            agent_id=ft_agent,
                            name="full_trigger.base_url_retry_failed",
                            params={"error": str(e)},
                            reason="Full trigger: Telegram base_url retry failed",
                            result={"ok": False, "error": str(e)},
                            kind="full_trigger",
                        )
        
        # Final validation: ensure base_url is valid before proceeding
        # For Telegram: be lenient - if bootstrap completed, allow /a/ or /k/ as valid
        if ft_base_url:
            url_lower = ft_base_url.lower()
            is_valid_chat_url = False
            if target_app == "discord_web":
                is_valid_chat_url = "discord.com/channels/" in url_lower
            elif target_app == "telegram_web":
                # Telegram chat URL: web.telegram.org/k/#-1234567890, web.telegram.org/a/#@username, web.telegram.org/a/#-1234567890, or t.me
                # Valid patterns: /k/#..., /a/#..., /k/, /a/
                # Accept /a/ and /k/ as valid (Telegram Web sometimes doesn't update URL immediately after select_chat)
                is_valid_chat_url = (
                    ("web.telegram.org" in url_lower and ("/#" in url_lower or url_lower.endswith("/k/") or url_lower.endswith("/a/"))) or 
                    ("t.me" in url_lower)
                )
            else:
                is_valid_chat_url = True

            if not is_valid_chat_url:
                log_action(
                    action_log_path,
                    run_id=args.run_id,
                    agent_id=ft_agent,
                    name="full_trigger.base_url_invalid_final",
                    params={"base_url": ft_base_url, "target_app": target_app},
                    reason="Full trigger: base_url does not appear to be inside a chat/channel. Skipping full_trigger.",
                    result={"ok": False, "skipped": True},
                    kind="full_trigger",
                )
                return  # Skip full_trigger if not in chat
        
        ft_max_this = min(ft_max_adjusted, max(0, max_actions_total - actions_total))
        run_result = run_full_trigger_phase(
            run_id=args.run_id,
            agent_id=ft_agent,
            conn=conns[ft_agent],
            action_log_path=action_log_path,
            base_url=ft_base_url,
            max_clicks=ft_max_this,
            wait_after_click_sec=ft_wait,
            model=model,
            api_key=api_key,
            use_llm=config.get("full_trigger_use_llm", True),
        )
        if isinstance(run_result, tuple):
            n_ft, stop_reason = run_result
        else:
            n_ft, stop_reason = run_result, "max_clicks"
        log_action(
            action_log_path,
            run_id=args.run_id,
            agent_id=ft_agent,
            name="full_trigger.done",
            params={"clicks_executed": n_ft, "stop_reason": stop_reason, "max_clicks": ft_max_this},
            reason="Full trigger phase completed",
            result={"ok": True, "clicks_executed": n_ft, "stop_reason": stop_reason, "max_clicks": ft_max_this},
            kind="full_trigger",
        )
        actions_total += n_ft
        full_trigger_executed = True

    # Main loop
    step = 0
    while True:
        # hard stops
        if actions_total >= max_actions_total:
            break
        if hard_stop_reached(start_ts, max_minutes):
            break

        # early stop if all agents are done
        if all(agent_done.values()):
            break

        progressed_this_round = False
        
        # Check if full trigger should run now (after some conversation has built up)
        if not full_trigger_executed and run_full_trigger:
            execute_full_trigger_if_needed()

        for aid in agent_ids:
            if actions_total >= max_actions_total:
                break
            if hard_stop_reached(start_ts, max_minutes):
                break

            if agent_done.get(aid) is True:
                continue  # already done

            # observations: latest messages + last actions excerpt
            #latest = fetch_latest_messages_if_supported(args.run_id, aid, conns[aid], specs_all, limit=args.latest_limit)
            latest = fetch_latest_messages_if_supported(args.run_id, aid, conns[aid], per_agent_specmap[aid], limit=args.latest_limit)
            
            recent: List[Dict[str, Any]] = []
            if action_log_path.exists():
                try:
                    lines = action_log_path.read_text(encoding="utf-8").splitlines()
                    for ln in lines[-20:]:
                        try:
                            recent.append(json.loads(ln))
                        except Exception:
                            pass
                except Exception:
                    pass

            # full_trigger_done: end click loop when enough clicks (even if run_full_trigger was false or phase skipped)
            full_trigger_done = False
            ft_agent = config.get("full_trigger_agent") or (agent_ids[0] if agent_ids else "")
            ft_max = int(config.get("full_trigger_max_clicks") or 3)
            if run_full_trigger:
                # Check if full_trigger.done was already logged
                for entry in reversed(recent):
                    if entry.get("name") == "full_trigger.done":
                        res = entry.get("result") or {}
                        reason = res.get("stop_reason", "")
                        if reason in ("no_clickables", "all_features_done", "no_base_url", "get_clickables_failed", "max_clicks"):
                            full_trigger_done = True
                        break
            # Always count clicks so loop ends after max_clicks even when run_full_trigger is false or phase was skipped
            if not full_trigger_done:
                click_action_count = 0
                if action_log_path.exists():
                    try:
                        all_lines = action_log_path.read_text(encoding="utf-8").splitlines()
                        for ln in all_lines:
                            try:
                                entry = json.loads(ln)
                                action_name = entry.get("action", {}).get("name", "") if isinstance(entry.get("action"), dict) else entry.get("name", "")
                                if action_name in ("browser.full_trigger_click", "browser.click", "browser.smart_click"):
                                    entry_agent = entry.get("agent_id") or entry.get("action", {}).get("agent_id", "")
                                    entry_kind = entry.get("kind", "")
                                    if entry_agent == ft_agent or entry_kind == "full_trigger":
                                        click_action_count += 1
                            except Exception:
                                pass
                    except Exception:
                        pass
                if click_action_count >= ft_max:
                    full_trigger_done = True
                    if not any(e.get("name") == "full_trigger.done" for e in recent):
                        log_action(
                            action_log_path,
                            run_id=args.run_id,
                            agent_id=ft_agent,
                            name="full_trigger.done",
                            params={"clicks_executed": click_action_count, "stop_reason": "max_clicks", "max_clicks": ft_max},
                            reason="Full trigger phase completed: reached max_clicks via click action count",
                            result={"ok": True, "clicks_executed": click_action_count, "stop_reason": "max_clicks", "max_clicks": ft_max},
                            kind="full_trigger",
                        )
            observations = {
                "agent_id": aid,
                "recent_actions": recent,
                "discord_latest_messages": latest,
                "full_trigger_done": full_trigger_done,
            }

            progress = {
                "actions_total": actions_total,
                "max_actions_total": max_actions_total,
                "minutes_elapsed": int((utc_now_ts() - start_ts) // 60),
                "max_minutes": max_minutes,
                "agent_done": agent_done,
            }

            sys_prompt = per_agent_system[aid]
            usr_prompt = build_agent_user_prompt(step=step, observations=observations, progress=progress)

            try:
                decision = llm_choose_next_action(
                    model=model,
                    api_key=api_key,
                    system_prompt=sys_prompt,
                    user_prompt=usr_prompt,
                    spec_map=per_agent_specmap[aid],
                    max_repairs=2,
                )
            except Exception as e:
                # mark agent done on repeated LLM failure? conservative: log and mark done
                log_action(
                    action_log_path,
                    run_id=args.run_id,
                    agent_id=aid,
                    name=None,
                    params={},
                    reason=f"LLM decision failed; marking agent done. err={type(e).__name__}: {e}",
                    result={"ok": False, "error": str(e)},
                    kind="error",
                )
                agent_done[aid] = True
                continue

            if decision.get("done") is True:
                agent_done[aid] = True
                log_action(
                    action_log_path,
                    run_id=args.run_id,
                    agent_id=aid,
                    name=None,
                    params={},
                    reason=decision.get("reason", "done"),
                    result={"ok": True, "note": "agent done"},
                    kind="done",
                )
                progressed_this_round = True
                continue

            action = decision["action"]
            name = action["name"]
            params = dict(action.get("params") or {})

            # When full_trigger_done, skip more click/get_clickables and mark agent done so loop ends
            if full_trigger_done and aid == ft_agent and name in ("browser.click", "browser.get_clickables", "browser.screenshot"):
                log_action(
                    action_log_path,
                    run_id=args.run_id,
                    agent_id=aid,
                    name=name,
                    params=params,
                    reason="Skipped: full_trigger_done (max_clicks reached); no more click coverage.",
                    result={"ok": False, "skipped": True, "reason": "full_trigger_done"},
                    kind="action",
                )
                agent_done[aid] = True
                progressed_this_round = True
                continue

            # Full trigger: A2 only seeds the channel (one message if empty). Block all other A2 actions.
            if run_full_trigger and aid != ft_agent:
                if name in ("discord.reply_message", "discord.react_message", "discord.upload_file", "discord.get_latest_messages",
                            "browser.click", "browser.smart_click", "browser.get_clickables", "browser.screenshot", "browser.smart_type"):
                    log_action(
                        action_log_path,
                        run_id=args.run_id,
                        agent_id=aid,
                        name=name,
                        params=params,
                        reason="Skipped: A2 role in full_trigger is only to send one seed message when channel is empty; no reply/react/upload/click.",
                        result={"ok": False, "skipped": True, "reason": "a2_seed_only"},
                        kind="action",
                    )
                    agent_done[aid] = True
                    progressed_this_round = True
                    continue
                if name == "discord.send_message":
                    a2_send_count = 0
                    if action_log_path.exists():
                        try:
                            for ln in action_log_path.read_text(encoding="utf-8").splitlines():
                                try:
                                    e = json.loads(ln)
                                    if (e.get("agent_id") == aid and (e.get("action") or {}).get("name") == "discord.send_message"
                                            and (e.get("result") or {}).get("ok") is True):
                                        a2_send_count += 1
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    if a2_send_count >= 1:
                        log_action(
                            action_log_path,
                            run_id=args.run_id,
                            agent_id=aid,
                            name=name,
                            params=params,
                            reason="Skipped: A2 already sent the one allowed seed message in full_trigger.",
                            result={"ok": False, "skipped": True, "reason": "a2_seed_only_one_message"},
                            kind="action",
                        )
                        agent_done[aid] = True
                        progressed_this_round = True
                        continue

            # After full trigger, avoid duplicate discord/telegram actions.
            if run_full_trigger and full_trigger_executed and name and (name.startswith("discord.") or name.startswith("telegram.")):
                log_action(
                    action_log_path,
                    run_id=args.run_id,
                    agent_id=aid,
                    name=name,
                    params=params,
                    reason="Skipped: full_trigger already completed; no additional discord/telegram interaction.",
                    result={"ok": False, "skipped": True, "reason": "full_trigger_done_no_discord_actions"},
                    kind="action",
                )
                agent_done[aid] = True
                progressed_this_round = True
                continue

            if name == "browser.launch" and "browser_config" not in params:
                params = {"browser_config": params}

            # Resolve file_key -> file_path for upload actions (discord.upload_file, telegram.upload_file)
            if name in ("discord.upload_file", "telegram.upload_file"):
                file_key_before = params.get("file_key")
                resolved = resolve_file_path(
                    file_catalog,
                    params.get("file_key"),
                    params.get("file_path"),
                )
                if resolved:
                    params["file_path"] = resolved
                if not params.get("file_path"):
                    log_action(
                        action_log_path,
                        run_id=args.run_id,
                        agent_id=aid,
                        name=name,
                        params=params,
                        reason="Upload action requires file_path or file_key from file.json",
                        result={"ok": False, "error": "missing file_path/file_key"},
                        kind="error",
                    )
                    agent_done[aid] = True
                    progressed_this_round = True
                    continue

                # Prevent duplicate uploads across all agents within this run.
                norm_path = normalize_vm_path(params.get("file_path"))
                file_key_norm = str(file_key_before).strip() if isinstance(file_key_before, str) and file_key_before.strip() else None

                already_used = (file_key_norm in used_upload_file_keys) if file_key_norm else False
                already_used = already_used or (norm_path in used_upload_file_paths if norm_path else False)

                if already_used:
                    # Provide remaining options to help the LLM pick a new file.
                    remaining_keys: List[str] = []
                    try:
                        for f in (file_catalog or {}).get("files") or []:
                            if isinstance(f, dict) and f.get("id") and str(f.get("id")) not in used_upload_file_keys:
                                remaining_keys.append(str(f.get("id")))
                    except Exception:
                        remaining_keys = []

                    log_action(
                        action_log_path,
                        run_id=args.run_id,
                        agent_id=aid,
                        name=name,
                        params=params,
                        reason="Skipped: duplicate file upload prevented (run-wide single-use).",
                        result={
                            "ok": False,
                            "skipped": True,
                            "reason": "duplicate_file_upload_prevented",
                            "file_key": file_key_norm,
                            "file_path": params.get("file_path"),
                            "remaining_file_keys": remaining_keys[:30],
                        },
                        kind="action",
                    )
                    progressed_this_round = True
                    continue

            if name not in per_agent_specmap[aid]:
                # should not happen; treat as done to avoid looping
                agent_done[aid] = True
                log_action(
                    action_log_path,
                    run_id=args.run_id,
                    agent_id=aid,
                    name=None,
                    params={},
                    reason=f"Invalid action name after validation: {name}. Marking done.",
                    result={"ok": False, "error": "invalid_action_name"},
                    kind="error",
                )
                progressed_this_round = True
                continue

            # Execute action
            req = {
                "schema_version": "1.0.0",
                "run_id": args.run_id,
                "agent_id": aid,
                "action_id": f"act_{aid}_{int(time.time()*1000)}",
                "name": name,
                "params": params,
            }
            res = vm_execute_action(conns[aid], req)

            # Mark upload actions as used on success (run-wide).
            if name in ("discord.upload_file", "telegram.upload_file") and isinstance(res, dict) and res.get("ok") is True:
                fk = params.get("file_key")
                if isinstance(fk, str) and fk.strip():
                    used_upload_file_keys.add(fk.strip())
                p = normalize_vm_path(params.get("file_path"))
                if p:
                    used_upload_file_paths.add(p)

            log_action(
                action_log_path,
                run_id=args.run_id,
                agent_id=aid,
                name=name,
                params=params,
                reason=decision.get("reason", ""),
                result=res,
                kind="action",
            )
            actions_total += 1
            progressed_this_round = True

        step += 1

        # if no one progressed and not all done, avoid infinite loop
        if not progressed_this_round and not all(agent_done.values()):
            # log and break conservatively
            for aid in agent_ids:
                if not agent_done.get(aid):
                    log_action(
                        action_log_path,
                        run_id=args.run_id,
                        agent_id=aid,
                        name=None,
                        params={},
                        reason="No progress in round; stopping to avoid infinite loop.",
                        result={"ok": True, "note": "no_progress_stop"},
                        kind="done",
                    )
                    agent_done[aid] = True
            break
    # FINALIZE: always trigger snapshot once
    try:
        finalize_run_with_snapshot_trigger(
            run_id=args.run_id,
            agent_ids=agent_ids,
            conns=conns,
            per_agent_specmap=per_agent_specmap,
            action_log_path=action_log_path,
        )
    except Exception as e:
        # Even in the worst case, the run must end, so we swallow the exception and leave only the log
        for aid in agent_ids:
            log_action(
                action_log_path,
                run_id=args.run_id,
                agent_id=aid,
                name=None,
                params={},
                reason=f"finalize snapshot trigger failed: {e}",
                result={"ok": False, "error": str(e)},
                kind="error",
            )
    # final run summary (optional)
    summary = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "finished_at": now_iso(),
        "purpose": purpose,
        "actions_total": actions_total,
        "max_actions_total": max_actions_total,
        "minutes_elapsed": int((utc_now_ts() - start_ts) // 60),
        "max_minutes": max_minutes,
        "agent_done": agent_done,
        "stop_reason": (
            "max_actions_total"
            if actions_total >= max_actions_total
            else ("max_minutes" if hard_stop_reached(start_ts, max_minutes) else "all_agents_done")
        ),
    }
    # write beside action log
    try:
        write_json(action_log_path.with_suffix(".summary.json"), summary)
    except Exception:
        pass

    for aid, conn in conns.items():
        try:
            conn.root.close_agent(aid)
        except Exception:
            pass

    # close conns
    for c in conns.values():
        try:
            c.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
