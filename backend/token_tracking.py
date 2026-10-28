"""
Lightweight helper for recording LLM token usage.

Call record_token_usage() after every anthropic messages.create() response
or after a messages.stream() block completes. It never raises so it is safe
to call fire-and-forget even in streaming hot paths.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TokenAccumulator:
    """Accumulate usage across multiple LLM turns before flushing one row."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def add(self, usage) -> None:
        """Accept an anthropic Usage object or any object with token attributes."""
        if usage is None:
            return
        from agent_trace import note_model_call

        note_model_call(usage)
        self.input_tokens          += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens         += getattr(usage, "output_tokens", 0) or 0
        self.cache_read_tokens     += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_creation_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0


def record_token_usage(
    source: str,
    model: str,
    usage,
    user_email: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> None:
    """
    Persist one token-usage row.  Accepts either an anthropic Usage object
    directly or a TokenAccumulator that has already summed multiple turns.

    Never raises — failures are logged at WARNING level only.
    """
    try:
        from database import SessionLocal, TokenUsageLog

        if isinstance(usage, TokenAccumulator):
            inp  = usage.input_tokens
            out  = usage.output_tokens
            cr   = usage.cache_read_tokens
            cc   = usage.cache_creation_tokens
        else:
            inp  = getattr(usage, "input_tokens", 0) or 0
            out  = getattr(usage, "output_tokens", 0) or 0
            cr   = getattr(usage, "cache_read_input_tokens", 0) or 0
            cc   = getattr(usage, "cache_creation_input_tokens", 0) or 0

        if inp == 0 and out == 0:
            return

        try:
            from metrics import record_tokens

            record_tokens(source, model, inp, out)
        except Exception:
            logger.debug("token_tracking: metrics update failed", exc_info=True)

        with SessionLocal() as session:
            session.add(TokenUsageLog(
                source=source,
                model=model,
                user_email=user_email,
                agent_id=agent_id,
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=cr,
                cache_creation_tokens=cc,
            ))
            session.commit()
    except Exception as exc:
        logger.warning("token_tracking: failed to record usage: %s", exc)
