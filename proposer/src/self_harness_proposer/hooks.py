from __future__ import annotations

import ast
import json
import re
from pathlib import PurePosixPath
from typing import Any, Mapping

PROMPT_INSTRUCTION_HOOKS = {
    "system_prompt": "build_system_prompt",
    "build_system_prompt": "build_system_prompt",
    "bootstrap_instruction": "build_bootstrap_instruction",
    "build_bootstrap_instruction": "build_bootstrap_instruction",
    "execution_instruction": "build_execution_instruction",
    "build_execution_instruction": "build_execution_instruction",
    "verification_instruction": "build_verification_instruction",
    "build_verification_instruction": "build_verification_instruction",
    "failure_recovery_instruction": "build_failure_recovery_instruction",
    "build_failure_recovery_instruction": "build_failure_recovery_instruction",
    "multimodal_instruction": "build_multimodal_instruction",
    "build_multimodal_instruction": "build_multimodal_instruction",
}

LITERAL_RETURN_HOOKS = {
    "subagents": "build_subagents",
    "build_subagents": "build_subagents",
    "skills": "build_skills",
    "build_skills": "build_skills",
    "permissions": "build_permissions",
    "build_permissions": "build_permissions",
    "interrupt_on": "build_interrupt_on",
    "build_interrupt_on": "build_interrupt_on",
    "tools": "build_tools",
    "build_tools": "build_tools",
    "runtime_control_policy": "build_runtime_control_policy",
    "runtime_policy": "build_runtime_control_policy",
    "build_runtime_control_policy": "build_runtime_control_policy",
}

COMPOSITE_HOOKS = {
    "subagent_call_policy": "subagent_call_policy",
    "subagent_policy": "subagent_call_policy",
    "subagent_with_call_policy": "subagent_call_policy",
    "skill_bundle": "skill_bundle",
    "skill_sources": "skill_bundle",
    "middleware_policy": "middleware_policy",
    "middleware": "middleware_policy",
}

HOOK_ALIASES = {
    **PROMPT_INSTRUCTION_HOOKS,
    **LITERAL_RETURN_HOOKS,
    **COMPOSITE_HOOKS,
}

HOOKS_BY_MECHANISM_FAMILY = {
    "prompt_instruction": tuple(dict.fromkeys(PROMPT_INSTRUCTION_HOOKS.values())),
    "subagent": ("build_subagents", "subagent_call_policy"),
    "skill_procedure": ("build_skills", "skill_bundle"),
    "tool_configuration": ("build_tools",),
    "middleware": ("middleware_policy",),
    "runtime_control": ("build_runtime_control_policy",),
    "permission_interrupt": ("build_permissions", "build_interrupt_on"),
}


def hook_alias_menu() -> dict[str, list[str]]:
    return {family: list(hooks) for family, hooks in HOOKS_BY_MECHANISM_FAMILY.items()}


def canonical_hook_name(raw_name: str) -> str | None:
    normalized = raw_name.strip().lower()
    normalized = re.sub(r"\(\)\s*$", "", normalized)
    normalized = normalized.rsplit("/", 1)[-1]
    normalized = normalized.rsplit(":", 1)[-1]
    normalized = normalized.rsplit(" - ", 1)[-1]
    normalized = normalized.rsplit(".", 1)[-1]
    return HOOK_ALIASES.get(normalized)


def apply_candidate_values(
    *,
    current_values: Mapping[str, str],
    candidate_values: Mapping[str, Any],
    baseline_surface_name: str = "baseline",
) -> dict[str, str]:
    """Apply virtual hook aliases to the current surface values."""

    values = dict(current_values)
    if not candidate_values:
        return values
    if baseline_surface_name not in values:
        raise ValueError(f"missing baseline surface {baseline_surface_name!r}")
    if len(candidate_values) != 1:
        raise ValueError("mechanism-diverse candidates must change exactly one virtual hook")

    raw_hook, hook_value = next(iter(candidate_values.items()))
    hook = canonical_hook_name(str(raw_hook))
    if hook is None:
        raise ValueError(f"unknown virtual hook alias: {raw_hook!r}")
    baseline = values[baseline_surface_name]
    updated = _apply_hook_to_baseline(baseline_value=baseline, hook=hook, hook_value=hook_value)
    if updated is None:
        raise ValueError(f"could not apply hook {hook!r} to baseline surface")
    values[baseline_surface_name] = updated
    return values


def _apply_hook_to_baseline(*, baseline_value: str, hook: str, hook_value: Any) -> str | None:
    if hook == "subagent_call_policy":
        return _apply_subagent_call_policy(baseline_value=baseline_value, policy_value=hook_value)
    if hook == "skill_bundle":
        return _apply_skill_bundle(baseline_value=baseline_value, bundle_value=hook_value)
    if hook == "middleware_policy":
        return _apply_middleware_policy(baseline_value=baseline_value, policy_value=hook_value)
    if hook in PROMPT_INSTRUCTION_HOOKS.values():
        return _replace_function_return(
            source=baseline_value,
            function_name=hook,
            replacement_expr=repr(str(hook_value)),
        )
    return _replace_function_return(
        source=baseline_value,
        function_name=hook,
        replacement_expr=_literal_return_expression(hook_value),
    )


def _apply_subagent_call_policy(*, baseline_value: str, policy_value: Any) -> str | None:
    policy = _normalize_subagent_call_policy(policy_value)
    _validate_subagents(policy["subagents"])
    updated = _replace_function_return(
        source=baseline_value,
        function_name="build_subagents",
        replacement_expr=_literal_return_expression(policy["subagents"]),
    )
    if updated is None:
        return None
    return _replace_function_return(
        source=updated,
        function_name="build_verification_instruction",
        replacement_expr=repr(_parent_subagent_call_instruction(policy)),
    )


def _apply_skill_bundle(*, baseline_value: str, bundle_value: Any) -> str | None:
    bundle = _normalize_skill_bundle(bundle_value)
    body = _build_skills_function_body(bundle)
    updated = _replace_function_body(
        source=baseline_value,
        function_name="build_skills",
        replacement_body=body,
    )
    if updated is None:
        return None
    return _inject_skill_backend_support(updated)


def _apply_middleware_policy(*, baseline_value: str, policy_value: Any) -> str | None:
    policy = _normalize_middleware_policy(policy_value)
    updated = _upsert_candidate_middleware_function(baseline_value, policy)
    if updated is None:
        return None
    return _inject_candidate_middleware_builder(updated)


def _normalize_subagent_call_policy(policy_value: Any) -> dict[str, Any]:
    if isinstance(policy_value, str):
        policy_value = json.loads(policy_value)
    if not isinstance(policy_value, Mapping):
        raise ValueError("subagent_call_policy must be an object")
    subagents = policy_value.get("subagents")
    call_when = policy_value.get("call_when")
    if not isinstance(subagents, list) or not subagents:
        raise ValueError("subagent_call_policy.subagents must be a non-empty list")
    if not isinstance(call_when, list) or not any(str(item).strip() for item in call_when):
        raise ValueError("subagent_call_policy.call_when must be a non-empty list")
    return {
        "subagents": subagents,
        "call_when": [str(item).strip() for item in call_when if str(item).strip()],
        "parent_call_instruction": str(policy_value.get("parent_call_instruction") or "").strip(),
    }


def _validate_subagents(subagents: Any) -> None:
    if not isinstance(subagents, list) or not subagents:
        raise ValueError("build_subagents must be a non-empty list")
    for index, spec in enumerate(subagents):
        if not isinstance(spec, Mapping):
            raise ValueError(f"subagent {index} must be an object")
        for key in ("name", "description", "system_prompt"):
            if not isinstance(spec.get(key), str) or not spec[key].strip():
                raise ValueError(f"subagent {index} must include non-empty {key!r}")
        for invalid_key in ("prompt", "instruction", "instructions"):
            if invalid_key in spec:
                raise ValueError(f"subagent {index} uses invalid key {invalid_key!r}; use 'system_prompt'")


def _parent_subagent_call_instruction(policy: Mapping[str, Any]) -> str:
    subagents = policy["subagents"]
    primary_name = str(subagents[0]["name"]).strip()
    trigger_text = "; ".join(policy["call_when"])
    parent_text = str(policy.get("parent_call_instruction") or "").strip()
    canonical_policy = (
        "Subagent call policy: when any of these conditions holds, call the Deep Agents task tool "
        f'as `task(subagent_type="{primary_name}", description="...")` before final response: {trigger_text}. '
        "Pass expected artifact paths, validity criteria, and current evidence. If the subagent reports a missing, "
        "invalid, or failed artifact, fix it and re-check before claiming success."
    )
    return f"{parent_text}\n\n{canonical_policy}".strip() if parent_text else canonical_policy


def _normalize_skill_bundle(bundle_value: Any) -> dict[str, Any]:
    if isinstance(bundle_value, str):
        bundle_value = json.loads(bundle_value)
    if not isinstance(bundle_value, Mapping):
        raise ValueError("skill_bundle must be an object")
    skills = bundle_value.get("skills")
    if not isinstance(skills, list) or not skills:
        raise ValueError("skill_bundle.skills must be a non-empty list")
    normalized: list[dict[str, Any]] = []
    for index, spec in enumerate(skills):
        if not isinstance(spec, Mapping):
            raise ValueError(f"skill {index} must be an object")
        name = str(spec.get("name") or "").strip()
        description = str(spec.get("description") or "").strip()
        skill_md = spec.get("skill_md") if isinstance(spec.get("skill_md"), str) else ""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
            raise ValueError(f"skill {index} has invalid name {name!r}")
        if not description:
            raise ValueError(f"skill {index} must include description")
        if not skill_md.strip().startswith("---"):
            raise ValueError(f"skill {index} skill_md must include YAML frontmatter")
        files = spec.get("files") or []
        if not isinstance(files, list):
            raise ValueError(f"skill {index} files must be a list")
        normalized_files = []
        for file_index, file_spec in enumerate(files):
            if not isinstance(file_spec, Mapping):
                raise ValueError(f"skill {index} file {file_index} must be an object")
            path = str(file_spec.get("path") or "").strip()
            _validate_relative_component_path(path)
            content = file_spec.get("content")
            if not isinstance(content, str):
                raise ValueError(f"skill {index} file {file_index} must include string content")
            normalized_files.append({"path": path, "content": content})
        normalized.append(
            {
                "name": name,
                "description": description,
                "skill_md": skill_md,
                "files": normalized_files,
            }
        )
    return {"skills": normalized}


def _validate_relative_component_path(path: str) -> None:
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or pure.name == "SKILL.md":
        raise ValueError(f"invalid skill companion path: {path!r}")


def _normalize_middleware_policy(policy_value: Any) -> dict[str, str]:
    if isinstance(policy_value, str):
        policy_value = json.loads(policy_value)
    if not isinstance(policy_value, Mapping):
        raise ValueError("middleware_policy must be an object")
    name = str(policy_value.get("name") or "CandidateMiddleware").strip()
    instruction = str(policy_value.get("instruction") or "").strip()
    apply_when = str(policy_value.get("apply_when") or "always").strip().lower()
    aliases = {
        "tool-error": "tool_error",
        "tool error": "tool_error",
        "on_tool_error": "tool_error",
        "image": "image_input",
        "image-input": "image_input",
        "image input": "image_input",
        "start": "initial",
        "startup": "initial",
        "first_turn": "initial",
        "all": "always",
        "every_turn": "always",
    }
    apply_when = aliases.get(apply_when, apply_when)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("middleware_policy.name must be an identifier-like name")
    if not instruction:
        raise ValueError("middleware_policy.instruction must be non-empty")
    if apply_when not in {"always", "initial", "tool_error", "image_input"}:
        raise ValueError("middleware_policy.apply_when must be always, initial, tool_error, or image_input")
    return {"name": name, "instruction": instruction, "apply_when": apply_when}


def _candidate_middleware_function_source(policy: Mapping[str, str]) -> str:
    predicate_by_apply_when = {
        "always": "None",
        "initial": "lambda messages: not any(_is_tool_message(message) for message in messages)",
        "tool_error": "_has_tool_error",
        "image_input": "_has_image_input",
    }
    predicate_expr = predicate_by_apply_when[policy["apply_when"]]
    return f'''def build_candidate_middleware():
    """Candidate-provided narrow prompt middleware synthesized from middleware_policy."""
    middleware = _build_prompt_middleware(
        {policy["name"]!r},
        lambda: {policy["instruction"]!r},
        predicate={predicate_expr},
    )
    return (middleware,) if middleware is not None else ()

'''


def _upsert_candidate_middleware_function(source: str, policy: Mapping[str, str]) -> str | None:
    function_source = _candidate_middleware_function_source(policy)
    if "def build_candidate_middleware(" in source:
        return _replace_function_body(
            source=source,
            function_name="build_candidate_middleware",
            replacement_body="\n".join(function_source.splitlines()[1:]) + "\n",
        )
    span = _function_body_span(source=source, function_name="build_multimodal_middleware")
    if span is None:
        return None
    _body_start, body_end = span
    lines = source.splitlines(keepends=True)
    return "".join([*lines[:body_end], "\n", function_source, *lines[body_end:]])


def _inject_candidate_middleware_builder(source: str) -> str | None:
    if "build_candidate_middleware," in source:
        return source
    marker = "        build_multimodal_middleware,\n    ):\n"
    if marker not in source:
        return None
    return source.replace(
        marker,
        "        build_multimodal_middleware,\n        build_candidate_middleware,\n    ):\n",
        1,
    )


def _build_skills_function_body(bundle: Mapping[str, Any]) -> str:
    specs_expr = repr(bundle["skills"])
    return f'''    """Materialize candidate-provided Deep Agents skills and return their source root."""
    from pathlib import Path

    skills_root = Path(__file__).resolve().parent / ".self_harness_generated_skills"
    skill_specs = {specs_expr}
    for spec in skill_specs:
        skill_dir = skills_root / spec["name"]
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(spec["skill_md"], encoding="utf-8")
        for file_spec in spec.get("files", []):
            component_path = skill_dir / file_spec["path"]
            component_path.parent.mkdir(parents=True, exist_ok=True)
            component_path.write_text(file_spec["content"], encoding="utf-8")
    return ["/.self_harness_generated_skills"]
'''


def _skill_backend_function_source() -> str:
    return '''def build_skill_backend(backend: Any | None = None) -> Any:
    """Expose generated skill files through the backend path used by Deep Agents."""
    from pathlib import Path

    from deepagents.backends.composite import CompositeBackend
    from deepagents.backends.filesystem import FilesystemBackend
    from deepagents.backends.state import StateBackend

    skills_root = Path(__file__).resolve().parent / ".self_harness_generated_skills"
    default_backend = backend if backend is not None else StateBackend()
    return CompositeBackend(
        default=default_backend,
        routes={
            "/.self_harness_generated_skills/": FilesystemBackend(
                root_dir=skills_root,
                virtual_mode=True,
            )
        },
    )

'''


def _inject_skill_backend_support(source: str) -> str | None:
    skills_assignment = '    if skills:\n        kwargs["skills"] = skills\n'
    if skills_assignment not in source:
        return None
    updated = source.replace(
        skills_assignment,
        skills_assignment + '        kwargs["backend"] = build_skill_backend(backend)\n',
        1,
    )
    backend_assignment = '    if backend is not None:\n        kwargs["backend"] = backend\n'
    if backend_assignment in updated:
        updated = updated.replace(
            backend_assignment,
            '    if backend is not None and "backend" not in kwargs:\n        kwargs["backend"] = backend\n',
            1,
        )
    if "def build_skill_backend(" in updated:
        return updated
    span = _function_body_span(source=updated, function_name="build_skills")
    helper_source = _skill_backend_function_source()
    if span is None:
        return f"{updated.rstrip()}\n\n\n{helper_source}"
    _body_start, body_end = span
    lines = updated.splitlines(keepends=True)
    return "".join([*lines[:body_end], "\n", helper_source, *lines[body_end:]])


def _replace_function_return(*, source: str, function_name: str, replacement_expr: str) -> str | None:
    return _replace_function_body(
        source=source,
        function_name=function_name,
        replacement_body=f"    return {replacement_expr}\n",
    )


def _replace_function_body(*, source: str, function_name: str, replacement_body: str) -> str | None:
    span = _function_body_span(source=source, function_name=function_name)
    if span is None:
        return None
    body_start, body_end = span
    body = replacement_body if replacement_body.endswith("\n") else f"{replacement_body}\n"
    lines = source.splitlines(keepends=True)
    return "".join([*lines[:body_start], body, "\n", *lines[body_end:]])


def _function_body_span(*, source: str, function_name: str) -> tuple[int, int] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name and node.end_lineno is not None:
            return node.lineno, node.end_lineno
    return None


def _literal_return_expression(value: Any) -> str:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in {"none", "null"}:
            return "None"
        if stripped[:1] in {"[", "{", '"'} or stripped.lower() in {"true", "false"}:
            try:
                return repr(json.loads(stripped))
            except json.JSONDecodeError:
                pass
        return repr(value)
    return repr(value)
