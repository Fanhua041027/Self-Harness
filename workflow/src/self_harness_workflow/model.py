from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

MODEL_ID = "gpt-5.6-sol"
ADAPTER_VERSION = "self-harness-openai-compatible.v1"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass(frozen=True)
class ModelSpec:
    provider: str = "openai-compatible"
    model: str = MODEL_ID
    temperature: float | None = None
    seed: int | None = None
    max_output_tokens: int = 4096
    adapter_version: str = ADAPTER_VERSION

    @classmethod
    def from_env(cls) -> "ModelSpec":
        if not os.environ.get("SELF_HARNESS_BASE_URL"):
            raise RuntimeError("SELF_HARNESS_BASE_URL is required")
        if not os.environ.get("SELF_HARNESS_API_KEY"):
            raise RuntimeError("SELF_HARNESS_API_KEY is required")
        return cls()

    def fingerprint(self, base_url: str) -> str:
        parsed = urllib.parse.urlsplit(base_url)
        path = parsed.path or "/"
        if path != "/":
            path = "/" + path.strip("/")
        endpoint = f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}{path}"
        if parsed.port:
            endpoint = f"{parsed.scheme.lower()}://{(parsed.hostname or '').lower()}:{parsed.port}{path}"
        return hashlib.sha256(_canonical_json({**asdict(self), "endpoint": endpoint})).hexdigest()


class FingerprintGate:
    REQUIRED_ROLES = frozenset({"execution", "diagnosis", "proposal"})

    def __init__(self) -> None:
        self._fingerprint: str | None = None
        self._roles: set[str] = set()

    def check(self, role: str, fingerprint: str) -> None:
        if role not in self.REQUIRED_ROLES:
            raise ValueError(f"unknown model role: {role}")
        if self._fingerprint is not None and fingerprint != self._fingerprint:
            raise RuntimeError("model fingerprint mismatch across experiment roles")
        self._fingerprint = fingerprint
        self._roles.add(role)

    def assert_complete(self) -> str:
        missing = self.REQUIRED_ROLES - self._roles
        if missing:
            raise RuntimeError(f"model fingerprint gate missing roles: {sorted(missing)}")
        return str(self._fingerprint)


@dataclass(frozen=True)
class AdapterResponse:
    content: str
    model: str
    request_id: str | None
    usage: Mapping[str, Any]
    elapsed_ms: int
    request_hash: str
    response_hash: str
    api_style: str


class OpenAICompatibleAdapter:
    """Dependency-free adapter supporting Responses and Chat Completions."""

    def __init__(self, spec: ModelSpec, *, base_url: str | None = None, api_key: str | None = None,
                 api_style: str | None = None) -> None:
        self.spec = spec
        self.base_url = (base_url or os.environ.get("SELF_HARNESS_BASE_URL") or "").rstrip("/")
        self._api_key = api_key or os.environ.get("SELF_HARNESS_API_KEY") or ""
        self.api_style = api_style or os.environ.get("SELF_HARNESS_API_STYLE", "auto")
        if self.api_style not in {"auto", "responses", "chat/completions"}:
            raise ValueError("SELF_HARNESS_API_STYLE must be auto, responses, or chat/completions")
        if not self.base_url or not self._api_key:
            raise RuntimeError("SELF_HARNESS_BASE_URL and SELF_HARNESS_API_KEY are required")

    @property
    def fingerprint(self) -> str:
        return self.spec.fingerprint(self.base_url)

    def invoke(self, messages: Sequence[Mapping[str, Any]]) -> AdapterResponse:
        if self.api_style != "auto":
            return self._invoke_selected(self.api_style, messages)
        try:
            return self._invoke_style("responses", messages)
        except urllib.error.HTTPError as exc:
            if not _route_absent(exc):
                detail = _http_error_detail(exc)
                raise RuntimeError(f"responses request rejected (HTTP {exc.code}): {detail}") from exc
        return self._invoke_selected("chat/completions", messages)

    def _invoke_selected(self, style: str, messages: Sequence[Mapping[str, Any]]) -> AdapterResponse:
        try:
            return self._invoke_style(style, messages)
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            raise RuntimeError(f"{style} request rejected (HTTP {exc.code}): {detail}") from exc

    def _invoke_style(self, style: str, messages: Sequence[Mapping[str, Any]]) -> AdapterResponse:
        if style == "responses":
            payload = {"model": self.spec.model, "input": list(messages),
                       "max_output_tokens": self.spec.max_output_tokens}
        else:
            payload = {"model": self.spec.model, "messages": list(messages),
                       "max_tokens": self.spec.max_output_tokens}
        if self.spec.temperature is not None:
            payload["temperature"] = self.spec.temperature
        if style == "chat/completions" and self.spec.seed is not None:
            payload["seed"] = self.spec.seed
        raw = _canonical_json(payload)
        request = urllib.request.Request(
            f"{self.base_url}/{style}", data=raw, method="POST",
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
        )
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=180) as response:
            response_raw = response.read()
            request_id = response.headers.get("x-request-id")
        body = json.loads(response_raw)
        actual_model = str(body.get("model") or "")
        if actual_model != self.spec.model:
            raise RuntimeError(f"endpoint model mismatch: expected {self.spec.model!r}, got {actual_model!r}")
        content = _response_text(body, style)
        return AdapterResponse(
            content=content, model=actual_model, request_id=request_id or body.get("id"),
            usage=body.get("usage") if isinstance(body.get("usage"), Mapping) else {},
            elapsed_ms=int((time.monotonic() - started) * 1000), request_hash=hashlib.sha256(raw).hexdigest(),
            response_hash=hashlib.sha256(content.encode()).hexdigest(), api_style=style,
        )


def _route_absent(exc: urllib.error.HTTPError) -> bool:
    if exc.code == 405:
        return True
    if exc.code != 404:
        return False
    try:
        body = exc.read(1000)
    except OSError:
        body = b""
    return not body.strip()


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        detail = exc.read(1000).decode("utf-8", errors="replace").strip()
    except OSError:
        detail = ""
    return detail or exc.reason or "request rejected"


def _response_text(body: Mapping[str, Any], style: str) -> str:
    if style == "chat/completions":
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("invalid chat/completions response") from exc
        if not isinstance(content, str):
            raise ValueError("chat/completions response content must be a string")
        return content
    if isinstance(body.get("output_text"), str):
        return str(body["output_text"])
    texts = []
    for item in body.get("output", []):
        for part in item.get("content", []):
            if part.get("type") in {"output_text", "text"}:
                texts.append(str(part.get("text") or ""))
    if not texts:
        raise ValueError("invalid responses response")
    return "".join(texts)
