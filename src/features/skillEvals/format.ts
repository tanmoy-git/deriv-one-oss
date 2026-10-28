import type { components } from "../../api/schema";

export type EvalCase = components["schemas"]["EvalCaseOut"];
export type EvalCaseInput = components["schemas"]["EvalCaseIn"];
export type EvalRun = components["schemas"]["EvalRunOut"];
export type EvalCaseResult = components["schemas"]["EvalCaseResultOut"];
export type EvalOverview = components["schemas"]["EvalOverviewOut"];
export type GraderName = EvalCaseInput["grader"];

export const GRADER_OPTIONS: { value: GraderName; label: string; expectedLabel: string; placeholder: string }[] = [
  { value: "contains", label: "Contains", expectedLabel: "Reply must contain", placeholder: "ticket created" },
  { value: "regex", label: "Regex", expectedLabel: "Reply must match pattern", placeholder: "[A-Z]+-\\d+" },
  { value: "exact", label: "Exact", expectedLabel: "Reply must equal", placeholder: "42" },
  { value: "llm_judge", label: "LLM judge", expectedLabel: "Rubric", placeholder: "Asks for the account ID before doing anything else." },
];

export function graderLabel(grader: string): string {
  return GRADER_OPTIONS.find(g => g.value === grader)?.label ?? grader;
}

export function emptyCaseDraft(): EvalCaseInput {
  return { name: "", input_prompt: "", grader: "contains", expected: "" };
}

/** Client-side checks that mirror the server's, so obvious mistakes fail fast. */
export function validateCaseDraft(draft: EvalCaseInput): string | null {
  if (!draft.name.trim()) return "Give the case a name.";
  if (!draft.input_prompt.trim()) return "Enter the input prompt.";
  if (!draft.expected.trim()) {
    return draft.grader === "llm_judge" ? "Enter the rubric." : "Enter the expected value.";
  }
  if (draft.grader === "regex") {
    if (draft.expected.length > 500) return "Regex must be 500 characters or fewer.";
    try {
      // JavaScript and Python regex syntax differ slightly; the server has the final say.
      new RegExp(draft.expected);
    } catch {
      return "That regex does not compile.";
    }
  }
  return null;
}

export function isRunInProgress(run: Pick<EvalRun, "status"> | null | undefined): boolean {
  return run?.status === "running";
}

export function runSummary(run: Pick<EvalRun, "status" | "passed" | "total" | "errored" | "skipped" | "error">): string {
  if (run.status === "running") return "Running…";
  if (run.status === "failed") return run.error ? `Run failed: ${run.error}` : "Run failed.";
  let text = `Passed ${run.passed} of ${run.total}`;
  if (run.errored > 0) text += `, ${run.errored} errored`;
  if (run.skipped > 0) text += ` (${run.skipped} skipped by the per-run cap)`;
  return text;
}

export function passRatePercent(run: Pick<EvalRun, "passed" | "total">): number | null {
  if (!run.total) return null;
  return Math.round((run.passed / run.total) * 100);
}

export type Tone = "good" | "bad" | "warn" | "neutral";

export function resultTone(status: string): Tone {
  if (status === "pass") return "good";
  if (status === "fail") return "bad";
  if (status === "error") return "warn";
  return "neutral";
}

export function runTone(run: Pick<EvalRun, "status" | "passed" | "total">): Tone {
  if (run.status !== "completed" || !run.total) return run.status === "failed" ? "bad" : "neutral";
  if (run.passed === run.total) return "good";
  return run.passed === 0 ? "bad" : "warn";
}

export function triggerLabel(trigger: string): string {
  if (trigger === "publish") return "On publish";
  if (trigger === "manual") return "Manual";
  return trigger;
}

export function shortHash(hash: string): string {
  return hash.slice(0, 8);
}
