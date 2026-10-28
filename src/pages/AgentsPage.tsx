import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  GitBranch,
  Hash,
  Loader2,
  Lock,
  Plus,
  RefreshCw,
  Search,
  Sparkles,
  UserCheck,
  Clock,
  LayoutGrid,
} from "lucide-react";
import { apiFetch } from "@/lib/api";
import { useBranding } from "@/lib/branding";
import {
  AgentRecord,
  statusChipClasses,
  statusDotColor,
  statusLabel,
} from "../data/agents";
import { parseUTC } from "@/lib/time";
import AgentAccessRequestModal from "@/components/AgentAccessRequestModal";

function fmtRelative(iso: string | null): string {
  if (!iso) return "—";
  const diff = (Date.now() - parseUTC(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

type AccessStatus = "owner" | "collaborator" | "approved" | "pending" | "none";
type Filter = "all" | "yours";
type PageView = "agents" | "registry";

interface RequestModal {
  agentId: string;
  agentName: string;
  agentDescription: string | null;
}

function AccessBadge({ status }: { status: AccessStatus | undefined }) {
  switch (status) {
    case "owner":
      return (
        <span className="inline-flex items-center gap-1 text-[11px] font-semibold bg-violet-50 border border-violet-200 text-violet-700 rounded-full px-2 py-0.5">
          <Sparkles className="w-3 h-3" /> Owner
        </span>
      );
    case "collaborator":
      return (
        <span className="inline-flex items-center gap-1 text-[11px] font-semibold bg-emerald-50 border border-emerald-200 text-emerald-700 rounded-full px-2 py-0.5">
          <UserCheck className="w-3 h-3" /> Collaborator
        </span>
      );
    case "approved":
      return (
        <span className="inline-flex items-center gap-1 text-[11px] font-semibold bg-emerald-50 border border-emerald-200 text-emerald-700 rounded-full px-2 py-0.5">
          <UserCheck className="w-3 h-3" /> Access granted
        </span>
      );
    case "pending":
      return (
        <span className="inline-flex items-center gap-1 text-[11px] font-semibold bg-amber-50 border border-amber-200 text-amber-700 rounded-full px-2 py-0.5">
          <Clock className="w-3 h-3" /> Pending
        </span>
      );
    default:
      return (
        <span className="inline-flex items-center gap-1 text-[11px] font-semibold bg-slate-50 border border-slate-200 text-slate-500 rounded-full px-2 py-0.5">
          <Lock className="w-3 h-3" /> No access
        </span>
      );
  }
}

/** True when manifest sync is enabled and the Slack collaborator is still missing. */
function needsServiceAccount(
  agent: AgentRecord,
  manifestSyncAvailable: boolean,
): boolean {
  if (!manifestSyncAvailable) return false;
  return (
    !!agent.slack_app_id &&
    agent.creation_method !== "auto_oauth" &&
    !agent.manifest_access_verified
  );
}

export function AgentCard({
  agent,
  manifestSyncAvailable,
  appName,
  slackCollaborator,
  onRequestAccess,
  onClick,
}: {
  agent: AgentRecord;
  manifestSyncAvailable: boolean;
  appName: string;
  slackCollaborator: string;
  onRequestAccess: (a: AgentRecord) => void;
  onClick: (a: AgentRecord) => void;
}) {
  const hasAccess =
    agent.my_access === "owner" ||
    agent.my_access === "collaborator" ||
    agent.my_access === "approved";
  const noAccess = !agent.my_access || agent.my_access === "none";
  const missingServiceAccount = needsServiceAccount(agent, manifestSyncAvailable);
  // /api/agents leaves these out for agents the viewer has no access to
  // (agent_to_public_dict), e.g. the built-in Platform agent for most users.
  const skillIds = agent.skill_ids ?? [];
  const channelIds = agent.channel_ids ?? [];

  return (
    <div
      onClick={() => onClick(agent)}
      className={`group relative bg-white border rounded-2xl p-5 flex flex-col gap-3 transition-all cursor-pointer
        ${hasAccess
          ? "border-slate-200 hover:border-slate-300 hover:shadow-md"
          : "border-slate-100 hover:border-slate-200 hover:shadow-sm opacity-80 hover:opacity-100"
        }`}
    >
      {/* Top row: avatar + access badge */}
      <div className="flex items-start justify-between gap-2">
        <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-slate-100 to-slate-200 border border-slate-200 flex items-center justify-center shrink-0 group-hover:from-slate-200 group-hover:to-slate-300 transition-all overflow-hidden">
          {agent.avatar_url
            ? <img src={agent.avatar_url} alt={agent.name} className="w-10 h-10 object-cover" />
            : <Bot className="w-5 h-5 text-slate-500" />
          }
        </div>
        <AccessBadge status={agent.my_access} />
      </div>

      {missingServiceAccount && (
        <span
          onClick={e => e.stopPropagation()}
          title={`This agent's Slack app doesn't have ${slackCollaborator || "the platform collaborator"} added yet — ${appName} can't sync its name, description, or photo to Slack until it is.`}
          className="inline-flex items-center gap-1 self-start text-[11px] font-semibold bg-amber-50 border border-amber-200 text-amber-700 rounded-full px-2 py-0.5"
        >
          <AlertTriangle className="w-3 h-3" /> Service account missing
        </span>
      )}

      {/* Name + description */}
      <div className="flex-1 min-w-0">
        <h3 className="font-bold text-slate-900 text-sm leading-tight truncate">{agent.name}</h3>
        {agent.description ? (
          <p className="text-xs text-slate-500 mt-1 line-clamp-2 leading-relaxed">{agent.description}</p>
        ) : (
          <p className="text-xs text-slate-400 mt-1 italic">No description</p>
        )}
      </div>

      {/* Meta row */}
      <div className="flex items-center gap-3 text-[11px] text-slate-400 flex-wrap">
        <span className={`inline-flex items-center gap-1 font-medium border rounded-full px-2 py-0.5 ${statusChipClasses(agent.status)}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${statusDotColor(agent.status)}`} />
          {statusLabel(agent.status)}
        </span>
        {skillIds.length > 0 && (
          <span>{skillIds.length} skill{skillIds.length !== 1 ? "s" : ""}</span>
        )}
        {channelIds.length > 0 && (
          <span className="flex items-center gap-0.5">
            <Hash className="w-3 h-3" />
            {channelIds[0].replace(/^#/, "")}
            {channelIds.length > 1 && ` +${channelIds.length - 1}`}
          </span>
        )}
      </div>

      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] text-slate-400">
          Active {fmtRelative(agent.last_active_at ?? agent.activated_at ?? agent.created_at)}
        </span>

        {noAccess ? (
          <button
            onClick={e => { e.stopPropagation(); onRequestAccess(agent); }}
            className="text-xs px-2.5 py-1 bg-slate-900 text-white rounded-lg hover:bg-slate-700 transition-colors font-semibold whitespace-nowrap"
          >
            Request Access
          </button>
        ) : agent.my_access === "pending" ? (
          <button
            onClick={e => { e.stopPropagation(); onRequestAccess(agent); }}
            className="text-xs px-2.5 py-1 border border-amber-300 text-amber-700 rounded-lg hover:bg-amber-50 transition-colors font-semibold whitespace-nowrap"
            disabled
          >
            Pending…
          </button>
        ) : null}
      </div>

      {/* Lock overlay for no-access cards */}
      {noAccess && (
        <div className="absolute inset-0 rounded-2xl pointer-events-none border border-dashed border-slate-200" />
      )}
    </div>
  );
}

// ── Slack App Registry ────────────────────────────────────────────────────────

interface SlackAppEntry {
  user_id: string;
  bot_id: string | null;
  app_id: string | null;
  name: string;
  display_name: string | null;
  real_name: string | null;
  is_app_user: boolean;
  updated_ts: number | null;
  image_192: string | null;
  synced_at: string | null;
  registered: boolean;
}

function fmtUnixDate(ts: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function SlackAppCard({ entry }: { entry: SlackAppEntry }) {
  const label = entry.real_name || entry.display_name || entry.name;
  const [imgError, setImgError] = useState(false);

  return (
    <div className="bg-white border border-slate-200 rounded-2xl p-5 flex flex-col gap-3 hover:border-slate-300 hover:shadow-md transition-all">
      {/* Avatar + badges */}
      <div className="flex items-start justify-between gap-2">
        <div className="w-10 h-10 rounded-xl border border-slate-200 overflow-hidden bg-slate-100 shrink-0 flex items-center justify-center">
          {entry.image_192 && !imgError ? (
            <img
              src={entry.image_192}
              alt={label}
              className="w-full h-full object-cover"
              onError={() => setImgError(true)}
            />
          ) : (
            <Bot className="w-5 h-5 text-slate-400" />
          )}
        </div>
        <div className="flex items-center gap-1 flex-wrap justify-end">
          {entry.registered && (
            <span className="inline-flex items-center gap-1 text-[11px] font-semibold bg-emerald-50 border border-emerald-200 text-emerald-700 rounded-full px-2 py-0.5">
              <CheckCircle2 className="w-3 h-3" /> Registered
            </span>
          )}
          {entry.is_app_user && (
            <span className="inline-flex items-center gap-1 text-[11px] font-semibold bg-blue-50 border border-blue-200 text-blue-600 rounded-full px-2 py-0.5">
              App User
            </span>
          )}
        </div>
      </div>

      {/* Name */}
      <div className="flex-1 min-w-0">
        <h3 className="font-bold text-slate-900 text-sm leading-tight truncate">{label}</h3>
        {label !== entry.name && (
          <p className="text-xs text-slate-400 mt-0.5 truncate">@{entry.name}</p>
        )}
      </div>

      {/* IDs */}
      <div className="flex flex-col gap-1 text-[11px] font-mono text-slate-400">
        {entry.app_id && (
          <span className="truncate" title={`App ID: ${entry.app_id}`}>
            app: {entry.app_id}
          </span>
        )}
        {entry.bot_id && (
          <span className="truncate" title={`Bot ID: ${entry.bot_id}`}>
            bot: {entry.bot_id}
          </span>
        )}
        <span className="truncate font-sans text-slate-300" title={`User ID: ${entry.user_id}`}>
          user: {entry.user_id}
        </span>
      </div>

      {/* Footer */}
      <div className="text-[11px] text-slate-400">
        Updated {fmtUnixDate(entry.updated_ts)}
      </div>
    </div>
  );
}

function SlackAppsTab() {
  const branding = useBranding();
  const [apps, setApps] = useState<SlackAppEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [registeredOnly, setRegisteredOnly] = useState(false);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/api/slack-apps");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setApps(await res.json());
    } catch {
      setError("Could not load Slack app registry.");
    } finally {
      setLoading(false);
    }
  };

  const sync = async () => {
    setSyncing(true);
    try {
      const res = await apiFetch("/api/slack-apps/sync", { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await load();
    } catch {
      setError("Sync failed — check that SLACK_WORKSPACE_TOKEN is configured.");
    } finally {
      setSyncing(false);
    }
  };

  useEffect(() => { load(); }, []);

  const filtered = apps.filter(a => {
    if (registeredOnly && !a.registered) return false;
    const q = search.toLowerCase();
    return (
      a.name.toLowerCase().includes(q) ||
      (a.real_name ?? "").toLowerCase().includes(q) ||
      (a.display_name ?? "").toLowerCase().includes(q) ||
      (a.app_id ?? "").toLowerCase().includes(q)
    );
  });

  const registeredCount = apps.filter(a => a.registered).length;
  const lastSynced = apps[0]?.synced_at
    ? new Date(apps[0].synced_at).toLocaleString()
    : null;

  return (
    <div className="flex-1 overflow-y-auto bg-[#F9FAFB]">
      {/* Registry sub-toolbar */}
      <div className="sticky top-0 z-10 bg-[#F9FAFB] border-b border-slate-100 px-6 py-3 flex items-center gap-3 flex-wrap">
        <button
          onClick={() => setRegisteredOnly(v => !v)}
          className={`flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg font-medium border transition-colors ${
            registeredOnly
              ? "bg-emerald-900 text-white border-emerald-900"
              : "bg-white border-slate-200 text-slate-500 hover:text-slate-700"
          }`}
        >
          <CheckCircle2 className="w-3.5 h-3.5" />
          Registered ({registeredCount})
        </button>

        <div className="flex items-center gap-2 bg-white border border-slate-200 rounded-lg px-3 py-1.5 flex-1 max-w-xs">
          <Search className="w-3.5 h-3.5 text-slate-400 shrink-0" />
          <input
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search by name or app ID…"
            className="flex-1 bg-transparent text-sm text-slate-700 placeholder:text-slate-400 focus:outline-none"
          />
        </div>

        <div className="ml-auto flex items-center gap-3">
          {lastSynced && (
            <span className="text-[11px] text-slate-400 hidden sm:block">
              Last synced: {lastSynced}
            </span>
          )}
          <button
            onClick={sync}
            disabled={syncing || loading}
            className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-900 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${syncing ? "animate-spin" : ""}`} />
            {syncing ? "Syncing…" : "Sync now"}
          </button>
        </div>
      </div>

      <div className="max-w-6xl mx-auto px-6 py-6">
        {loading ? (
          <div className="flex items-center justify-center py-20">
            <Loader2 className="w-5 h-5 animate-spin text-slate-400" />
          </div>
        ) : error ? (
          <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-4 py-3">
            {error}
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-center">
            <div className="w-14 h-14 rounded-2xl bg-slate-100 border border-slate-200 flex items-center justify-center mb-4">
              <Search className="w-6 h-6 text-slate-400" />
            </div>
            <h3 className="text-base font-bold text-slate-900 mb-2">
              {apps.length === 0 ? "Registry is empty" : "No matching apps"}
            </h3>
            <p className="text-sm text-slate-500">
              {apps.length === 0
                ? "Click \"Sync now\" to fetch all workspace bots from Slack."
                : "Try a different search or clear the Registered filter."}
            </p>
          </div>
        ) : (
          <>
            <p className="text-xs text-slate-400 mb-4">
              {filtered.length} app{filtered.length !== 1 ? "s" : ""} · {registeredCount} registered on {branding.app_name}
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
              {filtered.map(a => (
                <SlackAppCard key={a.user_id} entry={a} />
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default function AgentsPage() {
  const navigate = useNavigate();
  const branding = useBranding();
  const manifestSyncAvailable =
    branding.slack_auto_create_enabled && !!branding.slack_config_collaborator;
  const [agents, setAgents] = useState<AgentRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [requestModal, setRequestModal] = useState<RequestModal | null>(null);
  const [view, setView] = useState<PageView>("agents");
  const [hasDraft, setHasDraft] = useState(false);

  // Check whether the user has a saved registration draft to resume.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await apiFetch("/api/agents/draft");
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setHasDraft(!!data?.draft);
      } catch {
        /* ignore — draft is a convenience */
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/api/agents");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: AgentRecord[] = await res.json();
      setAgents(data);
    } catch {
      setError("Could not load agents.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const handleCardClick = (agent: AgentRecord) => {
    // Navigate to the detail page for all agents; the detail page will handle access state
    navigate(`/agents/${agent.id}`);
  };

  const filtered = agents
    .filter(a => {
      const q = search.toLowerCase();
      return a.name.toLowerCase().includes(q) || (a.description ?? "").toLowerCase().includes(q);
    })
    .filter(a => {
      if (filter === "yours") {
        return a.my_access === "owner" || a.my_access === "collaborator" || a.my_access === "approved";
      }
      return true;
    });

  const myCount = agents.filter(
    a => a.my_access === "owner" || a.my_access === "collaborator" || a.my_access === "approved"
  ).length;

  return (
    <div className="flex flex-col h-full overflow-hidden w-full">
      {/* ── Top toolbar ── */}
      <div className="shrink-0 bg-[#F9FAFB] border-b border-slate-200 px-6 py-3 flex items-center gap-3 flex-wrap">
        {/* View toggle: Agents | Slack Apps */}
        <div className="flex items-center bg-white border border-slate-200 rounded-lg p-0.5 gap-0.5">
          <button
            onClick={() => setView("agents")}
            className={`flex items-center gap-1.5 text-xs px-3 py-1 rounded-md font-medium transition-colors ${
              view === "agents"
                ? "bg-slate-900 text-white shadow-sm"
                : "text-slate-500 hover:text-slate-700"
            }`}
          >
            <Bot className="w-3.5 h-3.5" /> Agents
          </button>
          <button
            onClick={() => setView("registry")}
            className={`flex items-center gap-1.5 text-xs px-3 py-1 rounded-md font-medium transition-colors ${
              view === "registry"
                ? "bg-slate-900 text-white shadow-sm"
                : "text-slate-500 hover:text-slate-700"
            }`}
          >
            <LayoutGrid className="w-3.5 h-3.5" /> Slack Apps
          </button>
        </div>

        {view === "agents" && (
          <>
            {/* All / Yours toggle */}
            <div className="flex items-center bg-white border border-slate-200 rounded-lg p-0.5 gap-0.5">
              {(["all", "yours"] as Filter[]).map(f => (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={`flex items-center gap-1.5 text-xs px-3 py-1 rounded-md font-medium transition-colors ${
                    filter === f
                      ? "bg-slate-900 text-white shadow-sm"
                      : "text-slate-500 hover:text-slate-700"
                  }`}
                >
                  {f === "all" ? "All" : `Yours (${myCount})`}
                </button>
              ))}
            </div>

            {/* Canvas toggle */}
            <button
              onClick={() => navigate("/agents?view=canvas")}
              className="flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg font-medium border border-slate-200 text-slate-500 hover:text-slate-700 bg-white transition-colors"
            >
              <GitBranch className="w-3.5 h-3.5" />
              Canvas
            </button>

            {/* Search */}
            <div className="flex items-center gap-2 bg-white border border-slate-200 rounded-lg px-3 py-1.5 flex-1 max-w-xs">
              <Search className="w-3.5 h-3.5 text-slate-400 shrink-0" />
              <input
                type="text"
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder="Search agents..."
                className="flex-1 bg-transparent text-sm text-slate-700 placeholder:text-slate-400 focus:outline-none"
              />
            </div>

            <div className="ml-auto flex items-center gap-2">
              <button
                onClick={load}
                className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-900 transition-colors"
              >
                <RefreshCw className="w-3.5 h-3.5" /> Refresh
              </button>
              {hasDraft && (
                <button
                  onClick={() => navigate("/agents/new")}
                  className="flex items-center gap-1.5 text-xs px-3 py-1.5 text-violet-700 bg-violet-50 hover:bg-violet-100 border border-violet-200 rounded-lg transition-colors font-semibold"
                >
                  <Clock className="w-3.5 h-3.5" /> Resume draft
                </button>
              )}
              <button
                onClick={() => navigate("/agents/new")}
                className="flex items-center gap-1.5 text-xs px-3 py-1.5 bg-[#FF444F] hover:bg-[#e63d48] text-white rounded-lg transition-colors font-semibold"
              >
                <Plus className="w-3.5 h-3.5" /> New Agent
              </button>
            </div>
          </>
        )}
      </div>

      {/* ── Body ── */}
      {view === "registry" ? (
        <SlackAppsTab />
      ) : (
        <div className="flex-1 overflow-y-auto bg-[#F9FAFB]">
          <div className="max-w-6xl mx-auto px-6 py-6">
            {loading ? (
              <div className="flex items-center justify-center py-20">
                <Loader2 className="w-5 h-5 animate-spin text-slate-400" />
              </div>
            ) : error ? (
              <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-4 py-3">
                {error}
              </div>
            ) : filtered.length === 0 ? (
              <EmptyState hasSearch={search.length > 0} filter={filter} onNew={() => navigate("/agents/new")} />
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
                {filtered.map(a => (
                  <AgentCard
                    key={a.id}
                    agent={a}
                    manifestSyncAvailable={manifestSyncAvailable}
                    appName={branding.app_name}
                    slackCollaborator={branding.slack_config_collaborator}
                    onRequestAccess={agent =>
                      setRequestModal({
                        agentId: agent.id,
                        agentName: agent.name,
                        agentDescription: agent.description,
                      })
                    }
                    onClick={handleCardClick}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {requestModal && (
        <AgentAccessRequestModal
          agentId={requestModal.agentId}
          agentName={requestModal.agentName}
          agentDescription={requestModal.agentDescription}
          onClose={() => setRequestModal(null)}
          onSuccess={() => { setRequestModal(null); load(); }}
        />
      )}
    </div>
  );
}

function EmptyState({
  hasSearch,
  filter,
  onNew,
}: {
  hasSearch: boolean;
  filter: Filter;
  onNew: () => void;
}) {
  if (hasSearch) {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center">
        <div className="w-14 h-14 rounded-2xl bg-slate-100 border border-slate-200 flex items-center justify-center mb-4">
          <Search className="w-6 h-6 text-slate-400" />
        </div>
        <h3 className="text-base font-bold text-slate-900 mb-2">No matching agents</h3>
        <p className="text-sm text-slate-500">Try a different search term.</p>
      </div>
    );
  }
  if (filter === "yours") {
    return (
      <div className="flex flex-col items-center justify-center py-20 text-center">
        <div className="w-14 h-14 rounded-2xl bg-slate-100 border border-slate-200 flex items-center justify-center mb-4">
          <Lock className="w-6 h-6 text-slate-400" />
        </div>
        <h3 className="text-base font-bold text-slate-900 mb-2">No agents yet</h3>
        <p className="text-sm text-slate-500 max-w-xs mb-6">
          You don't own or have access to any agents. Register one or browse all agents to request access.
        </p>
        <button
          onClick={onNew}
          className="flex items-center gap-2 px-4 py-2 bg-slate-900 text-white text-sm rounded-lg hover:bg-slate-700 transition-colors"
        >
          <Plus className="w-4 h-4" /> Register an agent
        </button>
      </div>
    );
  }
  return (
    <div className="flex flex-col items-center justify-center py-20 text-center">
      <div className="w-14 h-14 rounded-2xl bg-slate-100 border border-slate-200 flex items-center justify-center mb-4">
        <Sparkles className="w-6 h-6 text-slate-400" />
      </div>
      <h3 className="text-base font-bold text-slate-900 mb-2">No agents registered</h3>
      <p className="text-sm text-slate-500 max-w-md leading-relaxed mb-6">
        Register a Slack-bot agent: paste your bot/app tokens, choose its skills, channels, and personality.
      </p>
      <button
        onClick={onNew}
        className="flex items-center gap-2 px-4 py-2 bg-slate-900 text-white text-sm rounded-lg hover:bg-slate-700 transition-colors"
      >
        <Plus className="w-4 h-4" /> Register your first agent
      </button>
    </div>
  );
}
