import type { TraceStep } from "./types";

export function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/** Small model costs need more than two decimals to be readable. */
export function formatCost(usd: number): string {
  if (usd === 0) return "$0";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

export function stepTitle(step: TraceStep): string {
  if (step.kind === "routing") {
    return step.selected.length ? `Skill routing: ${step.selected.join(", ")}` : "Skill routing: no skill";
  }
  if (step.kind === "model") return `Model call ${step.turn}`;
  const tool = step.tool ?? "tool";
  if (tool === "run_python") return "Tool: run Python";
  if (tool === "call_artifact") return `Tool: call artifact${step.artifact ? ` ${step.artifact}` : ""}`;
  if (tool === "activate_skill") return `Tool: activate skill${step.slug ? ` ${step.slug}` : ""}`;
  if (tool === "search_knowledge_base") return "Tool: search knowledge base";
  return `Tool: ${tool.replace(/_/g, " ")}`;
}

export function stepDetail(step: TraceStep): string {
  if (step.kind === "routing") {
    const parts = [step.mode ? `mode ${step.mode}` : "", step.confidence != null ? `confidence ${step.confidence}` : ""];
    return parts.filter(Boolean).join(" · ");
  }
  if (step.kind === "model") {
    const cached = step.cache_read_tokens ? ` (${formatTokens(step.cache_read_tokens)} from cache)` : "";
    return `${formatTokens(step.input_tokens)} in${cached} · ${formatTokens(step.output_tokens)} out · ${formatCost(step.cost_usd)}`;
  }
  const parts: string[] = [];
  if (step.ok === false) parts.push("failed");
  if (step.code_len != null) parts.push(`${step.code_len} characters of code`);
  if (step.output_len != null) parts.push(`${step.output_len} characters of output`);
  if (step.query) parts.push(`query "${step.query}"`);
  return parts.join(" · ");
}
