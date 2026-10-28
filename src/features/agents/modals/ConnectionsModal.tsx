import { useEffect, useState } from "react";
import { AlertCircle, GitMerge, Loader2, X } from "lucide-react";
import { apiFetch } from "@/lib/api";
import { AgentRecord } from "@/data/agents";

// ── Connections modal ──────────────────────────────────────────────────────

export function ConnectionsModal({
  agent,
  onClose,
  onSaved,
}: {
  agent: AgentRecord;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [allAgents, setAllAgents] = useState<AgentRecord[]>([]);
  const [canInvoke, setCanInvoke] = useState<string[]>(agent.can_invoke);
  const [canBeInvokedBy, setCanBeInvokedBy] = useState<string[]>(agent.can_be_invoked_by);
  const [wildcardAny, setWildcardAny] = useState(agent.can_be_invoked_by.includes("*"));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch("/api/agents")
      .then(r => r.ok ? r.json() : [])
      .then((agents: AgentRecord[]) => setAllAgents(agents.filter(a => a.id !== agent.id)))
      .catch(() => {});
  }, [agent.id]);

  const toggleInvoke = (id: string) =>
    setCanInvoke(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);

  const toggleInvokedBy = (id: string) =>
    setCanBeInvokedBy(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);

  const save = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const invokedByList = wildcardAny ? ["*"] : canBeInvokedBy;
      const res = await apiFetch(`/api/agents/${agent.id}/orchestration`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ can_invoke: canInvoke, can_be_invoked_by: invokedByList }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const msg = typeof body?.detail === "string"
          ? body.detail
          : Array.isArray(body?.detail)
            ? body.detail.map((e: { msg?: string }) => e.msg ?? String(e)).join("; ")
            : `Save failed (HTTP ${res.status}).`;
        throw new Error(msg);
      }
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 px-4">
      <div className="bg-white rounded-xl border border-slate-200 shadow-2xl w-full max-w-lg p-6 space-y-5 max-h-[90vh] overflow-y-auto">
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-base font-bold text-slate-900 flex items-center gap-2">
              <GitMerge className="w-4 h-4 text-indigo-600" />
              Agent connections
            </h2>
            <p className="text-xs text-slate-500 mt-1 leading-relaxed">
              Configure which agents <em>{agent.name}</em> can call, and which agents can call it.
              Skill code can then call peers via <code className="text-[11px] bg-slate-100 px-1 rounded">run_python</code> using the A2A API.
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600 p-1 -mt-1">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="space-y-4">
          <div>
            <p className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider mb-2">
              This agent can call
            </p>
            {allAgents.length === 0 ? (
              <p className="text-xs text-slate-400">No other agents registered.</p>
            ) : (
              <div className="flex flex-wrap gap-2">
                {allAgents.map(a => {
                  const selected = canInvoke.includes(a.id);
                  return (
                    <button
                      key={a.id}
                      type="button"
                      onClick={() => toggleInvoke(a.id)}
                      className={`px-3 py-1.5 rounded-lg border text-sm font-medium transition-colors ${
                        selected
                          ? "bg-indigo-600 border-indigo-600 text-white"
                          : "bg-white border-slate-200 text-slate-700 hover:border-indigo-300 hover:text-indigo-700"
                      }`}
                    >
                      {a.name}
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          <div className="border-t border-slate-100 pt-4">
            <p className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider mb-2">
              This agent can be called by
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => setWildcardAny(v => !v)}
                className={`px-3 py-1.5 rounded-lg border text-sm font-medium transition-colors ${
                  wildcardAny
                    ? "bg-emerald-600 border-emerald-600 text-white"
                    : "bg-white border-slate-200 text-slate-700 hover:border-emerald-300 hover:text-emerald-700"
                }`}
              >
                Any agent
              </button>
              {!wildcardAny && allAgents.map(a => {
                const selected = canBeInvokedBy.includes(a.id);
                return (
                  <button
                    key={a.id}
                    type="button"
                    onClick={() => toggleInvokedBy(a.id)}
                    className={`px-3 py-1.5 rounded-lg border text-sm font-medium transition-colors ${
                      selected
                        ? "bg-emerald-600 border-emerald-600 text-white"
                        : "bg-white border-slate-200 text-slate-700 hover:border-emerald-300 hover:text-emerald-700"
                    }`}
                  >
                    {a.name}
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 text-xs rounded-lg px-3 py-2 flex items-start gap-2">
            <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <div className="flex gap-2 pt-1">
          <button
            onClick={onClose}
            disabled={submitting}
            className="flex-1 px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={save}
            disabled={submitting}
            className="flex-1 px-4 py-2 text-sm font-bold bg-slate-900 text-white rounded-lg hover:bg-slate-700 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
          >
            {submitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            Save connections
          </button>
        </div>
      </div>
    </div>
  );
}
