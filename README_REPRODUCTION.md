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

## Pipeline reliability and recovery

Before creating a new work directory, the workflow validates static CLI constraints, the evaluation-config/input-artifact paths, portable candidate environment-variable syntax, and supplied surface syntax, uniqueness, and regular-file status. A clearly new run without surfaces is rejected without filesystem side effects. Initialization, queue/recovery, reuse, and other checks that depend on existing persisted state remain under the lock. The workflow then holds an OS-backed lock for the complete mutable run. `<work-dir>/.orchestrator.lock` is a persistent metadata file (format `self_harness.orchestrator_lock.v1`): POSIX uses advisory `flock`, while Windows locks a sentinel byte with `msvcrt`. File presence does not mean a run is active; the kernel lock is authoritative and is automatically released when the owning descriptor closes or its process exits. Do not delete or replace the file to unlock a run. A second cooperating process fails fast with sanitized owner PID/host metadata, and the next owner safely replaces old or malformed unlocked metadata.

State files are replaced atomically. Accepted-candidate finalization additionally uses `<work-dir>/finalization.intent.json` (`self_harness.finalization_intent.v1`) as a single write-ahead journal: the exact child branch and queue transitions are prepared first, branch state is written before queue state, and startup idempotently rolls any prepared operation forward. A deterministic `finalization_id` binds both records, prevents suffixed replay branches, and requires the declared parent to remain active. Legacy records without this ID remain readable only when their cross-file references are consistent. This protects against process crashes and ordinary write failures; file `fsync` plus atomic replacement is not a claim of full power-loss durability without parent-directory flushing.

External process-tree cleanup is deadline-bounded on timeout, interruption, or any other escaping `BaseException`. It attempts platform tree termination, falls back to direct-child terminate/kill, and uses bounded waits so cleanup cannot indefinitely delay lock release; cleanup failures are attached without masking the primary stage error. Detached descendants can still escape process-group cleanup, so this is best-effort rather than strict containment. Candidate leases remain separate crash-recovery state; expired claims have all transient ownership fields cleared before retry.

The lock coordinates cooperating Self-Harness writers; it is not a security boundary. POSIX locking is advisory, unusual/network filesystems may have different guarantees, and `0o600` does not establish a private Windows DACL. Use a locally administered private work directory and ensure every writer follows the same lock protocol. Harbor's per-case timeout and infrastructure retry policy remains the inner evaluation boundary.

When `--reuse-existing` is used, promotion-critical outputs must carry matching input identities (including configuration and surface hashes). Legacy outputs without identity metadata are deliberately not trusted or automatically migrated; rerun them into a clean output directory. A policy rejection is distinct from `pipeline_failed`: invalid Harbor results, stage failures, deadline expiry, and identity mismatches cannot be promoted.

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

## Clean64 checkpoint resume

The Clean64 Docker reproduction writes per-case Harbor checkpoints under `runs/` and per-model round progress to `round-progress.json`. To resume after closing the terminal or after a reboot, run:

```powershell
.\workflow\scripts\resume_clean64_reproduction.ps1
```

The launcher reads API keys from Windows user environment variables, treats whitespace-only `SELF_HARNESS_QWEN_API_KEY` values as unavailable, refuses to start a duplicate pipeline, runs detached by default, and reuses completed case, repeat, candidate, and round artifacts. Use `-Foreground` only when an attached process is desired.

## Audited invalid-case reruns

Before generating a paid rerun plan, create a read-only, hash-bound invalid audit. The command records both source `result.json` SHA256 values, verifies that neither source changes during classification, and emits deterministic totals by side, split/repeat, and reason category without modifying checkpoints or calling a model:

```powershell
python .\eval\scripts\audit_invalid_cases.py `
  --baseline-result .\runs\clean64-qwen-baseline\result.json `
  --candidate-result .\runs\clean64-qwen-self-harness\branches\baseline\candidates\anti_workaround_execution\eval\result.json `
  --output .\tmp\qwen-invalid-audit-v1.json
```

Strict acceptance rejects any baseline or candidate result that still contains an infrastructure-invalid evaluation cell. The effective-invalid audit covers both current explicit `status=invalid` cells and legacy cells mislabeled as `failed` despite a missing verifier reward, runtime failure, or inconsistent reward/status fields. The rerun planner, Self-Harness resume/proposal workflow, and Harbor executor share the same UTF-8/BOM JSON loader with duplicate-object-key rejection, so malformed or ambiguous result, queue, state, proposal, checkpoint, marker, or reused case-result inputs cannot silently change the rerun set, candidate selection, or paid execution. The rerun launcher is dry-run by default: it writes a JSON plan, prints the exact cases and configured upper bound, and makes no model calls.

```powershell
# Baseline plans
.\workflow\scripts\rerun_clean64_invalid.ps1 -Label deepseek
.\workflow\scripts\rerun_clean64_invalid.ps1 -Label qwen

# Candidate plan; resolves the exact candidate workspace from candidate_queue.json
.\workflow\scripts\rerun_clean64_invalid.ps1 `
  -Label qwen `
  -CandidateId anti_workaround_execution
```

Plans are written to `paper/generated/rerun-plans/`. Each single-side plan records `source_result_stable=true`; the planner hashes the aggregate result before and after invalid classification, and the execute path rechecks that hash immediately before archiving checkpoints or launching Harbor. After execution, the plan records separate `execution_provenance.pre_source_result_sha256` and `post_source_result_sha256`, evaluator return code, post-read stability, and selected/global invalid counts; the archived plan is updated with this final execution record. This prevents an execution-time result rewrite from being confused with the plan's input identity. To start a paid rerun, add `-Execute`; add `-Foreground` only when attached execution is desired. Both foreground and detached paths reject a missing or whitespace-only Windows User API key before launching the evaluator. Before execution, the Python tool verifies that the configured model matches the source result and, for a candidate, that its workspace contains the materialized harness. Existing Harbor job directories are preserved. Only JSON checkpoints are archived under the target run's `rerun_history/<timestamp>/`, after which `--reuse-existing` skips every valid case.

```powershell
.\workflow\scripts\rerun_clean64_invalid.ps1 -Label qwen -Execute -Foreground
.\workflow\scripts\rerun_clean64_invalid.ps1 `
  -Label qwen `
  -CandidateId anti_workaround_execution `
  -Execute `
  -Foreground
```

The strict promotion receipt is intentionally separate from the historical acceptance artifact. It can only be issued after an accepted, zero-invalid gate and binds the queue entry, candidate manifest, both result hashes, acceptance artifact, and invalid audit. Repeated issuance with the same inputs is idempotent; a different binding never overwrites an existing receipt. It is offline-only and does not call Harbor or a model:

```powershell
python .\workflow\scripts\issue_strict_promotion_receipt.py `
  --queue .\runs\clean64-qwen-self-harness\candidate_queue.json `
  --candidate-id anti_workaround_execution `
  --branches-root .\runs\clean64-qwen-self-harness\branches `
  --baseline-result .\runs\clean64-qwen-baseline\result.json `
  --acceptance .\runs\clean64-qwen-self-harness\branches\baseline\candidates\anti_workaround_execution\acceptance.strict.json `
  --invalid-audit .\tmp\qwen-invalid-audit-v1.json `
  --receipt .\runs\clean64-qwen-self-harness\branches\baseline\candidates\anti_workaround_execution\promotion.strict.receipt.json
```

The command must remain blocked while either source has unresolved invalid cells. Add `--verify-artifact` to recheck an existing receipt against all current inputs.


For the minimum Qwen confirmatory path, generate the paired phased plan before spending compute:

```powershell
python paper/build_paired_rerun_plan.py `
  --baseline-result runs/clean64-qwen-baseline/result.json `
  --candidate-result runs/clean64-qwen-self-harness/branches/baseline/candidates/anti_workaround_execution/eval/result.json `
  --output-dir paper/generated/paired-rerun

# Phase 0: one fixed infrastructure canary per side; dry-run by default
.\workflow\scripts\rerun_clean64_invalid.ps1 -Label qwen `
  -CellsFile .\paper\generated\paired-rerun\phase_0_infrastructure_canary.baseline.json
.\workflow\scripts\rerun_clean64_invalid.ps1 -Label qwen -CandidateId anti_workaround_execution `
  -CellsFile .\paper\generated\paired-rerun\phase_0_infrastructure_canary.candidate.json
```

Only add `-Execute -Foreground` after reviewing the generated plans and confirming the account and provider are healthy. The paired plan and every phase manifest record `source_hashes_stable=true` plus the baseline/candidate source `result.json` SHA256; the rerun executor requires that provenance and verifies both hashes before even writing an execution plan, rejecting an old or incomplete manifest. A phase manifest also records `side`, and the launcher requires `side=baseline` for baseline reruns or `side=candidate` for candidate-workspace reruns; cell-based plans record this as `selection_side` and `selection_phase`, preventing a valid list from being executed against the wrong harness role or phase. After a Phase 0 execution, the rerun plan records `canary_outcome`: only a numeric finite verifier reward with consistent `passed/status` counts as an infrastructure outcome; behavioral `passed=false` is retained as a valid canary result, while provider errors, timeouts, missing rewards, and inconsistent records keep `canary_ready=false`. Regenerate them whenever either result changes, even if the invalid counts happen to remain the same. Phase 0 is judged solely by whether both cells return numeric verifier outcomes, never by pass/fail. Regenerate the paired plan after every phase; completed cells make an old manifest stale, and the launcher rejects stale manifests. Phase 1 fills the 32 baseline-only and 5 candidate-only gaps; Phase 2 fills the 73 paired gaps on both sides. These phases reduce operational risk but do not change the preregistered requirement to resolve all 183 cells.
The paper consistency audit (`python paper/audit_paper_consistency.py`) checks more than displayed numbers: it requires stable `statistics.json` analysis-input provenance, v3 paired phase manifests with complete source hashes, and `source_result_stable=true` plus a 64-character SHA256 in every single-side rerun plan. Missing or stale provenance fails the audit even when table values are unchanged.
It also verifies the inference gate: while any summary row has unresolved invalid cells, `mcnemar_exact_p`, task-permutation p, and bootstrap interval fields must remain `null`, each row must carry a blocking reason, and both paper languages must explicitly withhold confirmatory significance claims.
The same audit derives the Qwen/DeepSeek total invalid counts, jointly valid sample counts, and paired-missingness breakdown from `statistics.json` and the paired plan, then requires those values to appear in both language sections; stale narrative numbers therefore fail even if the Markdown tables are untouched.
It also rechecks the reported common-valid sensitivity rates (including Qwen Heldout 72.22%→88.89% and the DeepSeek Train/Heldout common-valid counts), so a selected-subset result cannot drift independently from the generated statistics.
The audit enforces the statistical unit boundary as well: every Clean64 summary row must have 43 Train or 21 Heldout tasks, exactly two attempts per task, bounded invalid/pass counts, and rates/deltas that recompute from those counts.
For provenance, it does not trust flags or hash shape alone: it resolves every `statistics.json.source_files` path and every paired/phase/single-rerun source path, recomputes SHA256, and rejects any mismatch with the recorded digest.
The audit itself records `audit_input_integrity.stable_before_after_audit=true` and hashes the two paper sections plus all generated plans before and after checking; a change during auditing prevents the v3 report from being written.
For every candidate row it also resolves `acceptance_path`, verifies that the acceptance artifact and its baseline/candidate `result.json` files are in `statistics.json.source_files`, and recomputes decision, split deltas, invalid counts, and strict-gate status from those raw artifacts.
The paired rerun plan is likewise rebuilt from its bound baseline/candidate results during the audit; pair counts, reason categories, phase cells, retry limits, 183-cell total, and 148.69-hour budget must match the rebuild exactly.
Each of the six phase manifests is also checked cell-by-cell against that rebuilt plan, including phase/side labels and the bound source paths and hashes; an edited execution list cannot pass merely because the aggregate plan is unchanged.
The generated `PAIRED_RERUN_PLAN.md` and `paired_rerun_cells.csv` are checked against the same JSON plan, so the human-readable budget and CSV execution rows cannot drift independently.
The statistical exports are treated the same way: `summary.csv`, `candidates.csv`, and `STATISTICAL_AUDIT.md` are regenerated in memory from `statistics.json` and compared byte/row-wise before the paper audit passes. The `effect_sizes.svg` and `invalid_runs.svg` figures are also regenerated from the same JSON summary and compared byte-for-byte, so a hand-edited figure cannot drift from the tables.
The mechanism-evidence bundle (`MECHANISM_EVIDENCE.md`, `mechanism_transitions.csv`, `mechanism_evidence.svg`) and the outcome-blind split-coverage bundle (`SPLIT_COVERAGE_AUDIT.md`, `split_coverage_counts.csv`, `split_coverage.svg`) are likewise regenerated from their JSON audit objects and compared byte-for-byte; all eight supplementary files are included in the audit's stable input snapshot.
The two supplementary JSON objects also record and recheck the canonical proposal/result, Clean64/Sealed manifest, and task-metadata SHA256 inputs; split-distance aggregation uses sorted labels so its floating-point serialization is deterministic across Python processes.
Both supplementary generators now use the same strict JSON object loader as the main statistics and Sealed paths: BOM is accepted, nested duplicate keys are rejected, and the root must be an object before any audit or figure is generated.
The container reproducibility bundle (`container_reproducibility.json`, `CONTAINER_REPRODUCIBILITY_AUDIT.md`, `container_base_images.csv`) is also rebuilt from all 89 task manifests and 89 Dockerfiles; their SHA256 inputs and derived files are checked by the same paper audit.
The outcome-blind design-resolution bundle (`confirmation_design_audit.json`, `CONFIRMATION_DESIGN_AUDIT.md`) is rebuilt from the fixed design script plus the exact statistics/validity source files; its source hashes and LF-normalized UTF-8 outputs are checked as well.
The pre-run `sealed21_container_resolution.preview.json` is validated against the frozen 21-task manifest and Dockerfile reference set; the audit requires `pull_performed=false`, exactly five references, sorted usage bindings, and an explicit `resolved_count/complete` status.
The checked-in `sealed21-execution-plan.json` is also revalidated against the manifest, preregistration, sealed TOML, current harness surfaces, execution/analysis source bundles, and strict-readiness state; its 84-cell count and 52.5-hour upper bound cannot drift independently. The audit additionally cross-checks the machine plan against `SEALED_PROTOCOL.md` and the pre-registered missingness policy, so the protocol’s repeat count, retry limit, and no-imputation rule cannot silently diverge.
It recomputes that plan’s cell count and wall-clock budget from the Sealed TOML’s task count, repeats, timeout, concurrency, and infrastructure-retry fields, rather than trusting launcher constants.

```powershell
.\workflow\scripts\finalize_clean64_strict.ps1 `
  -Label qwen `
  -CandidateId anti_workaround_execution
```

## Independent Sealed21 confirmation

`Sealed21` is the frozen final generalization split for the Qwen confirmation experiment. It contains all 21 text-only tasks that are outside Clean64; four additional unused tasks with local media inputs are excluded by a declared capability rule. The manifest locks the task identities and full directory hashes, and the preregistration fixes the endpoint, missingness policy, and success rule before any sealed model call.

Verify the frozen split and generate an execution plan without making model calls:

```powershell
python .\eval\scripts\build_sealed_split.py `
  --task-root .\runs\terminal-bench-2-reliable-v2 `
  --clean-config .\eval\configs\harbor_local_clean64.toml `
  --runs-root .\runs `
  --output .\configs\splits\sealed21.json `
  --config-output .\eval\configs\harbor_local_sealed21.toml `
  --verify

.\workflow\scripts\run_qwen_sealed21.ps1
```

The launcher first feeds its queue, candidate manifest, strict acceptance, dependency lock, and existing freeze JSON through the shared `result_validity.py --emit-object` loader, which accepts BOM but rejects duplicate keys and non-object roots. It then checks `acceptance.strict.json`, verifies that its `accepted=true`/`decision=accepted` fields agree, verifies that its baseline/candidate paths and SHA256 values still match the current Clean64 result files, and invokes `run_acceptance_gate.py --verify-artifact` to recompute the rule, split comparisons, deltas, reason, and decision from those source results. It refuses `-Execute` until the Qwen candidate is strictly accepted with zero unresolved Clean64 invalid cells and `strict_acceptance_recomputed=true`. A stale, copied, malformed, or incomplete acceptance artifact therefore cannot unlock the run. Once that prerequisite is satisfied, the paid paired run is explicit:

```powershell
.\workflow\scripts\run_qwen_sealed21.ps1 -Execute
```

Baseline and frozen candidate each run 21 tasks twice, for 84 statistical evaluation cells. Each cell allows at most two infrastructure retries; retries do not become additional statistical samples. Both sides must finish before analysis, and any unresolved invalid cell makes the confirmation incomplete. The 52.5-hour configured upper bound assumes all cells use all three allowed attempts. Outputs are analyzed with the preregistered task-clustered bootstrap and task-level paired permutation test. See `paper/SEALED_PROTOCOL.md` and `configs/experiments/ei_confirmation_v1.json` for the authoritative protocol.

The launcher records a role-specific `run_identity` derived from the sealed-manifest, preregistration, exact evaluation-TOML, and harness-surface hashes. Before parsing the TOML, the runner verifies its raw SHA256 and stores that hash in both the output marker and aggregate result. It refuses `--reuse-existing` when either identity or config hash does not match. The analyzer rejects duplicate JSON object keys, requires the sealed manifest to be `self_harness.sealed_split.v1` with exactly 21 unique, outcome-blind tasks and zero prior result references, requires exactly outer repeat records 1 and 2 with matching per-case repeat IDs, requires the `passed` field in all 84 cells to be a JSON boolean and a non-null finite numeric verifier reward, and then re-hashes all 21 task directories, the exact TOML, strict acceptance artifact, and frozen inputs before reporting any confirmatory result. The task-level permutation p-value is exact rather than Monte Carlo. Run `python paper/audit_confirmation_design.py` to rebuild the outcome-blind statistical-resolution audit.

The identity also binds the result-producing runner, Harbor wrapper, and backend bridge source bundle. Analysis/validity source hashes and an execution-environment snapshot (timezone-bearing timestamp, OS, Python, Harbor, Docker client/server, Git state, model alias, and HTTP(S) endpoint) are stored in the freeze; the analyzer rejects missing, blank, malformed, or type-inconsistent snapshot fields before statistics. The PowerShell launcher writes the plan and freeze as BOM-free UTF-8 so Python can parse them consistently; the analyzer remains backward-compatible with legacy BOM-bearing JSON. Paid execution is blocked by default when the Git worktree is dirty; `-AllowDirtyWorktree` is an explicit, recorded exception and is not recommended for the final paper run.

The launcher also captures sorted `pip freeze --all` inventories for both the project interpreter and Harbor's own interpreter, plus executable hashes. Their stable dependency bundle is part of each run identity. Rebuild the static base-image audit with `python paper/audit_container_reproducibility.py`; it does not pull images or read outcomes.

After both Sealed harness runs, the launcher automatically runs a read-only local `docker image inspect` snapshot for the five base-image references used by Sealed21. It never pulls an image. The analyzer re-parses the frozen Dockerfiles and requires the snapshot reference set to match exactly, then verifies its bundle and task set, checks unique references and count/completeness consistency, and reports how many references have an image ID/RepoDigest; an incomplete cache snapshot remains explicit rather than silently blocking or changing the preregistered statistical endpoint.

The generated `sealed_analysis.v6` `sealed_statistics.json` hashes the raw baseline and candidate `result.json` files, the Sealed manifest, freeze, and container-resolution snapshot before and after validation, rejecting any change during analysis; it records all of those hashes, the 21-task/2-repeat/two-role cell count, bootstrap seed and sample count, the `q*(n-1)` linear percentile rule (Hyndman-Fan type 7), exact permutation method, unresolved-invalid counts, and container-resolution status. `SEALED_REPORT.md` repeats the design, completeness summary, and complete input-hash set so the paper tables can be regenerated from an auditable artifact rather than copied proportions.
Before those outputs are rendered, the Sealed analyzer also recomputes every outer repeat's `passed/total` from its `case_results` and rejects inconsistent aggregate metadata.
It parses the frozen preregistration as well, checking its model, candidate, split size, outcome-blind flag, bootstrap seed, retry limit, and no-replacement/no-imputation policy; the generated report repeats all provenance hashes, including the source and dependency bundles.
It also checks the primary endpoint contract—two attempts per task, 20,000 clustered-bootstrap resamples, exact paired sign permutation, α=0.05, and the delta/CI/p success rule—against the analyzer implementation.
The strict acceptance artifact is parsed and checked for the expected format, accepted/decision consistency, stable source hashes, exact internal baseline/candidate path and SHA256 bindings to the Clean64 source results recorded in the freeze, and a full gate recomputation before any model call. The launcher and analyzer use the same `verify_acceptance_artifact()` implementation, so a changed summary cannot pass one stage and fail only after a paid run. The paper audit derives the expected `strict_decision` and recomputation state from the artifact when it exists; before strict finalization it explicitly records the current `missing`/`false` state instead of treating an absent result as a completed experiment.
Its rule and split summaries are checked too: Train/Heldout, two repeats per side, pass-rate, the no-drop/at-least-one-improvement rule, repeat IDs 1/2, and finite deltas must all be present.
After statistics are computed it rechecks the preregistration, exact TOML, strict acceptance, both harness surfaces, execution/analysis code bundles, and all 21 task-directory hashes; any mid-analysis mutation prevents output generation.
The report uses scientific notation for exact p-values below 1e-4, so the smallest attainable nonzero permutation probability is never printed as `0.0000`.

Run `python paper/audit_split_coverage.py` to rebuild the outcome-blind task-composition audit. It accounts for all 89 local tasks and quantifies category and difficulty shift without reading any model outcome. Sealed21 is disjoint and difficulty-comparable to Clean64, but it is not category-stratified; claims must remain limited to the frozen local text-only set.
