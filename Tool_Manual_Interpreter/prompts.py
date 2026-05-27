# Tool_Manual_Interpreter/prompts.py
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)

def system_prompt_tmi() -> str:
    return (
        "You are the Tool Manual Interpreter (TMI) for PrZMA.\n"
        "You convert a forensic tool manual into a STRICT JSON plan for tool testing.\n"
        "\n"
        "Hard rules:\n"
        "- Output MUST be STRICT JSON and NOTHING else.\n"
        "- Do NOT include markdown, comments, or extra text.\n"
        "- You MUST ONLY use action names from allowed_action_names.\n"
        "- You MUST ONLY use artifact keys from artifact_catalog_keys.\n"
        "- Do NOT invent new artifact keys or actions.\n"
        "- Use artifact_candidates only as EVIDENCE to decide which KEYS to pick.\n"
        "- If uncertain, output fewer items rather than inventing.\n"
    )

# Step A: Extract IR-lite
def build_messages_extract_ir(
    tool_name: str,
    tool_version: str,
    purpose: str,
    manual_text: str,
) -> List[Dict[str, str]]:
    manual_trim = manual_text
    if len(manual_trim) > 18000:
        manual_trim = manual_trim[:18000] + "\n...<TRUNCATED>..."

    schema = {
        "ir": {
            "tool": {"name": "string", "version": "string"},
            "objective": "string",
            "capabilities": ["string"],
            "integrity_warnings": ["string"],
            "inputs_expected": ["string"],
            "outputs_expected": ["string"],
        }
    }

    user_payload = {
        "tool": {"name": tool_name, "version": tool_version or "unknown"},
        "purpose": purpose,
        "manual_text": manual_trim,
        "output_schema": schema,
        "instruction": (
            "Extract IR from the manual text. Keep it conservative and grounded in the text.\n"
            "If the manual is vague, write generic but truthful statements derived from the text.\n"
            "Keep lists short.\n"
        ),
    }

    return [
        {"role": "system", "content": system_prompt_tmi()},
        {"role": "user", "content": _json(user_payload)},
    ]

# Step B: Map IR -> actions + artifacts (bounded, but evidence-rich)
def build_messages_map_to_actions_and_artifacts(
    ir: Dict[str, Any],
    allowed_action_names: List[str],
    artifact_catalog_keys: List[str],
    artifact_candidates: List[Dict[str, Any]],
    allowed_actions: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, str]]:
    """
    Output must still choose ONLY from artifact_catalog_keys.
    artifact_candidates is provided as evidence (description/paths) to improve selection quality.
    """
    schema = {
        "mapping": {
            "objective": "string",
            "required_actions": [{"name": "string", "why": "string"}],
            "forbidden_actions": [{"name": "string", "why": "string"}],
            "required_artifacts": ["string"],
            "trigger_actions": ["string"],
        }
    }

    # candidates can get huge; keep them as-is but let caller pre-trim if needed.
    user_payload = {
        "ir": ir.get("ir", ir),
        "allowed_action_names": allowed_action_names,
        "allowed_actions": allowed_actions or [],
        "artifact_catalog_keys": artifact_catalog_keys,
        "artifact_candidates": artifact_candidates,
        "output_schema": schema,
        "instruction": (
            "Create a bounded mapping.\n\n"
            "1) required_actions: minimal set needed to generate traces/artifacts relevant to the tool.\n"
            "- You may use allowed_actions[].llm_guidance as intended-usage hints when selecting required/forbidden actions.\n"
            "2) forbidden_actions: actions that likely destroy/alter evidence OR prematurely terminate before artifacts exist.\n"
            "3) required_artifacts: choose artifact KEYS that the tool can actually parse/export according to the manual.\n"
            "   - Use artifact_candidates (description/paths) as evidence.\n"
            "   - Output ONLY KEYS, and they MUST be from artifact_catalog_keys.\n"
            "   - Prefer the smallest sufficient set.\n"
            " Trigger-actions policy (VERY IMPORTANT):\n"
            "- trigger_actions is used to trigger snapshots; choose ONLY high-signal actions.\n"
            "- Do NOT include repetitive/navigation actions that may happen many times.\n"
            "- Prefer actions that represent a meaningful, discrete state change likely to produce stable artifacts\n"
            "  (e.g., login completion, sending a message, uploading a file, finishing a download, closing the app/browser).\n"
            "- trigger_actions MUST NOT include any action listed in forbidden_actions.\n"
            "- Output 0 to 2 trigger_actions only. If uncertain, output an empty list [].\n\n"
            "- Use ONLY items from the provided lists.\n"
            "- Do NOT output non-leaf keys.\n"
            "- Keep 'why' short and concrete.\n"

            "Forbidden-actions policy (IMPORTANT):\n"
            "- forbidden_actions must ONLY include actions that directly harm artifact generation/preservation/reproducibility.\n"
            "  Examples of valid why: 'clears cache so ChromeCacheView cannot enumerate', 'resets profile so paths no longer match',\n"
            "  'closes browser before cache flush so cache_data is incomplete'.\n"
            "- Do NOT include actions for generic 'risk', 'privacy', 'policy', or 'security' reasons unless they directly affect artifacts.\n"
            "- Provide 0 to 5 items (prefer 1-3). If none are needed, output an empty list [].\n"
            "- Each forbidden_actions item MUST include a specific artifact impact in 'why' (mention what breaks).\n"
        ),
    }

    return [
        {"role": "system", "content": system_prompt_tmi()},
        {"role": "user", "content": _json(user_payload)},
    ]

# Step C: Finalize tool_plan
def build_messages_finalize_tool_plan(
    tool_name: str,
    tool_version: str,
    purpose: str,
    mapping: Dict[str, Any],
) -> List[Dict[str, str]]:
    schema = {
        "tool_plan": {
            "tool": {"name": "string", "version": "string"},
            "objective": "string",
            "required_actions": [{"name": "string", "why": "string"}],
            "forbidden_actions": [{"name": "string", "why": "string"}],
            "required_artifacts": ["string"],
            "trigger_actions": ["string"],
            "completion": {"type": "string", "detail": "string"},
            "action_intent_guidance": "string"
        }
    }
    
    user_payload = {
        "tool": {"name": tool_name, "version": tool_version or "unknown"},
        "purpose": purpose,
        "mapping": mapping.get("mapping", mapping),
        "output_schema": schema,
        "instruction": (
            "Finalize tool_plan from the mapping.\n"
            "completion.type must be one of: ['min_required_actions', 'required_artifacts_present', 'custom'].\n"
            "Choose a conservative completion criterion.\n\n"

            "IMPORTANT: You MUST also produce 'action_intent_guidance' as a concise execution guide for the Automation Agent.\n"
            "The goal is to make the chosen actions actually generate the required_artifacts in a realistic way.\n"
            "Do NOT add new actions or artifact keys.\n\n"

            "action_intent_guidance requirements:\n"
            "- Provide a short, practical 'Action Flow' description that explains how to execute the required_actions to produce required_artifacts.\n"
            "- action_intent_guidance MUST NOT introduce new actions. It must only describe how to instantiate and sequence the already-selected required_actions (using only their existing params, targets, and ordering) to maximize the chance of producing required_artifacts.\n" 
            "- Do NOT suggest clicks/scrolls/downloads/forms unless there is an explicit allowed action for it.\n"
            "- Make it artifact-driven: explicitly mention what kinds of user activity generate those artifacts.\n"
            "- Do NOT hardcode numeric counts (e.g., 'visit exactly N sites'). Use qualitative guidance like 'a few distinct domains'.\n"
            "- If browser.goto is used:\n"
            "  * Use reachable, real URLs. You Have to Check First! \n"
            "  * Prefer pages that naturally create the target artifacts (e.g., pages with images/scripts/resources for cache).\n"
            "  * Avoid actions that cause tool/download flows unless the artifact requires it.\n"
            "- Include 'Reason guidance': explain how to write reasons so they reflect the current page/context and the specific artifact effect.\n"
            "  * Avoid repeating a single fixed sentence.\n"
            "  * Reasons should reference what was done (e.g., 'loaded image-heavy page to populate cache entries').\n\n"

            "Output ONLY the JSON object.\n"
        ) 
    }

    return [
        {"role": "system", "content": system_prompt_tmi()},
        {"role": "user", "content": _json(user_payload)},
    ]

# Repair prompt
def build_messages_repair_json(
    previous_output: str,
    error_msg: str,
    expected_top_key: str,
) -> List[Dict[str, str]]:
    user_payload = {
        "previous_output": previous_output[:12000],
        "error": error_msg,
        "requirement": (
            "Fix the output so it becomes STRICT JSON.\n"
            f"It MUST be a single JSON object whose top-level key is '{expected_top_key}'.\n"
            "Do not add any extra keys.\n"
        ),
    }

    return [
        {"role": "system", "content": system_prompt_tmi()},
        {"role": "user", "content": _json(user_payload)},
    ]
