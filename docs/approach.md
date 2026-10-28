# DeploySense AI: input formats and AI approach

## 1. Supported input

Paste text or upload a plain-text file up to 5 MB (`.log`, `.txt`, `.json`, `.jsonl`, `.out`). The first 20,000 lines are analysed. One log can mix formats; each line is detected separately.

| Format | Example line | Time source |
|---|---|---|
| Timestamped app logs | `2026-10-28T09:14:24.006Z ERROR [checkout-api] FATAL ERROR: ...` | ISO timestamp |
| JSON lines | `{"time":"...","level":"error","msg":"query failed","error":"..."}` | `time`, `timestamp`, `ts`, `@timestamp` |
| GitHub Actions | `2026-10-28T08:22:04.11Z ##[error]Process completed with exit code 1.` | ISO timestamp |
| Docker BuildKit | `#7 9.802 npm ERR! code 1` | Build elapsed seconds |
| Classic docker build | `Step 4/8 : RUN npm install` | Line order |
| Kubernetes klog | `E1028 10:00:03.000000 1 main.go:42] ...` | Line order |
| Kubernetes events | `Warning  Failed  pod/x  Failed to pull image ...` | Line order |
| Jenkins | `[Pipeline] sh`, `ERROR: script returned exit code 1` | Line order |
| Syslog | `Oct 28 10:00:01 host service[123]: ...` | Timestamp |
| Plain text | `ERROR: something failed` | Line order |

Every line is normalized to one schema: `line id | timestamp | relative time | level | source | component | message`. When a line has no level, the level is inferred from its wording (for example "connection refused" becomes ERROR) and marked as inferred. OOMKilled, CrashLoopBackOff and image pull failures are promoted to ERROR even when Kubernetes reports them as warnings.

**Optional second log.** Upload the last successful run of the same pipeline to get a comparison: lines new in the failed run, steps missing from it, and version numbers that changed (for example `node 18.19.0 to 20.11.1`).

**Masking.** Before anything else, passwords, tokens, API keys, connection-string credentials, AWS keys, GitHub tokens, emails and IPv4 addresses are replaced with placeholders. Masked text is what the agent and the LLM see.

## 2. How a request is processed

```
upload -> mask -> parse and normalize -> agent -> safe-fix validator -> timeline and map -> report
                                          |
                     Docker plugin, Database plugin, General analysis,
                     knowledge base search, read more log lines
```

Facts come from code; reasoning and explanation come from the LLM.

| Done by code (deterministic) | Done by the LLM |
|---|---|
| Parsing, masking, ordering events by time | Choosing which plugins to call |
| Matching 28 knowledge base rules with evidence lines | Deciding the most probable root cause across findings |
| Filling fix commands with names, namespaces, ports from the log | Explaining causes for errors no rule covers |
| Risk class, dry-run preview and checks for every fix | Extra fix steps specific to the log |
| Timeline, first anomaly, final failure, component map | Explaining how one event led to the next |
| Confidence level | Plain-language summary |

## 3. The single agent

`app/agent.py` runs one agent (`openai/gpt-oss-120b` on Groq, temperature 0.1) with five tools:

- `docker_plugin`: build failures (npm, pip, native modules, COPY), registry pull and auth, OOMKilled, exit codes, port conflicts, disk full. Extracts the failing build step, base image, exit code and its meaning.
- `database_plugin`: connection refused or timed out, wrong credentials, missing database, unapplied migrations, pool exhaustion, TLS. Detects PostgreSQL, MySQL, MongoDB or Redis and rewrites commands for MySQL.
- `general_analysis`: missing environment variables, config parse errors, crash loops, failing probes, scheduling, DNS, TLS, permissions, CI test failures. Also returns error lines no rule matched.
- `search_knowledge_base`: keyword search over the rules.
- `get_log_lines`: read up to 40 raw lines for more context.

The agent sees the deduplicated error lines with three lines of context, each tagged with its line id. It loops for at most six steps, then must answer in a fixed JSON format: summary, root cause with evidence line ids, contributing factors, fixes, explanations for timeline links, prevention, open questions.

Every plugin also implements one contract, `can_handle(parsed)` and `analyze(parsed, context)`, so a Kubernetes or Terraform plugin can be added without changing the agent.

## 4. Keeping answers specific and correct

1. **Grounding.** All plugins run on every log, independently of the LLM. Their findings are the ground truth the AI answer is checked against.
2. **Cause over symptom.** Rules are marked as causes or symptoms. CrashLoopBackOff, failed probes, 502/503 and non-zero exit codes are symptoms. The rule engine ranks causes first, then the earliest one.
3. **Evidence check.** Line ids cited by the LLM that do not exist in the log are removed and the removal is reported.
4. **Confidence from signals, not from the model.** High: a rule matched, evidence exists, the match is a cause, and the AI agrees with the rule engine. Medium: evidence exists but the match is only a symptom, the AI disagreed, or only the AI found it. Low: no rule and no confirmed evidence. The report lists the reasons.
5. **Specific commands.** Fix commands are filled with the deployment, namespace, image, host, port, user, database, env var or package found in the log. Anything not found stays as `<placeholder>` and is flagged before running.
6. **Fallback.** If no key is set, Groq fails, or the model's answer does not fit the required format, the rule engine produces the full report and the page says which engine was used and why. Rate limits are retried when Groq asks for a short wait. Tool arguments such as `L12` are accepted, and wrongly typed fields in the answer are coerced or dropped.

## 5. Safe-fix validator

DeploySense never executes commands. `app/safe_fix.py` analyses each command's text:

| Risk | Examples | Shown as |
|---|---|---|
| Read-only | `kubectl get/describe/logs`, `docker ps`, `lsof`, `nc -zv`, `--dry-run` | Safe to run |
| Reversible | `kubectl set/patch/scale`, `rollout restart`, `docker stop`, `npm install` | Needs a preview first |
| Destructive | `kubectl delete`, `DROP TABLE`, `rm -rf`, `docker system prune`, `git push --force` | Never recommended |
| Needs review | Anything the rules do not recognize | Review before running |

For changes it adds a preview command where the tool supports one (`--dry-run=server -o yaml`, `terraform plan`, `helm diff`, `npm install --dry-run`, SQL wrapped in `BEGIN ... ROLLBACK`). It also checks command syntax, unfilled placeholders and production namespaces. Fixes are ordered read-only first, so each plan reads: confirm the cause, change it, verify.

## 6. Timeline and failure map

The timeline is built by code from parsed time (timestamps, build elapsed time, or line order, which the page states). It marks the first anomaly, the probable root cause and the final failure. Each later finding is linked to the earlier cause, and the AI explains the link when available.

The map places the components found in the log (CI pipeline, image build, registry, scheduler, container, config and secrets, application, database, load balancer) on a fixed deployment topology. Each component turns degraded on its first warning and failed on its first error. The replay plays the log's timeline in eight seconds, with a pulse travelling from an already failing neighbour. Clicking a component shows its log lines. A 2D view is used on small screens, with reduced motion, or without WebGL.

## 7. Data

- `samples/*.log`: seven demo scenarios, including a failed and successful Docker build pair.
- `samples/eval`: 46 synthetic logs over 23 failure types in three formats, generated by `scripts/generate_logs.py` with noise lines and downstream symptoms. Labels are in `labels.json`.
- No real or sensitive data is included.

## 8. Demo script

1. *Kubernetes pod killed for memory*: replay shows the container fail first and the load balancer later; the fix plan starts with read-only checks.
2. *Missing secret breaks database login*: the loudest errors are database auth and 502s; DeploySense picks the earlier missing `DB_PASSWORD`.
3. *Docker build after a Node upgrade* with comparison: the diff shows `node 18.19.0 to 20.11.1`, the cause of the `bcrypt` build failure.
4. *Error no rule recognizes*: low confidence in rule mode; with AI on, the agent reasons over the cited lines.
5. Rate a report and show `/api/feedback/stats`.
