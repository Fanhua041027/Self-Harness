from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import tomllib
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import FileDownloadResponse
from harbor.agents.base import BaseAgent
from harbor.models.agent.context import AgentContext

from .backend_bridge import HarborSandbox, _map_exception_to_standard_error


CANDIDATE_WORKSPACE_ENV = "SELF_HARNESS_CANDIDATE_WORKSPACE"
MODEL_ENV = "SELF_HARNESS_MODEL"
TIMEOUT_ENV = "SELF_HARNESS_AGENT_TIMEOUT_SEC"


def _streaming_enabled() -> bool:
    raw = os.environ.get("DEEPAGENTS_HARBOR_STREAM", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@contextmanager
def _candidate_import_context():
    old_path = list(sys.path)
    old_module = sys.modules.pop("repo_baseline", None)
    for path in reversed(_candidate_surface_paths()):
        sys.path.insert(0, str(path))
    importlib.invalidate_caches()
    try:
        yield
    finally:
        sys.path[:] = old_path
        sys.modules.pop("repo_baseline", None)
        if old_module is not None:
            sys.modules["repo_baseline"] = old_module
        importlib.invalidate_caches()


def _candidate_surface_paths() -> list[Path]:
    raw = os.environ.get(CANDIDATE_WORKSPACE_ENV)
    if not raw:
        return []
    root = Path(raw).expanduser().resolve()
    paths = []
    if (root / "repo_baseline.py").exists():
        paths.append(root)
    if (root / "current" / "repo_baseline.py").exists():
        paths.append(root / "current")
    if not paths:
        raise RuntimeError(
            f"{CANDIDATE_WORKSPACE_ENV} does not contain repo_baseline.py or current/repo_baseline.py: {root}"
        )
    return paths


@contextmanager
def _patched_process_env(extra_env: dict[str, str] | None):
    if not extra_env:
        yield
        return

    previous: dict[str, str | None] = {}
    for key, value in extra_env.items():
        previous[key] = os.environ.get(key)
        os.environ[key] = value

    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _normalize_chunk(chunk: object) -> tuple[list[str], str | None, Any]:
    if isinstance(chunk, tuple):
        if len(chunk) == 3:
            namespace, stream_mode, data = chunk
            if isinstance(namespace, tuple):
                namespace_parts = [str(part) for part in namespace]
            elif namespace:
                namespace_parts = [str(namespace)]
            else:
                namespace_parts = []
            return namespace_parts, str(stream_mode), data
        if len(chunk) == 2:
            stream_mode, data = chunk
            return [], str(stream_mode), data
    return [], None, chunk


def _extract_text_blocks(message: Any) -> list[str]:
    blocks = getattr(message, "content_blocks", None)
    texts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
    if texts:
        return texts
    text_attr = getattr(message, "text", "")
    return [text_attr] if isinstance(text_attr, str) and text_attr else []


def _message_text(message: Any) -> str:
    blocks = _extract_text_blocks(message)
    if blocks:
        return "\n".join(blocks)
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item) for item in content)
    return ""


def _messages_from_result(result: Any) -> list[Any]:
    if isinstance(result, dict):
        payload = result.get("messages")
        if isinstance(payload, list):
            return payload
    return []


def _latest_ai_text(messages: list[Any]) -> str:
    for message in reversed(messages):
        if _is_ai_message(message):
            return _message_text(message)
    return ""


def _is_ai_message(message: Any) -> bool:
    message_type = str(getattr(message, "type", "")).lower()
    return message_type in {"ai", "assistant"} or message.__class__.__name__ == "AIMessage"


def _serialize_message(message: Any, metadata: Any, namespace: list[str], index: int) -> dict[str, Any]:
    return {
        "event_index": index,
        "namespace": namespace,
        "type": message.__class__.__name__,
        "text": getattr(message, "text", ""),
        "content": _json_safe(getattr(message, "content", None)),
        "content_blocks": _json_safe(getattr(message, "content_blocks", None)),
        "tool_calls": _json_safe(getattr(message, "tool_calls", None)),
        "name": getattr(message, "name", None),
        "usage_metadata": _json_safe(getattr(message, "usage_metadata", None)),
        "metadata": _json_safe(metadata),
    }


def _serialize_stream_data(stream_mode: str | None, data: Any, namespace: list[str], index: int) -> Any:
    if stream_mode == "messages" and isinstance(data, tuple) and len(data) == 2:
        message_obj, metadata = data
        return {
            "message": _serialize_message(message_obj, metadata, namespace, index),
            "metadata": _json_safe(metadata),
        }
    return _json_safe(data)


def _invoke_timeout_for_seen_messages(
    seen_messages: int, *, hard_timeout_sec: float | None
) -> float | None:
    del seen_messages
    if hard_timeout_sec is not None and hard_timeout_sec > 0:
        return hard_timeout_sec
    return None


def _coerce_timeout_sec(value: object) -> float | None:
    try:
        timeout_sec = float(value)
    except (TypeError, ValueError):
        return None
    return timeout_sec if timeout_sec > 0 else None


def _task_agent_timeout_sec_from_environment(environment: Any) -> float | None:
    environment_dir = getattr(environment, "environment_dir", None)
    if environment_dir is None:
        return None

    task_toml = Path(environment_dir).parent / "task.toml"
    try:
        data = tomllib.loads(task_toml.read_text())
    except Exception:
        return None

    agent_config = data.get("agent", {})
    if not isinstance(agent_config, dict):
        return None
    return _coerce_timeout_sec(agent_config.get("timeout_sec"))


def _effective_hard_timeout_sec(configured_timeout_sec: float | None, environment: Any) -> float | None:
    explicit = _coerce_timeout_sec(configured_timeout_sec)
    if explicit is not None:
        return explicit
    env_timeout = _coerce_timeout_sec(os.environ.get(TIMEOUT_ENV))
    if env_timeout is not None:
        return env_timeout
    return _task_agent_timeout_sec_from_environment(environment)


class FixedHarnessSandbox(HarborSandbox):
    """Harbor sandbox shim that treats missing memory files as normal misses."""

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        results: list[FileDownloadResponse] = []
        for path in paths:
            with tempfile.TemporaryDirectory() as tmpdir:
                local = Path(tmpdir) / (Path(path).name or "file")
                try:
                    await self.environment.download_file(path, local)
                    content = local.read_bytes()
                    results.append(FileDownloadResponse(path=path, content=content, error=None))
                except Exception as exc:
                    error = _map_exception_to_standard_error(exc)
                    if error is None and isinstance(exc, RuntimeError) and "Could not find the file" in str(exc):
                        error = "file_not_found"
                    if error is None:
                        raise
                    results.append(FileDownloadResponse(path=path, content=None, error=error))
        return results


class FixedHarnessWrapper(BaseAgent):
    """Run a Self-Harness Deep Agents surface inside Harbor."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        hard_timeout_sec: float | None = None,
        extra_env: dict[str, str] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir, model_name, *args, **kwargs)
        self._model_name = model_name or os.environ.get(MODEL_ENV)
        if not self._model_name:
            raise RuntimeError(f"model_name or {MODEL_ENV} is required")
        self._hard_timeout_sec = hard_timeout_sec
        self._extra_env = dict(extra_env or {})

    @staticmethod
    def name() -> str:
        return "fixed-harness-harbor"

    def version(self) -> str | None:
        return "0.1.6-public"

    async def setup(self, environment: Any) -> None:  # type: ignore[override]
        del environment

    async def run(self, instruction: str, environment: Any, context: AgentContext) -> None:  # type: ignore[override]
        backend = FixedHarnessSandbox(environment)
        effective_hard_timeout_sec = _effective_hard_timeout_sec(self._hard_timeout_sec, environment)
        thread_id = str(uuid.uuid4())
        with _patched_process_env(self._extra_env), _candidate_import_context():
            repo_baseline = importlib.import_module("repo_baseline")
            build_fixed_harness_agent = getattr(repo_baseline, "build_fixed_harness_agent")
            agent = build_fixed_harness_agent(self._model_name, backend=backend)

            self.logs_dir.mkdir(parents=True, exist_ok=True)
            events_path = self.logs_dir / "events.jsonl"
            messages_path = self.logs_dir / "messages.json"
            final_message_path = self.logs_dir / "final_message.txt"
            result_path = self.logs_dir / "result.json"
            invoke_state_path = self.logs_dir / "invoke_state.json"

            serialized_messages: list[dict[str, Any]] = []
            final_fragments: list[str] = []
            event_count = 0
            timed_out = False
            timeout_limit_sec: float | None = None
            error: Exception | None = None
            failure: str | None = None

            async def _consume_stream() -> None:
                nonlocal event_count

                async def _stream_once() -> None:
                    nonlocal event_count
                    with events_path.open("w", encoding="utf-8") as events_file:
                        async for chunk in agent.astream(
                            {"messages": [{"role": "user", "content": instruction}]},
                            config={"configurable": {"thread_id": thread_id}},
                            stream_mode=["messages", "updates"],
                            subgraphs=True,
                        ):
                            event_count += 1
                            namespace, stream_mode, data = _normalize_chunk(chunk)
                            events_file.write(
                                json.dumps(
                                    {
                                        "event_index": event_count,
                                        "namespace": namespace,
                                        "stream_mode": stream_mode,
                                        "data": _serialize_stream_data(
                                            stream_mode, data, namespace, event_count
                                        ),
                                    },
                                    ensure_ascii=True,
                                )
                                + "\n"
                            )
                            events_file.flush()

                            if stream_mode != "messages" or not isinstance(data, tuple) or len(data) != 2:
                                continue
                            message_obj, metadata = data
                            serialized = _serialize_message(message_obj, metadata, namespace, event_count)
                            serialized_messages.append(serialized)
                            if not namespace and _is_ai_message(message_obj):
                                final_fragments.extend(_extract_text_blocks(message_obj))

                if effective_hard_timeout_sec is not None:
                    async with asyncio.timeout(effective_hard_timeout_sec):
                        await _stream_once()
                else:
                    await _stream_once()

            async def _consume_invoke() -> None:
                nonlocal event_count
                invoke_timeout_sec = _invoke_timeout_for_seen_messages(
                    0, hard_timeout_sec=effective_hard_timeout_sec
                )
                with events_path.open("w", encoding="utf-8") as events_file:
                    invoke_state_path.write_text(
                        json.dumps(
                            {
                                "phase": "before_ainvoke",
                                "invoke_timeout_sec": invoke_timeout_sec,
                                "thread_id": thread_id,
                                "user_message_preview": instruction[:800],
                            },
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    events_file.write(
                        json.dumps(
                            {
                                "event_index": event_count + 1,
                                "namespace": [],
                                "stream_mode": "invoke_start",
                                "data": {
                                    "invoke_timeout_sec": invoke_timeout_sec,
                                },
                            },
                            ensure_ascii=True,
                        )
                        + "\n"
                    )
                    events_file.flush()
                    try:
                        if invoke_timeout_sec is None:
                            result = await agent.ainvoke(
                                {"messages": [{"role": "user", "content": instruction}]},
                                config={"configurable": {"thread_id": thread_id}},
                            )
                        else:
                            async with asyncio.timeout(invoke_timeout_sec):
                                result = await agent.ainvoke(
                                    {"messages": [{"role": "user", "content": instruction}]},
                                    config={"configurable": {"thread_id": thread_id}},
                                )
                    except asyncio.TimeoutError:
                        invoke_state_path.write_text(
                            json.dumps(
                                {
                                    "phase": "ainvoke_timeout",
                                    "invoke_timeout_sec": invoke_timeout_sec,
                                    "thread_id": thread_id,
                                    "user_message_preview": instruction[:800],
                                },
                                indent=2,
                            )
                            + "\n",
                            encoding="utf-8",
                        )
                        raise
                    invoke_state_path.write_text(
                        json.dumps(
                            {
                                "phase": "after_ainvoke",
                                "invoke_timeout_sec": invoke_timeout_sec,
                                "thread_id": thread_id,
                                "result_keys": sorted(result.keys()) if isinstance(result, dict) else None,
                            },
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    messages = _messages_from_result(result)
                    event_count = len(messages)
                    serialized_messages.extend(
                        _serialize_message(message_obj, {}, [], index)
                        for index, message_obj in enumerate(messages, start=1)
                    )
                    latest_text = _latest_ai_text(messages)
                    if latest_text:
                        final_fragments[:] = [latest_text]
                    events_file.write(
                        json.dumps(
                            {
                                "event_index": event_count + 1,
                                "namespace": [],
                                "stream_mode": "invoke_result",
                                "data": _json_safe(result),
                            },
                            ensure_ascii=True,
                        )
                        + "\n"
                    )
                    events_file.flush()

            try:
                if _streaming_enabled():
                    await _consume_stream()
                else:
                    await _consume_invoke()
            except TimeoutError:
                timed_out = True
                timeout_limit_sec = effective_hard_timeout_sec
            except asyncio.TimeoutError:
                timed_out = True
                timeout_limit_sec = effective_hard_timeout_sec
            except asyncio.CancelledError:
                timed_out = True
                timeout_limit_sec = effective_hard_timeout_sec
            except Exception as exc:
                error = exc
                failure = f"{exc.__class__.__name__}: {exc}"

            final_text = "".join(final_fragments)
            final_message_path.write_text(final_text, encoding="utf-8")
            messages_path.write_text(json.dumps(serialized_messages, indent=2) + "\n", encoding="utf-8")
            result_payload = {
                "final_text": final_text,
                "timed_out": timed_out,
                "event_count": event_count,
                "thread_id": thread_id,
                "failure": failure,
                "timeout_limit_sec": timeout_limit_sec,
                "invoke_state_path": str(invoke_state_path),
                "candidate_workspace": os.environ.get(CANDIDATE_WORKSPACE_ENV),
            }
            result_path.write_text(json.dumps(result_payload, indent=2) + "\n", encoding="utf-8")

            context.metadata = {
                "final_text": final_text,
                "model_name": self._model_name,
                "hard_timeout_sec": effective_hard_timeout_sec,
                "configured_hard_timeout_sec": self._hard_timeout_sec,
                "events_path": str(events_path),
                "messages_path": str(messages_path),
                "thread_id": thread_id,
                "event_count": event_count,
                "timed_out": timed_out,
                "failure": failure,
                "timeout_limit_sec": timeout_limit_sec,
                "candidate_workspace": os.environ.get(CANDIDATE_WORKSPACE_ENV),
            }

            if error is not None:
                raise error

            if timed_out:
                raise TimeoutError(
                    "FixedHarnessWrapper exceeded "
                    f"{timeout_limit_sec or 'the Harbor task timeout'} "
                    "before the agent returned"
                )


AgentWrapper = FixedHarnessWrapper
