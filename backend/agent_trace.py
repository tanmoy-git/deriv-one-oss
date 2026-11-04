"""Per-interaction traces for agent runs: the timeline behind one reply.

A trace collects what already flows through an agent run without changing how
the run works:

* every model response, through TokenAccumulator.add (both transports use it),
* every tool call, by stamping the tool_audit entries the run already appends,
* the system prompt the run used, so the exchange can be replayed later.

Collection is scoped with a ContextVar, so code that runs outside a trace (chat,
skill tools, tests) is unaffected. The caller that owns the interaction starts
the trace and passes ``trace.to_record()`` to record_agent_exchange, which
stores it next to the interaction row.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

# A runaway tool loop must not grow a trace without bound.
MAX_MODEL_CALLS = 200
MAX_SYSTEM_PROMPT_CHARS = 100_000

_current: ContextVar[Optional["AgentTrace"]] = ContextVar("agent_trace", default=None)


class TimedToolAudit(list):
    """A tool_audit list that stamps each entry with its time in the trace."""

    def __init__(self, trace: "AgentTrace"):
        super().__init__()
        self._trace = trace

    def append(self, entry: Any) -> None:
        if isinstance(entry, dict):
            entry.setdefault("t_ms", self._trace.elapsed_ms())
        super().append(entry)


class AgentTrace:
    def __init__(self) -> None:
        self.started = time.time()
        self.model_calls: list[dict] = []
        self.system_prompt: Optional[str] = None
        self.tool_audit = TimedToolAudit(self)

    def elapsed_ms(self) -> int:
        return int((time.time() - self.started) * 1000)

    def note_model_call(self, usage: Any) -> None:
        if len(self.model_calls) >= MAX_MODEL_CALLS:
            return
        self.model_calls.append({
            "t_ms": self.elapsed_ms(),
            "input_tokens": getattr(usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        })

    def to_record(self) -> dict:
        return {
            "model_calls": list(self.model_calls),
            "system_prompt": (self.system_prompt or "")[:MAX_SYSTEM_PROMPT_CHARS] or None,
        }


@contextmanager
def collecting(trace: Optional[AgentTrace] = None) -> Iterator[AgentTrace]:
    trace = trace or AgentTrace()
    token = _current.set(trace)
    try:
        yield trace
    finally:
        _current.reset(token)


def current() -> Optional[AgentTrace]:
    return _current.get()


def note_model_call(usage: Any) -> None:
    trace = _current.get()
    if trace is not None and usage is not None:
        trace.note_model_call(usage)


def note_system_prompt(system_prompt: str) -> None:
    trace = _current.get()
    if trace is not None and trace.system_prompt is None:
        trace.system_prompt = system_prompt
