"""Measure root-cause accuracy on the labelled synthetic test set.
Run:  python scripts/evaluate.py            (rule engine only)
      python scripts/evaluate.py --llm      (full agent; needs GROQ_API_KEY)
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent import TroubleshootingAgent  # noqa: E402

EVAL = ROOT / "samples" / "eval"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="use the Groq-backed agent")
    ap.add_argument("--out", default=str(EVAL / "results.json"))
    args = ap.parse_args()
    if args.llm and not os.getenv("GROQ_API_KEY"):
        sys.exit("Set GROQ_API_KEY to evaluate the LLM agent.")

    labels = json.loads((EVAL / "labels.json").read_text())
    agent = TroubleshootingAgent()
    rows, by_type, confidence = [], Counter(), Counter()
    correct = 0
    for item in labels:
        report = agent.analyze((EVAL / item["file"]).read_text(), use_llm=args.llm)
        got = (report.get("root_cause") or {}).get("kb_id")
        ok = got == item["expected"]
        correct += ok
        by_type[(item["expected"], ok)] += 1
        confidence[(report["confidence"] or {}).get("level")] += 1
        rows.append({"file": item["file"], "expected": item["expected"], "predicted": got, "correct": ok,
                     "confidence": (report["confidence"] or {}).get("level"), "mode": report["mode"]})

    total = len(labels)
    print(f"Mode: {'llm agent' if args.llm else 'rule engine'}")
    print(f"Root cause correct: {correct}/{total} = {correct / total:.1%}")
    print("Confidence levels:", dict(confidence))
    misses = [r for r in rows if not r["correct"]]
    if misses:
        print("Misses:")
        for r in misses:
            print(f"  {r['file']}: expected {r['expected']}, got {r['predicted']}")
    Path(args.out).write_text(json.dumps({"mode": "llm" if args.llm else "rules", "correct": correct, "total": total,
                                          "accuracy": round(correct / total, 4), "rows": rows}, indent=1))


if __name__ == "__main__":
    main()
