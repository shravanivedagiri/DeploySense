# DeploySense AI

A single AI agent that reads a failed deployment log, finds the probable root cause, shows the log lines that support it, replays how the failure spread across the deployment, and proposes fixes that are checked for risk before anyone runs them.

Built for the TCS Technology Day problem statement *IT Application Maintenance: Deployment Issue Troubleshooting Assistant*.

## Run it

Requires Python 3.10 or newer.

```zsh
shravanivedagiri@Mac ~ % cd deploysense
shravanivedagiri@Mac deploysense % python3 -m venv .venv && source .venv/bin/activate
(.venv) shravanivedagiri@Mac deploysense % pip install -r requirements.txt
(.venv) shravanivedagiri@Mac deploysense % cp .env.example .env        # add your GROQ_API_KEY
(.venv) shravanivedagiri@Mac deploysense % uvicorn app.main:app --reload
```

Open http://localhost:8000 and pick a sample log, or paste your own.

The agent uses `openai/gpt-oss-120b` on Groq (`GROQ_MODEL` in `.env`). Without `GROQ_API_KEY` the app still works in rule-engine mode, and the header says so. If the key is set but Groq fails (rejected key, unknown model, rate limit), the header shows the reason and each report says it came from the rule engine. Get a free key at https://console.groq.com.

The free Groq tier allows about 8,000 tokens per minute and one analysis uses roughly 4,000 to 10,000, so back-to-back analyses can be rate limited. The client waits and retries when Groq asks for a short wait, and falls back to the rule engine otherwise.

Everything the page needs (three.js, fonts, icons) is served from `frontend/vendor`, so the demo works without internet access in rule-engine mode.

## Test and measure

```zsh
(.venv) shravanivedagiri@Mac deploysense % pytest -q                        # 33 tests, never uses the network or your key
(.venv) shravanivedagiri@Mac deploysense % python scripts/generate_logs.py  # rebuild the 46-log test set
(.venv) shravanivedagiri@Mac deploysense % python scripts/evaluate.py       # rule engine accuracy
(.venv) shravanivedagiri@Mac deploysense % python scripts/evaluate.py --llm # full agent, reads .env, paces calls (--pause 20)
```

Current result: the rule engine picks the labelled root cause on 46 of 46 synthetic logs. Read that number with care: the test logs and the knowledge base were written by the same team, so it shows the ranking logic works (cause chosen over symptoms such as CrashLoopBackOff and 502), not real-world accuracy. Before the demo, add public logs (for example from Loghub or GitHub issues) to `samples/eval` and run `evaluate.py --llm`.

Usefulness, the problem statement's 80 percent target, is measured from real ratings: every report has Useful and Not useful buttons, ratings are stored in `data/feedback.jsonl`, and `GET /api/feedback/stats` returns the live rate.

## Project layout

```
app/
  main.py            FastAPI server: /api/analyze, /api/samples, /api/feedback, static dashboard
  agent.py           The single troubleshooting agent (Groq tool calling, verification, confidence)
  log_analyzer.py    Format detection, normalization, secret masking, error extraction
  knowledge_base.py  Error-to-fix rules, placeholder filling, context extraction
  data/knowledge_base.json   28 curated rules with fixes and verify steps
  plugins/           Docker plugin, Database plugin, General analysis (the agent's tools)
  safe_fix.py        Risk classification, dry-run previews, pre-run checks
  timeline.py        Deterministic timeline, causal links, component map
  log_diff.py        Failed run vs last successful run
  llm.py             Groq client
frontend/            Dashboard (HTML, CSS, JS, three.js), privacy and terms pages, favicons
samples/             Demo logs and the labelled synthetic test set (samples/eval)
scripts/             Test set generator and evaluator
tests/               Pytest suite, including the agent loop with a scripted model
docs/                Input formats and AI approach
```

## Launch checklist

- [ ] Custom domain connected
- [x] Favicon added (`favicon.svg`, `favicon-32.png`, `apple-touch-icon.png`)
- [x] No "Made with" badge anywhere
- [x] Privacy policy page at `/privacy`
- [x] Terms and conditions page at `/terms`
- [ ] Fill the highlighted placeholders in both legal pages (team name, contact email, retention period, hosting provider, jurisdiction)
- [ ] Set `GROQ_API_KEY` on the host

See `docs/approach.md` for supported input formats and how the agent works.
