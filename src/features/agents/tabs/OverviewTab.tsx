import { useEffect, useState } from "react";
import { Hash, Lock, Sparkles } from "lucide-react";
import { apiFetch } from "@/lib/api";
import { agentModelLabel } from "@/components/AgentFormFields";
import AgentKnowledgeBaseCard from "@/components/AgentKnowledgeBaseCard";
import { AgentRecord } from "@/data/agents";
import { useSkillList } from "@/lib/SkillAccessContext";
import { Card } from "../components/Card";
import { ReinstallBotButton } from "../components/ReinstallBotButton";
import { GatewayKeyModal } from "../modals/GatewayKeyModal";
import { ScheduledTask } from "../shared";

// ── View mode ──────────────────────────────────────────────────────────────

export function ViewMode({
  agent,
  tasks,
  onRefresh,
  isOwner,
  canEdit,
}: {
  agent: AgentRecord;
  tasks: ScheduledTask[];
  onRefresh: () => void;
  isOwner: boolean;
  canEdit: boolean;
}) {
  const skillList = useSkillList();
  const [showGatewayKey, setShowGatewayKey] = useState(false);
  const [clearingKey, setClearingKey] = useState(false);
  const [savingArtifacts, setSavingArtifacts] = useState(false);
  const [savingRouting, setSavingRouting] = useState(false);
  const [generatingIndex, setGeneratingIndex] = useState(false);
  const [savingIndex, setSavingIndex] = useState(false);
  const [indexDraft, setIndexDraft] = useState(agent.skill_index_text ?? "");

  useEffect(() => {
    setIndexDraft(agent.skill_index_text ?? "");
  }, [agent.skill_index_text, agent.id]);

  const toggleArtifacts = async () => {
    if (!canEdit || savingArtifacts) return;
    setSavingArtifacts(true);
    try {
      const res = await apiFetch(`/api/agents/${agent.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ artifacts_enabled: !agent.artifacts_enabled }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(typeof body?.detail === "string" ? body.detail : `Update failed (HTTP ${res.status}).`);
      }
      onRefresh();
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Could not update skill artifacts setting.");
    } finally {
      setSavingArtifacts(false);
    }
  };

  const toggleSkillRouting = async () => {
    if (!canEdit || savingRouting) return;
    setSavingRouting(true);
    try {
      const res = await apiFetch(`/api/agents/${agent.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill_routing_enabled: !agent.skill_routing_enabled }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(typeof body?.detail === "string" ? body.detail : `Update failed (HTTP ${res.status}).`);
      }
      onRefresh();
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Could not update skill routing setting.");
    } finally {
      setSavingRouting(false);
    }
  };

  const generateSkillIndex = async () => {
    if (!canEdit || generatingIndex) return;
    if (
      agent.skill_index_text &&
      !window.confirm("Regenerate the skill index? This will replace the current index (you can edit before saving).")
    ) {
      return;
    }
    setGeneratingIndex(true);
    try {
      const res = await apiFetch(`/api/agents/${agent.id}/generate-skill-index`, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(typeof body?.detail === "string" ? body.detail : `Generate failed (HTTP ${res.status}).`);
      }
      const body = await res.json();
      setIndexDraft(body.agent?.skill_index_text ?? "");
      onRefresh();
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Could not generate skill index.");
    } finally {
      setGeneratingIndex(false);
    }
  };

  const saveSkillIndex = async () => {
    if (!canEdit || savingIndex) return;
    setSavingIndex(true);
    try {
      const res = await apiFetch(`/api/agents/${agent.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill_index_text: indexDraft }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(typeof body?.detail === "string" ? body.detail : `Save failed (HTTP ${res.status}).`);
      }
      onRefresh();
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Could not save skill index.");
    } finally {
      setSavingIndex(false);
    }
  };

  const clearGatewayKey = async () => {
    if (clearingKey) return;
    if (!window.confirm("Remove this agent's gateway key? It will fall back to the platform key.")) {
      return;
    }
    setClearingKey(true);
    try {
      const res = await apiFetch(`/api/agents/${agent.id}/gateway-key`, { method: "DELETE" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(typeof body?.detail === "string" ? body.detail : `Clear failed (HTTP ${res.status}).`);
      }
      onRefresh();
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Could not clear gateway key.");
    } finally {
      setClearingKey(false);
    }
  };

  // Absent on the public record shown to viewers without access.
  const skillIds = agent.skill_ids ?? [];
  const channelIds = agent.channel_ids ?? [];

  return (
    <div className="space-y-5">
      <Card title="Skills">
        {skillIds.length === 0 ? (
          <p className="text-xs text-slate-400">No skills configured.</p>
        ) : (
          <ul className="space-y-1.5">
            {skillIds.map(slug => {
              const meta = skillList.find(m => m.slug === slug);
              return (
                <li key={slug} className="text-sm text-slate-700 flex items-center gap-2">
                  <Sparkles className="w-3 h-3 text-amber-500 shrink-0" />
                  <span className="font-medium">{meta?.name ?? slug}</span>
                  <span className="text-xs text-slate-400 font-mono">{slug}</span>
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      <Card title="Skill artifacts">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="text-sm text-slate-700">
              {agent.artifacts_enabled
                ? "This agent can run approved Python artifacts attached to its skills."
                : "Artifacts are off — the agent uses run_python and other tools only."}
            </p>
            <p className="text-xs text-slate-400 mt-1">
              Turn on when you want call_artifact instead of ad-hoc sandbox code for supported skills.
            </p>
          </div>
          {canEdit ? (
            <button
              type="button"
              role="switch"
              aria-checked={!!agent.artifacts_enabled}
              disabled={savingArtifacts}
              onClick={toggleArtifacts}
              className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 disabled:opacity-50 ${
                agent.artifacts_enabled ? "bg-amber-500" : "bg-slate-200"
              }`}
            >
              <span
                className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition ${
                  agent.artifacts_enabled ? "translate-x-5" : "translate-x-0"
                }`}
              />
            </button>
          ) : (
            <span className="text-xs font-medium text-slate-500 shrink-0">
              {agent.artifacts_enabled ? "Enabled" : "Disabled"}
            </span>
          )}
        </div>
      </Card>

      <Card title="Auto-select skills">
        <div className="flex items-start justify-between gap-4 mb-4">
          <div className="min-w-0">
            <p className="text-sm text-slate-700">
              {agent.skill_routing_enabled
                ? "The router picks 1–2 relevant skills per Slack message using the index below."
                : "All attached skills load on every Slack turn (current default)."}
            </p>
            <p className="text-xs text-slate-400 mt-1">
              Generate a compact Skill Index, then enable auto-select. Use shadow mode globally before enforce.
            </p>
            {agent.skill_index_stale ? (
              <p className="text-xs text-amber-700 mt-2 font-medium">
                Skills changed since the index was generated — regenerate the index.
              </p>
            ) : null}
          </div>
          {canEdit ? (
            <button
              type="button"
              role="switch"
              aria-checked={!!agent.skill_routing_enabled}
              disabled={savingRouting}
              onClick={toggleSkillRouting}
              className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 disabled:opacity-50 ${
                agent.skill_routing_enabled ? "bg-amber-500" : "bg-slate-200"
              }`}
            >
              <span
                className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition ${
                  agent.skill_routing_enabled ? "translate-x-5" : "translate-x-0"
                }`}
              />
            </button>
          ) : (
            <span className="text-xs font-medium text-slate-500 shrink-0">
              {agent.skill_routing_enabled ? "Enabled" : "Disabled"}
            </span>
          )}
        </div>

        {canEdit ? (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={generateSkillIndex}
                disabled={generatingIndex || skillIds.length === 0}
                className="text-xs font-medium px-3 py-1.5 rounded-md bg-slate-900 text-white hover:bg-slate-800 disabled:opacity-50"
              >
                {generatingIndex ? "Generating…" : agent.skill_index_text ? "Regenerate index" : "Generate index"}
              </button>
              {(indexDraft !== (agent.skill_index_text ?? "") || indexDraft) && (
                <button
                  type="button"
                  onClick={saveSkillIndex}
                  disabled={savingIndex}
                  className="text-xs font-medium px-3 py-1.5 rounded-md border border-slate-300 text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                >
                  {savingIndex ? "Saving…" : "Save index"}
                </button>
              )}
            </div>
            <textarea
              value={indexDraft}
              onChange={e => setIndexDraft(e.target.value)}
              readOnly={!canEdit}
              rows={10}
              placeholder="Generate a skill index to describe when each attached skill should be used…"
              className="w-full text-xs font-mono border border-slate-200 rounded-lg p-3 text-slate-700 focus:outline-none focus:ring-2 focus:ring-amber-500"
            />
            <p className="text-[11px] text-slate-400">
              {(indexDraft || "").length.toLocaleString()} / 8,000 characters
              {agent.skill_index_generated_at
                ? ` · Last generated ${new Date(agent.skill_index_generated_at).toLocaleString()}`
                : ""}
            </p>
          </div>
        ) : agent.skill_index_text ? (
          <pre className="text-xs text-slate-600 whitespace-pre-wrap font-mono bg-slate-50 border border-slate-100 rounded-lg p-3">
            {agent.skill_index_text}
          </pre>
        ) : (
          <p className="text-xs text-slate-400">No skill index configured.</p>
        )}
      </Card>

      {/* Per-agent document retrieval. Its own card rather than a row in the
          Skills card above: it needs a status indicator, an upload widget and a
          document list, not just a toggle. */}
      <AgentKnowledgeBaseCard agentId={agent.id} canEdit={canEdit} />

      <Card title="Channels & trigger">
        <div className="grid sm:grid-cols-2 gap-4">
          <div>
            <p className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider mb-1.5">Channels</p>
            {channelIds.length === 0 ? (
              <p className="text-sm text-slate-400">None configured.</p>
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {channelIds.map(ch => (
                  <span
                    key={ch}
                    className="inline-flex items-center gap-1 text-xs bg-slate-100 text-slate-700 border border-slate-200 rounded-full px-2 py-0.5"
                  >
                    <Hash className="w-3 h-3 text-slate-400" />
                    {ch.replace(/^#/, "")}
                  </span>
                ))}
              </div>
            )}
          </div>
          <div>
            <p className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider mb-1.5">Trigger</p>
            <p className="text-sm text-slate-700">
              {agent.trigger_mode === "mention"
                ? "@mention"
                : agent.trigger_mode === "dm"
                  ? "Direct message"
                  : "Both @mention and DM"}
            </p>
          </div>
          <div>
            <p className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider mb-1.5">Model</p>
            <p className="text-sm text-slate-700">{agentModelLabel(agent.llm_model)}</p>
            {agent.llm_model && (
              <p className="text-[11px] text-slate-400 font-mono mt-0.5">{agent.llm_model}</p>
            )}
          </div>
          <div>
            <p className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider mb-1.5">gateway key</p>
            <p className="text-sm text-slate-700">
              {agent.has_gateway_api_key ? "Bring your own key" : "Platform default"}
            </p>
          </div>
        </div>
      </Card>

      <Card title="Slack scopes" hint="Slack permission scopes granted to this agent's bot token.">
        {(() => {
          const BASE_SCOPES = [
            "app_mentions:read", "chat:write", "channels:read", "channels:history",
            "groups:read", "groups:history", "users:read", "users:read.email",
          ];
          const baseSet = new Set(BASE_SCOPES);
          const scopes = agent.bot_scopes ?? BASE_SCOPES;
          return (
            <>
              <div className="flex flex-wrap gap-2">
                {scopes.map(scope => {
                  const isBase = baseSet.has(scope);
                  return (
                    <span
                      key={scope}
                      className={`inline-flex items-center gap-1 text-xs font-mono px-2.5 py-1 rounded-lg border ${
                        isBase
                          ? "bg-slate-50 border-slate-200 text-slate-600"
                          : "bg-violet-50 border-violet-200 text-violet-700"
                      }`}
                    >
                      {isBase && <Lock className="w-3 h-3 shrink-0 opacity-50" />}
                      {scope}
                    </span>
                  );
                })}
              </div>
              {!agent.bot_scopes && (
                <p className="text-[11px] text-slate-400 mt-2">Showing default base scopes.</p>
              )}
            </>
          );
        })()}
      </Card>

      <Card title="Personality / Soul">
        {agent.soul_text ? (
          <pre className="text-sm text-slate-700 bg-slate-50 border border-slate-200 rounded-lg p-3 whitespace-pre-wrap leading-relaxed font-mono">
            {agent.soul_text}
          </pre>
        ) : (
          <p className="text-xs text-slate-400">No personality configured (default Hub system prompt is used).</p>
        )}
      </Card>

      <Card
        title="Token storage"
        hint="Bot/app tokens and optional gateway keys live in the vault. Only resource refs are stored here."
      >
        <div className="space-y-1.5 font-mono text-xs">
          <p className="text-slate-600">
            <span className="text-slate-400">xoxb:</span>{" "}
            {agent.xoxb_secret_ref ?? <span className="text-slate-400 italic">not stored</span>}
          </p>
          <p className="text-slate-600">
            <span className="text-slate-400">xapp:</span>{" "}
            {agent.xapp_secret_ref ?? <span className="text-slate-400 italic">not stored</span>}
          </p>
          <p className="text-slate-600">
            <span className="text-slate-400">gateway:</span>{" "}
            {agent.gateway_api_key_ref ?? <span className="text-slate-400 italic">platform key</span>}
          </p>
        </div>
        {isOwner && agent.status !== "decommissioned" && (
          <div className="mt-3 pt-3 border-t border-slate-100 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => setShowGatewayKey(true)}
              className="text-xs px-3 py-1.5 rounded-lg border border-slate-200 bg-white text-slate-700 hover:border-slate-400 transition-colors"
            >
              {agent.has_gateway_api_key ? "Rotate gateway key" : "Set gateway key"}
            </button>
            {agent.has_gateway_api_key && (
              <button
                type="button"
                onClick={() => void clearGatewayKey()}
                disabled={clearingKey}
                className="text-xs px-3 py-1.5 rounded-lg border border-slate-200 bg-white text-slate-500 hover:border-red-300 hover:text-red-600 transition-colors disabled:opacity-50"
              >
                {clearingKey ? "Clearing…" : "Use platform key"}
              </button>
            )}
          </div>
        )}
        {agent.creation_method === "auto_oauth" && agent.status === "registered" && (
          <div className="mt-3 pt-3 border-t border-slate-100">
            <ReinstallBotButton agentId={agent.id} />
          </div>
        )}
      </Card>

      {showGatewayKey && (
        <GatewayKeyModal
          agentId={agent.id}
          onClose={() => setShowGatewayKey(false)}
          onSaved={() => {
            setShowGatewayKey(false);
            onRefresh();
          }}
        />
      )}

      <Card
        title={`Scheduled tasks (${tasks.length})`}
        hint="Recurring tasks scheduled for this agent."
      >
        {tasks.length === 0 ? (
          <p className="text-xs text-slate-400">No scheduled tasks yet.</p>
        ) : (
          <ul className="divide-y divide-slate-100">
            {tasks.map(t => (
              <li key={t.id} className="py-2.5 first:pt-0 last:pb-0">
                <p className="text-sm text-slate-700 line-clamp-2 leading-relaxed">{t.prompt}</p>
                <p className="text-xs text-slate-500 mt-1 font-mono">
                  {t.schedule} · {t.timezone} → {t.output_channel}
                </p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {(agent.can_invoke.length > 0 || agent.can_be_invoked_by.length > 0) && (
        <Card
          title="Agent connections"
          hint="Agents this bot can call (and agents that can call it) via the A2A API."
        >
          <div className="space-y-3">
            {agent.can_invoke.length > 0 && (
              <div>
                <p className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider mb-1.5">
                  Can call
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {agent.can_invoke.map(id => (
                    <span key={id} className="inline-flex items-center gap-1 text-xs bg-indigo-50 text-indigo-700 border border-indigo-200 rounded-full px-2 py-0.5 font-mono">
                      {id}
                    </span>
                  ))}
                </div>
              </div>
            )}
            {agent.can_be_invoked_by.length > 0 && (
              <div>
                <p className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider mb-1.5">
                  Can be called by
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {agent.can_be_invoked_by.map(id => (
                    <span key={id} className="inline-flex items-center gap-1 text-xs bg-emerald-50 text-emerald-700 border border-emerald-200 rounded-full px-2 py-0.5 font-mono">
                      {id === "*" ? "any agent" : id}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        </Card>
      )}
    </div>
  );
}
