from __future__ import annotations

import json
from importlib import resources
from typing import Any


def load_prompt(group: str, name: str) -> str:
    return resources.files("cairn.dispatcher.prompts").joinpath(group).joinpath(name).read_text(encoding="utf-8")


def render_prompt(template: str, replacements: dict[str, str]) -> str:
    text = template
    for key, value in replacements.items():
        text = text.replace("{" + key + "}", value)
    return text


def format_fact_ids(fact_ids: list[str]) -> str:
    return format_json_block(fact_ids)


def format_open_intents(intents: list[dict[str, Any]]) -> str:
    return format_json_block(intents)


def format_hints(hints: list[dict[str, Any]]) -> str:
    return format_json_block(hints)


def format_available_workers(workers: list[dict[str, Any]]) -> str:
    """Render the worker capability digest passed to reason prompts.

    Each entry contains only fields the LLM needs: name, task_types,
    capabilities. Returned as pretty JSON so the LLM can pattern-match keys
    like `egress_zone` / `reachable_networks` / `tools` / `offensive_distro`.
    Returns the literal string ``[]`` when no workers are configured.
    """
    if not workers:
        return "[]"
    return format_json_block(workers)


def format_json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
