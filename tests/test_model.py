from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from self_harness_workflow.model import FingerprintGate, ModelSpec, OpenAICompatibleAdapter


class FakeHandler(BaseHTTPRequestHandler):
    seen_authorization = ""
    seen_requests = []
    responses_status = 404

    def do_POST(self):
        FakeHandler.seen_authorization = self.headers.get("Authorization", "")
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length))
        FakeHandler.seen_requests.append((self.path, request))
        if self.path.endswith("/responses"):
            self.send_response(FakeHandler.responses_status)
            self.end_headers()
            return
        body = json.dumps({"id": "req_fake", "model": request["model"], "choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def test_adapter_falls_back_without_leaking_key():
    FakeHandler.responses_status = 404
    FakeHandler.seen_requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        adapter = OpenAICompatibleAdapter(ModelSpec(), base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key="secret-value")
        response = adapter.invoke([{"role": "user", "content": "hello"}])
        assert response.content == "ok"
        assert response.api_style == "chat/completions"
        assert "secret-value" not in repr(response)
        assert FakeHandler.seen_authorization == "Bearer secret-value"
        chat_payload = FakeHandler.seen_requests[-1][1]
        assert "temperature" not in chat_payload and "seed" not in chat_payload
    finally:
        server.shutdown()


def test_adapter_does_not_fallback_on_payload_error():
    FakeHandler.responses_status = 400
    FakeHandler.seen_requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        adapter = OpenAICompatibleAdapter(ModelSpec(), base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key="test")
        with pytest.raises(RuntimeError, match="responses request rejected.*400"):
            adapter.invoke([{"role": "user", "content": "hello"}])
        assert len(FakeHandler.seen_requests) == 1
    finally:
        server.shutdown()


def test_fingerprint_normalizes_path_and_gates_roles():
    spec = ModelSpec()
    assert spec.fingerprint("HTTPS://EXAMPLE.COM/v1") == spec.fingerprint("https://example.com/other")
    gate = FingerprintGate()
    fingerprint = spec.fingerprint("https://example.com/v1")
    for role in ("execution", "diagnosis", "proposal"):
        gate.check(role, fingerprint)
    assert gate.assert_complete() == fingerprint
    with pytest.raises(RuntimeError, match="mismatch"):
        gate.check("proposal", "different")
