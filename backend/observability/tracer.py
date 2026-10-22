"""
A small, dependency-free tracer.

Vocabulary (the same one OpenTelemetry and Langfuse use):

- trace:      everything that happened for ONE top-level call (e.g. one pipeline run).
- span:       one step inside a trace. It has a name, a start, a duration, an input,
              an output, and a parent (the step that was running when it started).
              Langfuse calls spans "observations".
- generation: a span that is an LLM call. It also records the model and token usage.

How nesting works:
    A ContextVar holds "the span that is currently open". When a new span starts, it
    reads that variable to find its parent, then sets itself as the current span.
    When it ends, the variable is restored. No function has to pass a parent around -
    the call hierarchy builds the tree by itself.

How storage works:
    Finished spans are kept in memory. When the ROOT span (the one with no parent)
    ends, the whole trace is written to one JSON file in traces/. Read them with
    observability/show_trace.py.

The function names (observe, start_as_current_observation, update_current_span, ...)
intentionally match the Langfuse Python SDK, so switching to Langfuse later is
mostly a matter of changing the import line.
"""

import functools
import inspect
import time
import uuid
import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

TRACE_DIR = Path(__file__).resolve().parent.parent / "traces"

# Very long strings (e.g. a huge prompt) are cut so trace files stay readable.
# The limit is deliberately generous: the whole point is to see the full prompt.
MAX_STRING_CHARS = 50_000

# The span that is open right now in this execution context (None = no trace running).
_current_span = ContextVar("current_span", default=None)


def _to_jsonable(obj):
    """Turn arbitrary Python objects into plain JSON-friendly data (a snapshot)."""
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        if len(obj) > MAX_STRING_CHARS:
            return obj[:MAX_STRING_CHARS] + f"...[truncated {len(obj) - MAX_STRING_CHARS} chars]"
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump"):  # pydantic models, e.g. ollama's Message
        return _to_jsonable(obj.model_dump())
    return repr(obj)


class Span:
    """One step of a trace. Created by Tracer.start_as_current_observation()."""

    def __init__(self, trace_id, parent_id, name, as_type):
        self.trace_id = trace_id
        self.span_id = uuid.uuid4().hex[:16]
        self.parent_id = parent_id
        self.name = name
        self.type = as_type
        self.start_time = time.time()  # wall-clock time, for display
        self._t0 = time.perf_counter()  # monotonic clock, for accurate durations
        self.duration_s = None
        self.input = None
        self.output = None
        self.metadata = {}
        self.model = None
        self.model_parameters = None
        self.usage = None
        self.level = None
        self.status = "ok"
        self.error = None

    def update(
        self,
        *,
        name=None,
        input=None,
        output=None,
        metadata=None,
        model=None,
        model_parameters=None,
        usage_details=None,
        level=None,
    ):
        """
        Attach information to this span. Only the fields you pass are changed
        (passing nothing for a field leaves it as it was). Values are snapshotted
        immediately, so later changes to the same list/dict don't alter the record.
        Metadata is merged into what is already there.
        """
        if name is not None:
            self.name = name
        if input is not None:
            self.input = _to_jsonable(input)
        if output is not None:
            self.output = _to_jsonable(output)
        if metadata is not None:
            self.metadata.update(_to_jsonable(metadata))
        if model is not None:
            self.model = model
        if model_parameters is not None:
            self.model_parameters = _to_jsonable(model_parameters)
        if usage_details is not None:
            self.usage = _to_jsonable(usage_details)
        if level is not None:
            self.level = level
        return self

    def to_dict(self):
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "type": self.type,
            "start_time": self.start_time,
            "duration_s": self.duration_s,
            "status": self.status,
            "error": self.error,
            "level": self.level,
            "model": self.model,
            "model_parameters": self.model_parameters,
            "usage": self.usage,
            "metadata": self.metadata,
            "input": self.input,
            "output": self.output,
        }


class Tracer:
    def __init__(self, trace_dir=TRACE_DIR, verbose=True):
        self.trace_dir = Path(trace_dir)
        self.verbose = verbose
        # trace_id -> spans of that trace, in the order they started
        self._open_traces = {}

    @contextmanager
    def start_as_current_observation(
        self,
        *,
        name,
        as_type="span",
        input=None,
        output=None,
        metadata=None,
        model=None,
        model_parameters=None,
        usage_details=None,
        level=None,
    ):
        """
        Open a span for the duration of a `with` block. Yields the Span so the caller can
        call span.update(...) with results once they are known.
        """
        parent = _current_span.get()
        # No parent -> this span is the root of a brand-new trace.
        trace_id = parent.trace_id if parent else uuid.uuid4().hex
        span = Span(trace_id, parent.span_id if parent else None, name, as_type)
        span.update(
            input=input,
            output=output,
            metadata=metadata,
            model=model,
            model_parameters=model_parameters,
            usage_details=usage_details,
            level=level,
        )
        self._open_traces.setdefault(trace_id, []).append(span)

        token = _current_span.set(span)  # from now on, new spans nest under this one
        try:
            yield span
        except BaseException as exc:
            span.status = "error"
            span.error = f"{type(exc).__name__}: {exc}"
            raise  # tracing must never swallow the application's own errors
        finally:
            span.duration_s = round(time.perf_counter() - span._t0, 4)
            _current_span.reset(token)  # restore the parent as the current span
            if parent is None:
                self._write_trace(trace_id)

    def update_current_span(self, **kwargs):
        """Update whichever span is open right now, without needing a reference to it."""
        span = _current_span.get()
        if span is not None:
            span.update(**kwargs)

    update_current_generation = update_current_span

    def flush(self):
        """
        No-op. Langfuse sends data in the background, so short scripts must flush before
        exiting. This tracer writes synchronously when the root span closes, so there is
        nothing left to flush. Kept so the call sites don't change if you switch later.
        """

    def _write_trace(self, trace_id):
        spans = self._open_traces.pop(trace_id, [])
        if not spans:
            return
        root = spans[0]
        try:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            started = datetime.fromtimestamp(root.start_time)
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in root.name)
            path = self.trace_dir / f"{started.strftime('%Y%m%d-%H%M%S')}_{safe_name}_{trace_id[:6]}.json"
            payload = {
                "trace_id": trace_id,
                "name": root.name,
                "started_at": started.isoformat(timespec="seconds"),
                "duration_s": root.duration_s,
                "spans": [s.to_dict() for s in spans],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            if self.verbose:
                print(f"[trace] saved {path.name} ({len(spans)} spans)")
        except Exception as exc:  # a tracing problem must never break the application
            print(f"[trace] could not write trace: {exc}")


_default_tracer = Tracer()


def get_client():
    """The shared tracer instance (same name as Langfuse's get_client())."""
    return _default_tracer


def observe(func=None, *, name=None, as_type="span", capture_input=True, capture_output=True):
    """
    Decorator: run the function inside a span.

        @observe(name="writer", as_type="agent")
        def write(...): ...

    The span records the function's arguments as its input and the return value as its
    output. Because the span becomes the "current span" while the function runs,
    every observed function it calls nests under it automatically.
    """

    def decorator(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            span_input = None
            if capture_input:
                try:
                    bound = signature.bind(*args, **kwargs)
                    bound.apply_defaults()
                    span_input = dict(bound.arguments)  # {parameter name: value}
                except TypeError:
                    span_input = {"args": args, "kwargs": kwargs}

            with _default_tracer.start_as_current_observation(
                name=name or fn.__name__, as_type=as_type, input=span_input
            ) as span:
                result = fn(*args, **kwargs)
                if capture_output:
                    span.update(output=result)
                return result

        return wrapper

    if func is not None and callable(func):  # used as a bare @observe
        return decorator(func)
    return decorator