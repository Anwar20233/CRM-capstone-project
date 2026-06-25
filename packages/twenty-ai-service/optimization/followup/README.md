# Follow-up agent prompt optimization (DSPy)

Automatically improve the **follow-up intelligence agents' prompts** so they
actually do what they're supposed to — pick the right action, calibrate urgency
honestly, ground every action in evidence, and stay silent when an email needs no
reply. We optimize with **GEPA** (reflective prompt evolution), scoring each
rollout against a labelled gold set, then **gate** the winners end-to-end through
`scripts/followup_eval.py`.

This extends the Writer-agent harness in `../` (same **carrier-predictor**
pattern: the agent's real system prompt is the `instructions` of a dummy
`dspy.Predict`; GEPA rewrites it; `forward()` runs the **real** agent with the
candidate prompt; a feedback metric scores it). See `../README.md` for the core
idea.

Everything routes at **OpenAI GPT-5.4 Mini** (the deploy model) with **GPT-5.4**
as the GEPA reflection/proposer LM, via `.env.training`.

### Fast & safe by construction

- **Isolated rollouts** — every agent runs *without* the live bridge, DB, RAG, or
  Presidio masking. Inputs are built in-memory from synthetic, **pre-masked**
  data (no real PII), so there's nothing to mask/unmask and no NER model load.
- **Two layers of parallelism** — GEPA runs `--threads` rollouts concurrently
  *within* each agent, and (by default) all agents are optimized concurrently
  across a thread pool, so wall-clock ≈ the slowest single agent.
- **Deterministic metrics** — no LLM-judge: scoring is instant and free; the only
  token cost is the agent rollouts + GEPA's reflections.

## Layout

```
optimization/followup/
  registry.py            # one AgentSpec per agent — the single extension point
  program/<agent>_program.py   # dspy carrier wrapping the live agent
  metric/<agent>_metric.py     # composite feedback metric (mirrors followup_eval)
  dataset/<agent>_cases.json   # synthetic gold; next_step via build_cases.py
  evaluate.py            # per-agent report
  run_all.py             # GEPA optimize one/all agents (parallel)
  gate.py                # end-to-end gate over two followup_eval --json reports
  selfcheck.py           # offline wiring check (stubs the LLM — no API calls)
  reports/               # outputs (gitignored)
```

## Agents (all wired & runnable in isolation)

| Agent | Prompt ported back to |
|---|---|
| `next_step` | `SYSTEM_PROMPT` in `followup/next_step/agents/next_step/prompts.py` |
| `drafting` | `DRAFTING_SYSTEM_PROMPT` in `followup/emailer/agents/drafting/prompts.py` |
| `extraction` | `EXTRACTION_INSTRUCTIONS` in `followup/profile/prompts.py` |
| `synthesis` | `_SYSTEM_PROMPT` in `followup/profile/synthesis.py` |
| `chat` | `_SYSTEM_PROMPT` in `followup/chat/agent.py` |

Adding another agent = (1) add a `system_prompt` seam on its entry point, (2) a
`*_cases.json` gold set, (3) a `*_metric.py`, then register an `AgentSpec` in
`registry.py`.

## Setup

```bash
cd packages/twenty-ai-service
.venv/bin/pip install -r requirements-optimization.txt
```

Set `OPENAI_API_KEY` (in `.env` or `.env.training`). `.env.training` routes the
agents + DSPy LMs at OpenAI GPT-5.4 family; production `.env` is untouched.
If your OpenAI model id differs from `gpt-5.4-mini` / `gpt-5.4`, edit the
`DSPY_TASK_MODEL` / `DSPY_REFLECTION_MODEL` / `LLM_MODEL` lines in `.env.training`
and the aliases in `agent/models.py`.

## Workflow

```bash
# 0. Offline wiring check — no API calls, proves all five loops run end-to-end.
.venv/bin/python optimization/followup/selfcheck.py

# 1. (Re)build the next-step dataset (the others are static JSON).
.venv/bin/python optimization/followup/dataset/build_cases.py

# 2. Optimize ALL agents in parallel (or one with --only), eval each on test.
.venv/bin/python optimization/followup/run_all.py --auto light --eval-after
#    rate-limited? add --sequential or lower --threads.

# 3. Freeze an E2E baseline on the CURRENT prompts (run on gpt-5.4-mini)
.venv/bin/python scripts/followup_eval.py --json optimization/followup/reports/baseline.json

# 4. Port reports/next_step_prompt.txt into SYSTEM_PROMPT, then re-run the eval
.venv/bin/python scripts/followup_eval.py --json optimization/followup/reports/optimized.json

# 5. Gate: accept only if no dimension regresses and PII stays clean
.venv/bin/python optimization/followup/gate.py \
    --baseline optimization/followup/reports/baseline.json \
    --candidate optimization/followup/reports/optimized.json
```

## Scoring signals (next_step)

Composite score in `[0,1]` over the applicable signals — the same dimensions
`scripts/followup_eval.py` grades:

| signal | weight | rewards |
|---|---|---|
| `tool_match` | 0.35 | headline action's `orchestrator_tool` ∈ the gold set |
| `urgency_match` | 0.25 | headline priority band (high/medium/low) ∈ gold |
| `grounded` | 0.15 | every action cites concrete deal evidence |
| `valid_schema` | 0.15 | 1–5 actions, no rollout error |
| `no_action` | 0.10 | returns exactly one `no_action`/`log_activity` iff the email needs no reply |

Each of the other four agents has its own composite metric in
`metric/<agent>_metric.py` (drafting: personalization/sign-off/placeholders/
length/required-points; extraction: deal choice/abstention/fact recall/new-person
discovery; synthesis: names/risks/risk-score/length/no-id-leak; chat: required
vs forbidden tool selection + parsimony).

Per-agent rollouts use **synthetic, pre-masked data** and bypass the Presidio
masking layer (fast, and nothing real to leak). The **end-to-end gate** still runs
the full pipeline with masking on and fails on any PII leak.

## Permanent runtime switch

Once prompts are accepted, set in the real `.env` so production runs on the model
the prompts were tuned for:

```
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.openai.com/v1
FOLLOWUP_SUBAGENT_MODEL_ALIAS=gpt-5.4-mini
FOLLOWUP_ORCHESTRATOR_MODEL_ALIAS=gpt-5.4-mini
```
