import { describe, expect, it } from "vitest";
import {
  emptyCaseDraft,
  graderLabel,
  isRunInProgress,
  passRatePercent,
  resultTone,
  runSummary,
  runTone,
  validateCaseDraft,
} from "./format";

const run = { status: "completed", passed: 18, total: 20, errored: 0, skipped: 0, error: null };

describe("skill eval helpers", () => {
  it("validates a case draft", () => {
    expect(validateCaseDraft(emptyCaseDraft())).toBe("Give the case a name.");
    const draft = { ...emptyCaseDraft(), name: "n", input_prompt: "hi" };
    expect(validateCaseDraft(draft)).toBe("Enter the expected value.");
    expect(validateCaseDraft({ ...draft, grader: "llm_judge" })).toBe("Enter the rubric.");
    expect(validateCaseDraft({ ...draft, grader: "regex", expected: "(" })).toBe("That regex does not compile.");
    expect(validateCaseDraft({ ...draft, grader: "regex", expected: "\\d+" })).toBeNull();
  });

  it("summarises runs", () => {
    expect(runSummary(run)).toBe("Passed 18 of 20");
    expect(runSummary({ ...run, errored: 2, skipped: 5 })).toBe(
      "Passed 18 of 20, 2 errored (5 skipped by the per-run cap)",
    );
    expect(runSummary({ ...run, status: "running" })).toBe("Running…");
    expect(runSummary({ ...run, status: "failed", error: "No eval model" })).toBe("Run failed: No eval model");
  });

  it("computes pass rate and tones", () => {
    expect(passRatePercent(run)).toBe(90);
    expect(passRatePercent({ passed: 0, total: 0 })).toBeNull();
    expect(runTone(run)).toBe("warn");
    expect(runTone({ ...run, passed: 20 })).toBe("good");
    expect(runTone({ ...run, passed: 0 })).toBe("bad");
    expect(resultTone("pass")).toBe("good");
    expect(resultTone("error")).toBe("warn");
  });

  it("labels graders and detects running runs", () => {
    expect(graderLabel("llm_judge")).toBe("LLM judge");
    expect(graderLabel("other")).toBe("other");
    expect(isRunInProgress({ status: "running" })).toBe(true);
    expect(isRunInProgress(null)).toBe(false);
  });
});
