"""Ollama-backed worker that runs Codex CLI against a local Ollama server.

This driver lets Cairn run end-to-end against a local LLM without any cloud
API key. It reuses the Codex CLI's agent loop, tool calling, and session
machinery -- the only thing that changes is where the LLM lives:

* ``wire_api`` is forced to ``"chat"`` (Ollama's OpenAI compatibility layer
  exposes ``/v1/chat/completions`` but not ``/v1/responses``).
* ``base_url`` points at ``$OLLAMA_BASE_URL/v1`` (typically
  ``http://localhost:11434/v1`` when the worker container runs with
  ``network_mode: host``, or ``http://host.docker.internal:11434/v1`` on
  Docker Desktop).
* No API key is required by Ollama, but Codex demands ``env_key`` resolve to
  *some* non-empty string. We inject a dummy ``OLLAMA_API_KEY=ollama`` into
  the process env so users don't have to.

IMPORTANT: the chosen Ollama model MUST support tool calling, otherwise the
agent loop has no way to actually run nmap/curl/etc. As of late 2025 that
includes ``qwen2.5``, ``qwen2.5-coder``, ``llama3.1``, ``llama3.2``,
``mistral-nemo``, ``firefunction-v2``, and others. Models without tool
support (``llama2``, ``gemma``, ``phi3``) will load but never actually
explore anything.
"""

from __future__ import annotations

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.adapters._curl import (
    build_verbose_curl_healthcheck,
    render_curl_command,
)
from cairn.dispatcher.workers.base import DriverResult, RegexSessionDriver


# Dummy API key written into the worker's env for Codex's auth bookkeeping.
# Ollama itself ignores Authorization headers, but Codex CLI refuses to start
# unless the configured env_key resolves to a non-empty value.
_DUMMY_API_KEY_ENV_VAR = "OLLAMA_API_KEY"
_DUMMY_API_KEY_VALUE = "ollama"

# Conservative ping: tiny chat-completions request that proves the server is
# up AND the requested model is loadable. Single token output keeps latency
# low enough for the dispatcher's healthcheck timeout.
_HEALTHCHECK_PATH = "/v1/chat/completions"


class OllamaDriver(RegexSessionDriver):
    type_name = "ollama"

    def build_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return [
            "curl",
            "-sS",
            "--fail",
            "-o",
            "/dev/null",
            self._chat_url(worker),
            "-H",
            "content-type: application/json",
            "-d",
            self._healthcheck_payload(worker),
        ]

    def build_startup_healthcheck(self, worker: WorkerConfig) -> list[str]:
        return build_verbose_curl_healthcheck(
            self._chat_url(worker),
            headers=["-H", "content-type: application/json"],
            payload=self._healthcheck_payload(worker),
        )

    def describe_startup_healthcheck(self, worker: WorkerConfig) -> str:
        return render_curl_command(
            self._chat_url(worker),
            headers=["-H", "content-type: application/json"],
            payload=self._healthcheck_payload(worker),
        )

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        return DriverResult(argv=self._codex_argv(worker, prompt))

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return self._codex_argv(worker, prompt, resume_session=session)

    def _codex_argv(self, worker: WorkerConfig, prompt: str, *, resume_session: str | None = None) -> list[str]:
        env = worker.env
        argv: list[str] = ["env", f"{_DUMMY_API_KEY_ENV_VAR}={_DUMMY_API_KEY_VALUE}", "codex", "exec"]
        if resume_session:
            argv.extend(["resume", resume_session])
        argv.extend(
            [
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                env["OLLAMA_MODEL"],
                "-c",
                'model_provider="cairn"',
                "-c",
                'model_providers.cairn.name="cairn"',
                # Ollama only speaks chat-completions, not the OpenAI Responses API.
                "-c",
                'model_providers.cairn.wire_api="chat"',
                "-c",
                f'model_providers.cairn.base_url="{self._openai_base_url(worker)}"',
                "-c",
                f'model_providers.cairn.env_key="{_DUMMY_API_KEY_ENV_VAR}"',
                "--",
                prompt,
            ]
        )
        return argv

    @staticmethod
    def _openai_base_url(worker: WorkerConfig) -> str:
        # Strip a trailing slash from the user-provided base URL so we don't
        # produce //v1 when concatenating.
        base = worker.env["OLLAMA_BASE_URL"].rstrip("/")
        return f"{base}/v1"

    @classmethod
    def _chat_url(cls, worker: WorkerConfig) -> str:
        return f"{worker.env['OLLAMA_BASE_URL'].rstrip('/')}{_HEALTHCHECK_PATH}"

    @staticmethod
    def _healthcheck_payload(worker: WorkerConfig) -> str:
        # max_tokens=1 + stream=false is the cheapest possible end-to-end
        # round-trip that still loads the model into Ollama memory.
        return (
            '{"model":"'
            + worker.env["OLLAMA_MODEL"]
            + '","messages":[{"role":"user","content":"ping"}],"max_tokens":1,"stream":false}'
        )
