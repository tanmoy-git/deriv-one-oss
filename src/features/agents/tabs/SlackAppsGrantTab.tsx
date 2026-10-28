import { useEffect, useState } from "react";
import { Bot, CheckCircle2, GitMerge, Loader2, Plus, Search, X } from "lucide-react";
import { apiFetch } from "@/lib/api";

// ── Slack App Grants tab ──────────────────────────────────────────────────────

export interface SlackAppGrant {
  id: string;
  agent_id: string;
  slack_user_id: string;
  app_name: string | null;
  granted_by_email: string | null;
  created_at: string | null;
  image_192: string | null;
  is_app_user: boolean | null;
  app_id: string | null;
}

export interface SlackAppRegistryEntry {
  user_id: string;
  name: string;
  display_name: string | null;
  real_name: string | null;
  image_192: string | null;
  is_app_user: boolean;
  registered: boolean;
}

// ─────────────────────────────────────────────────────────────────────────────

export function SlackAppsGrantTab({ agentId }: { agentId: string }) {
  const [grants, setGrants] = useState<SlackAppGrant[]>([]);
  const [registry, setRegistry] = useState<SlackAppRegistryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [removingId, setRemovingId] = useState<string | null>(null);

  const loadGrants = async () => {
    setLoading(true);
    setError(null);
    try {
      const [gRes, rRes] = await Promise.all([
        apiFetch(`/api/agents/${agentId}/slack-app-grants`),
        apiFetch("/api/slack-apps"),
      ]);
      if (!gRes.ok) throw new Error(`HTTP ${gRes.status}`);
      setGrants(await gRes.json());
      if (rRes.ok) setRegistry(await rRes.json());
    } catch {
      setError("Could not load authorized Slack apps.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadGrants(); }, [agentId]);

  const grantedIds = new Set(grants.map(g => g.slack_user_id));
  const available = registry.filter(r => !grantedIds.has(r.user_id));

  const filteredAvailable = available.filter(r => {
    const q = search.toLowerCase();
    return (
      r.name.toLowerCase().includes(q) ||
      (r.real_name ?? "").toLowerCase().includes(q) ||
      (r.display_name ?? "").toLowerCase().includes(q)
    );
  });

  const addGrant = async (entry: SlackAppRegistryEntry) => {
    setAdding(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/slack-app-grants`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          slack_user_id: entry.user_id,
          app_name: entry.real_name || entry.display_name || entry.name,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { detail?: string }).detail || `HTTP ${res.status}`);
      }
      setPickerOpen(false);
      setSearch("");
      await loadGrants();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to add app.");
    } finally {
      setAdding(false);
    }
  };

  const removeGrant = async (slackUserId: string) => {
    setRemovingId(slackUserId);
    setError(null);
    try {
      const res = await apiFetch(
        `/api/agents/${agentId}/slack-app-grants/${slackUserId}`,
        { method: "DELETE" }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadGrants();
    } catch {
      setError("Failed to remove app.");
    } finally {
      setRemovingId(null);
    }
  };

  return (
    <div className="space-y-8">
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">Authorized Slack Apps</h3>
          <p className="text-xs text-slate-500 mt-0.5">
            Slack bots listed here can invoke this agent with full skill access.
          </p>
        </div>
        <button
          onClick={() => { setPickerOpen(v => !v); setSearch(""); }}
          className="flex items-center gap-1.5 text-xs px-3 py-1.5 bg-slate-900 text-white rounded-lg hover:bg-slate-700 transition-colors font-semibold"
        >
          <Plus className="w-3.5 h-3.5" /> Add App
        </button>
      </div>

      {/* App picker dropdown */}
      {pickerOpen && (
        <div className="border border-slate-200 rounded-xl bg-white shadow-sm overflow-hidden">
          <div className="flex items-center gap-2 px-3 py-2 border-b border-slate-100">
            <Search className="w-3.5 h-3.5 text-slate-400 shrink-0" />
            <input
              autoFocus
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="Search Slack apps…"
              className="flex-1 bg-transparent text-sm text-slate-700 placeholder:text-slate-400 focus:outline-none"
            />
          </div>
          <div className="max-h-56 overflow-y-auto divide-y divide-slate-50">
            {filteredAvailable.length === 0 ? (
              <p className="px-4 py-3 text-xs text-slate-400 text-center">
                {registry.length === 0
                  ? "Sync the Slack App Registry first to see available apps."
                  : "No unregistered apps found."}
              </p>
            ) : (
              filteredAvailable.map(entry => {
                const label = entry.real_name || entry.display_name || entry.name;
                return (
                  <button
                    key={entry.user_id}
                    onClick={() => addGrant(entry)}
                    disabled={adding}
                    className="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-slate-50 transition-colors text-left disabled:opacity-50"
                  >
                    <div className="w-7 h-7 rounded-lg border border-slate-200 bg-slate-100 overflow-hidden shrink-0 flex items-center justify-center">
                      {entry.image_192 ? (
                        <img src={entry.image_192} alt={label} className="w-full h-full object-cover" />
                      ) : (
                        <Bot className="w-3.5 h-3.5 text-slate-400" />
                      )}
                    </div>
                    <div className="min-w-0">
                      <p className="text-xs font-semibold text-slate-900 truncate">{label}</p>
                      {label !== entry.name && (
                        <p className="text-[11px] text-slate-400 truncate">@{entry.name}</p>
                      )}
                    </div>
                    {entry.registered && (
                      <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 ml-auto shrink-0" />
                    )}
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-xs rounded-xl px-4 py-2">
          {error}
        </div>
      )}

      {/* Grants list */}
      {loading ? (
        <div className="flex items-center justify-center py-10">
          <Loader2 className="w-4 h-4 animate-spin text-slate-400" />
        </div>
      ) : grants.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-10 text-center border border-dashed border-slate-200 rounded-xl">
          <Bot className="w-8 h-8 text-slate-300 mb-2" />
          <p className="text-sm font-semibold text-slate-700">No Slack apps authorized</p>
          <p className="text-xs text-slate-400 mt-1">
            Click "Add App" to allow a Slack bot to invoke this agent.
          </p>
        </div>
      ) : (
        <div className="divide-y divide-slate-100 border border-slate-200 rounded-xl overflow-hidden">
          {grants.map(g => {
            const label = g.app_name || g.slack_user_id;
            return (
              <div key={g.slack_user_id} className="flex items-center gap-3 px-4 py-3 bg-white hover:bg-slate-50 transition-colors">
                <div className="w-8 h-8 rounded-xl border border-slate-200 bg-slate-100 overflow-hidden shrink-0 flex items-center justify-center">
                  {g.image_192 ? (
                    <img src={g.image_192} alt={label} className="w-full h-full object-cover" />
                  ) : (
                    <Bot className="w-4 h-4 text-slate-400" />
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-semibold text-slate-900 truncate">{label}</p>
                  <p className="text-[11px] font-mono text-slate-400 truncate">{g.slack_user_id}</p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-[11px] text-slate-400 hidden sm:block">
                    {g.granted_by_email ? `by ${g.granted_by_email}` : ""}
                  </span>
                  <span className="inline-flex items-center gap-1 text-[11px] font-semibold bg-emerald-50 border border-emerald-200 text-emerald-700 rounded-full px-2 py-0.5">
                    <CheckCircle2 className="w-3 h-3" /> Full access
                  </span>
                  <button
                    onClick={() => removeGrant(g.slack_user_id)}
                    disabled={removingId === g.slack_user_id}
                    className="text-slate-400 hover:text-red-500 transition-colors disabled:opacity-50"
                    title="Remove"
                  >
                    {removingId === g.slack_user_id ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <X className="w-3.5 h-3.5" />
                    )}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
    <SlackWorkflowsGrantSection agentId={agentId} />
    </div>
  );
}

export interface SlackWorkflowGrant {
  id: string;
  agent_id: string;
  workflow_id: string;
  label: string | null;
  granted_by_email: string | null;
  created_at: string | null;
}

export function SlackWorkflowsGrantSection({ agentId }: { agentId: string }) {
  const [grants, setGrants] = useState<SlackWorkflowGrant[]>([]);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [workflowId, setWorkflowId] = useState("");
  const [label, setLabel] = useState("");
  const [removingId, setRemovingId] = useState<string | null>(null);

  const loadGrants = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/slack-workflow-grants`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setGrants(await res.json());
    } catch {
      setError("Could not load authorized workflows.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadGrants(); }, [agentId]);

  const addGrant = async () => {
    const id = workflowId.trim();
    if (!id) return;
    setAdding(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/slack-workflow-grants`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workflow_id: id,
          label: label.trim() || null,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { detail?: string }).detail || `HTTP ${res.status}`);
      }
      setWorkflowId("");
      setLabel("");
      await loadGrants();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to add workflow.");
    } finally {
      setAdding(false);
    }
  };

  const removeGrant = async (id: string) => {
    setRemovingId(id);
    setError(null);
    try {
      const res = await apiFetch(
        `/api/agents/${agentId}/slack-workflow-grants/${encodeURIComponent(id)}`,
        { method: "DELETE" }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadGrants();
    } catch {
      setError("Failed to remove workflow.");
    } finally {
      setRemovingId(null);
    }
  };

  return (
    <div className="space-y-4 pt-2 border-t border-slate-200">
      <div>
        <h3 className="text-sm font-semibold text-slate-900">Authorized Slack Workflows</h3>
        <p className="text-xs text-slate-500 mt-0.5">
          This allows Slack Workflow Builder workflows to interact with this bot.
          Add a workflow ID so those workflows can @mention the agent and receive a response.
          You can paste the ID from the workflow&apos;s Workflow Builder URL.
        </p>
      </div>

      <div className="border border-slate-200 rounded-xl bg-white p-4 space-y-3">
        <div className="grid gap-3 sm:grid-cols-[1fr_1fr_auto]">
          <div>
            <label className="block text-[11px] font-semibold text-slate-500 mb-1">
              Workflow ID
            </label>
            <input
              type="text"
              value={workflowId}
              onChange={e => setWorkflowId(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") addGrant(); }}
              placeholder="Wf0123ABCD"
              className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-900/10"
            />
          </div>
          <div>
            <label className="block text-[11px] font-semibold text-slate-500 mb-1">
              Label <span className="font-normal">(optional)</span>
            </label>
            <input
              type="text"
              value={label}
              onChange={e => setLabel(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") addGrant(); }}
              placeholder="e.g. Access provisioning"
              className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-slate-900/10"
            />
          </div>
          <div className="flex items-end">
            <button
              type="button"
              onClick={addGrant}
              disabled={adding || !workflowId.trim()}
              className="w-full sm:w-auto flex items-center justify-center gap-1.5 text-xs px-3 py-2 bg-slate-900 text-white rounded-lg hover:bg-slate-700 transition-colors font-semibold disabled:opacity-50"
            >
              {adding ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : (
                <Plus className="w-3.5 h-3.5" />
              )}
              Add
            </button>
          </div>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-xs rounded-xl px-4 py-2">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-10">
          <Loader2 className="w-4 h-4 animate-spin text-slate-400" />
        </div>
      ) : grants.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-10 text-center border border-dashed border-slate-200 rounded-xl">
          <GitMerge className="w-8 h-8 text-slate-300 mb-2" />
          <p className="text-sm font-semibold text-slate-700">No workflows authorized</p>
          <p className="text-xs text-slate-400 mt-1 max-w-sm">
            Add a workflow ID so Slack workflows can interact with this bot.
          </p>
        </div>
      ) : (
        <div className="divide-y divide-slate-100 border border-slate-200 rounded-xl overflow-hidden">
          {grants.map(g => {
            const title = g.label || g.workflow_id;
            return (
              <div
                key={g.workflow_id}
                className="flex items-center gap-3 px-4 py-3 bg-white hover:bg-slate-50 transition-colors"
              >
                <div className="w-8 h-8 rounded-xl border border-slate-200 bg-slate-100 shrink-0 flex items-center justify-center">
                  <GitMerge className="w-4 h-4 text-slate-400" />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-semibold text-slate-900 truncate">{title}</p>
                  <p className="text-[11px] font-mono text-slate-400 truncate">{g.workflow_id}</p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-[11px] text-slate-400 hidden sm:block">
                    {g.granted_by_email ? `by ${g.granted_by_email}` : ""}
                  </span>
                  <span className="inline-flex items-center gap-1 text-[11px] font-semibold bg-emerald-50 border border-emerald-200 text-emerald-700 rounded-full px-2 py-0.5">
                    <CheckCircle2 className="w-3 h-3" /> Full access
                  </span>
                  <button
                    type="button"
                    onClick={() => removeGrant(g.workflow_id)}
                    disabled={removingId === g.workflow_id}
                    className="text-slate-400 hover:text-red-500 transition-colors disabled:opacity-50"
                    title="Remove"
                  >
                    {removingId === g.workflow_id ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <X className="w-3.5 h-3.5" />
                    )}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
