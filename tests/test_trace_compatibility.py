from __future__ import annotations

from self_harness_diagnosis.trace import extract_task_description, normalize_trace_steps


def test_harbor_serialized_langchain_messages_are_normalized() -> None:
    payload = {
        "outputs": {
            "messages": [
                {"type": "HumanMessage", "text": "solve this"},
                {
                    "type": "AIMessage",
                    "text": "",
                    "tool_calls": [{"name": "execute", "args": {"command": "pwd"}}],
                },
                {
                    "type": "ToolMessage",
                    "name": "execute",
                    "status": "success",
                    "text": "/app",
                },
            ]
        }
    }

    steps = normalize_trace_steps(payload)

    assert extract_task_description(payload) == "solve this"
    assert len(steps) == 1
    assert steps[0].tool_calls[0].name == "execute"
    assert steps[0].tool_results[0].name == "execute"
