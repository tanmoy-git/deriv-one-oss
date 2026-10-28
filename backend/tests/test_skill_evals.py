"""Skill eval harness: graders, runner, gate decision, API and the publish hook.

The model gateway is never called: every test passes or patches in a fake
model function.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

import skill_evals
from skill_evals import CaseSpec


SKILL_MD = "---\nname: Greeter\ndescription: says hi\n---\nAlways greet the user by saying HELLO.\n"


def _fake_model(replies: dict[str, str]):
    """A model whose reply depends on the user message; records system prompts."""
    seen: list[str] = []

    def call(system: str, user: str, model: str) -> str:
        seen.append(system)
        if user in replies:
            reply = replies[user]
            if isinstance(reply, Exception):
                raise reply
            return reply
        return "default reply"

    call.seen = seen  # type: ignore[attr-defined]
    return call


# ── Graders ───────────────────────────────────────────────────────────────────


def test_contains_is_case_insensitive():
    assert skill_evals.grade("contains", "Well, HELLO there", "hello").status == "pass"
    assert skill_evals.grade("contains", "Goodbye", "hello").status == "fail"


def test_exact_ignores_surrounding_whitespace_only():
    assert skill_evals.grade("exact", "  42\n", "42").status == "pass"
    assert skill_evals.grade("exact", "42.", "42").status == "fail"
    assert skill_evals.grade("exact", "Hello", "hello").status == "fail"


def test_regex_searches_anywhere_in_the_reply():
    assert skill_evals.grade("regex", "ticket ABC-123 created", r"[A-Z]+-\d+").status == "pass"
    assert skill_evals.grade("regex", "no ticket", r"[A-Z]+-\d+").status == "fail"
    assert skill_evals.grade("regex", "x", "(").status == "error"


def test_validate_case_rejects_bad_definitions():
    assert skill_evals.validate_case("regex", "(") is not None
    assert skill_evals.validate_case("nope", "x") is not None
    assert skill_evals.validate_case("contains", "   ") is not None
    assert skill_evals.validate_case("regex", "a" * 501) is not None
    assert skill_evals.validate_case("llm_judge", "Must be polite") is None


def test_llm_judge_parses_verdict_and_keeps_one_line_reason():
    judge = lambda system, user, model: '```json\n{"pass": false, "reason": "Too rude.\\nMore detail"}\n```'
    result = skill_evals.grade("llm_judge", "go away", "Be polite", input_prompt="hi", call_model=judge, model="m")
    assert result.status == "fail"
    assert result.reason == "Too rude."


def test_llm_judge_reply_and_rubric_reach_the_judge():
    captured = {}

    def judge(system, user, model):
        captured["user"] = user
        return '{"pass": true, "reason": "ok"}'

    result = skill_evals.grade("llm_judge", "the reply", "the rubric", input_prompt="q", call_model=judge, model="m")
    assert result.status == "pass"
    assert "the reply" in captured["user"] and "the rubric" in captured["user"]


def test_llm_judge_gateway_error_is_error_not_crash():
    def broken(system, user, model):
        raise ConnectionError("gateway down")

    result = skill_evals.grade("llm_judge", "x", "rubric", call_model=broken, model="m")
    assert result.status == "error"
    assert "ConnectionError" in result.reason


def test_llm_judge_unparseable_reply_is_error():
    result = skill_evals.grade("llm_judge", "x", "rubric", call_model=lambda s, u, m: "I think it passes", model="m")
    assert result.status == "error"


# ── System prompt ─────────────────────────────────────────────────────────────


def test_system_prompt_strips_front_matter_and_appends_references(tmp_path):
    refs = tmp_path / "references"
    refs.mkdir()
    (refs / "schema.md").write_text("table t", encoding="utf-8")
    prompt = skill_evals.skill_system_prompt(SKILL_MD, refs)
    assert prompt.startswith("Always greet")
    assert "name: Greeter" not in prompt
    assert "## schema" in prompt and "table t" in prompt


# ── Runner ────────────────────────────────────────────────────────────────────


def test_run_cases_grades_each_case_and_counts():
    cases = [
        CaseSpec("1", "greets", "hi", "contains", "hello"),
        CaseSpec("2", "number", "num", "exact", "7"),
        CaseSpec("3", "boom", "boom", "contains", "x"),
    ]
    model = _fake_model({"hi": "HELLO friend", "num": "8", "boom": RuntimeError("503")})
    outcome = skill_evals.run_cases(cases, "SYS", model="m", call_model=model, max_cases=10)
    assert [r["status"] for r in outcome.results] == ["pass", "fail", "error"]
    assert (outcome.passed, outcome.failed, outcome.errored, outcome.total) == (1, 1, 1, 3)
    assert outcome.results[0]["output_excerpt"] == "HELLO friend"
    assert set(model.seen) == {"SYS"}


def test_run_cases_applies_the_per_run_cap():
    cases = [CaseSpec(str(i), f"c{i}", "hi", "contains", "default") for i in range(5)]
    outcome = skill_evals.run_cases(cases, "SYS", model="m", call_model=_fake_model({}), max_cases=2)
    assert outcome.total == 2
    assert outcome.skipped == 3


def test_run_cases_marks_slow_cases_as_timed_out():
    def slow(system, user, model):
        time.sleep(0.5)
        return "late"

    cases = [CaseSpec("1", "slow", "hi", "contains", "late")]
    outcome = skill_evals.run_cases(cases, "SYS", model="m", call_model=slow, deadline_seconds=0.05)
    assert outcome.results[0]["status"] == "error"
    assert outcome.results[0]["reason"] == "Timed out."


def test_output_excerpt_is_truncated():
    cases = [CaseSpec("1", "long", "hi", "contains", "a")]
    outcome = skill_evals.run_cases(cases, "SYS", model="m", call_model=lambda s, u, m: "a" * 5000)
    assert len(outcome.results[0]["output_excerpt"]) == 600


# ── Gate decision ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "passed,total,errored,baseline,enabled,expected",
    [
        (18, 20, 0, 0.9, True, "pass"),       # equal to baseline
        (19, 20, 0, 0.9, True, "pass"),       # better
        (17, 20, 0, 0.9, True, "block"),      # worse, gate on
        (17, 20, 0, 0.9, False, "warn"),      # worse, gate off
        (5, 20, 0, None, True, "no_baseline"),
        (0, 20, 20, 0.9, True, "inconclusive"),  # gateway down
        (0, 0, 0, 0.9, True, "inconclusive"),
        (10, 20, 10, 0.9, True, "block"),     # partial errors count as not passed
    ],
)
def test_decide_gate(passed, total, errored, baseline, enabled, expected):
    assert skill_evals.decide_gate(passed, total, errored, baseline, enabled=enabled) == expected


# ── Database-backed: runs, the publish gate and the API ───────────────────────


@pytest.fixture
def eval_db(sqlite_session_factory, monkeypatch):
    monkeypatch.setenv("SKILL_EVAL_MODEL", "test-model")
    from database import SkillEvalCase, SkillEvalRun
    from skill_submission_models import SkillCollaborator, SkillSubmission

    return sqlite_session_factory(SkillEvalCase, SkillEvalRun, SkillSubmission, SkillCollaborator)


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    import api_skill_publish

    root = tmp_path / "skills"
    (root / "greeter").mkdir(parents=True)
    (root / "greeter" / "skill.md").write_text(SKILL_MD, encoding="utf-8")
    monkeypatch.setattr(api_skill_publish, "_skills_dir", lambda: root)
    return root


def _add_case(db, slug="greeter", name="greets", prompt="hi", grader="contains", expected="hello"):
    from database import SkillEvalCase

    row = SkillEvalCase(slug=slug, name=name, input_prompt=prompt, grader=grader, expected=expected)
    db.add(row)
    db.commit()
    return row


def _greeting_model(word: str):
    """Replies with *word*, so 'hello' passes and anything else fails."""
    return lambda system, user, model: f"{word} there"


def test_publish_gate_blocks_regression_against_published_baseline(eval_db):
    _add_case(eval_db, name="a", prompt="p1")
    _add_case(eval_db, name="b", prompt="p2", expected="there")
    published = "---\nname: x\n---\nv1"
    candidate = "---\nname: x\n---\nv2"

    base = skill_evals.publish_gate_check(
        eval_db, "greeter", published, None, call_model=_greeting_model("hello"), enabled=True
    )
    assert base["decision"] == "no_baseline"
    assert base["run"]["passed"] == 2

    report = skill_evals.publish_gate_check(
        eval_db, "greeter", candidate, published, call_model=_greeting_model("bye"), enabled=True
    )
    assert report["decision"] == "block"
    assert report["blocked"] is True
    assert report["baseline_pass_rate"] == 1.0
    assert "1 of 2" in report["message"]

    off = skill_evals.publish_gate_check(
        eval_db, "greeter", candidate, published, call_model=_greeting_model("bye"), enabled=False
    )
    assert off["decision"] == "warn"
    assert off["blocked"] is False


def test_publish_gate_skips_skills_without_cases_and_unchanged_text(eval_db):
    from database import SkillEvalRun

    assert skill_evals.publish_gate_check(eval_db, "greeter", "a", None, call_model=_greeting_model("x")) is None
    _add_case(eval_db)
    assert skill_evals.publish_gate_check(eval_db, "greeter", "same", "same", call_model=_greeting_model("x")) is None
    assert eval_db.query(SkillEvalRun).count() == 0


def test_publish_hook_marks_submission_failed_when_blocked(eval_db, skills_dir, monkeypatch):
    import api_skill_publish
    from skill_submission_models import REVIEW_STATUS_AWAITING_CONFIRMATION, REVIEW_STATUS_FAIL, SkillSubmission

    _add_case(eval_db)
    # Baseline: the published text passes.
    skill_evals.publish_gate_check(eval_db, "greeter", SKILL_MD, None, call_model=_greeting_model("hello"))

    submission = SkillSubmission(
        user_id="owner", user_email="o@x", proposed_slug="greeter", description="d",
        skill_file_content=SKILL_MD + "\nchanged", review_status=REVIEW_STATUS_AWAITING_CONFIRMATION,
        pending_secrets=json.dumps([{"key": "K", "value": "v"}]), editor_sub="owner",
    )
    eval_db.add(submission)
    eval_db.commit()

    monkeypatch.setattr(skill_evals, "gateway_model_call", _greeting_model("bye"))
    monkeypatch.setenv("SKILL_EVAL_GATE_ENABLED", "false")
    assert api_skill_publish._eval_gate_before_publish(submission, eval_db) is None

    monkeypatch.setenv("SKILL_EVAL_GATE_ENABLED", "true")
    report = api_skill_publish._eval_gate_before_publish(submission, eval_db)
    assert report["blocked"] is True
    eval_db.refresh(submission)
    assert submission.review_status == REVIEW_STATUS_FAIL
    assert "blocked" in submission.review_reason
    assert submission.pending_secrets is None


def test_publish_hook_fails_open_when_the_harness_errors(eval_db, skills_dir, monkeypatch):
    import api_skill_publish
    from skill_submission_models import REVIEW_STATUS_AWAITING_CONFIRMATION, SkillSubmission

    _add_case(eval_db)
    submission = SkillSubmission(
        user_id="owner", user_email="o@x", proposed_slug="greeter", description="d",
        skill_file_content="new", review_status=REVIEW_STATUS_AWAITING_CONFIRMATION,
    )
    eval_db.add(submission)
    eval_db.commit()

    def explode(*args, **kwargs):
        raise RuntimeError("db gone")

    monkeypatch.setenv("SKILL_EVAL_GATE_ENABLED", "true")
    monkeypatch.setattr(skill_evals, "publish_gate_check", explode)
    assert api_skill_publish._eval_gate_before_publish(submission, eval_db) is None
    assert submission.review_status == REVIEW_STATUS_AWAITING_CONFIRMATION


@pytest.fixture
def client(eval_db, skills_dir, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    import api_skill_evals
    from app_factory import create_app
    from database import get_db

    factory = sessionmaker(bind=eval_db.get_bind())
    monkeypatch.setattr(api_skill_evals, "_open_session", factory)
    monkeypatch.setattr(skill_evals, "gateway_model_call", _greeting_model("hello"))

    app = create_app()
    app.dependency_overrides[get_db] = lambda: eval_db
    with TestClient(app) as c:
        yield c


def _as_member(monkeypatch, sub="member-sub", email="member@example.com"):
    import auth_middleware
    from auth_policy import AuthContext

    monkeypatch.setattr(
        auth_middleware,
        "build_auth_context",
        lambda headers: AuthContext(sub=sub, email=email, groups=("platform-users",)),
    )


CASE = {"name": "greets", "input_prompt": "hi", "grader": "contains", "expected": "hello"}


def test_case_crud_as_admin(client):
    res = client.post("/api/skills/greeter/evals/cases", json=CASE)
    assert res.status_code == 201
    case_id = res.json()["id"]

    overview = client.get("/api/skills/greeter/evals").json()
    assert [c["id"] for c in overview["cases"]] == [case_id]
    assert overview["last_run"] is None
    assert overview["gate_enabled"] is False

    updated = client.put(f"/api/skills/greeter/evals/cases/{case_id}", json={**CASE, "expected": "hey"})
    assert updated.status_code == 200
    assert updated.json()["expected"] == "hey"

    assert client.delete(f"/api/skills/greeter/evals/cases/{case_id}").status_code == 200
    assert client.get("/api/skills/greeter/evals").json()["cases"] == []
    assert client.delete(f"/api/skills/greeter/evals/cases/{case_id}").status_code == 404


def test_case_validation(client):
    assert client.post("/api/skills/greeter/evals/cases", json={**CASE, "grader": "python"}).status_code == 422
    assert client.post("/api/skills/greeter/evals/cases", json={**CASE, "grader": "regex", "expected": "("}).status_code == 422
    assert client.post("/api/skills/missing/evals/cases", json=CASE).status_code == 404


def test_start_run_records_results(client):
    assert client.post("/api/skills/greeter/evals/runs").status_code == 422  # no cases yet
    client.post("/api/skills/greeter/evals/cases", json=CASE)
    client.post("/api/skills/greeter/evals/cases", json={**CASE, "name": "bye", "expected": "goodbye"})

    started = client.post("/api/skills/greeter/evals/runs")
    assert started.status_code == 202
    run_id = started.json()["id"]

    # TestClient runs background tasks before returning, so the run is finished.
    run = client.get(f"/api/skills/greeter/evals/runs/{run_id}").json()
    assert run["status"] == "completed"
    assert (run["passed"], run["total"]) == (1, 2)
    assert [r["status"] for r in run["results"]] == ["pass", "fail"]
    assert run["model"] == "test-model"

    runs = client.get("/api/skills/greeter/evals/runs").json()["runs"]
    assert [r["id"] for r in runs] == [run_id]
    assert client.get("/api/skills/greeter/evals").json()["last_run"]["id"] == run_id


def test_run_is_refused_while_another_is_in_progress(client, eval_db):
    client.post("/api/skills/greeter/evals/cases", json=CASE)
    skill_evals.start_run(eval_db, "greeter", SKILL_MD, trigger="manual", model="m")
    assert client.post("/api/skills/greeter/evals/runs").status_code == 409


def test_non_owner_member_is_forbidden(client, monkeypatch):
    _as_member(monkeypatch)
    assert client.get("/api/skills/greeter/evals").status_code == 403
    assert client.post("/api/skills/greeter/evals/cases", json=CASE).status_code == 403
    assert client.post("/api/skills/greeter/evals/runs").status_code == 403
    assert client.get("/api/skills/greeter/evals/runs").status_code == 403


def test_owner_and_collaborator_can_manage_cases(client, eval_db, monkeypatch):
    from skill_submission_models import REVIEW_STATUS_PASS, SkillCollaborator, SkillSubmission

    eval_db.add(SkillSubmission(
        user_id="owner-sub", user_email="owner@example.com", proposed_slug="greeter",
        description="d", skill_file_content=SKILL_MD, review_status=REVIEW_STATUS_PASS,
    ))
    eval_db.add(SkillCollaborator(slug="greeter", user_email="collab@example.com", added_by_email="owner@example.com"))
    eval_db.commit()

    _as_member(monkeypatch, sub="owner-sub", email="owner@example.com")
    assert client.post("/api/skills/greeter/evals/cases", json=CASE).status_code == 201

    _as_member(monkeypatch, sub="collab-sub", email="collab@example.com")
    assert len(client.get("/api/skills/greeter/evals").json()["cases"]) == 1


def test_cases_of_one_skill_are_not_reachable_through_another(client, skills_dir):
    (skills_dir / "other").mkdir()
    (skills_dir / "other" / "skill.md").write_text(SKILL_MD, encoding="utf-8")
    case_id = client.post("/api/skills/greeter/evals/cases", json=CASE).json()["id"]
    assert client.delete(f"/api/skills/other/evals/cases/{case_id}").status_code == 404


def test_confirm_systems_route_refuses_a_regressing_edit(client, eval_db, skills_dir, monkeypatch):
    import api_skill_publish
    from cf_access import MOCK_USER_SUB
    from skill_submission_models import REVIEW_STATUS_AWAITING_CONFIRMATION, SkillSubmission

    _add_case(eval_db)
    skill_evals.publish_gate_check(eval_db, "greeter", SKILL_MD, None, call_model=_greeting_model("hello"))
    submission = SkillSubmission(
        user_id=MOCK_USER_SUB, user_email="o@x", proposed_slug="greeter", description="d",
        skill_file_content=SKILL_MD + "\nworse", review_status=REVIEW_STATUS_AWAITING_CONFIRMATION,
        editor_sub=MOCK_USER_SUB,
    )
    eval_db.add(submission)
    eval_db.commit()

    client.app.dependency_overrides[api_skill_publish._db] = lambda: eval_db
    monkeypatch.setenv("SKILL_EVAL_GATE_ENABLED", "true")
    monkeypatch.setattr(skill_evals, "gateway_model_call", _greeting_model("bye"))

    res = client.post(f"/api/skills/submissions/{submission.id}/confirm-systems", json={"system_links": []})
    assert res.status_code == 200
    body = res.json()
    assert body["review_status"] == "fail"
    assert body["eval_gate"]["blocked"] is True
    # Nothing was published: the live skill text is unchanged.
    assert (skills_dir / "greeter" / "skill.md").read_text(encoding="utf-8") == SKILL_MD
