import { useEffect, useState } from "react";
import EmployeePicker, { type EmployeeResult } from "@/components/EmployeePicker";
import { AlertCircle, Loader2, Plus, UserPlus, Zap } from "lucide-react";
import { apiFetch } from "@/lib/api";

// ── Skill Access tab (owner-managed skill grants for agent skills) ────────────

export interface GrantableSkill {
  slug: string;
  name: string;
  risk_level?: string;
  grantable: boolean;
  reason: string | null;
}

export interface SkillAccessGrant {
  id: string;
  user_email: string;
  user_name: string | null;
  skill_slug: string;
  skill_name: string;
  status: string;
  grant_type: string;
  expires_at: string | null;
  decided_by_email: string | null;
  decided_at: string | null;
}

export function SkillAccessTab({ agentId }: { agentId: string }) {
  const [grantableSkills, setGrantableSkills] = useState<GrantableSkill[]>([]);
  const [grants, setGrants] = useState<SkillAccessGrant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [selectedEmployees, setSelectedEmployees] = useState<EmployeeResult[]>([]);
  const [justification, setJustification] = useState("");
  const [grantType, setGrantType] = useState<"time_limited" | "permanent" | "restricted">("time_limited");
  const [expiresDays, setExpiresDays] = useState("90");
  const [selectedSlugs, setSelectedSlugs] = useState<Set<string>>(new Set());
  const [addError, setAddError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [actionGrantId, setActionGrantId] = useState<string | null>(null);

  const grantableOnly = grantableSkills.filter(s => s.grantable);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const [skillsRes, grantsRes] = await Promise.all([
        apiFetch(`/api/agents/${agentId}/skills/grantable`),
        apiFetch(`/api/agents/${agentId}/skills/grants`),
      ]);
      if (!skillsRes.ok) throw new Error(`Skills fetch failed (${skillsRes.status})`);
      if (!grantsRes.ok) throw new Error(`Grants fetch failed (${grantsRes.status})`);
      const skillsData = await skillsRes.json();
      const grantsData = await grantsRes.json();
      const skills: GrantableSkill[] = skillsData.skills ?? [];
      setGrantableSkills(skills);
      setGrants(grantsData.grants ?? []);
      setSelectedSlugs(new Set(skills.filter(s => s.grantable).map(s => s.slug)));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load skill access data.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, [agentId]);

  const toggleSkill = (slug: string) => {
    setSelectedSlugs(prev => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });
  };

  const selectAllSkills = () => {
    setSelectedSlugs(new Set(grantableOnly.map(s => s.slug)));
  };

  const grantSkills = async () => {
    const unique = Array.from(new Set(selectedEmployees.map(e => e.email)));
    if (unique.length === 0) {
      setAddError("Add at least one employee.");
      return;
    }
    if (justification.trim().length < 10) {
      setAddError("Justification must be at least 10 characters.");
      return;
    }
    const slugs = Array.from(selectedSlugs);
    if (slugs.length === 0) {
      setAddError("Select at least one skill to grant.");
      return;
    }

    setSubmitting(true);
    setAddError(null);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/skills/grant`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_emails: unique,
          skill_slugs: slugs.length === grantableOnly.length ? undefined : slugs,
          grant_type: grantType,
          expires_in_days: grantType === "permanent" ? null : parseInt(expiresDays, 10) || 90,
          justification: justification.trim(),
        }),
      });
      const body = await res.json().catch(() => ({})) as {
        detail?: string | { message?: string; errors?: Array<{ email: string; skill_slug?: string; error: string }> };
        granted?: number;
        failed?: number;
        errors?: Array<{ email: string; skill_slug?: string; error: string }>;
      };
      if (!res.ok) {
        const detail = body.detail;
        const msg = typeof detail === "string"
          ? detail
          : detail?.message ?? `HTTP ${res.status}`;
        throw new Error(msg);
      }
      if (body.failed && body.failed > 0 && body.errors?.length) {
        const summary = body.errors
          .map(e => `${e.email}${e.skill_slug ? ` (${e.skill_slug})` : ""}: ${e.error}`)
          .join("; ");
        setAddError(`Granted ${body.granted ?? 0}, failed ${body.failed}: ${summary}`);
        await load();
        return;
      }
      setSelectedEmployees([]);
      setJustification("");
      setAdding(false);
      await load();
    } catch (e) {
      setAddError(e instanceof Error ? e.message : "Failed to grant skill access.");
    } finally {
      setSubmitting(false);
    }
  };

  const revokeGrant = async (grant: SkillAccessGrant) => {
    if (!window.confirm(`Revoke ${grant.skill_slug} access for ${grant.user_email}?`)) return;
    setActionGrantId(grant.id);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/skills/grants/${grant.id}/revoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "Revoked by agent owner" }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await load();
    } catch {
      setError("Failed to revoke skill grant.");
    } finally {
      setActionGrantId(null);
    }
  };

  const statusBadge = (status: string) => {
    const styles: Record<string, string> = {
      approved: "bg-emerald-50 text-emerald-700 border-emerald-200",
      pending: "bg-amber-50 text-amber-700 border-amber-200",
    };
    return (
      <span className={`inline-flex text-[11px] font-medium border rounded-full px-2 py-0.5 ${styles[status] ?? "bg-slate-50 text-slate-500 border-slate-200"}`}>
        {status}
      </span>
    );
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">Skill Access</h3>
          <p className="text-xs text-slate-500 mt-0.5">
            Grant users access to the skills this agent uses so they can interact with it fully in Slack.
          </p>
        </div>
        {!adding && grantableOnly.length > 0 && (
          <button
            type="button"
            onClick={() => { setAdding(true); setAddError(null); setSelectedEmployees([]); }}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 bg-slate-900 text-white rounded-lg hover:bg-slate-700 transition-colors font-semibold shrink-0"
          >
            <UserPlus className="w-3.5 h-3.5" /> Grant Skills
          </button>
        )}
      </div>

      {adding && (
        <div className="border border-slate-200 rounded-xl bg-white p-4 space-y-3">
          <p className="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
            <Zap className="w-3.5 h-3.5 text-amber-500" />
            Grant skill access
          </p>
          <EmployeePicker selected={selectedEmployees} onChange={setSelectedEmployees} disabled={submitting} />

          <div>
            <div className="flex items-center justify-between mb-2">
              <p className="text-xs font-semibold text-slate-700">Skills to grant</p>
              <button type="button" onClick={selectAllSkills} className="text-[11px] text-indigo-600 hover:underline">
                Select all
              </button>
            </div>
            <div className="flex flex-wrap gap-2">
              {grantableOnly.map(skill => (
                <label
                  key={skill.slug}
                  className={`inline-flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg border cursor-pointer transition-colors ${
                    selectedSlugs.has(skill.slug)
                      ? "bg-amber-50 border-amber-300 text-amber-800"
                      : "bg-white border-slate-200 text-slate-600 hover:bg-slate-50"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={selectedSlugs.has(skill.slug)}
                    onChange={() => toggleSkill(skill.slug)}
                    className="rounded border-slate-300 text-amber-600 focus:ring-amber-200"
                  />
                  {skill.name || skill.slug}
                </label>
              ))}
            </div>
            {grantableSkills.some(s => !s.grantable) && (
              <p className="text-[11px] text-slate-400 mt-2">
                Some agent skills cannot be granted by you (blocked, critical-risk, or no access).
              </p>
            )}
          </div>

          <textarea
            value={justification}
            onChange={e => setJustification(e.target.value)}
            placeholder="Why do these users need access? (min 10 characters)"
            rows={2}
            maxLength={2000}
            className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-200 focus:border-amber-400 placeholder:text-slate-400 resize-none"
          />

          <div className="flex flex-wrap items-center gap-3">
            <select
              value={grantType}
              onChange={e => setGrantType(e.target.value as typeof grantType)}
              className="text-sm px-3 py-1.5 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-200"
            >
              <option value="time_limited">Time-limited</option>
              <option value="permanent">Permanent</option>
              <option value="restricted">Restricted</option>
            </select>
            {grantType !== "permanent" && (
              <>
                <label className="text-xs text-slate-600">Expires in (days)</label>
                <input
                  type="number"
                  min={1}
                  max={365}
                  value={expiresDays}
                  onChange={e => setExpiresDays(e.target.value)}
                  className="w-20 text-sm px-3 py-1.5 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-200"
                />
              </>
            )}
          </div>

          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => void grantSkills()}
              disabled={submitting || selectedEmployees.length === 0 || justification.trim().length < 10}
              className="flex items-center gap-1.5 px-4 py-2 text-xs font-semibold bg-amber-600 text-white rounded-lg hover:bg-amber-700 disabled:opacity-50 transition-colors"
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
      ) : grantableOnly.length === 0 && grants.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-center border border-dashed border-slate-200 rounded-xl">
          <Zap className="w-8 h-8 text-slate-300 mb-2" />
          <p className="text-sm font-semibold text-slate-700">No grantable skills</p>
          <p className="text-xs text-slate-400 mt-1 max-w-xs">
            Add skills to this agent first, or contact an admin for critical-risk skills.
          </p>
        </div>
      ) : (
        <>
          {grants.length > 0 && (
            <div className="divide-y divide-slate-100 border border-slate-200 rounded-xl overflow-hidden">
              {grants.map(g => (
                <div key={g.id} className="flex items-center gap-3 px-4 py-3 bg-white hover:bg-slate-50 transition-colors group">
                  <div className="w-8 h-8 rounded-full bg-amber-100 border border-amber-200 flex items-center justify-center shrink-0 text-amber-700 text-xs font-bold">
                    {(g.user_name ?? g.user_email).slice(0, 1).toUpperCase()}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-xs font-semibold text-slate-900 truncate">
                      {g.user_name ?? g.user_email}
                      <span className="text-slate-400 font-normal ml-1">· {g.skill_name || g.skill_slug}</span>
                    </p>
                    <p className="text-xs text-slate-500 truncate">{g.user_email}</p>
                    {g.expires_at && g.status === "approved" && (
                      <p className="text-[11px] text-slate-400 mt-0.5">
                        Expires {new Date(g.expires_at).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}
                      </p>
                    )}
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {statusBadge(g.status)}
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
        </>
      )}
    </div>
  );
}
