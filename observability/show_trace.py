"""
Read the trace files written by observability/tracer.py.

    python3 observability/show_trace.py                  # list recent traces
    python3 observability/show_trace.py 1                # tree of the most recent trace
    python3 observability/show_trace.py 2                # tree of the second most recent
    python3 observability/show_trace.py 1 --io           # tree + short input/output previews
    python3 observability/show_trace.py 1 --span writer  # full input/output of matching spans
"""

import argparse
import json
from pathlib import Path
 
TRACE_DIR = Path(__file__).resolve().parent.parent / "traces"


def load_trace_files():
    # File names start with a timestamp, so reverse alphabetical order = newest first.
    return sorted(TRACE_DIR.glob("*.json"), reverse=True)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def fmt_seconds(seconds):
    if seconds is None:
        return "?"
    return f"{seconds * 1000:.0f}ms" if seconds < 1 else f"{seconds:.1f}s"


def token_totals(spans):
    tokens_in = sum((s["usage"] or {}).get("input_tokens", 0) for s in spans)
    tokens_out = sum((s["usage"] or {}).get("output_tokens", 0) for s in spans)
    return tokens_in, tokens_out


def short(value, limit=90):
    """One-line preview of any value."""
    if value is None:
        return "-"
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def list_traces(files, limit=10):
    if not files:
        print(f"No traces found in {TRACE_DIR}. Run the pipeline once first.")
        return
    print(f"{'#':>2}  {'started':<19}  {'root':<22} {'time':>7}  {'spans':>5}  {'tokens in/out':>14}  errors")
    for i, path in enumerate(files[:limit], start=1):
        trace = read(path)
        spans = trace["spans"]
        tokens_in, tokens_out = token_totals(spans)
        errors = sum(1 for s in spans if s["status"] == "error")
        print(
            f"{i:>2}  {trace['started_at'].replace('T', ' '):<19}  {trace['name']:<22} "
            f"{fmt_seconds(trace['duration_s']):>7}  {len(spans):>5}  {f'{tokens_in}/{tokens_out}':>14}  {errors}"
        )
    print("\nOpen one with:  python3 observability/show_trace.py <number>")


def describe(span, root_duration):
    parts = [span["name"], f"[{span['type']}]", fmt_seconds(span["duration_s"])]

    if root_duration and span["duration_s"] is not None:
        parts.append(f"({span['duration_s'] / root_duration * 100:.0f}%)")

    usage = span["usage"]
    if usage:
        parts.append(f"tokens in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}")

    metadata = span["metadata"] or {}
    if span["type"] == "generation":
        load = metadata.get("ollama_model_load_seconds", 0)
        if load and load > 0.5:
            parts.append(f"model_load={load}s")  # first call after the model was unloaded
    elif metadata:
        parts.append(short(metadata, 100))

    if span["status"] == "error":
        parts.append(f"ERROR: {span['error']}")
    return "  ".join(parts)


def print_tree(trace, show_io):
    spans = trace["spans"]
    children = {}
    for s in spans:
        children.setdefault(s["parent_id"], []).append(s)
    for siblings in children.values():
        siblings.sort(key=lambda s: s["start_time"])

    root = spans[0]
    root_duration = root["duration_s"]

    print(f"trace {trace['trace_id'][:8]}  started {trace['started_at']}  total {fmt_seconds(root_duration)}")
    print("(percentages are relative to the root span; if the pipeline waited for your")
    print(" approval at input(), that waiting time is inside the root's total)\n")

    def walk(span, prefix, is_last, is_root):
        branch = "" if is_root else ("└─ " if is_last else "├─ ")
        print(prefix + branch + describe(span, root_duration))
        child_prefix = prefix + ("" if is_root else ("   " if is_last else "│  "))
        if show_io:
            print(child_prefix + "   in : " + short(span["input"]))
            print(child_prefix + "   out: " + short(span["output"]))
        kids = children.get(span["span_id"], [])
        for i, kid in enumerate(kids):
            walk(kid, child_prefix, i == len(kids) - 1, False)

    walk(root, "", True, True)

    tokens_in, tokens_out = token_totals(spans)
    print(f"\ntotal tokens: in={tokens_in} out={tokens_out}   spans: {len(spans)}")


def pretty(value):
    """Readable full dump: chat messages as role + raw text, everything else as indented JSON."""
    if value is None:
        return "-"
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(m, dict) and "role" in m for m in value):
        blocks = []
        for m in value:
            blocks.append(f"[{m['role']}]\n{m.get('content', '')}")
            if m.get("tool_calls"):
                blocks.append("tool_calls: " + json.dumps(m["tool_calls"], ensure_ascii=False))
        return "\n\n".join(blocks)
    if isinstance(value, dict) and "role" in value:
        return pretty([value])
    return json.dumps(value, ensure_ascii=False, indent=2)


def print_spans(trace, name_part):
    matches = [s for s in trace["spans"] if name_part.lower() in s["name"].lower()]
    if not matches:
        print(f"No span with '{name_part}' in its name. Names in this trace:")
        for s in trace["spans"]:
            print("  -", s["name"])
        return
    for s in matches:
        print("=" * 78)
        print(f"{s['name']}  [{s['type']}]  {fmt_seconds(s['duration_s'])}  status={s['status']}")
        if s["model"]:
            print(f"model: {s['model']}   parameters: {s['model_parameters']}")
        if s["usage"]:
            print(f"usage: {s['usage']}")
        if s["metadata"]:
            print(f"metadata: {json.dumps(s['metadata'], ensure_ascii=False)}")
        if s["error"]:
            print(f"error: {s['error']}")
        print("-" * 30, "INPUT", "-" * 30)
        print(pretty(s["input"]))
        print("-" * 30, "OUTPUT", "-" * 29)
        print(pretty(s["output"]))
        print()


def main():
    parser = argparse.ArgumentParser(description="Inspect traces written by observability/tracer.py")
    parser.add_argument("which", nargs="?", type=int, help="1 = most recent trace, 2 = the one before, ...")
    parser.add_argument("--io", action="store_true", help="show short input/output previews in the tree")
    parser.add_argument("--span", metavar="NAME", help="print the full input/output of spans whose name contains NAME")
    args = parser.parse_args()

    files = load_trace_files()
    if args.which is None:
        list_traces(files)
        return
    if not 1 <= args.which <= len(files):
        print(f"Pick a number between 1 and {len(files)}.")
        return

    trace = read(files[args.which - 1])
    if args.span:
        print_spans(trace, args.span)
    else:
        print_tree(trace, args.io)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass  # e.g. output piped into `head` or `less` and closed early