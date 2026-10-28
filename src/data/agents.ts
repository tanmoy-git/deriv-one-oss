export type AgentStatus = "pending_oauth" | "registered" | "active" | "paused" | "decommissioned";
export type AgentType = "interactive" | "scheduled" | "orchestrator";
export type TriggerMode = "mention" | "dm" | "both";

export interface AgentRecord {
  id: string;
  name: string;
  description: string | null;
  usecase_description: string | null;
  avatar_url: string | null;
  creator_email: string;
  creator_department: string;
  slack_app_id: string;
  slack_bot_user_id: string | null;
  xoxb_secret_ref: string | null;
  xapp_secret_ref: string | null;
  signing_secret_ref: string | null;
  // Absent when the viewer has no access to the agent.
  skill_ids?: string[];
  soul_text: string | null;
  channel_ids?: string[];
  trigger_mode: TriggerMode;
  agent_type: AgentType;
  /** Canonical model slug, or null for platform default. */
  llm_model: string | null;
  /** When true, approved skill artifacts are available via call_artifact at runtime. */
  artifacts_enabled?: boolean;
  /** When true, per-turn skill routing selects a subset of attached skills. */
  skill_routing_enabled?: boolean;
  skill_index_text?: string | null;
  skill_index_generated_at?: string | null;
  skill_index_source_skill_ids?: string[];
  skill_index_stale?: boolean;
  /** vault reference for optional BYOK gateway key (never the raw key). */
  gateway_api_key_ref?: string | null;
  /** True when a BYOK gateway key is stored for this agent. */
  has_gateway_api_key?: boolean;
  /** True when a Slack signing secret is stored for this agent. */
  has_signing_secret?: boolean;
  can_invoke: string[];
  can_be_invoked_by: string[];
  status: AgentStatus;
  created_at: string;
  updated_at: string;
  activated_at: string | null;
  paused_at: string | null;
  decommissioned_at: string | null;
  last_active_at: string | null;
  session_count: number;
  total_messages: number;
  scheduled_task_count: number;
  runtime: "stubbed" | "connected";
  creation_method?: "manual" | "guided" | "auto_oauth";
  manifest_access_verified?: boolean;
  slack_client_id?: string | null;
  bot_scopes?: string[] | null;
  my_access?: "owner" | "collaborator" | "approved" | "pending" | "none";
}

export type AgentInteractionTrigger = "mention" | "dm" | "a2a";

export interface AgentInteractionToolSummary {
  total: number;
  failed: number;
  run_python: number;
  call_artifact: number;
  activate_skill: number;
}

export interface AgentInteractionRoutingMetadata {
  mode?: string;
  enabled?: boolean;
  candidate_slugs?: string[];
  selected_slugs?: string[];
  fallback?: boolean;
  reason?: string;
  shadow_only?: boolean;
  index_present?: boolean;
  confidence?: number;
  active_skill_hint?: string;
}

export interface AgentToolAuditRunPython {
  tool: "run_python";
  code_len?: number;
  output_len?: number;
  ok?: boolean;
  code_sha256?: string;
  shape_sha256?: string;
  code_redacted?: string;
  output_redacted?: string;
}

export interface AgentToolAuditCallArtifact {
  tool: "call_artifact";
  artifact?: string;
  version?: number;
  output_len?: number;
  ok?: boolean;
  output_redacted?: string;
}

export interface AgentToolAuditActivateSkill {
  tool: "activate_skill";
  slug?: string;
  ok?: boolean;
  error?: string;
  secret_keys?: string[];
  artifact_names?: string[];
}

export type AgentToolAuditEntry =
  | AgentToolAuditRunPython
  | AgentToolAuditCallArtifact
  | AgentToolAuditActivateSkill
  | Record<string, unknown>;

export interface AgentInteractionRecord {
  id: string;
  agent_id: string;
  session_id: string;
  invoker_email: string;
  invoker_slack_user_id: string | null;
  trigger_type: AgentInteractionTrigger;
  channel_id: string | null;
  thread_ts: string | null;
  source_agent_id: string | null;
  source_agent_name: string | null;
  user_message: string;
  assistant_message: string;
  duration_ms: number | null;
  created_at: string;
  routing_metadata?: AgentInteractionRoutingMetadata | null;
  failure_kind?: string | null;
  failure_detail?: string | null;
  resolved_model?: string | null;
  tool_summary?: AgentInteractionToolSummary;
}

export interface AgentInteractionDetail extends AgentInteractionRecord {
  tools_called: AgentToolAuditEntry[];
  secrets_loaded: string[];
}

export interface AgentInteractionListResponse {
  items: AgentInteractionRecord[];
  total: number;
  page: number;
  limit: number;
}

export type AgentMemoryScope = "agent" | "user";
export type AgentMemorySource = "auto" | "manual";

export interface AgentMemoryRecord {
  id: string;
  agent_id: string;
  scope: AgentMemoryScope;
  user_email: string | null;
  key: string;
  value: string;
  source: AgentMemorySource;
  created_at: string;
  updated_at: string;
  expires_at: string | null;
}

export interface AgentMemoryListResponse {
  items: AgentMemoryRecord[];
  total: number;
  page: number;
  limit: number;
}

export interface AgentMemoryArchiveObjectRef {
  path: string;
  size_bytes: number;
  updated_at: string | null;
  storage_url: string | null;
  run_id?: string;
  memory_id?: string;
  day?: string;
  episode_id?: string;
}

export interface AgentMemoryArchiveResponse {
  agent_id: string;
  bucket: string | null;
  gcs_prefix: string | null;
  configured: boolean;
  backend_mode: string;
  curated: (AgentMemoryArchiveObjectRef & { content: string | null }) | null;
  migrations: Array<
    AgentMemoryArchiveObjectRef & {
      run_id: string;
      manifest_path: string;
      manifest?: Record<string, unknown>;
      manifest_preview?: string;
    }
  >;
  hybrid_episodes: AgentMemoryArchiveObjectRef[];
  migration_episodes: AgentMemoryArchiveObjectRef[];
  object_count: number;
}

export interface AvailableSkill {
  slug: string;
  tier: "tier1" | "tier2";
  reason: string;
  expires_at: string | null;
  restrictions: Record<string, unknown>;
}

export interface InferredSkill {
  slug: string;
  confidence: number;
  reason: string;
}

export const SOUL_TEMPLATES: Array<{ id: string; label: string; text: string }> = [
  {
    id: "finance",
    label: "Finance Bot",
    text:
      "Professional and concise. Always includes data sources. Defaults to tabular output. Flags risks in bold. Signs off responses with the data timestamp.",
  },
  {
    id: "support",
    label: "Support Bot",
    text:
      "Warm, empathetic, and direct. Acknowledges the user's situation in one short sentence before answering. Uses bullet lists for steps. Never speculates about ETAs.",
  },
  {
    id: "ops",
    label: "Ops Bot",
    text:
      "Calm under pressure. Leads with status (green/amber/red), then root cause, then remediation. Tags incident commanders by name when escalating. No filler.",
  },
];

/** Letters, digits, spaces, hyphen, underscore, period, apostrophe. Must start/end alnum. */
const AGENT_NAME_RE = /^[A-Za-z0-9](?:[A-Za-z0-9 _.\-']*[A-Za-z0-9])?$/;

export const AGENT_NAME_INVALID_MSG =
  "Agent name cannot contain emojis or special characters (!, @, #, $, %, &, *, (, )). Use letters, numbers, spaces, hyphens, or underscores only.";

/** Return an error message if the agent name is invalid, else null. */
export function validateAgentName(name: string): string | null {
  const trimmed = name.trim();
  if (!trimmed) return "Agent name is required.";
  if (trimmed.length > 120) return "Agent name must be at most 120 characters.";
  if (!AGENT_NAME_RE.test(trimmed)) return AGENT_NAME_INVALID_MSG;
  return null;
}

export function statusLabel(s: AgentStatus): string {
  if (s === "pending_oauth") return "Pending Slack install";
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function statusDotColor(s: AgentStatus): string {
  switch (s) {
    case "active":
      return "bg-emerald-500";
    case "paused":
      return "bg-amber-500";
    case "decommissioned":
      return "bg-slate-400";
    case "pending_oauth":
      return "bg-violet-400 animate-pulse";
    case "registered":
    default:
      return "bg-slate-300";
  }
}

export function statusChipClasses(s: AgentStatus): string {
  switch (s) {
    case "active":
      return "bg-emerald-50 text-emerald-700 border-emerald-200";
    case "paused":
      return "bg-amber-50 text-amber-700 border-amber-200";
    case "decommissioned":
      return "bg-slate-100 text-slate-500 border-slate-200";
    case "pending_oauth":
      return "bg-violet-50 text-violet-700 border-violet-200";
    case "registered":
    default:
      return "bg-slate-50 text-slate-600 border-slate-200";
  }
}
