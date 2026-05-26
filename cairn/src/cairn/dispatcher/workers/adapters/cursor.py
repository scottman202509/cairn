"""Cursor-backed worker that drives Cursor's headless ``cursor-agent`` CLI.

Cursor is its own coding agent runtime (same loop as the Cursor IDE),
billed through a Cursor subscription rather than a model-provider API
key. This driver exposes it to Cairn so users with Cursor Pro can run
projects without separate Anthropic / OpenAI / DeepSeek credentials.

Design notes:

* ``-p`` runs the agent non-interactively; ``--force`` and ``--trust`` are
  mandatory in headless mode or the CLI will stall waiting for approval and
  workspace-trust prompts.
* ``--output-format json`` makes the CLI emit a single
  ``{"type":"result","session_id":"...","result":"..."}`` object on stdout
  when the run finishes. We parse ``session_id`` for resume and ``result``
  for the assistant text -- same pattern as :class:`PiDriver`.
* The healthcheck does a real but tool-free LLM ping via ``--mode ask``.
  ``ask`` mode is Q&A only (read-only, no tool calling), so it returns
  fast and doesn't burn full agent overhead. This matches how the
  Anthropic / OpenAI / Pi healthchecks send a tiny "ping" message.
* Cursor always runs inside a workspace directory. We point it at an
  empty per-worker dir under ``/tmp/cairn-cursor/<worker_name>`` so it
  has nothing to crawl and different workers don't clobber each other.
"""

from __future__ import annotations

import json
import shlex
from pathlib import PurePosixPath
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, WorkerDriver


# Shell preamble: create the per-worker workspace dir and exec the actual
# cursor-agent invocation from inside it. Kept as a single string so the
# argv stays a stable shape across healthcheck / execute / conclude.
_WORKSPACE_SCRIPT = (
    'workdir="$1"\n'
    'shift\n'
    'mkdir -p "$workdir"\n'
    'cd "$workdir"\n'
    'exec "$@"\n'
)


class CursorDriver(WorkerDriver):
    type_name = "cursor"

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return self._wrap_in_workspace(worker, self._healthcheck_argv(worker))

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        # Startup uses the same ping as runtime -- it's already cheap.
        return self.build_healthcheck(worker)

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        return shlex.join(self._healthcheck_argv(worker))

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        # Cursor mints its own chatId server-side, so we never pre-seed a
        # session here. We learn the id from stdout (extract_session below)
        # and pass it back via --resume on the conclude call.
        return DriverResult(argv=self._wrap_in_workspace(worker, self._exec_argv(worker, prompt, resume=None)))

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return self._wrap_in_workspace(worker, self._exec_argv(worker, prompt, resume=session))

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        for event in self._iter_events(stdout):
            sid = event.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
        return None

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        # In --output-format json mode Cursor emits exactly one result event;
        # in stream-json mode it emits many. Walk all events and prefer the
        # latest one whose type is "result" -- falling back to stdout if
        # nothing parses (e.g. the CLI errored before producing JSON).
        latest_result: str | None = None
        for event in self._iter_events(stdout):
            if event.get("type") != "result":
                continue
            result = event.get("result")
            if isinstance(result, str):
                latest_result = result
        if latest_result is not None:
            return latest_result.strip() or stdout
        return stdout

    # ------------------------------------------------------------------ argv

    @staticmethod
    def _healthcheck_argv(worker: WorkerConfig) -> list[str]:
        return [
            "cursor-agent",
            "-p",
            "--force",
            "--trust",
            "--mode",
            "ask",
            "--model",
            worker.env["CURSOR_MODEL"],
            "--output-format",
            "text",
            "Reply with exactly pong.",
        ]

    @staticmethod
    def _exec_argv(worker: WorkerConfig, prompt: str, *, resume: str | None) -> list[str]:
        argv = [
            "cursor-agent",
            "-p",
            "--force",
            "--trust",
            "--model",
            worker.env["CURSOR_MODEL"],
            "--output-format",
            "json",
        ]
        if resume:
            argv.extend(["--resume", resume])
        # "--" terminates option parsing so prompts that start with `-` are
        # not mistaken for flags. cursor-agent is built on Commander.js
        # which honors this convention.
        argv.extend(["--", prompt])
        return argv

    @classmethod
    def _wrap_in_workspace(cls, worker: WorkerConfig, argv: list[str]) -> list[str]:
        return [
            "/bin/sh",
            "-lc",
            _WORKSPACE_SCRIPT,
            "--",
            cls._workspace_dir(worker),
            *argv,
        ]

    @staticmethod
    def _workspace_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath("/tmp/cairn-cursor") / worker.name)

    # ------------------------------------------------------------------ json

    @staticmethod
    def _iter_events(stdout: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events
