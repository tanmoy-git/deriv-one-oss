import { useEffect, useState } from "react";
import { AlertCircle, Loader2, Plus, UserPlus, Users, X } from "lucide-react";
import { apiFetch } from "@/lib/api";

// ── Collaborators tab ─────────────────────────────────────────────────────────

export interface Collaborator {
  id: string;
  agent_id: string;
  user_email: string;
  user_name: string | null;
  added_by_email: string;
  created_at: string | null;
}

export const EMAIL_RE = /^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{1,63}$/;

export function CollaboratorsTab({
  agentId,
  isOwner,
}: {
  agentId: string;
  isOwner: boolean;
}) {
  const [collaborators, setCollaborators] = useState<Collaborator[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [removingEmail, setRemovingEmail] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/collaborators`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setCollaborators(data.collaborators ?? []);
    } catch {
      setError("Could not load collaborators.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [agentId]);

  const addCollaborator = async () => {
    const parsed = newEmail
      .split(",")
      .map(e => e.trim().toLowerCase())
      .filter(e => e.length > 0);
    const emails = Array.from(new Set<string>(parsed));
    if (emails.length === 0) {
      setAddError("Enter at least one email address.");
      return;
    }
    const invalid = emails.filter(e => !EMAIL_RE.test(e));
    if (invalid.length > 0) {
      setAddError(`Invalid email: ${invalid[0]}`);
      return;
    }
    setSubmitting(true);
    setAddError(null);
    try {
      const failures: string[] = [];
      for (const email of emails) {
        const res = await apiFetch(`/api/agents/${agentId}/collaborators`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email }),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          failures.push(`${email}: ${(body as { detail?: string }).detail || `HTTP ${res.status}`}`);
        }
      }
      if (failures.length === emails.length) {
        throw new Error(failures.join("; "));
      }
      setNewEmail("");
      if (failures.length === 0) setAdding(false);
      else setAddError(`Added ${emails.length - failures.length}; failed: ${failures.join("; ")}`);
      await load();
    } catch (e) {
      setAddError(e instanceof Error ? e.message : "Failed to add collaborator.");
    } finally {
      setSubmitting(false);
    }
  };

  const removeCollaborator = async (email: string) => {
    if (!window.confirm(`Remove ${email} as a collaborator?`)) return;
    setRemovingEmail(email);
    setError(null);
    try {
      const res = await apiFetch(
        `/api/agents/${agentId}/collaborators/${encodeURIComponent(email)}`,
        { method: "DELETE" }
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setCollaborators(prev => prev.filter(c => c.user_email !== email));
    } catch {
      setError("Failed to remove collaborator.");
    } finally {
      setRemovingEmail(null);
    }
  };

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">Collaborators</h3>
          <p className="text-xs text-slate-500 mt-0.5">
            Co-owners who have the same access as you — they can view and edit this agent, access logs and memory.
          </p>
        </div>
        {isOwner && !adding && (
          <button
            type="button"
            onClick={() => { setAdding(true); setAddError(null); setNewEmail(""); }}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 bg-slate-900 text-white rounded-lg hover:bg-slate-700 transition-colors font-semibold shrink-0"
          >
            <UserPlus className="w-3.5 h-3.5" /> Add
          </button>
        )}
      </div>

      {/* Add form */}
      {adding && (
        <div className="border border-slate-200 rounded-xl bg-white p-4 space-y-3">
          <p className="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
            <UserPlus className="w-3.5 h-3.5 text-indigo-500" />
            Add collaborator
          </p>
          <div className="flex flex-col gap-2">
            <textarea
              autoFocus
              value={newEmail}
              onChange={e => setNewEmail(e.target.value)}
              placeholder="colleague@example.com, other@example.com"
              rows={2}
              maxLength={4000}
              className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-400 placeholder:text-slate-400 resize-none"
            />
            <p className="text-[11px] text-slate-400">Comma-separate multiple emails to add several at once.</p>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => void addCollaborator()}
                disabled={submitting || !newEmail.trim()}
                className="flex items-center gap-1.5 px-4 py-2 text-xs font-semibold bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition-colors"
              >
                {submitting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
                Add
              </button>
              <button
                type="button"
                onClick={() => { setAdding(false); setAddError(null); }}
                className="px-3 py-2 text-xs text-slate-500 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
          {addError && (
            <p className="text-[11px] text-red-600 flex items-center gap-1">
              <AlertCircle className="w-3 h-3 shrink-0" /> {addError}
            </p>
          )}
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-xs rounded-xl px-4 py-2 flex items-center gap-2">
          <AlertCircle className="w-3.5 h-3.5 shrink-0" /> {error}
        </div>
      )}

      {/* List */}
      {loading ? (
        <div className="flex items-center justify-center py-10">
          <Loader2 className="w-4 h-4 animate-spin text-slate-400" />
        </div>
      ) : collaborators.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-center border border-dashed border-slate-200 rounded-xl">
          <Users className="w-8 h-8 text-slate-300 mb-2" />
          <p className="text-sm font-semibold text-slate-700">No collaborators yet</p>
          <p className="text-xs text-slate-400 mt-1 max-w-xs">
            {isOwner
              ? 'Click "Add" to invite a colleague as a co-owner of this agent.'
              : "The agent owner hasn't added any collaborators yet."}
          </p>
        </div>
      ) : (
        <div className="divide-y divide-slate-100 border border-slate-200 rounded-xl overflow-hidden">
          {collaborators.map(c => (
            <div key={c.user_email} className="flex items-center gap-3 px-4 py-3 bg-white hover:bg-slate-50 transition-colors group">
              {/* Avatar placeholder */}
              <div className="w-8 h-8 rounded-full bg-indigo-100 border border-indigo-200 flex items-center justify-center shrink-0 text-indigo-700 text-xs font-bold">
                {(c.user_name ?? c.user_email).slice(0, 1).toUpperCase()}
              </div>

              <div className="flex-1 min-w-0">
                {c.user_name && (
                  <p className="text-xs font-semibold text-slate-900 truncate">{c.user_name}</p>
                )}
                <p className="text-xs text-slate-500 truncate">{c.user_email}</p>
                <p className="text-[11px] text-slate-400 mt-0.5">
                  Added by {c.added_by_email}
                  {c.created_at && (
                    <span className="ml-1">
                      · {new Date(c.created_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}
                    </span>
                  )}
                </p>
              </div>

              <div className="flex items-center gap-2 shrink-0">
                <span className="inline-flex items-center gap-1 text-[11px] font-semibold bg-indigo-50 border border-indigo-200 text-indigo-700 rounded-full px-2 py-0.5">
                  <Users className="w-3 h-3" /> Co-owner
                </span>
                {isOwner && (
                  <button
                    type="button"
                    onClick={() => removeCollaborator(c.user_email)}
                    disabled={removingEmail === c.user_email}
                    className="opacity-0 group-hover:opacity-100 text-slate-400 hover:text-red-500 transition-all disabled:opacity-50"
                    title="Remove collaborator"
                  >
                    {removingEmail === c.user_email ? (
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    ) : (
                      <X className="w-3.5 h-3.5" />
                    )}
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
