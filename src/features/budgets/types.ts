export type BudgetScope = "org" | "agent" | "user";

export type BudgetState = "ok" | "soft" | "exceeded";

export interface BudgetStatus {
  spent_usd: number;
  limit_usd: number;
  soft_limit_usd: number;
  used_pct: number;
  state: BudgetState;
  forecast_usd: number;
  forecast_exceed_at: string | null;
}

export interface Budget {
  id: string;
  scope: BudgetScope;
  scope_id: string;
  label: string;
  monthly_limit_usd: number;
  soft_limit_pct: number;
  enforce: boolean;
  updated_at: string;
  status: BudgetStatus;
}

export interface BudgetList {
  period_start: string;
  period_end: string;
  budgets: Budget[];
}
