"""Code paths that used to raise NameError on an undefined module-level name."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def test_agent_llm_call_reads_the_platform_gateway_key(monkeypatch):
    # _call_llm passed `_GATEWAY_API_KEY`, which does not exist, so every Slack
    # reply, scheduled task and agent-to-agent call failed before the model.
    import agent_llm
    import api_slack_events

    seen = {}

    def resolve(agent, *, platform_key, read_secret):
        seen["platform_key"] = platform_key
        return "", "none"

    monkeypatch.setattr(api_slack_events, "_MODEL_GATEWAY_API_KEY", "platform-key")
    monkeypatch.setattr(agent_llm, "resolve_agent_gateway_api_key", resolve)

    outcome = asyncio.run(
        api_slack_events._call_llm("system", "hi", SimpleNamespace(id="a1", llm_model=None))
    )

    assert seen == {"platform_key": "platform-key"}
    assert outcome.text.startswith("I'm unable to process your request")


def test_manifest_leaves_out_the_ip_allowlist_when_none_is_configured(monkeypatch):
    from slack_oauth import generate_manifest

    monkeypatch.delenv("SLACK_MANIFEST_IP_ALLOWLIST", raising=False)
    settings = generate_manifest("Support bot")["settings"]
    assert "allowed_ip_address_ranges" not in settings


def test_manifest_uses_the_configured_ip_allowlist(monkeypatch):
    from slack_oauth import generate_manifest

    monkeypatch.setenv("SLACK_MANIFEST_IP_ALLOWLIST", "203.0.113.0/24, 198.51.100.7/32")
    settings = generate_manifest("Support bot")["settings"]
    assert settings["allowed_ip_address_ranges"] == ["203.0.113.0/24", "198.51.100.7/32"]


def test_system_classifier_truncates_skill_text(monkeypatch):
    import anthropic

    import skill_system_matcher

    prompts = []

    class FakeClient:
        def __init__(self, **_kwargs):
            self.messages = self

        def create(self, **kwargs):
            prompts.append(kwargs["messages"][0]["content"])
            raise RuntimeError("no gateway in tests")

    monkeypatch.setattr(skill_system_matcher, "MODEL_GATEWAY_API_KEY", "key")
    monkeypatch.setattr(skill_system_matcher, "_MAX_CONTENT_CHARS", 10)
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)

    systems = {"s1": {"system_name": "Salesforce"}}
    result = asyncio.run(skill_system_matcher.classify_systems_with_llm("x" * 50, systems))

    assert result == []
    assert "x" * 10 in prompts[0]
    assert "x" * 11 not in prompts[0]


@pytest.mark.parametrize(
    "error, expected",
    [
        ("timeout", "Error: Request timed out after 15 seconds."),
        ("status", "Error: HTTP 503"),
    ],
)
def test_fetch_url_reports_http_errors_instead_of_crashing(monkeypatch, error, expected):
    # main.py caught httpx exceptions without importing httpx, so any fetch
    # error turned into a NameError.
    import httpx

    import main
    import url_fetch_guard

    request = httpx.Request("GET", "https://example.com/")

    async def fetch(*_a, **_k):
        if error == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("bad", request=request, response=response)

    monkeypatch.setattr(url_fetch_guard, "validate_fetch_url", lambda _url: (True, ""))
    monkeypatch.setattr(url_fetch_guard, "async_fetch_response", fetch)

    assert asyncio.run(main._fetch_url_content("https://example.com/")).startswith(expected)
