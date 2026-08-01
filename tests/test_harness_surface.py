from __future__ import annotations

import pytest


def test_prompt_middleware_implements_langchain_contract() -> None:
    pytest.importorskip("deepagents")
    middleware_module = pytest.importorskip("langchain.agents.middleware")
    messages_module = pytest.importorskip("langchain_core.messages")

    from eval.harness_workspace import repo_baseline

    middleware = repo_baseline.build_execution_middleware()

    assert len(middleware) == 1
    assert isinstance(middleware[0], middleware_module.AgentMiddleware)

    class Request:
        messages: list[object] = []
        system_message = messages_module.SystemMessage("base")

        def override(self, **changes: object) -> object:
            return changes["system_message"]

    updated = middleware[0]._modify_request(Request())
    assert isinstance(updated, messages_module.SystemMessage)
