# Writer-agent prompt optimization (DSPy)

Automatically optimize the **Writer agent's system prompt** so it follows the
optimal tool workflow, reaches the right outcome in the **fewest tool calls**,
knows when it is "done", and **rejects** requests outside its purpose (e.g. reads).

We optimize with **GEPA** (reflective prompt evolution) and use **MIPROv2** as a
comparison baseline, scoring each rollout against a labelled dataset of 100
orchestrator→writer instructions. Every rollout runs the **real** Writer worker
(`BaseWorker.run()`) against the **live Twenty bridge** with **PII masking on**, so
optimization reflects a genuine interaction. The deliverable is a single optimized
string that ports back into `_WRITER_SYSTEM_PROMPT` in
[`agent/workers/writer_worker.py`](../agent/workers/writer_worker.py).

## Layout

```
optimization/
  dataset/
    generate_cases.py   # deterministic generator, seeded from notebooks/cases_data.json
    writer_cases.json   # 100 labelled cases (regenerate with the script)
  harness/
    config.py           # loads .env + .env.training, configures DSPy LMs (OpenAI)
    bridge_runtime.py    # runs the worker live, captures trajectory, tears down writes
    worker_program.py    # dspy.Module (carrier-predictor) — the optimizable prompt
    metric.py            # composite trajectory metric + GEPA feedback
    evaluation.py        # report builder + regression gate
  run_baseline.py        # eval the current prompt -> reports/baseline.json
  run_gepa.py            # GEPA optimization (primary)
  run_mipro.py           # MIPROv2 optimization (baseline)
  evaluate.py            # eval any prompt on a split, optional regression gate
  reports/               # outputs (gitignored)
```

## Setup

```bash
cd packages/twenty-ai-service
.venv/bin/pip install -r requirements-optimization.txt
```

Configure a **disposable** Twenty workspace in `.env.training` (every tier-1/2
rollout performs real creates/updates there; teardown deletes what it can). The
training env routes the worker and DSPy LMs at OpenAI using `OPENAI_API_KEY` from
`.env` — production `.env` is untouched.

Make sure the Twenty server / agent-bridge is running (`NODE_BRIDGE_BASE_URL`).

## Id-based dataset (important)

The Writer is **write-only and never searches**, and the CRM keys everything by
UUID. So every case that touches an existing record carries a **real record id**,
not a name — mirroring production, where the orchestrator resolves names→ids and
hands the writer ids. Those ids come from `fixtures.py`, which seeds prerequisite
records (companies, people, opportunities, notes, tasks) into the workspace and
writes `dataset/fixtures.json`. **Ids are workspace-specific**, so re-seed (and
regenerate cases) whenever you point at a different workspace.

## Workflow

```bash
# 0. Seed prerequisite records + capture their ids (run once per workspace)
.venv/bin/python optimization/dataset/fixtures.py --seed

# 1. (Re)generate the dataset from those fixtures (bakes the ids into the cases)
.venv/bin/python optimization/dataset/generate_cases.py

# 2. Baseline the current production prompt on the held-out test split
.venv/bin/python optimization/run_baseline.py --split test

# 3. Optimize with GEPA, then auto-eval the winner on test
.venv/bin/python optimization/run_gepa.py --auto light --eval-after

# 4. (Optional) MIPROv2 baseline for comparison
.venv/bin/python optimization/run_mipro.py --auto light --eval-after

# 5. Gate the optimized prompt against the baseline (no category may regress >0.05)
.venv/bin/python optimization/evaluate.py \
    --prompt-file optimization/reports/gepa_prompt.txt \
    --split test --gate optimization/reports/baseline.json \
    --out gepa_eval.json --label GEPA

# 6. If it passes, paste reports/gepa_prompt.txt into _WRITER_SYSTEM_PROMPT.
```

## Scoring signals

Composite score in `[0,1]`, weighted over the **applicable** signals per case:

| signal | weight | what it rewards |
|---|---|---|
| `outcome_match` | 0.35 | executed / confirmation_required / rejected / clarify matches gold |
| `action_correct` | 0.20 | `execute_tool(tool=...)` is the gold action (or no write when it should refuse) |
| `protocol_order` | 0.10 | `learn_tools` before `execute_tool` |
| `parsimony` | 0.10 | fewest tool calls (penalizes extra `get_tool_catalog`/re-fetches) |
| `arg_validity` | 0.10 | no `INVALID_ARGUMENTS` / `UNRESOLVED_HANDLE` / `OUT_OF_SCOPE` |
| `resolve_date` | 0.05 | `resolve_date` used iff the request has a relative date |
| `confirmation` | 0.05 | tier-3 returns `CONFIRMATION_REQUIRED`, no blind retry |
| `response` | 0.05 | reply refuses / asks / presents a draft appropriately |

`masking_rate` is reported as a health check (was PII masked?) but not scored.

## Regression detection

`run_baseline.py` freezes a reference report on the **test** split. After
optimization, `evaluate.py --gate` accepts the winner only if the **aggregate ≥
baseline** and **no per-category mean drops > 0.05**. Run with `--repeats 2` on the
test split for a stabler signal (the live bridge + LLM are non-deterministic).

## Reusing for reader / orchestrator

`metric.py` and `worker_program.py` are parameterised by prompt + worker; a reader
variant is a new `bridge_runtime` worker built from `READER_SCOPE` plus a
read-oriented gold dataset and metric weights.
