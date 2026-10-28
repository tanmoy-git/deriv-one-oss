import type { Budget, BudgetScope, BudgetState } from "./types";

export function formatUsd(value: number): string {
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export const SCOPE_LABEL: Record<BudgetScope, string> = {
  org: "Platform",
  agent: "Agent",
  user: "User",
};

export const STATE_LABEL: Record<BudgetState, string> = {
  ok: "On track",
  soft: "Near limit",
  exceeded: "Over limit",
};

/** One line on where the month is heading, for the budget row. */
export function forecastText(budget: Budget): string {
  const { status } = budget;
  if (status.state === "exceeded") {
    return budget.enforce
      ? "Limit reached. Model calls are blocked until next month or a higher limit."
      : "Limit reached. Alert only, so calls continue.";
  }
  if (status.forecast_exceed_at) {
    const day = new Date(status.forecast_exceed_at).toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    });
    return `At the current rate it reaches the limit on ${day}.`;
  }
  return `On pace for ${formatUsd(status.forecast_usd)} this month.`;
}

/** Width of the progress bar, capped so an overspent budget fills it exactly. */
export function barWidth(usedPct: number): string {
  return `${Math.max(0, Math.min(100, usedPct))}%`;
}
