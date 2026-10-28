# DeploySense AI

Hackathon entry for TCS Technology Day, problem "IT Application Maintenance: Deployment Issue Troubleshooting Assistant". A single agent reads a failed deployment log, finds the probable root cause, cites the log lines that support it, replays how the failure spread, and proposes fixes that are risk-checked but never executed.

Stack: Python 3.10+ (FastAPI, httpx), vanilla JS dashboard with three.js, Groq chat completions (OpenAI-compatible API, model `openai/gpt-oss-120b`) for the optional LLM.

## Request flow

`POST /api/analyze` (app/main.py) calls `TroubleshootingAgent.analyze` (app/agent.py):

1. **Upload checks** (main.py): allowed suffixes, 5 MB limit (`MAX_UPLOAD_MB`), binary rejection.
2. **Mask secrets** (`log_analyzer.mask_secrets`): passwords, tokens, API keys, connection-string credentials, AWS keys, GitHub tokens, emails, IPv4. Runs first; everything downstream (rules, LLM, UI) sees masked text.
3. **Parse** (`log_analyzer.parse_log`): per-line format detection (JSON, ISO timestamped, GitHub Actions, BuildKit, classic docker build, klog, k8s events, Jenkins, syslog, plain), normalized `Event` (id = 1-based line number, level, source, component, relative time `t`). Missing levels are inferred from wording; OOMKilled, CrashLoopBackOff, image pull failures are promoted to ERROR. `t` comes from timestamps, else BuildKit elapsed time, else line order (`time_source`). First 20,000 lines only.
4. **Context** (`knowledge_base.extract_context`): deployment, namespace, image, host, port, db_name, user pulled from the log to fill `{placeholders}` in fix commands. Unfilled ones become `<placeholder>`.
5. **Plugins always run deterministically** (app/plugins): `docker_plugin`, `database_plugin`, `general_analysis`. Contract: `can_handle(parsed) -> 0..1` and `analyze(parsed, context) -> findings`, plus optional `enrich` and `adapt_command` hooks (base.py). Each plugin owns the KB entries whose `plugin` field equals its `kb_plugin`. Findings are ranked by `rank_findings`: causes before symptoms, then earliest, then most evidence. This ranking is the rule-engine answer and the ground truth the LLM is checked against.
6. **LLM agent loop** (only if `use_llm` and a key is set, `_run_llm_agent`): one agent, up to 6 steps, tools = the three plugins (no args) + `search_knowledge_base(query)` + `get_log_lines(start, end)`. Input is `compact_for_llm`: deduplicated error lines with 3 lines of context, prefixed `L<id>`. Final answer is a fixed JSON schema (see `SYSTEM_PROMPT`). On `LLMError` the report falls back to rules and records `llm_error`.
7. **Assemble and verify** (`_assemble`): cited line ids that are not real event ids are dropped and counted in the confidence reasons. If the AI names a `kb_id` that a plugin found, the rule finding (evidence, fixes) is used with the AI explanation; otherwise an AI-only root cause is built from its cited lines. AI fixes are appended after KB fixes (max 8, deduplicated by command).
8. **Safe-fix validator** (app/safe_fix.py): text-only analysis. Risk = read-only, reversible, destructive, or review (unrecognized); the riskiest `&&`/`;` segment decides. Adds a dry-run preview where possible and static checks (shlex syntax, unfilled placeholders, production namespace, destructive). Destructive fixes are never `recommended`. Fixes are sorted read-only first.
9. **Timeline and map** (app/timeline.py, deterministic): significant events and milestones, marks for first anomaly, root cause, final failure; causal links from each later finding to the closest earlier cause (AI may supply the explanation text, `_explain_links`); component map on a fixed topology with ok/degraded/failed state changes over time. Rendered by frontend/js/failure-map.js (three.js 3D, SVG 2D fallback for small screens, reduced motion, no WebGL).
10. **Optional diff** (app/log_diff.py): failed vs last successful run by message template, plus version changes.
11. **Report** JSON includes `mode` ("llm" or "rules"), `llm_model`, `llm_error`, `trace` (what the agent did), and is rendered by frontend/js/app.js. `GET /api/health` reports whether a key is configured.

Feedback: `POST /api/feedback` appends to `data/feedback.jsonl` (gitignored); `GET /api/feedback/stats` counts each analysis once by its latest rating.

## Deterministic code vs LLM

| Code (facts) | LLM (reasoning and wording) |
|---|---|
| Masking, parsing, time ordering, component classification | Which tools to call |
| KB rule matching (28 rules in app/data/knowledge_base.json), evidence lines | Picking the root cause across findings |
| Cause vs symptom ranking | Explaining errors no rule covers |
| Filling fix commands from log context | Extra log-specific fix steps (still risk-checked) |
| Risk class, dry-run, pre-run checks | Explanations for timeline links |
| Timeline, map, diff, line-id validation | Summary, prevention, open questions |
| Confidence level and reasons | |

## Confidence (agent.py `_assemble`)

Computed only from verifiable signals, never from the model:

- **high**: root has a KB rule (`kb_id`), has at least one valid evidence line, is not a symptom, and the AI agrees with the rule engine's top finding (always true in rules mode).
- **medium**: evidence exists and either a KB rule matched (but it is a symptom or the AI disagreed) or the root came from AI reasoning only.
- **low**: no evidence, or no rule and no AI (fallback "Unrecognized error").

Reasons shown to the user include agreement or disagreement, the matched rule and line count, removed invalid line ids, and symptom warnings.

## Run, test, evaluate (macOS, zsh)

```zsh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # GROQ_API_KEY, GROQ_MODEL, MAX_UPLOAD_MB
uvicorn app.main:app --reload     # http://localhost:8000

pytest -q                         # pytest.ini sets pythonpath; tests/conftest.py blanks the key and blocks httpx
python scripts/evaluate.py        # rule engine on samples/eval (writes samples/eval/results.json)
python scripts/evaluate.py --llm  # full agent; reads .env, --pause between logs, writes results_llm.json
python scripts/generate_logs.py   # regenerate the 46 synthetic eval logs and labels.json
```

`app/main.py` loads `.env` at import with `os.environ.setdefault`, so an exported variable wins over `.env`.

Model: `openai/gpt-oss-120b` (Llama 3.3 70B is no longer available on Groq). Free tier limit is 8,000 tokens per minute; one analysis uses about 4k to 10k, so rate limits are expected. `GroqClient` retries 429/5xx with the wait Groq asks for (up to 4 retries, waits over 30s fail at once), stores `last_error`, and `check()` verifies key and model via `GET /models/{model}` (cached 60s) for `/api/health`. `GroqClient(api_key="")` means no LLM; only `None` reads the environment.

Robustness: `_line_arg` accepts `12` or `L12`; `clean_ai_answer` coerces the model's JSON or returns None (fall back to rules); any other exception in the loop also falls back. `/api/analyze` runs the agent with `run_in_threadpool`.

## Adding a plugin

Subclass `Plugin` in app/plugins, set `name` (the tool name), `label`, `description`, `kb_plugin`, `signals`; add KB entries with that `plugin` value (patterns, cause, `symptom`, severity, component, fixes with `{placeholder}` commands and verify steps, prevention); register it in `app/plugins/__init__.py`. The agent picks it up as a tool automatically. Update `COMPONENTS`/topology only if a new component is needed.

## Project rules (follow these in all future work)

Architecture
- It must stay a single-agent system. Plugins are tools the one agent calls, not separate agents.
- Facts come from code: parsing, time ordering, rule matching, risk classification, confidence. The LLM only chooses tools, reasons and explains. Do not move facts into the LLM.
- Confidence must come from verifiable signals, never from the model's self-reported confidence.
- Every root cause must cite real log line ids. Invalid ids are removed and reported.
- The app never executes commands on any system. Safe-fix only analyses command text.
- Everything must still work with no GROQ_API_KEY (rule-engine mode), and the UI must say which mode was used.
- Keep frontend assets local in frontend/vendor so the demo works offline.

Honesty
- No fake metrics, reviews, testimonials or user counters. The only numbers shown are measured ones (feedback ratings, evaluation results), labelled with what they measure.
- The 46/46 synthetic evaluation is optimistic because the same team wrote the rules and the test logs. Never present it as real-world accuracy.
- Never claim something works unless you ran it. Say what you tested and what you did not.

Design (the dashboard must not look like a generic AI product)
- No purple or gradients, no pill-shaped buttons, no emoji icons (use the Lucide icons in frontend/js/icons.js), no em dashes in any copy, no vague hero text, no AI-style marketing copy, no stock or AI-generated images, no cursor animation, no scroll animations.
- Keep the existing look: cool grey background, ink navy text, steel blue for actions, red, amber and green only for status, IBM Plex Sans and Plex Mono, small corner radius.
- Motion only where it carries meaning (the failure replay). Respect prefers-reduced-motion.
- Copy is plain, specific and in sentence case.

Code
- Clean, minimal changes. Only change what the task needs, with no unrelated refactors.
- Add or update tests in tests/ for every behaviour change, and run pytest before saying a task is done.
- Never print, log or commit the GROQ_API_KEY. .env stays in .gitignore.
- I use macOS with zsh.
