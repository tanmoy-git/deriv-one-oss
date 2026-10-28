import { useEffect, useState } from "react";
import EmployeePicker, { type EmployeeResult } from "@/components/EmployeePicker";
import { AlertCircle, Loader2, Plus, ShieldCheck, UserPlus } from "lucide-react";
import { apiFetch } from "@/lib/api";

// ── User Access tab (owner-managed consumer grants) ───────────────────────────

export interface UserAccessGrant {
  id: string;
  agent_id: string;
  user_email: string;
  user_name: string | null;
  status: string;
  justification: string | null;
  granted_by_email: string | null;
  decided_at: string | null;
  expires_at: string | null;
  created_at: string | null;
}

export function UserAccessTab({ agentId }: { agentId: string }) {
  const [grants, setGrants] = useState<UserAccessGrant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [selectedEmployees, setSelectedEmployees] = useState<EmployeeResult[]>([]);
  const [expiresDays, setExpiresDays] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [actionGrantId, setActionGrantId] = useState<string | null>(null);
  const [showPendingOnly, setShowPendingOnly] = useState(false);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/access/grants`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setGrants(data.grants ?? []);
    } catch {
      setError("Could not load user access grants.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, [agentId]);

  const grantAccess = async () => {
    const unique = Array.from(new Set(selectedEmployees.map(e => e.email)));
    if (unique.length === 0) {
      setAddError("Add at least one employee.");
      return;
    }
    setSubmitting(true);
    setAddError(null);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/access/grant`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_emails: unique,
          expires_in_days: expiresDays ? parseInt(expiresDays, 10) : null,
        }),
      });
      const body = await res.json().catch(() => ({})) as {
        detail?: string | { message?: string; errors?: Array<{ email: string; error: string }> };
        granted?: number;
        failed?: number;
        errors?: Array<{ email: string; error: string }>;
      };
      if (!res.ok) {
        const detail = body.detail;
        const msg = typeof detail === "string"
          ? detail
          : detail?.message ?? `HTTP ${res.status}`;
        throw new Error(msg);
      }
      if (body.failed && body.failed > 0 && body.errors?.length) {
        const summary = body.errors.map(e => `${e.email}: ${e.error}`).join("; ");
        setAddError(`Granted ${body.granted ?? 0}, failed ${body.failed}: ${summary}`);
        await load();
        return;
      }
      setSelectedEmployees([]);
      setExpiresDays("");
      setAdding(false);
      await load();
    } catch (e) {
      setAddError(e instanceof Error ? e.message : "Failed to grant access.");
    } finally {
      setSubmitting(false);
    }
  };

  const approveGrant = async (grant: UserAccessGrant) => {
    setActionGrantId(grant.id);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/access/${grant.id}/approve`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await load();
    } catch {
      setError("Failed to approve request.");
    } finally {
      setActionGrantId(null);
    }
  };

  const denyGrant = async (grant: UserAccessGrant) => {
    const reason = window.prompt("Deny reason (optional):");
    if (reason === null) return;
    setActionGrantId(grant.id);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/access/${grant.id}/deny`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ deny_reason: reason }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await load();
    } catch {
      setError("Failed to deny request.");
    } finally {
      setActionGrantId(null);
    }
  };

  const revokeGrant = async (grant: UserAccessGrant) => {
    if (!window.confirm(`Revoke access for ${grant.user_email}?`)) return;
    setActionGrantId(grant.id);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/access/${grant.id}/revoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await load();
    } catch {
      setError("Failed to revoke access.");
    } finally {
      setActionGrantId(null);
    }
  };

  const statusBadge = (status: string) => {
    const styles: Record<string, string> = {
      approved: "bg-emerald-50 text-emerald-700 border-emerald-200",
      pending: "bg-amber-50 text-amber-700 border-amber-200",
      denied: "bg-red-50 text-red-700 border-red-200",
      revoked: "bg-slate-50 text-slate-500 border-slate-200",
    };
    return (
      <span className={`inline-flex text-[11px] font-medium border rounded-full px-2 py-0.5 ${styles[status] ?? "bg-slate-50 text-slate-500 border-slate-200"}`}>
        {status}
      </span>
    );
  };

  const displayed = showPendingOnly ? grants.filter(g => g.status === "pending") : grants;
  const pendingCount = grants.filter(g => g.status === "pending").length;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">User Access</h3>
          <p className="text-xs text-slate-500 mt-0.5">
            Users who can interact with this agent in Slack. This is separate from collaborators (co-owners).
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {pendingCount > 0 && (
            <button
              type="button"
              onClick={() => setShowPendingOnly(v => !v)}
              className={`text-xs px-2.5 py-1.5 rounded-lg border transition-colors ${
                showPendingOnly
                  ? "bg-amber-50 border-amber-200 text-amber-700 font-semibold"
                  : "border-slate-200 text-slate-500 hover:bg-slate-50"
              }`}
            >
              Pending ({pendingCount})
            </button>
          )}
          {!adding && (
            <button
              type="button"
              onClick={() => { setAdding(true); setAddError(null); setSelectedEmployees([]); setExpiresDays(""); }}
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 bg-slate-900 text-white rounded-lg hover:bg-slate-700 transition-colors font-semibold"
            >
              <UserPlus className="w-3.5 h-3.5" /> Grant Access
            </button>
          )}
        </div>
      </div>

      {adding && (
        <div className="border border-slate-200 rounded-xl bg-white p-4 space-y-3">
          <p className="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
            <ShieldCheck className="w-3.5 h-3.5 text-emerald-500" />
            Grant user access
          </p>
          <EmployeePicker selected={selectedEmployees} onChange={setSelectedEmployees} disabled={submitting} />
          <div className="flex items-center gap-2">
            <label className="text-xs text-slate-600 shrink-0">Expires in (days)</label>
            <input
              type="number"
              min={1}
              max={3650}
              value={expiresDays}
              onChange={e => setExpiresDays(e.target.value)}
              placeholder="Permanent"
              className="w-28 text-sm px-3 py-1.5 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-200"
            />
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => void grantAccess()}
              disabled={submitting || selectedEmployees.length === 0}
              className="flex items-center gap-1.5 px-4 py-2 text-xs font-semibold bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50 transition-colors"
            >
              {submitting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
              Grant
            </button>
            <button
              type="button"
              onClick={() => { setAdding(false); setAddError(null); }}
              className="px-3 py-2 text-xs text-slate-500 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
            >
              Cancel
            </button>
          </div>
          {addError && (
            <p className="text-[11px] text-red-600 flex items-center gap-1">
              <AlertCircle className="w-3 h-3 shrink-0" /> {addError}
            </p>
          )}
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-xs rounded-xl px-4 py-2 flex items-center gap-2">
          <AlertCircle className="w-3.5 h-3.5 shrink-0" /> {error}
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-10">
          <Loader2 className="w-4 h-4 animate-spin text-slate-400" />
        </div>
      ) : displayed.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-center border border-dashed border-slate-200 rounded-xl">
          <ShieldCheck className="w-8 h-8 text-slate-300 mb-2" />
          <p className="text-sm font-semibold text-slate-700">
            {showPendingOnly ? "No pending requests" : "No user access grants yet"}
          </p>
          <p className="text-xs text-slate-400 mt-1 max-w-xs">
            {showPendingOnly
              ? "All access requests have been handled."
              : 'Click "Grant Access" to add users who can use this agent in Slack.'}
          </p>
        </div>
      ) : (
        <div className="divide-y divide-slate-100 border border-slate-200 rounded-xl overflow-hidden">
          {displayed.map(g => (
            <div key={g.id} className="flex items-center gap-3 px-4 py-3 bg-white hover:bg-slate-50 transition-colors group">
              <div className="w-8 h-8 rounded-full bg-emerald-100 border border-emerald-200 flex items-center justify-center shrink-0 text-emerald-700 text-xs font-bold">
                {(g.user_name ?? g.user_email).slice(0, 1).toUpperCase()}
              </div>
              <div className="flex-1 min-w-0">
                {g.user_name && (
                  <p className="text-xs font-semibold text-slate-900 truncate">{g.user_name}</p>
                )}
                <p className="text-xs text-slate-500 truncate">{g.user_email}</p>
                {g.justification && (
                  <p className="text-[11px] text-slate-400 mt-0.5 truncate" title={g.justification}>
                    {g.justification}
                  </p>
                )}
                {g.expires_at && g.status === "approved" && (
                  <p className="text-[11px] text-slate-400 mt-0.5">
                    Expires {new Date(g.expires_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}
                  </p>
                )}
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {statusBadge(g.status)}
                {g.status === "pending" && (
                  <>
                    <button
                      type="button"
                      onClick={() => void approveGrant(g)}
                      disabled={actionGrantId === g.id}
                      className="text-[11px] font-semibold px-2 py-1 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50"
                    >
                      {actionGrantId === g.id ? <Loader2 className="w-3 h-3 animate-spin" /> : "Approve"}
                    </button>
                    <button
                      type="button"
                      onClick={() => void denyGrant(g)}
                      disabled={actionGrantId === g.id}
                      className="text-[11px] font-semibold px-2 py-1 border border-slate-200 text-slate-600 rounded-lg hover:bg-slate-50 disabled:opacity-50"
                    >
                      Deny
                    </button>
                  </>
                )}
                {g.status === "approved" && (
                  <button
                    type="button"
                    onClick={() => void revokeGrant(g)}
                    disabled={actionGrantId === g.id}
                    className="opacity-0 group-hover:opacity-100 text-[11px] font-semibold px-2 py-1 text-red-600 border border-red-200 rounded-lg hover:bg-red-50 disabled:opacity-50 transition-all"
                  >
                    {actionGrantId === g.id ? <Loader2 className="w-3 h-3 animate-spin" /> : "Revoke"}
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
