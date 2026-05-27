# Tool_Manual_Interpreter/tmi_core.py
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

# OpenAI call 

def openai_chat(model: str, api_key: str, messages: List[Dict[str, str]], temperature: float = 0.2) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"model": model, "messages": messages, "temperature": temperature}
    r = requests.post(url, headers=headers, json=body, timeout=90)
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI error {r.status_code}: {r.text[:900]}")
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

# JSON handling helpers
def _extract_json_object(text: str) -> str:
    m = re.search(r"\{.*\}\s*$", text, re.DOTALL)
    if not m:
        raise ValueError("No JSON object found in output.")
    return m.group(0)


def safe_json_load(text: str) -> Dict[str, Any]:
    try:
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise ValueError("Top-level JSON must be an object.")
        return obj
    except Exception:
        j = _extract_json_object(text)
        obj = json.loads(j)
        if not isinstance(obj, dict):
            raise ValueError("Top-level JSON must be an object.")
        return obj

# Specs loader
@dataclass
class ActionSpec:
    name: str
    summary: str
    params_schema: Dict[str, Any]
    llm_guidance: List[str]


def load_action_specs(actions_json: Dict[str, Any]) -> Dict[str, ActionSpec]:
    specs: Dict[str, ActionSpec] = {}
    for a in actions_json.get("actions", []) or []:
        name = a["name"]
        specs[name] = ActionSpec(
            name=name,
            summary=a.get("summary", ""),
            params_schema=a.get("params_schema", {"type": "object", "properties": {}, "required": []}),
            llm_guidance=a.get("llm_guidance", []) or [],
        )
    return specs


# Artifact catalog leaf extraction
def load_catalog_leaf_map(artifact_catalog_json: Dict[str, Any], platform: str = "windows") -> Dict[str, Any]:
    """
    artifact_catalog.json Structure:
      {
        "schema_version": ...,
        "platforms": {
          "windows": {
             "<leaf_key>": {...},
             ...
          }
        }
      }
    """
    platforms = artifact_catalog_json.get("platforms")
    if not isinstance(platforms, dict):
        raise RuntimeError("artifact_catalog.json missing 'platforms' dict")

    p = platforms.get(platform)
    if not isinstance(p, dict):
        raise RuntimeError(f"artifact_catalog.json missing platforms.{platform} dict")

    # leaf map (key -> artifact spec)
    return p

# Normalization helpers
def _norm_str_list(x: Any, max_items: int = 20, max_len: int = 200) -> List[str]:
    if not isinstance(x, list):
        return []
    out: List[str] = []
    for v in x:
        if isinstance(v, str):
            s = v.strip()
            if s:
                out.append(s[:max_len])
        if len(out) >= max_items:
            break
    return list(dict.fromkeys(out))


def normalize_ir(ir_obj: Dict[str, Any], tool_name: str, tool_version: str) -> Dict[str, Any]:
    ir = ir_obj.get("ir") if "ir" in ir_obj else ir_obj
    if not isinstance(ir, dict):
        raise ValueError("IR must be an object.")

    tool = ir.get("tool") or {}
    out = {
        "ir": {
            "tool": {
                "name": str(tool.get("name") or tool_name or "unknown"),
                "version": str(tool.get("version") or tool_version or "unknown"),
            },
            "objective": str(ir.get("objective") or "").strip()[:400],
            "capabilities": _norm_str_list(ir.get("capabilities")),
            "integrity_warnings": _norm_str_list(ir.get("integrity_warnings")),
            "inputs_expected": _norm_str_list(ir.get("inputs_expected")),
            "outputs_expected": _norm_str_list(ir.get("outputs_expected")),
        }
    }
    if not out["ir"]["objective"]:
        out["ir"]["objective"] = f"Tool testing IR for {out['ir']['tool']['name']}"
    return out


def _dedupe_action_list(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
    seen = set()
    out = []
    for x in items:
        n = x.get("name")
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(x)
    return out


def normalize_mapping(
    mapping_obj: Dict[str, Any],
    specs: Dict[str, ActionSpec],
    leaf_catalog: Dict[str, Any],
    fallback_objective: str,
) -> Dict[str, Any]:
    m = mapping_obj.get("mapping") if "mapping" in mapping_obj else mapping_obj
    if not isinstance(m, dict):
        raise ValueError("mapping must be an object")

    # actions
    def norm_action_list(key: str) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for item in (m.get(key) or []):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            why = item.get("why", "")
            if isinstance(name, str) and name in specs:
                out.append({"name": name, "why": str(why)[:240]})
        return _dedupe_action_list(out)

    required_actions = norm_action_list("required_actions")
    forbidden_actions = norm_action_list("forbidden_actions")

    # artifacts (leaf only)
    raw_art = m.get("required_artifacts") or []
    invalid_art = []
    required_artifacts: List[str] = []
    if isinstance(raw_art, list):
        for k in raw_art:
            if isinstance(k, str) and k in leaf_catalog:
                required_artifacts.append(k)
            elif isinstance(k, str):
                invalid_art.append(k)

    required_artifacts = list(dict.fromkeys(required_artifacts))

    # If the LLM returns artifact keys outside the candidate set, log and drop them
    if invalid_art:
        print(f"[TMI] WARNING: LLM returned artifacts not in candidates (dropped): {invalid_art}")

    # Error out if the LLM returns an empty list despite valid artifact candidates.
    if isinstance(raw_art, list) and len(raw_art) == 0:
        raise RuntimeError(
            "[TMI] ERROR: required_artifacts is empty ([]). "
            "No suitable artifacts found in artifact_catalog.json. Add artifacts or adjust prompts."
        )
    # Error if all requested artifacts were dropped during validation.
    if raw_art and not required_artifacts:
        raise RuntimeError(
            "[TMI] ERROR: required_artifacts contained no valid leaf keys after validation. "
            "Check artifact_catalog candidates or prompt constraints."
        )

    # trigger actions
    trigger_actions: List[str] = []
    for a in (m.get("trigger_actions") or []):
        if isinstance(a, str) and a in specs:
            trigger_actions.append(a)
    trigger_actions = list(dict.fromkeys(trigger_actions))

    objective = str(m.get("objective") or "").strip() or fallback_objective
    return {
        "mapping": {
            "objective": objective[:500],
            "required_actions": required_actions,
            "forbidden_actions": forbidden_actions,
            "required_artifacts": required_artifacts,
            "trigger_actions": trigger_actions,
        }
    }


def normalize_tool_plan(
    tool_plan_obj: Dict[str, Any],
    specs: Dict[str, ActionSpec],
    leaf_catalog: Dict[str, Any],
    tool_name: str,
    tool_version: str,
    fallback_objective: str,
) -> Dict[str, Any]:
    tp = tool_plan_obj.get("tool_plan") if "tool_plan" in tool_plan_obj else tool_plan_obj
    if not isinstance(tp, dict):
        raise ValueError("tool_plan must be an object")

    def norm_action_list(key: str) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for item in (tp.get(key) or []):
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            why = item.get("why", "")
            if isinstance(name, str) and name in specs:
                out.append({"name": name, "why": str(why)[:240]})
        return _dedupe_action_list(out)

    required_actions = norm_action_list("required_actions")
    forbidden_actions = norm_action_list("forbidden_actions")

    raw_art = tp.get("required_artifacts") or []
    invalid_art = []
    required_artifacts: List[str] = []
    if isinstance(raw_art, list):
        for k in raw_art:
            if isinstance(k, str) and k in leaf_catalog:
                required_artifacts.append(k)
            elif isinstance(k, str):
                invalid_art.append(k)
    required_artifacts = list(dict.fromkeys(required_artifacts))

    if invalid_art:
        print(f"[TMI] WARNING: LLM returned artifacts not in candidates (dropped): {invalid_art}")
    if isinstance(raw_art, list) and len(raw_art) == 0:
        raise RuntimeError(
            "[TMI] ERROR: tool_plan.required_artifacts is empty ([]). "
            "No suitable artifacts found in artifact_catalog.json. Add artifacts or adjust prompts."
        )
    if raw_art and not required_artifacts:
        raise RuntimeError(
            "[TMI] ERROR: tool_plan.required_artifacts contained no valid leaf keys after validation."
        )

    trigger_actions: List[str] = []
    for a in (tp.get("trigger_actions") or []):
        if isinstance(a, str) and a in specs:
            trigger_actions.append(a)
    trigger_actions = list(dict.fromkeys(trigger_actions))
    if not trigger_actions:
        trigger_actions = [x["name"] for x in required_actions[:2]]

    completion = tp.get("completion") or {}
    if not isinstance(completion, dict):
        completion = {}
    ctype = str(completion.get("type") or "min_required_actions")
    if ctype not in ("min_required_actions", "required_artifacts_present", "custom"):
        ctype = "min_required_actions"
    cdetail = str(completion.get("detail") or "").strip()[:400]

    tool = tp.get("tool") or {}
    out_tool_name = str(tool.get("name") or tool_name or "unknown")
    out_tool_version = str(tool.get("version") or tool_version or "unknown")

    action_intent_guidance = str(tp.get("action_intent_guidance") or "").strip()
    if action_intent_guidance:
        action_intent_guidance = action_intent_guidance[:500]

    objective = str(tp.get("objective") or "").strip() or fallback_objective

    return {
        "tool_plan": {
            "tool": {"name": out_tool_name, "version": out_tool_version},
            "objective": objective[:600],
            "required_actions": required_actions,
            "forbidden_actions": forbidden_actions,
            "required_artifacts": required_artifacts,
            "trigger_actions": trigger_actions,
            "completion": {"type": ctype, "detail": cdetail},
            "action_intent_guidance": action_intent_guidance,
        }
    }

# Repair loop
def run_with_repairs(
    model: str,
    api_key: str,
    messages: List[Dict[str, str]],
    expected_top_key: str,
    build_repair_messages_fn,
    max_repairs: int = 2,
) -> Dict[str, Any]:
    last_err = None
    cur_messages = list(messages)

    for _ in range(max_repairs + 1):
        out = openai_chat(model=model, api_key=api_key, messages=cur_messages, temperature=0.2)
        try:
            obj = safe_json_load(out)
            if expected_top_key not in obj:
                raise ValueError(f"Top-level key must be '{expected_top_key}'")
            if not isinstance(obj[expected_top_key], dict):
                raise ValueError(f"'{expected_top_key}' must be an object")
            return obj
        except Exception as e:
            last_err = str(e)
            cur_messages = build_repair_messages_fn(
                previous_output=out,
                error_msg=last_err,
                expected_top_key=expected_top_key,
            )

    raise RuntimeError(f"LLM failed after repairs. last_err={last_err}")

# Public API
def build_tool_plan(
    *,
    model: str,
    api_key: str,
    tool_name: str,
    tool_version: str,
    purpose: str,
    manual_text: str,
    actions_json: Dict[str, Any],
    artifact_catalog_json: Dict[str, Any],
    prompts_module,
    platform: str = "windows",
) -> Dict[str, Any]:

    specs = load_action_specs(actions_json)
    leaf_catalog = load_catalog_leaf_map(artifact_catalog_json, platform=platform)

    action_names = sorted(specs.keys())
    catalog_keys = sorted(leaf_catalog.keys())

    # IR
    msgs_ir = prompts_module.build_messages_extract_ir(
        tool_name=tool_name,
        tool_version=tool_version,
        purpose=purpose,
        manual_text=manual_text,
    )
    ir_obj = run_with_repairs(
        model=model,
        api_key=api_key,
        messages=msgs_ir,
        expected_top_key="ir",
        build_repair_messages_fn=prompts_module.build_messages_repair_json,
        max_repairs=2,
    )
    ir_obj = normalize_ir(ir_obj, tool_name, tool_version)
    fallback_objective = ir_obj["ir"]["objective"]

    # catalog: dict[leaf_key] -> {description, paths, ...} 
    artifact_candidates = []
    for k in catalog_keys:
        v = leaf_catalog.get(k, {})
        if not isinstance(v, dict):
            v = {}
        desc = str(v.get("description") or v.get("desc") or "")[:300]

        paths = v.get("paths") or v.get("path") or []
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list):
            paths = []
        paths = [str(p)[:260] for p in paths][:12]

        artifact_candidates.append({
            "key": k,
            "description": desc,
            "paths": paths,
        })

    # B) bounded mapping
    # *action detail bundle (lets LLM use llm_guidance to pick better actions like smart_click/type)
    allowed_actions = []
    for n in action_names:
        s = specs.get(n)
        if not s:
            continue
        allowed_actions.append({
            "name": s.name,
            "summary": s.summary,
            "params_schema": s.params_schema,
            "llm_guidance": s.llm_guidance,
        })

    msgs_map = prompts_module.build_messages_map_to_actions_and_artifacts(
        ir=ir_obj,
        allowed_action_names=action_names,
        allowed_actions=allowed_actions,
        artifact_catalog_keys=catalog_keys,
        artifact_candidates=artifact_candidates,
    )

    map_obj = run_with_repairs(
        model=model,
        api_key=api_key,
        messages=msgs_map,
        expected_top_key="mapping",
        build_repair_messages_fn=prompts_module.build_messages_repair_json,
        max_repairs=2,
    )
    map_obj = normalize_mapping(map_obj, specs, leaf_catalog, fallback_objective=fallback_objective)

    # C) finalize tool_plan
    msgs_tp = prompts_module.build_messages_finalize_tool_plan(
        tool_name=tool_name,
        tool_version=tool_version,
        purpose=purpose,
        mapping=map_obj,
    )
    tp_obj = run_with_repairs(
        model=model,
        api_key=api_key,
        messages=msgs_tp,
        expected_top_key="tool_plan",
        build_repair_messages_fn=prompts_module.build_messages_repair_json,
        max_repairs=2,
    )
    tp_obj = normalize_tool_plan(
        tp_obj,
        specs=specs,
        leaf_catalog=leaf_catalog,
        tool_name=tool_name,
        tool_version=tool_version,
        fallback_objective=fallback_objective,
    )

    tp_obj["tool_plan"]["_provenance"] = {
        "model": model,
        "notes": "3-stage TMI: IR -> bounded mapping -> tool_plan (leaf artifact keys only; with JSON repairs)",
        "platform": platform,
    }
    return tp_obj
