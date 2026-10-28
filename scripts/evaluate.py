"""Measure root-cause accuracy on the labelled synthetic test set.
Run:  python scripts/evaluate.py            (rule engine only)
      python scripts/evaluate.py --llm      (full agent; needs GROQ_API_KEY in the environment or .env)
With --llm, logs are spaced out (--pause) to stay under Groq's free-tier token limits. Logs where the
model failed and the rule engine answered instead are reported separately, never counted as AI answers.
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import TroubleshootingAgent  # noqa: E402
from app.llm import load_env_file  # noqa: E402

EVAL = ROOT / "samples" / "eval"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="use the Groq-backed agent")
    ap.add_argument("--out", help="default: samples/eval/results.json, or results_llm.json with --llm")
    ap.add_argument("--pause", type=float, default=20.0, help="seconds between logs with --llm (default 20)")
    args = ap.parse_args()
    load_env_file(ROOT / ".env")
    out = args.out or str(EVAL / ("results_llm.json" if args.llm else "results.json"))
    if args.llm and not os.getenv("GROQ_API_KEY"):
        sys.exit("Set GROQ_API_KEY in .env or the environment to evaluate the LLM agent.")

    labels = json.loads((EVAL / "labels.json").read_text())
    agent = TroubleshootingAgent()
    rows, by_type, confidence = [], Counter(), Counter()
    correct = 0
    stopped = None
    for i, item in enumerate(labels):
        if args.llm and i:
            time.sleep(args.pause)
        report = agent.analyze((EVAL / item["file"]).read_text(), use_llm=args.llm)
        got = (report.get("root_cause") or {}).get("kb_id")
        ok = got == item["expected"]
        correct += ok
        by_type[(item["expected"], ok)] += 1
        confidence[(report["confidence"] or {}).get("level")] += 1
        rows.append({"file": item["file"], "expected": item["expected"], "predicted": got, "correct": ok,
                     "confidence": (report["confidence"] or {}).get("level"), "mode": report["mode"],
                     "llm_error": report.get("llm_error")})
        if args.llm:
            print(f"  {item['file']}: {report['mode']}, {'correct' if ok else 'wrong'}"
                  + (f" ({report['llm_error']})" if report.get("llm_error") else ""), flush=True)
            if "per day" in (report.get("llm_error") or ""):
                stopped = f"stopped after {i + 1} logs: {report['llm_error']}"
                break

    total = len(rows)
    print(f"Mode: {'llm agent' if args.llm else 'rule engine'}")
    if stopped:
        print(f"Run incomplete, {stopped}")
    print(f"Root cause correct: {correct}/{total} = {correct / total:.1%}")
    ai_rows = [r for r in rows if r["mode"] == "llm"]
    if args.llm:
        ai_ok = sum(r["correct"] for r in ai_rows)
        print(f"Answered by the AI agent: {len(ai_rows)}/{total}"
              + (f", correct {ai_ok}/{len(ai_rows)} = {ai_ok / len(ai_rows):.1%}" if ai_rows else ""))
        print(f"Fell back to the rule engine: {total - len(ai_rows)}/{total}")
    print("Confidence levels:", dict(confidence))
    misses = [r for r in rows if not r["correct"]]
    if misses:
        print("Misses:")
        for r in misses:
            print(f"  {r['file']}: expected {r['expected']}, got {r['predicted']}")
    Path(out).write_text(json.dumps({"mode": "llm" if args.llm else "rules", "correct": correct, "total": total,
                                     "accuracy": round(correct / total, 4), "answered_by_llm": len(ai_rows),
                                     "incomplete": stopped, "rows": rows}, indent=1))


if __name__ == "__main__":
    main()
