# Mechanism Reproduction

This checkout preserves the upstream Self-Harness skeleton and adds an auditable mechanism reproduction. It does not claim to reproduce the paper's Terminal-Bench 2.0 scores.

## Provenance and license

The baseline is fixed to `qzzqzzb/Self-Harness` commit `2720dbb3f52283684f4b85a1065d642df1779dd8`, tree `06b167ef705b6cc0d43a34d6cdec430479fc1f7e`. `provenance.yaml` keeps this identity even when the source was downloaded as an archive without `.git`. No standalone upstream license was found; redistribution requires permission or legal review.

## Environment

Use Python 3.11+ on Windows 11. Install the auditable mechanism layer with `python -m pip install -e ".[dev]"`. The core workflow and compatible API adapter use only the standard library; `.[harness]` installs the pinned DeepAgents integration and `.[harbor]` installs Harbor separately. Harbor 0.20.0 currently pulls a large `litellm` source dependency on this Windows/Python environment, so use a dedicated virtual environment and expect installation to take time; do not install these optional groups into an unrelated application's environment. Full Harbor evaluation additionally requires Docker Desktop with WSL2, the external eval project, and `eval/configs/harbor_eval.example.toml`. Harbor is execution/evaluation only and is not used for proposal or acceptance.

The compatible endpoint must expose either `/responses` or `/chat/completions` below its base URL. Responses is probed first. Credentials are read only from the process environment:

```bash
export SELF_HARNESS_BASE_URL=https://provider.example/v1
export SELF_HARNESS_API_KEY=...
```

The logical and remote model are fixed to `gpt-5.6-sol`. Execution, diagnosis, and proposal must pass the same normalized fingerprint gate. Fingerprints include provider, model, endpoint scheme/host/port, generation parameters, and adapter version; they never include the key or endpoint path.

## Offline deterministic run

No endpoint, Docker, Harbor, or secret is used:

```bash
python workflow/scripts/run_mechanism_reproduction.py --offline-demo --output-dir runs/offline-demo
pytest -q
```

The run creates `baseline`, `weakness_mining`, `proposals`, `candidates`, `validation`, `sealed`, and `logs`. It emits failure records, exact-signature clusters, evidence, independent `H+d1` and `H+d2` results, a fresh-parent `H+d1+d2` retest, frozen artifact identity, and one sealed result.

## Experimental policy

Only Evolution failures enter diagnosis and proposal. Exact signature is `(terminal verifier cause, causal agent behavior, abstract mechanism)`. A cluster requires at least two failures across at least two tasks; one failure cannot trigger a proposal. Regression affects promotion but its traces and answers remain hidden from proposer. Acceptance is:

`delta_evo >= 0 and delta_reg >= 0 and max(delta_evo, delta_reg) > 0`

The paper's held-in/held-out rule is retained conceptually; this reproduction names the selection splits Evolution and Regression. Sealed is the final generalization test, runs only after freeze, and cannot feed diagnosis, proposal, or version selection. A completed or aborted sealed attempt is terminal.

Candidate edits are limited to one component, one file, and one declared surface. Read-only snapshots protect model parameters, verifier, tests, answers, thresholds, split manifest, and acceptance policy. Prompt token delta uses a clearly labeled local byte approximation because compatible APIs do not provide a universal preflight tokenizer.

`configs/replication_defaults.json` contains K=3, T=2, two repeats, seeds, budgets, and minimum support. These are reproduction defaults, not paper-reported parameters. `configs/splits/mechanism_smoke.json` is a versioned deterministic smoke split; real benchmark task answers and verifiers remain external/read-only.

## Online probe and orchestration

A capability probe sends exactly one minimal non-Sealed request and prints only model identity, API style, request ID, usage, and hashes:

```bash
python workflow/scripts/run_mechanism_reproduction.py --probe-model
```

The online orchestration requires an explicit evaluator plugin; it never pretends to run Harbor. The plugin is a Python file defining `evaluate(stage, payload)` and is called for `baseline` and `candidates`. The execution input must be a non-Sealed JSON object. All execution, diagnosis, and proposal model calls use the same adapter and fingerprint gate; diagnosis and proposals must return strict JSON.

```bash
python workflow/scripts/run_mechanism_reproduction.py --online-workflow \
  --eval-plugin eval/plugins/mechanism_smoke.py \
  --execution-input eval/fixtures/evolution_input.json \
  --split-manifest configs/splits/mechanism_smoke.json \
  --output-dir runs/online-smoke
```

The bundled `mechanism_smoke.py` is a real deterministic verifier for the three non-Sealed fixture cases. It validates that the workflow actually dispatches baseline and candidate artifacts, but it is not Terminal-Bench and it does not substitute for Harbor. If execution already passes every Evolution case, the workflow stops after diagnosis with `stop_reason=no_actionable_evolution_failures`; it does not invent a patch. Replace the plugin with a Harbor-backed evaluator when the external benchmark project and Docker runtime are available.

Before an online run, verify the returned model is exactly `gpt-5.6-sol`, all three role fingerprints match, request IDs/usage are present, and Docker/Harbor preflight succeeds when the plugin uses Harbor. The adapter omits `temperature` and `seed` by default for compatibility; they are fingerprinted and sent only when explicitly configured. It falls back from Responses to Chat Completions only for HTTP 404/405 endpoint absence. Payload errors such as HTTP 400 stop with the provider response instead of silently changing APIs. Do not unlock Sealed after a failed preflight. A service or environment failure during Sealed is recorded as `aborted` and is not automatically retried.

Online calls may incur provider and Docker compute charges. Logs contain hashes, timing, usage, request IDs, and error categories; they should not contain API keys. Do not commit generated `runs/` artifacts.
