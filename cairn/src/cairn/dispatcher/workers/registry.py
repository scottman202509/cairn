from __future__ import annotations

from cairn.dispatcher.workers.adapters import (
    ClaudeCodeDriver,
    CodexDriver,
    CursorDriver,
    MockDriver,
    OllamaDriver,
    PiDriver,
)
from cairn.dispatcher.workers.base import WorkerDriver


DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": ClaudeCodeDriver(),
    "codex": CodexDriver(),
    "cursor": CursorDriver(),
    "ollama": OllamaDriver(),
    "pi": PiDriver(),
    "mock": MockDriver(),
}


def get_driver(name: str) -> WorkerDriver:
    return DRIVERS[name]
