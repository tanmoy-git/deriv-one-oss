import { useEffect, useState } from "react";
import { NavLink, useNavigate, useParams, useSearchParams } from "react-router-dom";
import AgentAccessRequestModal from "@/components/AgentAccessRequestModal";
import {
  AlertCircle,
  ArrowLeft,
  Bot,
  Brain,
  CalendarClock,
  CheckCircle2,
  Clock,
  GitMerge,
  Loader2,
  Lock,
  MessageSquare,
  Pause,
  Play,
  PowerOff,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Tag,
  Zap,
  Users,
} from "lucide-react";
import { validateAgentDescription, validateUsecaseDescription } from "@/lib/agentDescription";
import { apiFetch } from "@/lib/api";
import AgentFormFields, { AgentFormState } from "@/components/AgentFormFields";
import AgentIdentityEditor from "@/components/AgentIdentityEditor";
import { AgentRecord, statusChipClasses, statusDotColor, statusLabel } from "@/data/agents";
import { useSkillAccess } from "@/lib/SkillAccessContext";
import { ActionButton } from "./components/ActionButton";
import { PendingOAuthBanner } from "./components/PendingOAuthBanner";
import { ServiceAccountBanner } from "./components/ServiceAccountBanner";
import { fromAgent } from "./form/agentForm";
import { ConnectionsModal } from "./modals/ConnectionsModal";
import { RotateTokensModal } from "./modals/RotateTokensModal";
import { ScheduledTask } from "./shared";
import { AutomationsTab } from "./tabs/AutomationsTab";
import { CollaboratorsTab } from "./tabs/CollaboratorsTab";
import { GuardrailsTab } from "./tabs/GuardrailsTab";
import { InteractionLogTab } from "./tabs/InteractionLogTab";
import { MemoryTab } from "./tabs/MemoryTab";
import { ViewMode } from "./tabs/OverviewTab";
import { RolesTab } from "./tabs/RolesTab";
import { SkillAccessTab } from "./tabs/SkillAccessTab";
import { SlackAppsGrantTab } from "./tabs/SlackAppsGrantTab";
import { UserAccessTab } from "./tabs/UserAccessTab";

export type AgentDetailTab = "overview" | "interactions" | "memory" | "slack-apps" | "guardrails" | "collaborators" | "access" | "skill-access" | "roles" | "automations";

export default function AgentDetailPage() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const initialInteractionId = searchParams.get("interaction");

  const [agent, setAgent] = useState<AgentRecord | null>(null);
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [accessStatus, setAccessStatus] = useState<"owner" | "collaborator" | "approved" | "pending" | "none" | null>(null);
  const { isAdmin } = useSkillAccess();
  const [showRequestModal, setShowRequestModal] = useState(false);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<AgentFormState | null>(null);
  const [busy, setBusy] = useState(false);
  const [showRotate, setShowRotate] = useState(false);
  const [showConnections, setShowConnections] = useState(false);
  const [activeTab, setActiveTab] = useState<AgentDetailTab>("overview");
  const [interactionsTotal, setInteractionsTotal] = useState<number | null>(null);
  const [interactionFailuresTotal, setInteractionFailuresTotal] = useState<number | null>(null);
  const [memoriesTotal, setMemoriesTotal] = useState<number | null>(null);

  useEffect(() => {
    const tab = searchParams.get("tab");
    if (tab === "interactions") {
      setActiveTab("interactions");
    }
  }, [searchParams]);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const [aRes, tRes] = await Promise.all([
        apiFetch(`/api/agents/${id}`),
        apiFetch(`/api/agents/${id}/scheduled-tasks`),
      ]);
      if (!aRes.ok) {
        if (aRes.status === 404 || aRes.status === 403) {
          // Not the owner — try public endpoint to show access state
          const pubRes = await apiFetch(`/api/agents/${id}/public`);
          if (pubRes.ok) {
            const a: AgentRecord = await pubRes.json();
            setAgent(a);
            setAccessStatus(a.my_access ?? "none");
            setForm(null); // no editing
            return;
          }
          setError("Agent not found.");
          setAgent(null);
          return;
        }
        throw new Error(`HTTP ${aRes.status}`);
      }
      const a: AgentRecord = await aRes.json();
      setAgent(a);
      // my_access "approved" means the user has a grant but is not the owner —
      // show the full page read-only; "owner" gets full edit access.
      const myAccess = (a as AgentRecord & { my_access?: string }).my_access;
      if (myAccess === "approved") {
        setAccessStatus("approved");
        setForm(null);
      } else if (myAccess === "collaborator") {
        setAccessStatus("collaborator");
        setForm(fromAgent(a));
      } else {
        setAccessStatus("owner");
        setForm(fromAgent(a));
      }
      const t: ScheduledTask[] = tRes.ok ? await tRes.json() : [];
      setTasks(Array.isArray(t) ? t : []);
    } catch {
      setError("Could not load agent.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (id) load();
  }, [id]);

  const callLifecycle = async (action: "activate" | "pause" | "decommission") => {
    if (!agent) return;
    if (action === "decommission") {
      const confirmed = window.confirm(
        `Decommission "${agent.name}"? This deletes the bot tokens from the vault and cannot be undone.`
      );
      if (!confirmed) return;
    }
    setBusy(true);
    try {
      const res = await apiFetch(`/api/agents/${agent.id}/${action}`, { method: "POST" });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail?.detail || `Action failed (HTTP ${res.status}).`);
      }
      if (action === "decommission") {
        navigate("/agents");
      } else {
        await load();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed.");
    } finally {
      setBusy(false);
    }
  };

  const saveEdits = async () => {
    if (!agent || !form) return;
    const descErr = validateAgentDescription(form.description);
    if (descErr) {
      setError(descErr);
      return;
    }
    const usecaseErr = validateUsecaseDescription(form.usecase_description);
    if (usecaseErr) {
      setError(usecaseErr);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const body = {
        name: form.name.trim(),
        description: form.description.trim() || null,
        usecase_description: form.usecase_description.trim() || null,
        avatar_url: form.avatar_url,
        skill_ids: form.skill_ids,
        soul_text: form.soul_text || null,
        channel_ids: form.channel_ids,
        trigger_mode: form.trigger_mode,
        llm_model: form.llm_model,
      };
      const res = await apiFetch(`/api/agents/${agent.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail?.detail || `Update failed (HTTP ${res.status}).`);
      }
      const updated: AgentRecord = await res.json();
      setAgent(updated);
      setForm(fromAgent(updated));
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Update failed.");
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full bg-[#F9FAFB]">
        <Loader2 className="w-5 h-5 animate-spin text-slate-400" />
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="flex flex-col items-center justify-center h-full bg-[#F9FAFB] text-center px-6">
        <Sparkles className="w-10 h-10 text-slate-300 mb-3" />
        <h2 className="text-base font-bold text-slate-900 mb-1">{error ?? "Agent not found"}</h2>
        <NavLink
          to="/agents"
          className="mt-4 inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-900"
        >
          <ArrowLeft className="w-3 h-3" /> Back to agents
        </NavLink>
      </div>
    );
  }

  // Non-owner view: polished public profile with access request prompt
  if (accessStatus !== "owner" && accessStatus !== "collaborator" && accessStatus !== "approved") {
    return (
      <div className="h-full overflow-y-auto bg-[#F9FAFB]">
        <div className="max-w-2xl mx-auto px-6 py-10 flex flex-col gap-6">
          {/* Back link */}
          <NavLink
            to="/agents"
            className="inline-flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-700 transition-colors w-fit"
          >
            <ArrowLeft className="w-3 h-3" /> Back to agents
          </NavLink>

          {/* Hero card */}
          <div className="bg-white border border-slate-200 rounded-2xl overflow-hidden shadow-sm">
            {/* Coloured top band */}
            <div className="h-24 bg-gradient-to-r from-slate-800 to-slate-600 relative">
              {/* Avatar pinned to bottom-left */}
              <div className="absolute -bottom-6 left-6 w-14 h-14 rounded-2xl bg-white border-2 border-white shadow-md flex items-center justify-center overflow-hidden">
                {agent.avatar_url
                  ? <img src={agent.avatar_url} alt={agent.name} className="w-14 h-14 object-cover" />
                  : <Bot className="w-7 h-7 text-slate-500" />
                }
              </div>
            </div>

            <div className="pt-10 pb-6 px-6 flex flex-col gap-5">
              {/* Name + status */}
              <div className="flex items-start justify-between gap-4 flex-wrap">
                <div>
                  <h1 className="text-xl font-bold text-slate-900">{agent.name}</h1>
                  <div className="flex items-center gap-2 mt-1.5 flex-wrap">
                    <span className={`inline-flex items-center gap-1 text-xs font-medium border rounded-full px-2 py-0.5 ${statusChipClasses(agent.status)}`}>
                      <span className={`w-1.5 h-1.5 rounded-full ${statusDotColor(agent.status)}`} />
                      {statusLabel(agent.status)}
                    </span>
                    {(agent.skill_ids ?? []).length > 0 && (
                      <span className="text-xs text-slate-400">{(agent.skill_ids ?? []).length} skill{(agent.skill_ids ?? []).length !== 1 ? "s" : ""}</span>
                    )}
                  </div>
                </div>
                {accessStatus === "pending" ? (
                  <span className="inline-flex items-center gap-1.5 text-xs font-semibold bg-amber-50 border border-amber-200 text-amber-700 rounded-full px-3 py-1">
                    <Clock className="w-3 h-3" /> Pending approval
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1.5 text-xs font-semibold bg-slate-50 border border-slate-200 text-slate-500 rounded-full px-3 py-1">
                    <Lock className="w-3 h-3" /> No access
                  </span>
                )}
              </div>

              {/* Description */}
              {agent.description && (
                <p className="text-sm text-slate-600 leading-relaxed">{agent.description}</p>
              )}

              {/* Divider */}
              <div className="border-t border-slate-100" />

              {/* Access CTA */}
              {accessStatus === "pending" ? (
                <div className="flex items-start gap-3 bg-amber-50 border border-amber-200 rounded-xl px-4 py-4">
                  <div className="w-8 h-8 rounded-lg bg-amber-100 flex items-center justify-center shrink-0 mt-0.5">
                    <Clock className="w-4 h-4 text-amber-600" />
                  </div>
                  <div>
                    <p className="text-sm font-semibold text-amber-900">Access request submitted</p>
                    <p className="text-xs text-amber-700 mt-0.5">Your request is awaiting admin approval. You'll be able to interact with this agent once approved.</p>
                  </div>
                </div>
              ) : (
                <div className="flex items-center justify-between gap-4 flex-wrap">
                  <div>
                    <p className="text-sm font-semibold text-slate-900">Request access to use this agent</p>
                    <p className="text-xs text-slate-500 mt-0.5">Submit a request and an admin will review it shortly.</p>
                  </div>
                  <button
                    onClick={() => setShowRequestModal(true)}
                    className="shrink-0 px-5 py-2.5 bg-slate-900 text-white text-sm font-semibold rounded-xl hover:bg-slate-700 transition-colors"
                  >
                    Request Access
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>

        {showRequestModal && (
          <AgentAccessRequestModal
            agentId={agent.id}
            agentName={agent.name}
            agentDescription={agent.description}
            onClose={() => setShowRequestModal(false)}
            onSuccess={() => {
              setShowRequestModal(false);
              setAccessStatus("pending");
            }}
          />
        )}
      </div>
    );
  }

  const decommissioned = agent.status === "decommissioned";
  const pendingOAuth = agent.status === "pending_oauth";

  return (
    <div className="h-full overflow-y-auto bg-[#F9FAFB]">
      <div className="mx-auto w-full max-w-6xl px-6 py-6 space-y-5">
        <NavLink
          to="/agents"
          className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-900 transition-colors"
        >
          <ArrowLeft className="w-3 h-3" /> Back to agents
        </NavLink>

        {/* Pending OAuth banner */}
        {pendingOAuth && (
          <PendingOAuthBanner agentId={agent.id} />
        )}

        {/* Self-created agent missing the service-account collaborator */}
        {!decommissioned &&
          !pendingOAuth &&
          (accessStatus === "owner" || accessStatus === "collaborator") &&
          agent.creation_method !== "auto_oauth" &&
          !agent.manifest_access_verified &&
          agent.slack_app_id && (
            <ServiceAccountBanner
              agent={agent}
              onVerified={() => setAgent({ ...agent, manifest_access_verified: true })}
            />
          )}

        {/* Header */}
        <div className="bg-white border border-slate-200 rounded-xl p-5 space-y-3">
          <div className="flex items-start justify-between gap-3">
            {(accessStatus === "owner" || accessStatus === "collaborator") && !decommissioned ? (
              <AgentIdentityEditor
                agent={agent}
                disabled={busy || editing}
                onUpdated={updated => {
                  setAgent(updated);
                  setForm(fromAgent(updated));
                }}
                onError={msg => setError(msg || null)}
              >
                <div className="flex items-center gap-2 mt-2 flex-wrap">
                  <span className={`inline-flex items-center gap-1 text-xs font-medium border rounded-full px-2 py-0.5 ${statusChipClasses(agent.status)}`}>
                    <span className={`w-1.5 h-1.5 rounded-full ${statusDotColor(agent.status)}`} />
                    {statusLabel(agent.status)}
                  </span>
                  <span className="text-xs text-slate-500 font-mono">{agent.slack_app_id}</span>
                  <span className="text-xs text-slate-500">{agent.creator_email}</span>
                </div>
                {agent.description && (
                  <p className="text-sm text-slate-600 mt-3 leading-relaxed max-w-3xl">{agent.description}</p>
                )}
              </AgentIdentityEditor>
            ) : (
              <div className="flex items-start gap-3 min-w-0 flex-1">
                <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-slate-100 to-slate-200 border border-slate-200 flex items-center justify-center shrink-0 overflow-hidden">
                  {agent.avatar_url
                    ? <img src={agent.avatar_url} alt={agent.name} className="w-12 h-12 object-cover" />
                    : <Bot className="w-6 h-6 text-slate-400" />
                  }
                </div>
                <div className="min-w-0">
                  <h1 className="text-xl font-bold text-slate-900 truncate">{agent.name}</h1>
                  <div className="flex items-center gap-2 mt-2 flex-wrap">
                    <span className={`inline-flex items-center gap-1 text-xs font-medium border rounded-full px-2 py-0.5 ${statusChipClasses(agent.status)}`}>
                      <span className={`w-1.5 h-1.5 rounded-full ${statusDotColor(agent.status)}`} />
                      {statusLabel(agent.status)}
                    </span>
                    <span className="text-xs text-slate-500 font-mono">{agent.slack_app_id}</span>
                    <span className="text-xs text-slate-500">{agent.creator_email}</span>
                  </div>
                  {agent.description && (
                    <p className="text-sm text-slate-600 mt-3 leading-relaxed max-w-3xl">{agent.description}</p>
                  )}
                </div>
              </div>
            )}
            <button
              onClick={load}
              className="text-slate-400 hover:text-slate-700 p-1.5"
              title="Refresh"
            >
              <RefreshCw className="w-3.5 h-3.5" />
            </button>
          </div>

          <div className="flex flex-wrap gap-1.5 pt-2 border-t border-slate-100">
            {accessStatus === "approved" && (
              <span className="inline-flex items-center gap-1.5 text-xs font-semibold bg-emerald-50 border border-emerald-200 text-emerald-700 rounded-full px-3 py-1">
                <CheckCircle2 className="w-3 h-3" /> Access granted
              </span>
            )}
            {accessStatus === "collaborator" && (
              <span className="inline-flex items-center gap-1.5 text-xs font-semibold bg-indigo-50 border border-indigo-200 text-indigo-700 rounded-full px-3 py-1">
                <Users className="w-3 h-3" /> Collaborator
              </span>
            )}
            {(accessStatus === "owner" || accessStatus === "collaborator") && (<>
            {(accessStatus === "owner" || accessStatus === "collaborator") && (agent.status === "registered" || agent.status === "paused") ? (
              <ActionButton onClick={() => callLifecycle("activate")} disabled={busy} icon={<Play className="w-3 h-3" />} primary>
                Activate
              </ActionButton>
            ) : null}
            {(accessStatus === "owner" || accessStatus === "collaborator") && agent.status === "active" ? (
              <ActionButton onClick={() => callLifecycle("pause")} disabled={busy} icon={<Pause className="w-3 h-3" />}>
                Pause
              </ActionButton>
            ) : null}
            {!decommissioned && !editing ? (
              <ActionButton onClick={() => setEditing(true)} disabled={busy}>
                Edit
              </ActionButton>
            ) : null}
            {(accessStatus === "owner" || accessStatus === "collaborator") && !decommissioned ? (
              <ActionButton onClick={() => setShowRotate(true)} disabled={busy} icon={<ShieldCheck className="w-3 h-3" />}>
                Rotate tokens
              </ActionButton>
            ) : null}
            {!decommissioned ? (
              <ActionButton onClick={() => setShowConnections(true)} disabled={busy} icon={<GitMerge className="w-3 h-3" />}>
                Connections
              </ActionButton>
            ) : null}
            {(accessStatus === "owner" || accessStatus === "collaborator") && !decommissioned ? (
              <ActionButton
                onClick={() => callLifecycle("decommission")}
                disabled={busy}
                icon={<PowerOff className="w-3 h-3" />}
                destructive
              >
                Decommission
              </ActionButton>
            ) : null}
            </>)}
          </div>
        </div>


        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-4 py-3 flex items-start gap-2">
            <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {/* Edit / view */}
        {editing && form ? (
          <>
            <AgentFormFields
              form={form}
              onChange={setForm}
              disableSlackAppId
              showSchedule={false}
              agentId={agent.id}
            />
            <div className="flex gap-2 sticky bottom-0 bg-[#F9FAFB] py-3 -mx-6 px-6 border-t border-slate-200">
              <button
                onClick={() => {
                  setEditing(false);
                  setForm(fromAgent(agent));
                }}
                disabled={busy}
                className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={saveEdits}
                disabled={busy}
                className="flex-1 sm:flex-none px-6 py-2 text-sm font-bold bg-slate-900 text-white rounded-lg hover:bg-slate-700 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                Save changes
              </button>
            </div>
          </>
        ) : (
          <>
            {!editing && (
              <div className="flex gap-1 bg-white border border-slate-200 rounded-xl p-1 overflow-x-auto scrollbar-none">
                <button
                  type="button"
                  onClick={() => setActiveTab("overview")}
                  className={`shrink-0 flex items-center gap-1.5 px-3 h-9 text-xs font-semibold rounded-lg transition-colors whitespace-nowrap ${
                    activeTab === "overview"
                      ? "bg-slate-900 text-white"
                      : "text-slate-500 hover:text-slate-900 hover:bg-slate-50"
                  }`}
                >
                  <Sparkles className="w-3.5 h-3.5" />
                  Overview
                </button>
                <button
                  type="button"
                  onClick={() => setActiveTab("interactions")}
                  className={`shrink-0 flex items-center gap-1.5 px-3 h-9 text-xs font-semibold rounded-lg transition-colors whitespace-nowrap ${
                    activeTab === "interactions"
                      ? "bg-slate-900 text-white"
                      : "text-slate-500 hover:text-slate-900 hover:bg-slate-50"
                  }`}
                >
                  <MessageSquare className="w-3.5 h-3.5" />
                  Interaction log
                  {interactionsTotal != null && interactionsTotal > 0 && (
                    <span
                      className={`rounded-full px-1.5 text-[10px] font-bold ${
                        activeTab === "interactions"
                          ? "bg-white/20 text-white"
                          : "bg-slate-100 text-slate-600"
                      }`}
                    >
                      {interactionsTotal}
                    </span>
                  )}
                  {interactionFailuresTotal != null && interactionFailuresTotal > 0 && (
                    <span
                      className={`rounded-full px-1.5 text-[10px] font-bold ${
                        activeTab === "interactions"
                          ? "bg-red-500/30 text-white"
                          : "bg-red-50 text-red-600"
                      }`}
                    >
                      {interactionFailuresTotal} failed
                    </span>
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => setActiveTab("memory")}
                  className={`shrink-0 flex items-center gap-1.5 px-3 h-9 text-xs font-semibold rounded-lg transition-colors whitespace-nowrap ${
                    activeTab === "memory"
                      ? "bg-slate-900 text-white"
                      : "text-slate-500 hover:text-slate-900 hover:bg-slate-50"
                  }`}
                >
                  <Brain className="w-3.5 h-3.5" />
                  Memory
                  {memoriesTotal != null && memoriesTotal > 0 && (
                    <span
                      className={`rounded-full px-1.5 text-[10px] font-bold ${
                        activeTab === "memory"
                          ? "bg-white/20 text-white"
                          : "bg-slate-100 text-slate-600"
                      }`}
                    >
                      {memoriesTotal}
                    </span>
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => setActiveTab("slack-apps")}
                  className={`shrink-0 flex items-center gap-1.5 px-3 h-9 text-xs font-semibold rounded-lg transition-colors whitespace-nowrap ${
                    activeTab === "slack-apps"
                      ? "bg-slate-900 text-white"
                      : "text-slate-500 hover:text-slate-900 hover:bg-slate-50"
                  }`}
                >
                  <Bot className="w-3.5 h-3.5" />
                  Slack Apps
                </button>
                <button
                  type="button"
                  onClick={() => setActiveTab("guardrails")}
                  className={`shrink-0 flex items-center gap-1.5 px-3 h-9 text-xs font-semibold rounded-lg transition-colors whitespace-nowrap ${
                    activeTab === "guardrails"
                      ? "bg-slate-900 text-white"
                      : "text-slate-500 hover:text-slate-900 hover:bg-slate-50"
                  }`}
                >
                  <ShieldCheck className="w-3.5 h-3.5" />
                  Guardrails
                </button>
                <button
                  type="button"
                  onClick={() => setActiveTab("collaborators")}
                  className={`shrink-0 flex items-center gap-1.5 px-3 h-9 text-xs font-semibold rounded-lg transition-colors whitespace-nowrap ${
                    activeTab === "collaborators"
                      ? "bg-slate-900 text-white"
                      : "text-slate-500 hover:text-slate-900 hover:bg-slate-50"
                  }`}
                >
                  <Users className="w-3.5 h-3.5" />
                  Collaborators
                </button>
                {(accessStatus === "owner" || accessStatus === "collaborator") && (
                  <button
                    type="button"
                    onClick={() => setActiveTab("access")}
                    className={`shrink-0 flex items-center gap-1.5 px-3 h-9 text-xs font-semibold rounded-lg transition-colors whitespace-nowrap ${
                      activeTab === "access"
                        ? "bg-slate-900 text-white"
                        : "text-slate-500 hover:text-slate-900 hover:bg-slate-50"
                    }`}
                  >
                    <ShieldCheck className="w-3.5 h-3.5" />
                    User Access
                  </button>
                )}
                {(accessStatus === "owner" || accessStatus === "collaborator") && (
                  <button
                    type="button"
                    onClick={() => setActiveTab("skill-access")}
                    className={`shrink-0 flex items-center gap-1.5 px-3 h-9 text-xs font-semibold rounded-lg transition-colors whitespace-nowrap ${
                      activeTab === "skill-access"
                        ? "bg-slate-900 text-white"
                        : "text-slate-500 hover:text-slate-900 hover:bg-slate-50"
                    }`}
                  >
                    <Zap className="w-3.5 h-3.5" />
                    Skill Access
                  </button>
                )}
                {(accessStatus === "owner" || accessStatus === "collaborator") && (
                  <button
                    type="button"
                    onClick={() => setActiveTab("roles")}
                    className={`shrink-0 flex items-center gap-1.5 px-3 h-9 text-xs font-semibold rounded-lg transition-colors whitespace-nowrap ${
                      activeTab === "roles"
                        ? "bg-slate-900 text-white"
                        : "text-slate-500 hover:text-slate-900 hover:bg-slate-50"
                    }`}
                  >
                    <Tag className="w-3.5 h-3.5" />
                    Roles
                  </button>
                )}
                {(accessStatus === "owner" || accessStatus === "collaborator") && (
                  <button
                    type="button"
                    onClick={() => setActiveTab("automations")}
                    className={`shrink-0 flex items-center gap-1.5 px-3 h-9 text-xs font-semibold rounded-lg transition-colors whitespace-nowrap ${
                      activeTab === "automations"
                        ? "bg-slate-900 text-white"
                        : "text-slate-500 hover:text-slate-900 hover:bg-slate-50"
                    }`}
                  >
                    <CalendarClock className="w-3.5 h-3.5" />
                    Automations
                    {tasks.length > 0 && (
                      <span
                        className={`rounded-full px-1.5 text-[10px] font-bold ${
                          activeTab === "automations"
                            ? "bg-white/20 text-white"
                            : "bg-slate-100 text-slate-600"
                        }`}
                      >
                        {tasks.length}
                      </span>
                    )}
                  </button>
                )}
              </div>
            )}

            <div className="w-full">
              {activeTab === "overview" ? (
                <ViewMode
                  agent={agent}
                  tasks={tasks}
                  onRefresh={load}
                  isOwner={accessStatus === "owner" || accessStatus === "collaborator"}
                  canEdit={accessStatus === "owner" || accessStatus === "collaborator"}
                />
              ) : activeTab === "interactions" ? (
                <InteractionLogTab
                  agentId={agent.id}
                  initialInteractionId={initialInteractionId}
                  onTotalChange={setInteractionsTotal}
                  onFailureTotalChange={setInteractionFailuresTotal}
                />
              ) : activeTab === "slack-apps" ? (
                <SlackAppsGrantTab agentId={agent.id} />
              ) : activeTab === "guardrails" ? (
                <GuardrailsTab agentId={agent.id} readonly={accessStatus !== "owner" && accessStatus !== "collaborator"} />
              ) : activeTab === "collaborators" ? (
                <CollaboratorsTab agentId={agent.id} isOwner={accessStatus === "owner" || isAdmin} />
              ) : activeTab === "access" ? (
                <UserAccessTab agentId={agent.id} />
              ) : activeTab === "skill-access" ? (
                <SkillAccessTab agentId={agent.id} />
              ) : activeTab === "roles" ? (
                <RolesTab agentId={agent.id} />
              ) : activeTab === "automations" ? (
                <AutomationsTab
                  agentId={agent.id}
                  tasks={tasks}
                  onTasksChange={setTasks}
                  readonly={accessStatus !== "owner" && accessStatus !== "collaborator"}
                />
              ) : (
                <MemoryTab
                  agentId={agent.id}
                  onTotalChange={setMemoriesTotal}
                />
              )}
            </div>
          </>
        )}

        {showRotate && (
          <RotateTokensModal
            agentId={agent.id}
            onClose={() => setShowRotate(false)}
            onRotated={load}
          />
        )}

        {showConnections && (
          <ConnectionsModal
            agent={agent}
            onClose={() => setShowConnections(false)}
            onSaved={load}
          />
        )}
      </div>
    </div>
  );
}
