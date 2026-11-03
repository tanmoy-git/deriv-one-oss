"""Best-effort matching of a skill to catalogued systems (name + risk level only).

Two sources feed this:
  1. Declared `integrations` tags (e.g. "Data Warehouse (read)", "Slack (write)")
     from src/data/skills-acl.yaml — matched via match_system() (exact/fuzzy
     string matching; safe here because tags are short and deliberately typed).
     A trailing "(read)"/"(write)"/"(read/write)" annotation is parsed into
     `direction` rather than discarded.
  2. The skill's own content — its name, description, and SKILL.md body —
     classified via classify_systems_with_llm(), which asks a fast/cheap LLM
     to pick which candidate the system catalogue systems the skill genuinely integrates
     with, and whether it reads from, writes to, or both. A plain
     substring/word-boundary scan was tried first but produced heavy false
     positives on generic-word system names (e.g. "Make", "Reach", "Interact"
     are common English words as well as real systems) — an LLM can tell
     "this skill calls Make.com" apart from "the word 'make' appears in a
     sentence," and catches synonyms/paraphrasing a literal scan would miss.

resolve_skill_systems() combines both sources so new/edited skills get their
system + criticality identified automatically from what's actually in the
skill, not just from a manually-maintained tag list.

Platform-wide systems every skill runs on regardless of what it does (Slack —
this whole platform is Slack bots) are excluded via _IGNORED_SYSTEM_NAMES:
flagging "this skill uses Slack" is true of every skill and tells you nothing.

Only system_name and risk_level are pulled from the system catalogue (via
system_catalog.get_system_map) — no
owner, Slack channel, system key, or other identifying metadata, even though
that query supports fetching them.

Matches against the connected-system catalogue, which is a
separate curated static list of systems for API-feasibility hints — this
module matches against the live the system catalogue system registry itself.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Literal

import system_catalog
from llm_json import MODEL_GATEWAY_API_KEY, MODEL_GATEWAY_URL

logger = logging.getLogger(__name__)

MatchConfidence = Literal["exact", "fuzzy", "content", "manual", "unmatched"]
MatchSource = Literal["integration_tag", "skill_content", "manual"]
Direction = Literal["read", "write", "read_write"]

# Trailing access-mode annotations like "(read)", "(write)", "(read/write)"
_PAREN_SUFFIX_RE = re.compile(r"\s*\(([^)]*)\)\s*$")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")
_WHITESPACE_RE = re.compile(r"\s+")

# Ignore common filler words when scoring token overlap — they inflate the
# apparent match confidence of otherwise unrelated system names.
_STOPWORDS = frozenset({"api", "app", "the", "and", "of", "for", "graphql", "rest"})

# Platform-wide systems every skill runs on regardless of function — not
# useful signal for "which system does this skill depend on."
_IGNORED_SYSTEM_NAMES = frozenset({"slack"})

from model_catalog import platform_default_model

_CLASSIFIER_MODEL = (
    os.environ.get("SKILL_SYSTEM_CLASSIFIER_MODEL", "").strip()
    or platform_default_model()
)
_CLASSIFIER_TIMEOUT_S = float(os.environ.get("SKILL_SYSTEM_CLASSIFIER_TIMEOUT_S", "20"))
# Skill text sent to the classifier is cut to this many characters.
_MAX_CONTENT_CHARS = int(os.environ.get("SKILL_SYSTEM_CLASSIFIER_MAX_CHARS", "12000"))


@dataclass(frozen=True)
class MatchResult:
    integration_tag: str
    system_name: str | None
    risk_level: str | None
    confidence: MatchConfidence
    source: MatchSource = "integration_tag"
    direction: Direction | None = None


def normalize_integration_tag(tag: str) -> str:
    """Strip access-mode annotations and normalize for comparison."""
    stripped = _PAREN_SUFFIX_RE.sub("", tag or "").strip().lower()
    stripped = _NON_ALNUM_RE.sub(" ", stripped)
    return _WHITESPACE_RE.sub(" ", stripped).strip()


def _parse_direction(tag: str) -> Direction | None:
    """Parse a trailing "(read)"/"(write)"/"(read/write)" annotation, if any."""
    m = _PAREN_SUFFIX_RE.search(tag or "")
    if not m:
        return None
    inner = m.group(1).lower()
    has_read = "read" in inner
    has_write = "write" in inner
    if has_read and has_write:
        return "read_write"
    if has_write:
        return "write"
    if has_read:
        return "read"
    return None


def _is_ignored_system(system_name: str | None) -> bool:
    if not system_name:
        return False
    return normalize_integration_tag(system_name) in _IGNORED_SYSTEM_NAMES


def _tokens(text: str) -> set[str]:
    return {t for t in text.split(" ") if t and t not in _STOPWORDS}


def match_system(tag: str, systems: dict[str, dict]) -> MatchResult:
    """Match a single integration tag against the the system catalogue systems map.

    *systems* is keyed by lowercase systemName, as returned by
    system_catalog.get_system_map().
    """
    direction = _parse_direction(tag)
    normalized = normalize_integration_tag(tag)
    if not normalized:
        return MatchResult(tag, None, None, "unmatched", direction=direction)

    # Exact match against the normalized system name.
    if normalized in systems:
        rec = systems[normalized]
        return _to_result(tag, rec, "exact", direction=direction)

    # Fuzzy: substring match either direction, or significant token overlap.
    tag_tokens = _tokens(normalized)
    best_rec: dict | None = None
    best_score = 0.0

    for sys_name_lower, rec in systems.items():
        sys_norm = normalize_integration_tag(sys_name_lower)
        if not sys_norm:
            continue

        score = 0.0
        if normalized in sys_norm or sys_norm in normalized:
            score = 0.9
        else:
            sys_tokens = _tokens(sys_norm)
            if tag_tokens and sys_tokens:
                overlap = tag_tokens & sys_tokens
                if overlap:
                    score = len(overlap) / max(len(tag_tokens), len(sys_tokens))

        if score > best_score:
            best_score = score
            best_rec = rec

    if best_rec is not None and best_score >= 0.5:
        return _to_result(tag, best_rec, "fuzzy", direction=direction)

    return MatchResult(tag, None, None, "unmatched", direction=direction)


def _to_result(
    tag: str,
    rec: dict,
    confidence: MatchConfidence,
    source: MatchSource = "integration_tag",
    direction: Direction | None = None,
) -> MatchResult:
    return MatchResult(
        integration_tag=tag,
        system_name=rec.get("system_name"),
        risk_level=rec.get("risk_level"),
        confidence=confidence,
        source=source,
        direction=direction,
    )


_CLASSIFY_TOOL_NAME = "classify_skill_systems"


def _classify_tool(candidate_names: list[str]) -> dict:
    return {
        "name": _CLASSIFY_TOOL_NAME,
        "description": (
            "Identify which of the candidate systems this skill genuinely "
            "integrates with, queries, or otherwise depends on, and whether "
            "it reads from, writes to, or both."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "systems": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "system": {
                                "type": "string",
                                "enum": candidate_names,
                                "description": "Exact name from the candidate list.",
                            },
                            "direction": {
                                "type": "string",
                                "enum": ["read", "write", "read_write"],
                                "description": (
                                    "read = the skill only retrieves/queries information FROM "
                                    "this system. write = the skill only sends/creates/updates "
                                    "information IN this system. read_write = it does both."
                                ),
                            },
                            "evidence_quote": {
                                "type": "string",
                                "description": (
                                    "A short VERBATIM quote (a few words to one sentence) copied "
                                    "exactly from the skill documentation above that names or "
                                    "clearly refers to this system. Must be an exact substring of "
                                    "the provided text, not a paraphrase or summary. If you cannot "
                                    "find an exact quote supporting this system, do not include "
                                    "the system at all."
                                ),
                            },
                        },
                        "required": ["system", "direction", "evidence_quote"],
                    },
                    "description": (
                        "Only systems THIS skill directly reads from or writes to itself via "
                        "its own documented API calls, tool calls, or data access. Do not "
                        "include a system just because a similar word appears in passing prose "
                        "(e.g. the word 'make' in a sentence does not mean the skill uses the "
                        "tool 'Make'). Do not include a system just because it appears in the "
                        "NAME or description of another agent/bot this skill tags or delegates "
                        "to (e.g. mentioning or @-tagging 'some other agent' in Slack "
                        "is NOT the same as this skill itself calling a system named "
                        "'Backoffice' — the other agent may use that system internally, but "
                        "that is that agent's dependency, not this skill's). Only include a "
                        "system if the skill's own steps — not another agent's — clearly "
                        "integrate with, query, or depend on it. Do not pick a candidate just "
                        "because its name sounds similar or shares a word with what the skill "
                        "describes — two systems can share a word (e.g. a generic 'Backoffice' "
                        "tool vs. a distinct 'IAM Backoffice' portal are NOT automatically the "
                        "same system). Only select a candidate if you are confident it refers "
                        "to the exact same real-world system the skill describes, not merely a "
                        "similarly-named one. Prefer an empty list over a guess. Every entry "
                        "MUST include an exact verbatim evidence_quote from the text — never "
                        "include a system with no supporting quote."
                    ),
                },
            },
            "required": ["systems"],
        },
    }


def _is_grounded(quote: str, source_text: str) -> bool:
    """Verify *quote* is an actual (near-verbatim) substring of *source_text*.

    Guards against the model naming a system with no real textual support in
    the skill's own content — a paraphrase or fabricated quote fails this
    check and the system is dropped. Whitespace is collapsed and the
    comparison is case-insensitive; otherwise the match must be exact, since
    a genuine quote should be directly copyable from the source.
    """
    if not quote or not quote.strip():
        return False
    norm_quote = _WHITESPACE_RE.sub(" ", quote.strip().lower())
    norm_source = _WHITESPACE_RE.sub(" ", source_text.lower())
    return norm_quote in norm_source


async def classify_systems_with_llm(text: str, systems: dict[str, dict]) -> list[MatchResult]:
    """Use a fast/cheap LLM to identify which the system catalogue systems a skill's content
    genuinely references (and whether it reads/writes/both), avoiding the
    false positives a plain substring scan produces on generic-word system
    names (e.g. "Make", "Reach", "Interact").

    Best-effort: returns [] on missing config, LLM failure, or bad output —
    callers should not fall back to a noisier heuristic on failure.
    """
    if not text or not systems or not MODEL_GATEWAY_API_KEY:
        return []

    candidate_names = sorted({
        rec["system_name"] for rec in systems.values()
        if rec.get("system_name") and not _is_ignored_system(rec.get("system_name"))
    })
    if not candidate_names:
        return []

    truncated = text[:_MAX_CONTENT_CHARS]
    prompt = (
        "Skill documentation:\n\n"
        f"{truncated}\n\n"
        f"Call the {_CLASSIFY_TOOL_NAME} tool with the systems THIS skill directly reads "
        "from or writes to itself, and the direction for each.\n\n"
        "Important: this skill may @-mention or delegate to other agents/bots in Slack "
        "(e.g. \"tag @SomeOtherAgent\"). That is NOT the same as this skill "
        "using whatever system is named in that other agent's name or description — only "
        "count a system if this skill's own steps call it directly (its own API endpoints, "
        "its own tool calls, its own data reads/writes).\n\n"
        "Also: do not match a candidate system just because it shares a word or sounds "
        "similar to something the skill describes (e.g. a generic 'Backoffice' tool in the "
        "candidate list is NOT the same system as a distinct 'IAM Backoffice' portal the "
        "skill actually uses, even though both contain the word 'Backoffice'). Only pick a "
        "candidate if you are confident it is the exact same real-world system — if unsure, "
        "leave it out rather than guess."
    )

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=MODEL_GATEWAY_API_KEY, base_url=MODEL_GATEWAY_URL)
        response = await asyncio.to_thread(
            client.messages.create,
            model=_CLASSIFIER_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
            tools=[_classify_tool(candidate_names)],
            tool_choice={"type": "tool", "name": _CLASSIFY_TOOL_NAME},
            timeout=_CLASSIFIER_TIMEOUT_S,
        )
    except Exception:
        logger.exception("classify_systems_with_llm: LLM call failed")
        return []

    entries: list[dict] = []
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == _CLASSIFY_TOOL_NAME:
            raw = block.input.get("systems") if isinstance(block.input, dict) else None
            if isinstance(raw, list):
                entries = [e for e in raw if isinstance(e, dict)]
            break

    results: list[MatchResult] = []
    seen: set[str] = set()
    for entry in entries:
        name = entry.get("system")
        if not isinstance(name, str):
            continue
        if _is_ignored_system(name):
            # Tool-schema `enum` isn't strictly enforced server-side — the model
            # can still return a name outside the candidate list, so this is a
            # necessary backstop, not redundant with the candidate-list filter.
            continue
        rec = systems.get(name.lower())
        if not rec:
            continue
        evidence_quote = entry.get("evidence_quote")
        if not isinstance(evidence_quote, str) or not _is_grounded(evidence_quote, truncated):
            # No verifiable textual support for this system in the skill's own
            # content — treat as an ungrounded/hallucinated attribution and drop it.
            logger.info(
                "classify_systems_with_llm: dropping ungrounded system %r (quote=%r not found in text)",
                name, evidence_quote,
            )
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        direction = entry.get("direction")
        if direction not in ("read", "write", "read_write"):
            direction = None
        results.append(_to_result(name, rec, "content", source="skill_content", direction=direction))

    return results


async def resolve_skill_integrations(integrations: list[str]) -> list[MatchResult]:
    """Resolve every declared integration tag against the system catalogue."""
    if not integrations:
        return []
    systems = system_catalog.get_system_map()
    return [r for r in (match_system(tag, systems) for tag in integrations) if not _is_ignored_system(r.system_name)]


async def resolve_skill_systems(
    *,
    integrations: list[str],
    name: str = "",
    description: str = "",
    skill_content: str = "",
) -> list[MatchResult]:
    """Resolve a skill's connected systems from BOTH its declared integration
    tags AND its own content (name, description, SKILL.md body).

    Declared-tag matches take priority; content-detected systems already
    covered by a declared tag are not duplicated. Platform-wide systems
    (e.g. Slack) are excluded — see _IGNORED_SYSTEM_NAMES.
    """
    systems = system_catalog.get_system_map()

    tag_results = [
        r for r in (match_system(tag, systems) for tag in integrations)
        if not _is_ignored_system(r.system_name)
    ]
    matched_names = {
        normalize_integration_tag(r.system_name) for r in tag_results if r.system_name
    }

    corpus = "\n".join(part for part in (name, description, skill_content) if part)
    llm_results = await classify_systems_with_llm(corpus, systems)
    content_results = [
        r for r in llm_results
        if normalize_integration_tag(r.system_name or "") not in matched_names
    ]

    return tag_results + content_results
