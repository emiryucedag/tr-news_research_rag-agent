"""
Does the relevance filter give the same answer when asked the same question twice?

It reuses the exact candidate list stored in a saved trace (the input of the
"relevance-filter" span), so the ONLY thing that can differ between runs is the
model's own sampling - not new articles, not a different time window.

    python3 evals/check_determinism.py               # most recent usable trace, 3 runs
    python3 evals/check_determinism.py 2 --runs 4    # second most recent, 4 runs

Needs Ollama running. Each run takes about as long as the filter step in a normal
pipeline run (roughly a minute).
"""

import argparse
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from observability.tracer import TRACE_DIR
from agents.multi_agent import filter_candidates, tracer


def load_filter_input(which):
    """Find the which-th most recent trace that contains a usable relevance-filter span."""
    candidates = []
    for path in sorted(TRACE_DIR.glob("*.json"), reverse=True):
        trace = json.loads(path.read_text(encoding="utf-8"))
        if trace["name"] == "determinism-check":  # skip this script's own traces
            continue
        for span in trace["spans"]:
            span_input = span.get("input")
            if span["name"] == "relevance-filter" and isinstance(span_input, dict) and "raw_results" in span_input:
                candidates.append((path.name, span_input["user_question"], span_input["raw_results"]))
                break
    if not candidates:
        return None
    if not 1 <= which <= len(candidates):
        return None
    return candidates[which - 1]


def main():
    parser = argparse.ArgumentParser(description="Check whether the relevance filter is repeatable")
    parser.add_argument("which", nargs="?", type=int, default=1, help="1 = most recent usable trace")
    parser.add_argument("--runs", type=int, default=3, help="how many times to run the filter")
    args = parser.parse_args()

    loaded = load_filter_input(args.which)
    if loaded is None:
        print("No usable trace found. Run the pipeline once first (python3 agents/multi_agent_ollama.py).")
        return
    trace_name, question, raw_results = loaded
    titles = {item["link"]: item["title"] for item in raw_results}

    print(f"Using trace {trace_name}: {len(raw_results)} candidates, question: {question!r}")
    print(f"Running the filter {args.runs} times on exactly the same candidates...\n")

    selections = []
    with tracer.start_as_current_observation(
        name="determinism-check", as_type="evaluator", input={"source_trace": trace_name, "runs": args.runs}
    ) as check_span:
        for _ in range(args.runs):
            findings = filter_candidates(question, raw_results)
            selections.append({item["link"] for item in findings})
        in_all = set.intersection(*selections)
        in_some = set.union(*selections) - in_all
        check_span.update(output={"selected_per_run": [len(s) for s in selections], "in_all_runs": len(in_all), "in_some_runs": len(in_some)})

    print("\n" + "=" * 70)
    for i, selected in enumerate(selections, start=1):
        print(f"run {i}: {len(selected)} selected")
    print(f"\nselected in ALL {args.runs} runs : {len(in_all)}")
    print(f"selected in SOME runs   : {len(in_some)}   (the model is not consistent about these)")
    for link in sorted(in_some, key=lambda l: titles.get(l, "")):
        count = sum(1 for s in selections if link in s)
        print(f"   {count}/{args.runs}  {titles.get(link, link)}")

    print("\nVERDICT:", "identical across runs" if not in_some else "answers differ between runs")


if __name__ == "__main__":
    main()