export interface RoutingStep {
  kind: "routing";
  t_ms: number;
  selected: string[];
  mode?: string | null;
  confidence?: number | null;
}

export interface ModelStep {
  kind: "model";
  t_ms: number | null;
  turn: number;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cost_usd: number;
}

export interface ToolStep {
  kind: "tool";
  t_ms: number | null;
  tool: string | null;
  ok?: boolean;
  code_len?: number;
  output_len?: number;
  artifact?: string;
  slug?: string;
  query?: string;
  error?: string;
}

export type TraceStep = RoutingStep | ModelStep | ToolStep;

export interface TraceTotals {
  duration_ms: number | null;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  tool_calls: number;
  tool_errors: number;
}

export interface InteractionTrace {
  interaction_id: string;
  resolved_model: string | null;
  failure_kind: string | null;
  replayable: boolean;
  timed: boolean;
  steps: TraceStep[];
  totals: TraceTotals;
}

export interface ReplayResult {
  model: string;
  output: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  duration_ms: number;
  dry_run: boolean;
  original: { model: string | null; output: string };
}
