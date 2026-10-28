"""Trace and replay for one agent interaction.

GET  /api/agents/{agent_id}/interactions/{interaction_id}/trace
     The timeline behind a reply: skill routing, each model call with tokens
     and cost, each tool call, and totals. Interactions recorded before traces
     existed still get a timeline from their tool audit, without timings.

POST /api/agents/{agent_id}/interactions/{interaction_id}/replay
     Run the same system prompt and user message on another model, as a dry
     run: no tools are offered, so nothing outside the model is touched. Used
     to compare models on a real exchange.

Both are limited to the agent's owner, collaborators and admins, the same as
the interaction detail endpoint.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agents import AgentInteractionLog, AgentInteractionTrace
from api_agents import _auth_context, _get_agent_for_interaction_view, _user_email_from_request
from auth_middleware import deps_owner_sub
from database import DB
from model_pricing import cost_for_model

logger = logging.getLogger(__name__)

router = APIRouter()

REPLAY_MAX_TOKENS = 2048


def _loads(raw: Optional[str], default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


def build_timeline(row: AgentInteractionLog, trace: Optional[AgentInteractionTrace]) -> dict:
    """Pure: merge routing, model calls and tool calls into one ordered timeline."""
    model = row.resolved_model or ""
    steps: list[dict] = []

    routing = _loads(row.routing_metadata, None)
    if isinstance(routing, dict) and routing:
        steps.append({
            "kind": "routing",
            "t_ms": 0,
            "selected": routing.get("selected_slugs") or routing.get("selected") or [],
            "mode": routing.get("mode"),
            "confidence": routing.get("confidence"),
        })

    calls = _loads(trace.model_calls, []) if trace is not None else []
    for i, call in enumerate(calls, start=1):
        inp = int(call.get("input_tokens") or 0)
        out = int(call.get("output_tokens") or 0)
        cr = int(call.get("cache_read_tokens") or 0)
        cc = int(call.get("cache_creation_tokens") or 0)
        steps.append({
            "kind": "model",
            "t_ms": call.get("t_ms"),
            "turn": i,
            "model": model,
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cr,
            "cost_usd": round(cost_for_model(model, inp, out, cr, cc), 6),
        })

    # Tool audit entries never contain secret values; code bodies only appear
    # when AGENT_TOOL_AUDIT_BODIES opted in, already redacted.
    for entry in _loads(row.tools_called, []):
        if not isinstance(entry, dict):
            continue
        step = {"kind": "tool", "t_ms": entry.get("t_ms"), "tool": entry.get("tool") or entry.get("name")}
        for key in ("ok", "code_len", "output_len", "artifact", "slug", "query", "code_redacted", "error"):
            if key in entry:
                step[key] = entry[key]
        steps.append(step)

    # Steps without a time (older interactions) keep their recorded order.
    timed = all(s.get("t_ms") is not None for s in steps)
    if timed:
        steps.sort(key=lambda s: s["t_ms"])

    model_steps = [s for s in steps if s["kind"] == "model"]
    tool_steps = [s for s in steps if s["kind"] == "tool"]
    return {
        "steps": steps,
        "timed": timed and trace is not None,
        "totals": {
            "duration_ms": row.duration_ms,
            "model_calls": len(model_steps),
            "input_tokens": sum(s["input_tokens"] for s in model_steps),
            "output_tokens": sum(s["output_tokens"] for s in model_steps),
            "cost_usd": round(sum(s["cost_usd"] for s in model_steps), 6),
            "tool_calls": len(tool_steps),
            "tool_errors": sum(1 for s in tool_steps if s.get("ok") is False),
        },
    }


def _load(db, agent_id: str, interaction_id: str, request: Request, owner_sub: str):
    auth = _auth_context(request)
    agent = _get_agent_for_interaction_view(
        db, agent_id, owner_sub, auth.is_admin, _user_email_from_request(request)
    )
    row = (
        db.query(AgentInteractionLog)
        .filter(AgentInteractionLog.id == interaction_id, AgentInteractionLog.agent_id == agent_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Interaction not found")
    return agent, row, db.get(AgentInteractionTrace, interaction_id)


@router.get("/api/agents/{agent_id}/interactions/{interaction_id}/trace")
def get_interaction_trace(
    agent_id: str,
    interaction_id: str,
    request: Request,
    db: DB,
    owner_sub: str = Depends(deps_owner_sub),
) -> dict[str, Any]:
    _, row, trace = _load(db, agent_id, interaction_id, request, owner_sub)
    timeline = build_timeline(row, trace)
    return {
        "interaction_id": row.id,
        "created_at": row.created_at.isoformat(),
        "trigger_type": row.trigger_type,
        "resolved_model": row.resolved_model,
        "failure_kind": row.failure_kind,
        "failure_detail": row.failure_detail,
        "user_message": row.user_message,
        "assistant_message": row.assistant_message,
        "replayable": bool(trace is not None and trace.system_prompt),
        **timeline,
    }


class ReplayBody(BaseModel):
    model: str = Field(min_length=1, max_length=120)


async def _complete(model: str, system: str, user: str, api_key: str) -> tuple[str, int, int]:
    """One tool-free model call through the gateway. Returns text and token counts."""
    import anthropic
    from openai import AsyncOpenAI

    from agent_llm import uses_openai_agent_transport
    from api_slack_events import _MODEL_GATEWAY_URL, _MODEL_GATEWAY_URL_RAW
    from model_catalog import gateway_openai_base_url

    if uses_openai_agent_transport(model):
        client = AsyncOpenAI(api_key=api_key, base_url=gateway_openai_base_url(_MODEL_GATEWAY_URL_RAW))
        try:
            response = await client.chat.completions.create(
                model=model,
                max_tokens=REPLAY_MAX_TOKENS,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
        finally:
            await client.close()
        usage = response.usage
        text = (response.choices[0].message.content or "") if response.choices else ""
        return (
            text,
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
        )

    client = anthropic.AsyncAnthropic(api_key=api_key, base_url=_MODEL_GATEWAY_URL)
    response = await client.messages.create(
        model=model,
        max_tokens=REPLAY_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text")
    return text, int(response.usage.input_tokens or 0), int(response.usage.output_tokens or 0)


@router.post("/api/agents/{agent_id}/interactions/{interaction_id}/replay")
async def replay_interaction(
    agent_id: str,
    interaction_id: str,
    body: ReplayBody,
    request: Request,
    db: DB,
    owner_sub: str = Depends(deps_owner_sub),
) -> dict[str, Any]:
    from agent_llm import resolve_agent_gateway_api_key
    from api_slack_events import _MODEL_GATEWAY_API_KEY, _MODEL_GATEWAY_URL_RAW, _read_secret
    from model_catalog import get_model_catalog, is_chat_model_allowed, platform_default_model
    from token_tracking import record_token_usage

    agent, row, trace = _load(db, agent_id, interaction_id, request, owner_sub)
    if trace is None or not trace.system_prompt:
        raise HTTPException(
            status_code=409,
            detail="This interaction was recorded before traces were kept, so it cannot be replayed.",
        )

    catalog = await get_model_catalog(
        api_key=_MODEL_GATEWAY_API_KEY,
        base_url_raw=_MODEL_GATEWAY_URL_RAW,
        default_model=platform_default_model(),
    )
    if not is_chat_model_allowed(body.model, catalog):
        raise HTTPException(status_code=400, detail=f"Model {body.model!r} is not available.")

    api_key, _src = resolve_agent_gateway_api_key(
        agent, platform_key=_MODEL_GATEWAY_API_KEY, read_secret=_read_secret
    )
    if not api_key:
        raise HTTPException(status_code=503, detail="No model gateway key is configured.")

    started = time.monotonic()
    try:
        text, inp, out = await _complete(body.model, trace.system_prompt, row.user_message, api_key)
    except Exception as exc:
        logger.warning("trace replay failed agent_id=%s model=%s: %s", agent_id, body.model, exc)
        raise HTTPException(status_code=502, detail="The model call failed. Try again or pick another model.") from exc

    class _Usage:
        input_tokens = inp
        output_tokens = out

    record_token_usage("trace_replay", body.model, _Usage(), agent_id=agent_id)
    return {
        "model": body.model,
        "output": text,
        "input_tokens": inp,
        "output_tokens": out,
        "cost_usd": round(cost_for_model(body.model, inp, out, 0, 0), 6),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "dry_run": True,
        "original": {"model": row.resolved_model, "output": row.assistant_message},
    }
