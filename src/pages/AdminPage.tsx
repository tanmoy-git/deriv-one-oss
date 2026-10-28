/**
 * Unified admin hub:
 *   1. Access Requests — pending / active grants / expiring / audit log
 *   2. Users           — search + per-user skill breakdown
 *   3. Departments     — dept drill-down with inbound/outbound grants
 *   4. Skills          — per-skill access roster
 *   5. Infrastructure  — live pod metrics
 *   6. Usage           — token consumption
 *      Budgets         — monthly spend limits (src/features/budgets)
 *   7. Feedback        — user-submitted UI feedback inbox
 *   8. Systems         — connected systems ↔ skills ↔ agents
 */

import {
  useState,
  useEffect,
  useMemo,
  useCallback,
  useRef,
  type ReactNode,
  type FormEvent,
} from "react";
import {
  Activity,
  AlertCircle,
  BookUser,
  Bot,
  Building2,
  CalendarClock,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  ClipboardList,
  Clock,
  Cpu,
  Layers,
  Loader2,
  Lock,
  MemoryStick,
  MessageSquare,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Shield,
  ShieldCheck,
  ShieldOff,
  Tag,
  Trash2,
  UserCheck,
  Users,
  X,
  XCircle,
  Zap,
  DollarSign,
  TrendingUp,
  BarChart2,
  Rocket,
  Server,
  Wallet,
} from "lucide-react";
import {
  AreaChart,
  Area,
  BarChart,
  Bar,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
  ReferenceLine,
  Cell,
} from "recharts";
import { apiFetch } from "@/lib/api";
import { parseUTC } from "@/lib/time";
import AdminFeedbackList from "@/src/feedback/AdminFeedbackList";
import BudgetsTab from "@/src/features/budgets/BudgetsTab";
import { AdminGrantModal } from "@/components/AdminGrantModal";
import { SystemCriticalityBadge } from "@/components/SystemCriticalityBadge";
import { useNavigate } from "react-router-dom";
import { useBranding } from "@/lib/branding";

// ── Shared design tokens ───────────────────────────────────────────────────────

const RISK_COLORS: Record<string, string> = {
  standard: "text-emerald-700 bg-emerald-50 border-emerald-200",
  elevated: "text-amber-700 bg-amber-50 border-amber-200",
  critical: "text-red-700 bg-red-50 border-red-200",
};

// ── Shared helpers ─────────────────────────────────────────────────────────────

function relative(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - parseUTC(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 2) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return parseUTC(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function skillLabel(slug: string, name?: string): string {
  return name ?? slug.replace(/-/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

// ── Access-requests types ──────────────────────────────────────────────────────

interface Grant {
  id: string;
  user_email: string;
  user_name: string | null;
  user_department: string;
  skill_slug: string;
  skill_name: string;
  skill_department: string;
  skill_risk_level: "standard" | "elevated" | "critical";
  justification: string;
  requested_at: string;
  status: string;
  decided_by_email: string | null;
  decided_by_name: string | null;
  decided_at: string | null;
  deny_reason: string | null;
  grant_type: string | null;
  expires_at: string | null;
  restrictions: string | null;
  last_used_at: string | null;
  usage_count: number;
  revoked_at: string | null;
  revoke_reason: string | null;
  role_id: string | null;
  role_name: string | null;
}

// ── Directory types ────────────────────────────────────────────────────────────

interface GrantedSkill {
  skill_slug: string;
  grant_type: string | null;
  expires_at: string | null;
}

interface EmployeeSkills {
  tier1: string[];
  granted: GrantedSkill[];
  pending: string[];
}

interface Employee {
  email: string;
  name: string;
  department: string;
  sage_department: string;
  job_title: string;
  manager: string;
  team_level2: string;
  skills: EmployeeSkills;
}

interface SkillMeta {
  name: string;
  risk_level: "standard" | "elevated" | "critical";
  departments: string[];
  description: string;
  is_universal: boolean;
}

interface DirectoryData {
  employees: Employee[];
  skill_meta: Record<string, SkillMeta>;
  total: number;
}

// ═══════════════════════════════════════════════════════════════════════════════
// ACCESS REQUESTS TAB
// ═══════════════════════════════════════════════════════════════════════════════

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    approved: "text-emerald-700 bg-emerald-50 border-emerald-200",
    denied: "text-red-700 bg-red-50 border-red-200",
    expired: "text-slate-500 bg-slate-100 border-slate-200",
    revoked: "text-orange-700 bg-orange-50 border-orange-200",
  };
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded border text-[10px] font-bold uppercase tracking-wider ${map[status] ?? "text-slate-500 bg-slate-100 border-slate-200"}`}>
      {status}
    </span>
  );
}

function GrantOriginBadge({ g }: { g: Grant }) {
  if (g.role_id) {
    return (
      <span
        className="inline-flex items-center text-[10px] font-semibold px-2 py-0.5 rounded-full border text-violet-700 bg-violet-50 border-violet-200 max-w-[160px] truncate"
        title={`Granted via the "${g.role_name ?? g.role_id}" role`}
      >
        Via role: {g.role_name ?? "unknown"}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center text-[10px] font-semibold px-2 py-0.5 rounded-full border text-slate-600 bg-slate-100 border-slate-200">
      Direct
    </span>
  );
}

function SectionWrap({ title, icon, count, children }: { title: string; icon: ReactNode; count?: number; children: ReactNode }) {
  return (
    <section className="space-y-3">
      <div className="flex items-center gap-2">
        {icon}
        <h2 className="text-sm font-bold text-slate-900">{title}</h2>
        {count !== undefined && <span className="text-xs text-slate-500 bg-slate-100 rounded-full px-2 py-0.5">{count}</span>}
      </div>
      {children}
    </section>
  );
}

function PendingCard({ g, onApprove, onDeny }: {
  g: Grant;
  onApprove: (id: string, days: number | null) => void | Promise<void>;
  onDeny: (id: string) => void | Promise<void>;
}) {
  const [expanded, setExpanded] = useState(false);
  const [denyReason, setDenyReason] = useState("");
  const [denyMode, setDenyMode] = useState(false);

  return (
    <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
      <div className="px-4 py-3 flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-bold text-slate-900">{g.user_name || g.user_email}</span>
            <span className="text-xs text-slate-400">from <strong>{g.user_department}</strong></span>
            <span className="text-xs text-slate-400">→</span>
            <span className="text-sm font-semibold text-slate-700">{g.skill_slug}</span>
            <span className={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded border ${RISK_COLORS[g.skill_risk_level]}`}>{g.skill_risk_level}</span>
          </div>
          <div className="text-xs text-slate-500 mt-0.5">{relative(g.requested_at)}</div>
          <div className="mt-2 text-xs text-slate-700 leading-relaxed bg-slate-50 rounded-lg px-3 py-2 border border-slate-100">"{g.justification}"</div>
        </div>
        <button onClick={() => setExpanded(e => !e)} className="text-slate-400 hover:text-slate-700 shrink-0 mt-0.5">
          {expanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
        </button>
      </div>

      {expanded && !denyMode && (
        <div className="border-t border-slate-100 px-4 py-3 bg-slate-50 flex flex-wrap gap-2">
          <button onClick={() => onApprove(g.id, 30)} className="flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 transition-colors">
            <CheckCircle2 className="w-3.5 h-3.5" /> Approve (30d)
          </button>
          <button onClick={() => onApprove(g.id, 90)} className="flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 bg-emerald-50 text-emerald-700 border border-emerald-300 rounded-lg hover:bg-emerald-100 transition-colors">
            Approve (90d)
          </button>
          <button onClick={() => onApprove(g.id, null)} className="flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 bg-slate-100 text-slate-700 border border-slate-300 rounded-lg hover:bg-slate-200 transition-colors">
            Approve (permanent)
          </button>
          <button onClick={() => setDenyMode(true)} className="flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 text-red-600 border border-red-200 rounded-lg hover:bg-red-50 transition-colors">
            <XCircle className="w-3.5 h-3.5" /> Deny
          </button>
        </div>
      )}

      {denyMode && (
        <div className="border-t border-slate-100 px-4 py-3 bg-red-50 space-y-2">
          <textarea rows={2} value={denyReason} onChange={e => setDenyReason(e.target.value)}
            placeholder="Reason for denial (required)"
            className="w-full text-xs px-3 py-2 border border-red-200 rounded-lg focus:outline-none focus:border-red-400 bg-white resize-none" />
          <div className="flex gap-2">
            <button onClick={() => { if (denyReason.trim()) onDeny(g.id); }}
              className="text-xs font-bold px-3 py-1.5 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors">
              Confirm Deny
            </button>
            <button onClick={() => { setDenyMode(false); setDenyReason(""); }}
              className="text-xs px-3 py-1.5 text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50">
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function GrantRow({ g, onRevoke }: { g: Grant; onRevoke: (id: string) => void }) {
  const isExpiring = g.expires_at && parseUTC(g.expires_at) < new Date(Date.now() + 14 * 86400000);
  return (
    <tr className="border-b border-slate-100 last:border-0 hover:bg-slate-50 transition-colors">
      <td className="px-4 py-3 text-sm">
        <div className="font-semibold text-slate-900">{g.user_name || g.user_email}</div>
        <div className="text-xs text-slate-500">{g.user_department}</div>
      </td>
      <td className="px-4 py-3">
        <div className="text-sm font-mono text-slate-700">{g.skill_slug}</div>
        <div className="text-xs text-slate-500">{g.skill_department}</div>
      </td>
      <td className="px-4 py-3"><GrantOriginBadge g={g} /></td>
      <td className="px-4 py-3 text-xs text-slate-600">{g.decided_by_name || g.decided_by_email || "—"}</td>
      <td className="px-4 py-3 text-xs">
        <span className={`font-medium ${isExpiring ? "text-amber-700" : "text-slate-600"}`}>
          {g.expires_at ? formatDate(g.expires_at) : "Permanent"}
        </span>
        {isExpiring && <div className="text-[10px] text-amber-600">Expiring soon</div>}
      </td>
      <td className="px-4 py-3 text-xs text-slate-500">{g.usage_count} sessions</td>
      <td className="px-4 py-3">
        <button onClick={() => onRevoke(g.id)}
          className="text-[10px] font-semibold text-red-600 border border-red-200 rounded px-2 py-0.5 hover:bg-red-50 transition-colors">
          Revoke
        </button>
      </td>
    </tr>
  );
}

function RevokeModal({ grantId, onClose, onRevoked }: { grantId: string; onClose: () => void; onRevoked: () => void }) {
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!reason.trim()) { setError("Reason is required."); return; }
    setSubmitting(true);
    try {
      const res = await apiFetch(`/api/admin/access/${grantId}/revoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: reason.trim() }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error((d as { detail?: string })?.detail || `Error ${res.status}`);
      }
      onRevoked();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm border border-slate-200 p-6 space-y-4">
        <h2 className="text-base font-bold text-slate-900">Revoke Grant</h2>
        <form onSubmit={handleSubmit} className="space-y-3">
          <textarea rows={3} value={reason} onChange={e => setReason(e.target.value)}
            placeholder="Reason for revocation"
            className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:border-slate-400 resize-none" />
          {error && <p className="text-xs text-red-600">{error}</p>}
          <div className="flex gap-2">
            <button type="button" onClick={onClose} className="flex-1 px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50">Cancel</button>
            <button type="submit" disabled={submitting} className="flex-1 px-4 py-2 text-sm font-bold bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50">
              {submitting ? "Revoking…" : "Revoke"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function AccessRequestsTab() {
  const [pending, setPending] = useState<Grant[]>([]);
  const [grants, setGrants] = useState<Grant[]>([]);
  const [expiring, setExpiring] = useState<Grant[]>([]);
  const [audit, setAudit] = useState<Grant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [revokeId, setRevokeId] = useState<string | null>(null);
  const [sub, setSub] = useState<"pending" | "grants" | "expiring" | "audit">("pending");

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [pR, gR, eR, aR] = await Promise.all([
        apiFetch("/api/admin/access/pending"),
        apiFetch("/api/admin/access/grants"),
        apiFetch("/api/admin/access/expiring"),
        apiFetch("/api/admin/access/audit"),
      ]);
      if (!pR.ok || !gR.ok || !eR.ok || !aR.ok) throw new Error("Failed to load access data.");
      const [p, g, e, a] = await Promise.all([pR.json(), gR.json(), eR.json(), aR.json()]);
      setPending(p.pending ?? []);
      setGrants(g.grants ?? []);
      setExpiring(e.expiring ?? []);
      setAudit(a.audit ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Error.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const handleApprove = async (id: string, days: number | null) => {
    const body = days ? { grant_type: "time_limited", expires_in_days: days } : { grant_type: "permanent" };
    const res = await apiFetch(`/api/admin/access/${id}/approve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (res.ok) void load();
  };

  const handleDeny = async (id: string) => {
    const res = await apiFetch(`/api/admin/access/${id}/deny`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason: "Denied by admin." }) });
    if (res.ok) void load();
  };

  const SUB_TABS = [
    { id: "pending" as const, label: "Pending", icon: <Clock className="w-3.5 h-3.5" />, count: pending.length },
    { id: "grants" as const, label: "Active Grants", icon: <CheckCircle2 className="w-3.5 h-3.5" />, count: grants.length },
    { id: "expiring" as const, label: "Expiring Soon", icon: <CalendarClock className="w-3.5 h-3.5" />, count: expiring.length },
    { id: "audit" as const, label: "Audit Log", icon: <ClipboardList className="w-3.5 h-3.5" />, count: audit.length },
  ];

  const tableHeaders = sub === "expiring"
    ? ["User", "Skill", "Access", "Expires", "Usage", ""]
    : ["User", "Skill", "Access", "Granted By", "Expires", "Usage", ""];

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-5xl mx-auto space-y-4">
        {/* Sub-tab bar */}
        <div className="flex gap-1 bg-white border border-slate-200 rounded-xl p-1">
          {SUB_TABS.map(n => (
            <button key={n.id} onClick={() => setSub(n.id)}
              className={`flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-semibold rounded-lg transition-colors ${sub === n.id ? "bg-slate-900 text-white" : "text-slate-500 hover:text-slate-900 hover:bg-slate-50"}`}>
              {n.icon} {n.label}
              {n.count > 0 && (
                <span className={`rounded-full px-1.5 text-[10px] font-bold ${sub === n.id ? "bg-white/20 text-white" : "bg-slate-100 text-slate-600"}`}>{n.count}</span>
              )}
            </button>
          ))}
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-16 gap-3 text-slate-400">
            <Loader2 className="w-5 h-5 animate-spin text-[#FF444F]" />
            <span className="text-sm">Loading…</span>
          </div>
        ) : error ? (
          <div className="flex items-center gap-2 text-sm text-red-700 bg-red-50 border border-red-200 rounded-xl px-4 py-3">
            <AlertCircle className="w-4 h-4 shrink-0" /> {error}
          </div>
        ) : (
          <>
            {sub === "pending" && (
              <SectionWrap title="Pending Requests" icon={<Clock className="w-4 h-4 text-amber-600" />} count={pending.length}>
                {pending.length === 0 ? (
                  <div className="text-center py-12 bg-white border border-slate-200 rounded-xl text-slate-500 text-sm">No pending requests. 🎉</div>
                ) : (
                  <div className="space-y-3">
                    {pending.map(g => (
                      <PendingCard key={g.id} g={g}
                        onApprove={(id, days) => void handleApprove(id, days)}
                        onDeny={(id) => void handleDeny(id)} />
                    ))}
                  </div>
                )}
              </SectionWrap>
            )}

            {(sub === "grants" || sub === "expiring") && (
              <SectionWrap
                title={sub === "grants" ? "Active Grants" : "Expiring in Next 14 Days"}
                icon={sub === "grants" ? <Users className="w-4 h-4 text-emerald-600" /> : <CalendarClock className="w-4 h-4 text-amber-600" />}
                count={sub === "grants" ? grants.length : expiring.length}>
                {(sub === "grants" ? grants : expiring).length === 0 ? (
                  <div className="text-center py-12 bg-white border border-slate-200 rounded-xl text-slate-500 text-sm">
                    {sub === "grants" ? "No active grants." : "No grants expiring soon."}
                  </div>
                ) : (
                  <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
                    <table className="w-full text-left">
                      <thead>
                        <tr className="border-b border-slate-100 bg-slate-50">
                          {tableHeaders.map(h => <th key={h} className="px-4 py-2.5 text-[10px] font-bold text-slate-400 uppercase tracking-wider">{h}</th>)}
                        </tr>
                      </thead>
                      <tbody>
                        {(sub === "grants" ? grants : expiring).map(g => (
                          <GrantRow key={g.id} g={g} onRevoke={id => setRevokeId(id)} />
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </SectionWrap>
            )}

            {sub === "audit" && (
              <SectionWrap title="Audit Log" icon={<ClipboardList className="w-4 h-4 text-slate-600" />} count={audit.length}>
                {audit.length === 0 ? (
                  <div className="text-center py-12 bg-white border border-slate-200 rounded-xl text-slate-500 text-sm">No audit records.</div>
                ) : (
                  <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
                    <table className="w-full text-left">
                      <thead>
                        <tr className="border-b border-slate-100 bg-slate-50">
                          {["User", "Skill", "Access", "Status", "Decided By", "Date", "Details"].map(h => (
                            <th key={h} className="px-4 py-2.5 text-[10px] font-bold text-slate-400 uppercase tracking-wider">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {audit.map(g => (
                          <tr key={g.id} className="border-b border-slate-100 last:border-0 hover:bg-slate-50 transition-colors">
                            <td className="px-4 py-3 text-sm">
                              <div className="font-medium text-slate-800">{g.user_name || g.user_email}</div>
                              <div className="text-xs text-slate-500">{g.user_department}</div>
                            </td>
                            <td className="px-4 py-3 text-xs font-mono text-slate-700">{g.skill_slug}</td>
                            <td className="px-4 py-3"><GrantOriginBadge g={g} /></td>
                            <td className="px-4 py-3"><StatusBadge status={g.status} /></td>
                            <td className="px-4 py-3 text-xs text-slate-600">{g.decided_by_name || g.decided_by_email || "—"}</td>
                            <td className="px-4 py-3 text-xs text-slate-500">{relative(g.decided_at)}</td>
                            <td className="px-4 py-3 text-xs text-slate-500">
                              {g.deny_reason && <span className="text-red-600">"{g.deny_reason}"</span>}
                              {g.revoke_reason && <span className="text-orange-600">"{g.revoke_reason}"</span>}
                              {g.expires_at && !g.deny_reason && !g.revoke_reason && <span>until {formatDate(g.expires_at)}</span>}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </SectionWrap>
            )}
          </>
        )}
      </div>

      {revokeId && (
        <RevokeModal grantId={revokeId} onClose={() => setRevokeId(null)} onRevoked={() => { setRevokeId(null); void load(); }} />
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// SHARED DIRECTORY COMPONENTS
// ═══════════════════════════════════════════════════════════════════════════════

function SkillBadge({ slug, meta, variant, expiry }: {
  slug: string;
  meta: Record<string, SkillMeta>;
  variant: "tier1" | "granted" | "pending";
  expiry?: string | null;
}) {
  const risk = meta[slug]?.risk_level ?? "standard";
  const base = "inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 rounded-full border";
  const styles = variant === "tier1" ? "bg-emerald-50 text-emerald-700 border-emerald-200"
    : variant === "granted" ? "bg-blue-50 text-blue-700 border-blue-200"
      : "bg-amber-50 text-amber-700 border-amber-200";
  return (
    <span className={`${base} ${styles}`} title={meta[slug]?.description ?? slug}>
      {variant === "tier1" && <Shield className="w-2.5 h-2.5" />}
      {variant === "granted" && <Zap className="w-2.5 h-2.5" />}
      {skillLabel(slug, meta[slug]?.name)}
      {expiry && <span className="opacity-60">· {formatDate(expiry)}</span>}
      {variant !== "tier1" && (
        <span className={`ml-0.5 text-[8px] uppercase tracking-wide px-1 rounded border ${RISK_COLORS[risk]}`}>{risk}</span>
      )}
    </span>
  );
}

function UserRow({ emp, meta, onOpenRoles }: { emp: Employee; meta: Record<string, SkillMeta>; onOpenRoles: (emp: Employee) => void }) {
  const [open, setOpen] = useState(false);
  const total = emp.skills.tier1.length + emp.skills.granted.length + emp.skills.pending.length;

  return (
    <div className="bg-white border border-gray-200 rounded-xl overflow-hidden">
      <div
        className="w-full px-4 py-3 flex items-center gap-3 hover:bg-gray-50 transition-colors text-left cursor-pointer"
        onClick={() => setOpen(o => !o)}
      >
        <div className="w-8 h-8 rounded-full bg-[#FF444F]/10 border border-[#FF444F]/30 flex items-center justify-center text-xs font-bold text-[#FF444F] shrink-0">
          {(emp.name || emp.email).slice(0, 2).toUpperCase()}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-semibold text-slate-900">{emp.name || emp.email}</span>
            {emp.job_title && <span className="text-xs text-slate-400 truncate">{emp.job_title}</span>}
          </div>
          <div className="flex items-center gap-2 mt-0.5">
            <span className="text-xs text-slate-500">{emp.department || "—"}</span>
            {emp.team_level2 && <span className="text-[10px] text-slate-400">· {emp.team_level2}</span>}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-[10px] text-slate-400 font-medium">{total} skill{total !== 1 ? "s" : ""}</span>
          <button
            type="button"
            onClick={e => { e.stopPropagation(); onOpenRoles(emp); }}
            className="inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 rounded-full border bg-violet-50 text-violet-700 border-violet-200 hover:bg-violet-100 transition-colors"
          >
            <Tag className="w-2.5 h-2.5" /> Roles
          </button>
          {open ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronDown className="w-4 h-4 text-slate-400" />}
        </div>
      </div>

      {open && (
        <div className="px-4 pb-4 pt-0 border-t border-gray-100 space-y-3">
          <div className="text-[10px] text-slate-400 pt-2">{emp.email}</div>

          {emp.skills.tier1.length > 0 && (
            <div>
              <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wide mb-1.5">Tier 1 — Automatic ({emp.skills.tier1.length})</div>
              <div className="flex flex-wrap gap-1.5">
                {emp.skills.tier1.map(s => <SkillBadge key={s} slug={s} meta={meta} variant="tier1" />)}
              </div>
            </div>
          )}
          {emp.skills.granted.length > 0 && (
            <div>
              <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wide mb-1.5">Granted — Cross-dept ({emp.skills.granted.length})</div>
              <div className="flex flex-wrap gap-1.5">
                {emp.skills.granted.map(g => <SkillBadge key={g.skill_slug} slug={g.skill_slug} meta={meta} variant="granted" expiry={g.expires_at} />)}
              </div>
            </div>
          )}
          {emp.skills.pending.length > 0 && (
            <div>
              <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wide mb-1.5">Pending ({emp.skills.pending.length})</div>
              <div className="flex flex-wrap gap-1.5">
                {emp.skills.pending.map(s => <SkillBadge key={s} slug={s} meta={meta} variant="pending" />)}
              </div>
            </div>
          )}
          {total === 0 && <div className="text-xs text-slate-400 italic">No skill access configured.</div>}
        </div>
      )}
    </div>
  );
}

interface UserRoleEntry {
  role_id: string;
  role_name: string;
  agent_id: string;
  agent_name: string;
  skill_slugs: string[];
  grants: { skill_slug: string; grant_id: string; expires_at: string | null }[];
  missing_skill_slugs: string[];
  has_agent_access: boolean;
  status: "full" | "partial";
}

function UserRolesModal({ emp, onClose }: { emp: Employee; onClose: () => void }) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [roles, setRoles] = useState<UserRoleEntry[]>([]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiFetch(`/api/admin/users/${encodeURIComponent(emp.email)}/roles`)
      .then(r => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(d => { if (!cancelled) setRoles(d.roles ?? []); })
      .catch(e => { if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load roles."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [emp.email]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative bg-white rounded-2xl shadow-2xl border border-slate-200 w-full max-w-lg max-h-[80vh] flex flex-col">
        <div className="px-6 pt-5 pb-4 border-b border-slate-100 flex items-start justify-between shrink-0">
          <div>
            <h3 className="text-sm font-bold text-slate-900 flex items-center gap-1.5">
              <Tag className="w-4 h-4 text-violet-600" /> Roles
            </h3>
            <p className="text-xs text-slate-500 mt-0.5">
              {emp.name || emp.email} <span className="text-slate-400">· {emp.email}</span>
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700 shrink-0">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="px-6 py-4 overflow-y-auto space-y-3">
          {loading ? (
            <div className="flex items-center justify-center py-10">
              <Loader2 className="w-4 h-4 animate-spin text-slate-400" />
            </div>
          ) : error ? (
            <p className="text-xs text-red-600">{error}</p>
          ) : roles.length === 0 ? (
            <div className="text-center py-10">
              <Tag className="w-8 h-8 text-slate-300 mx-auto mb-2" />
              <p className="text-sm font-semibold text-slate-700">Not a member of any role</p>
              <p className="text-xs text-slate-400 mt-1">This person hasn't been assigned any agent role yet.</p>
            </div>
          ) : (
            roles.map(r => (
              <div key={r.role_id} className="border border-slate-200 rounded-xl p-3">
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-slate-900 truncate">{r.role_name}</p>
                    <p className="text-[11px] text-slate-400 truncate">on {r.agent_name}</p>
                  </div>
                  <span
                    className={`inline-flex shrink-0 text-[10px] font-bold uppercase px-2 py-0.5 rounded-full border ${
                      r.status === "full"
                        ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                        : "bg-amber-50 text-amber-700 border-amber-200"
                    }`}
                  >
                    {r.status}
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5 mt-2">
                  {r.skill_slugs.map(slug => {
                    const missing = r.missing_skill_slugs.includes(slug);
                    return (
                      <span
                        key={slug}
                        className={`text-[10px] px-2 py-0.5 rounded-full border ${
                          missing
                            ? "bg-slate-50 text-slate-400 border-slate-200"
                            : "bg-emerald-50 text-emerald-700 border-emerald-200"
                        }`}
                      >
                        {slug}{missing && " (missing)"}
                      </span>
                    );
                  })}
                </div>
                {!r.has_agent_access && (
                  <p className="text-[11px] text-amber-600 mt-1.5">Missing agent access to this agent</p>
                )}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// USERS TAB
// ═══════════════════════════════════════════════════════════════════════════════

function UsersTab({ data }: { data: DirectoryData }) {
  const [search, setSearch] = useState("");
  const [deptFilter, setDeptFilter] = useState("");
  const [rolesModalEmp, setRolesModalEmp] = useState<Employee | null>(null);

  const departments = useMemo(() => {
    const s = new Set(data.employees.map(e => e.department).filter(Boolean));
    return Array.from(s).sort();
  }, [data.employees]);

  const filtered = useMemo(() => {
    const q = search.toLowerCase().trim();
    return data.employees.filter(e => {
      if (deptFilter && e.department !== deptFilter) return false;
      if (!q) return true;
      return e.name.toLowerCase().includes(q) || e.email.toLowerCase().includes(q)
        || e.department.toLowerCase().includes(q) || e.job_title.toLowerCase().includes(q);
    });
  }, [data.employees, search, deptFilter]);

  return (
    <div className="flex flex-col gap-4 h-full overflow-hidden">
      <div className="flex items-center gap-3 flex-wrap shrink-0">
        <div className="relative flex-1 min-w-[200px]">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
          <input className="w-full pl-8 pr-8 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-[#FF444F]/30 focus:border-[#FF444F]"
            placeholder="Search by name, email, department, title…" value={search} onChange={e => setSearch(e.target.value)} />
          {search && <button className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600" onClick={() => setSearch("")}><X className="w-3.5 h-3.5" /></button>}
        </div>
        <select className="px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-[#FF444F]/30 focus:border-[#FF444F] bg-white"
          value={deptFilter} onChange={e => setDeptFilter(e.target.value)}>
          <option value="">All Departments</option>
          {departments.map(d => <option key={d} value={d}>{d}</option>)}
        </select>
        <span className="text-xs text-slate-400 shrink-0">{filtered.length} of {data.total}</span>
      </div>
      <div className="flex-1 overflow-y-auto space-y-2 pr-1">
        {filtered.length === 0
          ? <div className="text-sm text-slate-400 text-center py-12">No employees match the current filters.</div>
          : filtered.map(emp => <UserRow key={emp.email} emp={emp} meta={data.skill_meta} onOpenRoles={setRolesModalEmp} />)
        }
      </div>
      {rolesModalEmp && <UserRolesModal emp={rolesModalEmp} onClose={() => setRolesModalEmp(null)} />}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// DEPARTMENTS TAB
// ═══════════════════════════════════════════════════════════════════════════════

function DepartmentsTab({ data }: { data: DirectoryData }) {
  const [selectedDept, setSelectedDept] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [rolesModalEmp, setRolesModalEmp] = useState<Employee | null>(null);

  const deptSummary = useMemo(() => {
    const map = new Map<string, { employees: Employee[]; ownedSlugs: string[] }>();
    for (const emp of data.employees) {
      const dept = emp.department || "Unknown";
      if (!map.has(dept)) map.set(dept, { employees: [], ownedSlugs: [] });
      map.get(dept)!.employees.push(emp);
    }
    for (const [slug, meta] of Object.entries(data.skill_meta)) {
      if (meta.is_universal) continue;
      for (const d of meta.departments) {
        if (map.has(d)) map.get(d)!.ownedSlugs.push(slug);
      }
    }
    return Array.from(map.entries())
      .map(([dept, info]) => ({ dept, ...info }))
      .sort((a, b) => b.employees.length - a.employees.length);
  }, [data]);

  const filteredDepts = useMemo(() => {
    const q = search.toLowerCase();
    return q ? deptSummary.filter(d => d.dept.toLowerCase().includes(q)) : deptSummary;
  }, [deptSummary, search]);

  const selected = selectedDept ? deptSummary.find(d => d.dept === selectedDept) : null;

  const inboundGrants = useMemo(() => {
    if (!selected) return [];
    return data.employees.filter(emp =>
      emp.department !== selected.dept &&
      emp.skills.granted.some(g => selected.ownedSlugs.includes(g.skill_slug))
    );
  }, [selected, data.employees]);

  const outboundGrants = useMemo(() =>
    selected ? selected.employees.filter(emp => emp.skills.granted.length > 0) : [],
    [selected]);

  return (
    <div className="flex gap-4 h-full overflow-hidden">
      <div className="w-64 shrink-0 flex flex-col gap-3 overflow-hidden">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
          <input className="w-full pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-[#FF444F]/30 focus:border-[#FF444F]"
            placeholder="Filter departments…" value={search} onChange={e => setSearch(e.target.value)} />
        </div>
        <div className="flex-1 overflow-y-auto space-y-1 pr-1">
          {filteredDepts.map(({ dept, employees, ownedSlugs }) => (
            <button key={dept} onClick={() => setSelectedDept(dept === selectedDept ? null : dept)}
              className={`w-full text-left px-3 py-2.5 rounded-lg transition-colors border ${selectedDept === dept ? "bg-[#FF444F] text-white border-[#FF444F]" : "bg-white border-gray-200 hover:bg-gray-50 text-slate-700"}`}>
              <div className="text-sm font-semibold truncate">{dept}</div>
              <div className={`text-[10px] mt-0.5 flex items-center gap-2 ${selectedDept === dept ? "text-white/70" : "text-slate-400"}`}>
                <span>{employees.length} member{employees.length !== 1 ? "s" : ""}</span>
                {ownedSlugs.length > 0 && <span>· {ownedSlugs.length} skill{ownedSlugs.length !== 1 ? "s" : ""}</span>}
              </div>
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        {!selected ? (
          <div className="flex flex-col items-center justify-center h-full text-slate-400 gap-2">
            <Building2 className="w-10 h-10 opacity-30" />
            <span className="text-sm">Select a department to view details</span>
          </div>
        ) : (
          <div className="space-y-5">
            <div className="bg-white rounded-xl border border-gray-200 p-5">
              <h2 className="text-lg font-bold text-slate-900">{selected.dept}</h2>
              <p className="text-sm text-slate-500 mt-0.5">
                {selected.employees.length} active employee{selected.employees.length !== 1 ? "s" : ""}
                {selected.ownedSlugs.length > 0 && ` · owns ${selected.ownedSlugs.length} skill${selected.ownedSlugs.length !== 1 ? "s" : ""}`}
              </p>
              {selected.ownedSlugs.length > 0 && (
                <div className="mt-4">
                  <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wide mb-2">Skills Owned By This Department</div>
                  <div className="flex flex-wrap gap-1.5">
                    {selected.ownedSlugs.map(slug => {
                      const risk = data.skill_meta[slug]?.risk_level ?? "standard";
                      return (
                        <span key={slug} className={`inline-flex items-center text-[10px] font-semibold px-2 py-0.5 rounded-full border ${RISK_COLORS[risk]}`}>
                          {skillLabel(slug, data.skill_meta[slug]?.name)} <span className="ml-1 opacity-60">{risk}</span>
                        </span>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>

            <section className="space-y-2">
              <div className="flex items-center gap-2">
                <Users className="w-4 h-4 text-slate-400" />
                <span className="text-sm font-bold text-slate-900">Members</span>
                <span className="text-xs bg-slate-100 text-slate-500 rounded-full px-2 py-0.5">{selected.employees.length}</span>
              </div>
              {selected.employees.slice(0, 50).map(emp => <UserRow key={emp.email} emp={emp} meta={data.skill_meta} onOpenRoles={setRolesModalEmp} />)}
              {selected.employees.length > 50 && <p className="text-xs text-slate-400 text-center py-2">Showing first 50 of {selected.employees.length} members.</p>}
            </section>

            {inboundGrants.length > 0 && (
              <section className="space-y-2">
                <div className="flex items-center gap-2">
                  <Shield className="w-4 h-4 text-blue-500" />
                  <span className="text-sm font-bold text-slate-900">External Access — Into This Dept's Skills</span>
                  <span className="text-xs bg-blue-50 text-blue-600 border border-blue-200 rounded-full px-2 py-0.5">{inboundGrants.length}</span>
                </div>
                <div className="space-y-2">
                  {inboundGrants.map(emp => (
                    <div key={emp.email} className="bg-white border border-blue-100 rounded-xl px-4 py-3">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm font-semibold text-slate-900">{emp.name || emp.email}</span>
                        <span className="text-xs text-slate-400">from <strong>{emp.department}</strong></span>
                      </div>
                      <div className="flex flex-wrap gap-1.5 mt-1.5">
                        {emp.skills.granted.filter(g => selected.ownedSlugs.includes(g.skill_slug)).map(g => (
                          <SkillBadge key={g.skill_slug} slug={g.skill_slug} meta={data.skill_meta} variant="granted" expiry={g.expires_at} />
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {outboundGrants.length > 0 && (
              <section className="space-y-2">
                <div className="flex items-center gap-2">
                  <Zap className="w-4 h-4 text-amber-500" />
                  <span className="text-sm font-bold text-slate-900">Members With Cross-Dept Access</span>
                  <span className="text-xs bg-amber-50 text-amber-600 border border-amber-200 rounded-full px-2 py-0.5">{outboundGrants.length}</span>
                </div>
                <div className="space-y-2">
                  {outboundGrants.map(emp => (
                    <div key={emp.email} className="bg-white border border-amber-100 rounded-xl px-4 py-3">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-sm font-semibold text-slate-900">{emp.name || emp.email}</span>
                        <span className="text-xs text-slate-400">{emp.job_title}</span>
                      </div>
                      <div className="flex flex-wrap gap-1.5 mt-1.5">
                        {emp.skills.granted.map(g => <SkillBadge key={g.skill_slug} slug={g.skill_slug} meta={data.skill_meta} variant="granted" expiry={g.expires_at} />)}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            )}
          </div>
        )}
      </div>
      {rolesModalEmp && <UserRolesModal emp={rolesModalEmp} onClose={() => setRolesModalEmp(null)} />}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// SKILLS DIRECTORY TAB (new)
// ═══════════════════════════════════════════════════════════════════════════════

function SkillCard({ slug, meta, employees }: {
  slug: string;
  meta: SkillMeta;
  employees: Employee[];
}) {
  const [open, setOpen] = useState(false);
  const [showGrantModal, setShowGrantModal] = useState(false);

  const tier1Users = useMemo(() => employees.filter(e => e.skills.tier1.includes(slug)), [employees, slug]);
  const grantedUsers = useMemo(() => employees.filter(e => e.skills.granted.some(g => g.skill_slug === slug)), [employees, slug]);
  const pendingUsers = useMemo(() => employees.filter(e => e.skills.pending.includes(slug)), [employees, slug]);

  // Group tier1 users by department
  const tier1ByDept = useMemo(() => {
    const m = new Map<string, Employee[]>();
    for (const emp of tier1Users) {
      const dept = emp.department || "Unknown";
      if (!m.has(dept)) m.set(dept, []);
      m.get(dept)!.push(emp);
    }
    return Array.from(m.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [tier1Users]);

  const totalAccess = tier1Users.length + grantedUsers.length;

  return (
    <div className="bg-white border border-gray-200 rounded-xl overflow-hidden">
      <button className="w-full px-5 py-4 flex items-start gap-4 hover:bg-gray-50 transition-colors text-left" onClick={() => setOpen(o => !o)}>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-bold text-slate-900">{meta.name || skillLabel(slug)}</span>
            <span className={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded border ${RISK_COLORS[meta.risk_level]}`}>{meta.risk_level}</span>
            {meta.is_universal && (
              <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded border text-violet-700 bg-violet-50 border-violet-200">universal</span>
            )}
          </div>
          {meta.description && <p className="text-xs text-slate-500 mt-1 line-clamp-1">{meta.description}</p>}
          <div className="flex items-center gap-3 mt-1.5 flex-wrap">
            {meta.departments.length > 0 && !meta.is_universal && (
              <span className="text-[10px] text-slate-400">
                Owned by: <span className="font-medium text-slate-600">{meta.departments.join(", ")}</span>
              </span>
            )}
            <span className="text-[10px] text-slate-400">
              {totalAccess} user{totalAccess !== 1 ? "s" : ""} with access
            </span>
            {pendingUsers.length > 0 && (
              <span className="text-[10px] text-amber-600 font-medium">{pendingUsers.length} pending</span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0 mt-0.5">
          {open ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronRight className="w-4 h-4 text-slate-400" />}
        </div>
      </button>

      {open && (
        <div className="border-t border-gray-100 px-5 pb-5 pt-4 space-y-5">

          {/* Tier 1 — grouped by dept */}
          {tier1ByDept.length > 0 ? (
            <div>
              <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
                <Shield className="w-3 h-3 text-emerald-600" />
                Tier 1 Access — {tier1Users.length} user{tier1Users.length !== 1 ? "s" : ""} across {tier1ByDept.length} dept{tier1ByDept.length !== 1 ? "s" : ""}
              </div>
              <div className="space-y-3">
                {tier1ByDept.map(([dept, emps]) => (
                  <div key={dept}>
                    <div className="text-[10px] font-semibold text-slate-400 mb-1.5">{dept} ({emps.length})</div>
                    <div className="flex flex-wrap gap-1.5">
                      {emps.map(emp => (
                        <span key={emp.email} className="inline-flex items-center text-[10px] font-medium px-2 py-0.5 rounded-full bg-emerald-50 border border-emerald-200 text-emerald-700">
                          {emp.name || emp.email}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <p className="text-xs text-slate-400 italic">No tier 1 access (skill not assigned to any department).</p>
          )}

          {/* Granted cross-dept */}
          {grantedUsers.length > 0 && (
            <div>
              <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
                <Zap className="w-3 h-3 text-blue-500" />
                Cross-Dept Grants — {grantedUsers.length} user{grantedUsers.length !== 1 ? "s" : ""}
              </div>
              <div className="space-y-1.5">
                {grantedUsers.map(emp => {
                  const grant = emp.skills.granted.find(g => g.skill_slug === slug)!;
                  return (
                    <div key={emp.email} className="flex items-center gap-3 text-xs">
                      <span className="font-medium text-slate-800 min-w-[140px]">{emp.name || emp.email}</span>
                      <span className="text-slate-400">{emp.department}</span>
                      <span className="text-slate-400 ml-auto">
                        {grant.expires_at ? `until ${formatDate(grant.expires_at)}` : "permanent"}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Pending requests */}
          {pendingUsers.length > 0 && (
            <div>
              <div className="text-[10px] font-bold text-slate-500 uppercase tracking-wide mb-3 flex items-center gap-1.5">
                <Clock className="w-3 h-3 text-amber-500" />
                Pending Requests — {pendingUsers.length}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {pendingUsers.map(emp => (
                  <span key={emp.email} className="inline-flex items-center text-[10px] font-medium px-2 py-0.5 rounded-full bg-amber-50 border border-amber-200 text-amber-700">
                    {emp.name || emp.email} <span className="ml-1 opacity-60">{emp.department}</span>
                  </span>
                ))}
              </div>
            </div>
          )}

          {totalAccess === 0 && pendingUsers.length === 0 && (
            <p className="text-xs text-slate-400 italic">No users have access to this skill yet.</p>
          )}

          {/* Admin: direct grant action */}
          <div className="pt-2 border-t border-gray-100 mt-2">
            <button
              onClick={e => { e.stopPropagation(); setShowGrantModal(true); }}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg border border-[#FF444F]/30 bg-[#FF444F]/5 text-[#FF444F] hover:bg-[#FF444F]/10 transition-colors"
            >
              <UserCheck className="w-3.5 h-3.5" /> Grant Access
            </button>
          </div>
        </div>
      )}

      {showGrantModal && (
        <AdminGrantModal
          skillSlug={slug}
          skillName={meta.name}
          onClose={() => setShowGrantModal(false)}
        />
      )}
    </div>
  );
}

type RiskFilter = "all" | "standard" | "elevated" | "critical";

function SkillsDirectoryTab({ data }: { data: DirectoryData }) {
  const [search, setSearch] = useState("");
  const [risk, setRisk] = useState<RiskFilter>("all");
  const [showUniversal, setShowUniversal] = useState(true);

  const skills = useMemo(() => {
    return Object.entries(data.skill_meta)
      .filter(([, meta]) => {
        if (risk !== "all" && meta.risk_level !== risk) return false;
        if (!showUniversal && meta.is_universal) return false;
        if (search) {
          const q = search.toLowerCase();
          return (meta.name ?? "").toLowerCase().includes(q) ||
            meta.description?.toLowerCase().includes(q) ||
            meta.departments.some(d => d.toLowerCase().includes(q));
        }
        return true;
      })
      .sort((a, b) => {
        const order = { critical: 0, elevated: 1, standard: 2 };
        return (order[a[1].risk_level] ?? 3) - (order[b[1].risk_level] ?? 3) || a[0].localeCompare(b[0]);
      });
  }, [data.skill_meta, search, risk, showUniversal]);

  return (
    <div className="flex flex-col gap-4 h-full overflow-hidden">
      {/* Filters */}
      <div className="flex items-center gap-3 flex-wrap shrink-0">
        <div className="relative flex-1 min-w-[200px]">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
          <input className="w-full pl-8 pr-8 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-[#FF444F]/30 focus:border-[#FF444F]"
            placeholder="Search by name, description, department…" value={search} onChange={e => setSearch(e.target.value)} />
          {search && <button className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600" onClick={() => setSearch("")}><X className="w-3.5 h-3.5" /></button>}
        </div>
        <select className="px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-[#FF444F]/30 focus:border-[#FF444F] bg-white"
          value={risk} onChange={e => setRisk(e.target.value as RiskFilter)}>
          <option value="all">All Risk Levels</option>
          <option value="standard">Standard</option>
          <option value="elevated">Elevated</option>
          <option value="critical">Critical</option>
        </select>
        <label className="flex items-center gap-1.5 text-xs text-slate-500 cursor-pointer select-none">
          <input type="checkbox" checked={showUniversal} onChange={e => setShowUniversal(e.target.checked)} className="rounded" />
          Universal skills
        </label>
        <span className="text-xs text-slate-400 shrink-0">{skills.length} skill{skills.length !== 1 ? "s" : ""}</span>
      </div>

      {/* Legend */}
      <div className="flex items-center gap-3 text-[10px] text-slate-400 shrink-0">
        <span className="flex items-center gap-1"><Shield className="w-3 h-3 text-emerald-500" /> Tier 1 (dept-automatic)</span>
        <span className="flex items-center gap-1"><Zap className="w-3 h-3 text-blue-500" /> Cross-dept grant</span>
        <span className="flex items-center gap-1"><Clock className="w-3 h-3 text-amber-500" /> Pending request</span>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto space-y-2 pr-1">
        {skills.length === 0 ? (
          <div className="text-sm text-slate-400 text-center py-12">No skills match the current filters.</div>
        ) : (
          skills.map(([slug, meta]) => (
            <SkillCard key={slug} slug={slug} meta={meta} employees={data.employees} />
          ))
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// INFRASTRUCTURE TAB
// ═══════════════════════════════════════════════════════════════════════════════

interface ContainerMetric {
  name: string;
  cpu_m: number | null;
  mem_mi: number | null;
  cpu_limit_m: number | null;
  mem_limit_mi: number | null;
  cpu_req_m: number | null;
  mem_req_mi: number | null;
}
interface PodMetric {
  name: string;
  workload: string;
  phase: string;
  ready: boolean;
  node: string;
  age_seconds: number;
  containers: ContainerMetric[];
  cpu_m_total: number;
  mem_mi_total: number;
}
interface InfraData { pods: PodMetric[]; pools?: PoolStatus[]; fetched_at: string; error?: string; }

const WORKLOAD_ORDER = ["backend", "frontend", "slack-consumer", "scheduler", "sandbox-router", "cloudflared", "sandbox-executor"];

function fmtAge(s: number): string {
  if (s < 120) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function UsageBar({ used, limit, colorClass }: { used: number | null; limit: number | null; colorClass: string }) {
  if (used === null) return <span className="text-xs text-slate-400">—</span>;
  const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : null;
  return (
    <div className="flex items-center gap-2 min-w-0">
      <span className="text-xs font-mono text-slate-700 w-14 shrink-0 text-right">{used}{limit ? `/${limit}` : ""}</span>
      {pct !== null && (
        <div className="flex-1 bg-slate-100 rounded-full h-1.5 min-w-[40px]">
          <div
            className={`h-1.5 rounded-full transition-all ${pct > 85 ? "bg-red-500" : pct > 60 ? "bg-amber-400" : colorClass}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
      {pct !== null && <span className="text-xs text-slate-400 w-8 shrink-0">{pct}%</span>}
    </div>
  );
}

function PodCard({ pod }: { pod: PodMetric }) {
  const [expanded, setExpanded] = useState(false);
  const phaseColor = pod.phase === "Running" && pod.ready
    ? "text-emerald-700 bg-emerald-50 border-emerald-200"
    : pod.phase === "Running"
      ? "text-amber-700 bg-amber-50 border-amber-200"
      : "text-slate-500 bg-slate-50 border-slate-200";

  return (
    <div className="border border-slate-200 rounded-xl bg-white overflow-hidden">
      <button
        onClick={() => setExpanded(e => !e)}
        className="w-full flex items-center gap-3 px-4 py-3 hover:bg-slate-50 transition-colors text-left"
      >
        <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded border ${phaseColor}`}>
          {pod.ready ? "Ready" : pod.phase}
        </span>
        <span className="text-xs font-mono text-slate-700 flex-1 truncate">{pod.name}</span>
        <div className="flex items-center gap-4 shrink-0">
          <div className="flex items-center gap-1.5">
            <Cpu className="w-3 h-3 text-slate-400" />
            <span className="text-xs font-mono text-slate-600">{pod.cpu_m_total}m</span>
          </div>
          <div className="flex items-center gap-1.5">
            <MemoryStick className="w-3 h-3 text-slate-400" />
            <span className="text-xs font-mono text-slate-600">{pod.mem_mi_total} MiB</span>
          </div>
          <span className="text-xs text-slate-400">{fmtAge(pod.age_seconds)}</span>
          {expanded ? <ChevronUp className="w-3.5 h-3.5 text-slate-400" /> : <ChevronDown className="w-3.5 h-3.5 text-slate-400" />}
        </div>
      </button>

      {expanded && (
        <div className="border-t border-slate-100 px-4 py-3 bg-slate-50/50 space-y-3">
          <p className="text-[10px] text-slate-400 font-mono truncate">Node: {pod.node || "—"}</p>
          <table className="w-full text-xs">
            <thead>
              <tr className="text-[10px] text-slate-400 uppercase tracking-wide">
                <th className="text-left font-medium pb-1 pr-4">Container</th>
                <th className="text-left font-medium pb-1 pr-4">CPU (m)</th>
                <th className="text-left font-medium pb-1">Memory (MiB)</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {pod.containers.map(c => (
                <tr key={c.name} className="align-middle">
                  <td className="py-1.5 pr-4 font-mono text-slate-600 text-[11px]">{c.name}</td>
                  <td className="py-1.5 pr-4">
                    <UsageBar used={c.cpu_m} limit={c.cpu_limit_m} colorClass="bg-blue-400" />
                  </td>
                  <td className="py-1.5">
                    <UsageBar used={c.mem_mi} limit={c.mem_limit_mi} colorClass="bg-violet-400" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// Chart colour palette — one per workload (cycles if more than palette length)
const CHART_COLORS = [
  "#FF444F", "#3b82f6", "#10b981", "#f59e0b",
  "#8b5cf6", "#06b6d4", "#f43f5e", "#84cc16",
];

interface PoolStatus {
  name: string;
  configured: number;
  total: number;
  ready: number;
  in_use: number;
}
interface HistorySeries {
  [workload: string]: { ts: string; cpu_m: number; mem_mi: number; pods: number; ready: number }[];
}
interface PoolHistorySeries {
  [pool: string]: { ts: string; configured: number; total: number; ready: number; in_use: number }[];
}
interface HistoryData {
  series: HistorySeries; workloads: string[];
  pool_series: PoolHistorySeries; pools: string[];
  points: number;
}

function fmtTs(iso: string): string {
  const d = new Date(iso);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}

function WorkloadChart({
  title, dataKey, unit, color, data,
}: {
  title: string; dataKey: "cpu_m" | "mem_mi"; unit: string; color: string; data: { ts: string;[w: string]: number | string }[];
  workloads: string[];
}) {
  if (data.length < 2) {
    return (
      <div className="flex items-center justify-center h-32 text-xs text-slate-400">
        Collecting data… ({data.length} / 2 points)
      </div>
    );
  }
  return (
    <ResponsiveContainer width="100%" height={140}>
      <AreaChart data={data} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
        <defs>
          <linearGradient id={`grad-${dataKey}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={color} stopOpacity={0.25} />
            <stop offset="95%" stopColor={color} stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
        <XAxis dataKey="ts" tick={{ fontSize: 9, fill: "#94a3b8" }} tickLine={false} axisLine={false} interval="preserveStartEnd" tickFormatter={fmtTs} />
        <YAxis tick={{ fontSize: 9, fill: "#94a3b8" }} tickLine={false} axisLine={false} unit={unit} width={42} />
        <Tooltip
          contentStyle={{ fontSize: 11, borderRadius: 8, border: "1px solid #e2e8f0", boxShadow: "0 2px 8px rgba(0,0,0,.06)" }}
          labelFormatter={fmtTs}
          formatter={(v: number) => [`${v}${unit}`, title]}
        />
        <Area type="monotone" dataKey={dataKey} stroke={color} strokeWidth={1.5} fill={`url(#grad-${dataKey})`} dot={false} isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function MultiWorkloadChart({
  title, icon, dataKey, unit, chartData, workloads,
}: {
  title: string; icon: ReactNode; dataKey: "cpu_m" | "mem_mi"; unit: string;
  chartData: { ts: string;[w: string]: number | string }[]; workloads: string[];
}) {
  if (chartData.length < 2) {
    return (
      <div className="bg-white border border-slate-200 rounded-xl p-4">
        <div className="flex items-center gap-2 mb-3">
          {icon}
          <span className="text-xs font-semibold text-slate-700">{title}</span>
        </div>
        <div className="flex items-center justify-center h-32 text-xs text-slate-400">
          Collecting data… ({chartData.length} / 2 points)
        </div>
      </div>
    );
  }
  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4">
      <div className="flex items-center gap-2 mb-3">
        {icon}
        <span className="text-xs font-semibold text-slate-700">{title}</span>
        <span className="text-[10px] text-slate-400 ml-auto">{chartData.length} data points</span>
      </div>
      <ResponsiveContainer width="100%" height={160}>
        <AreaChart data={chartData} margin={{ top: 4, right: 4, left: -16, bottom: 0 }}>
          <defs>
            {workloads.map((w, i) => (
              <linearGradient key={w} id={`grad-${dataKey}-${i}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={CHART_COLORS[i % CHART_COLORS.length]} stopOpacity={0.2} />
                <stop offset="95%" stopColor={CHART_COLORS[i % CHART_COLORS.length]} stopOpacity={0.01} />
              </linearGradient>
            ))}
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
          <XAxis dataKey="ts" tick={{ fontSize: 9, fill: "#94a3b8" }} tickLine={false} axisLine={false}
            interval="preserveStartEnd" tickFormatter={fmtTs} />
          <YAxis tick={{ fontSize: 9, fill: "#94a3b8" }} tickLine={false} axisLine={false} unit={unit} width={44} />
          <Tooltip
            contentStyle={{ fontSize: 11, borderRadius: 8, border: "1px solid #e2e8f0", boxShadow: "0 2px 8px rgba(0,0,0,.06)" }}
            labelFormatter={fmtTs}
          />
          <Legend wrapperStyle={{ fontSize: 10, paddingTop: 4 }} />
          {workloads.map((w, i) => (
            <Area key={w} type="monotone" dataKey={w} name={w}
              stroke={CHART_COLORS[i % CHART_COLORS.length]}
              strokeWidth={1.5}
              fill={`url(#grad-${dataKey}-${i})`}
              dot={false}
              isAnimationActive={false}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function SandboxPoolsSection({
  pools, poolHistory, historyPools,
}: {
  pools: PoolStatus[];
  poolHistory: PoolHistorySeries;
  historyPools: string[];
}) {
  const poolLabel = (name: string) =>
    name === "python-executor-pool" ? "Agent Pool" :
      name === "lab-python-executor-pool" ? "Lab Pool" : name;

  const POOL_COLORS: Record<string, { available: string; in_use: string }> = {
    "python-executor-pool": { available: "#10b981", in_use: "#FF444F" },
    "lab-python-executor-pool": { available: "#3b82f6", in_use: "#f59e0b" },
  };
  const defaultColors = { available: "#94a3b8", in_use: "#64748b" };

  // All pool names (live ∪ history)
  const allPools = Array.from(new Set([...pools.map(p => p.name), ...historyPools]));

  // Combined timeline for the top "pods in use" line chart
  const allTs = new Set<string>();
  for (const pn of allPools) (poolHistory[pn] ?? []).forEach(pt => allTs.add(pt.ts));
  const sortedTs = [...allTs].sort();
  const combinedData = sortedTs.map(ts => {
    const row: Record<string, string | number> = { ts };
    for (const pn of allPools) {
      const pt = (poolHistory[pn] ?? []).find(p => p.ts === ts);
      row[pn] = pt?.in_use ?? 0;
    }
    return row;
  });

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4">
      <p className="text-xs font-semibold text-slate-700 mb-4 flex items-center gap-2">
        <Layers className="w-3.5 h-3.5 text-amber-500" /> Sandbox Warm Pool Utilisation
      </p>

      {/* ── Live status cards ── */}
      <div className="grid grid-cols-2 gap-3 mb-5">
        {allPools.map(poolName => {
          const live = pools.find(p => p.name === poolName);
          const colors = POOL_COLORS[poolName] ?? defaultColors;
          const pts = poolHistory[poolName] ?? [];
          const configured = live?.configured ?? pts.at(-1)?.configured ?? 10;
          const ready = live?.ready ?? 0;
          const inUse = live?.in_use ?? 0;
          const pctUsed = configured > 0 ? Math.round((inUse / configured) * 100) : 0;
          return (
            <div key={poolName} className="border border-slate-100 rounded-lg p-3 bg-slate-50">
              <div className="flex items-center justify-between mb-2">
                <div>
                  <p className="text-xs font-semibold text-slate-800">{poolLabel(poolName)}</p>
                  <p className="text-[10px] text-slate-400 font-mono">{poolName}</p>
                </div>
                <div className="text-right">
                  <p className="text-sm font-bold text-slate-900">{ready}<span className="text-[10px] font-normal text-slate-400"> / {configured}</span></p>
                  <p className="text-[10px] text-slate-400">available</p>
                </div>
              </div>
              <div className="flex items-center justify-between text-[10px] mb-1">
                <span className="flex items-center gap-1">
                  <span className="w-2 h-2 rounded-full inline-block" style={{ background: colors.in_use }} />
                  <span className="text-slate-600">{inUse} in use</span>
                </span>
                <span className={`font-semibold ${pctUsed > 70 ? "text-red-600" : pctUsed > 40 ? "text-amber-600" : "text-emerald-600"}`}>
                  {pctUsed}%
                </span>
              </div>
              <div className="w-full bg-slate-200 rounded-full h-1.5 overflow-hidden">
                <div className="h-1.5 rounded-full transition-all duration-500"
                  style={{ width: `${pctUsed}%`, background: pctUsed > 70 ? "#FF444F" : pctUsed > 40 ? "#f59e0b" : colors.in_use }} />
              </div>
              {pctUsed > 70 && <p className="text-[10px] text-red-600 mt-1">⚠ Consider scaling warm pool</p>}
            </div>
          );
        })}
      </div>

      {/* ── Line chart: pods in use over time ── */}
      <p className="text-[10px] text-slate-500 font-medium mb-1 flex items-center gap-1">
        Pods in use over time
        {combinedData.length < 2 && <span className="text-slate-400 font-normal ml-1">· collecting data ({combinedData.length}/2 points)</span>}
      </p>
      {combinedData.length >= 2 ? (
        <ResponsiveContainer width="100%" height={140}>
          <LineChart data={combinedData} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
            <XAxis dataKey="ts" tick={{ fontSize: 9, fill: "#94a3b8" }} tickLine={false} axisLine={false}
              interval="preserveStartEnd" tickFormatter={fmtTs} />
            <YAxis tick={{ fontSize: 9, fill: "#94a3b8" }} tickLine={false} axisLine={false}
              allowDecimals={false} width={22} />
            <Tooltip contentStyle={{ fontSize: 11, borderRadius: 8, border: "1px solid #e2e8f0" }}
              labelFormatter={fmtTs} formatter={(v: number, name: string) => [`${v} pods`, poolLabel(name)]} />
            <Legend wrapperStyle={{ fontSize: 10 }} formatter={poolLabel} />
            {allPools.map(pn => (
              <Line key={pn} type="monotone" dataKey={pn} name={pn}
                stroke={(POOL_COLORS[pn] ?? defaultColors).in_use}
                strokeWidth={2} dot={false} isAnimationActive={false} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      ) : (
        <div className="flex items-center justify-center h-24 rounded-lg bg-slate-50 border border-dashed border-slate-200">
          <div className="text-center">
            <Layers className="w-5 h-5 text-slate-300 mx-auto mb-1" />
            <p className="text-xs text-slate-400">Collecting data — refreshes every 15s</p>
          </div>
        </div>
      )}

      {/* ── Per-pool available-vs-in-use detail charts ── */}
      <div className="grid grid-cols-2 gap-3 mt-4">
        {allPools.map(poolName => {
          const pts = poolHistory[poolName] ?? [];
          const colors = POOL_COLORS[poolName] ?? defaultColors;
          const live = pools.find(p => p.name === poolName);
          const configured = live?.configured ?? pts.at(-1)?.configured ?? 10;
          const chartData = pts.map(p => ({ ts: p.ts, available: p.ready, in_use: p.in_use }));
          if (chartData.length < 2) return null;
          return (
            <div key={poolName}>
              <p className="text-[10px] text-slate-400 mb-1">{poolLabel(poolName)} — available vs in-use</p>
              <ResponsiveContainer width="100%" height={110}>
                <LineChart data={chartData} margin={{ top: 2, right: 4, left: -22, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                  <XAxis dataKey="ts" tick={{ fontSize: 8, fill: "#94a3b8" }} tickLine={false} axisLine={false}
                    interval="preserveStartEnd" tickFormatter={fmtTs} />
                  <YAxis tick={{ fontSize: 8, fill: "#94a3b8" }} tickLine={false} axisLine={false}
                    domain={[0, configured]} allowDecimals={false} width={20} />
                  <ReferenceLine y={configured} stroke="#e2e8f0" strokeDasharray="4 2" />
                  <Tooltip contentStyle={{ fontSize: 10, borderRadius: 6, border: "1px solid #e2e8f0" }}
                    labelFormatter={fmtTs} />
                  <Line type="monotone" dataKey="available" name="Available" stroke={colors.available}
                    strokeWidth={1.5} dot={false} isAnimationActive={false} />
                  <Line type="monotone" dataKey="in_use" name="In Use" stroke={colors.in_use}
                    strokeWidth={2} dot={false} isAnimationActive={false} />
                  <Legend wrapperStyle={{ fontSize: 9 }} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function InfraTab() {
  const [data, setData] = useState<InfraData | null>(null);
  const [history, setHistory] = useState<HistoryData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [countdown, setCountdown] = useState(15);
  const [view, setView] = useState<"charts" | "pods">("charts");

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [r1, r2] = await Promise.all([
        apiFetch("/api/admin/infra"),
        apiFetch("/api/admin/infra/history"),
      ]);
      if (!r1.ok) { const b = await r1.json().catch(() => ({})); throw new Error((b as { detail?: string }).detail ?? `HTTP ${r1.status}`); }
      const [j1, j2] = await Promise.all([r1.json(), r2.ok ? r2.json() : null]);
      setData(j1 as InfraData);
      if (j2) setHistory(j2 as HistoryData);
      setCountdown(15);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load metrics.");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const t = setInterval(() => setCountdown(c => { if (c <= 1) { void load(); return 15; } return c - 1; }), 1000);
    return () => clearInterval(t);
  }, [load]);

  // Pool data from live snapshot
  const livePools: PoolStatus[] = data?.pools ?? [];

  // Build flat chart-ready data for recharts (one row per timestamp)
  const { cpuChartData, memChartData } = useMemo(() => {
    if (!history || history.workloads.length === 0) return { cpuChartData: [], memChartData: [] };
    // Align timestamps across all workloads
    const allTs = new Set<string>();
    for (const pts of Object.values(history.series)) pts.forEach(p => allTs.add(p.ts));
    const sorted = [...allTs].sort();

    const cpuByTs: Record<string, Record<string, number>> = {};
    const memByTs: Record<string, Record<string, number>> = {};
    for (const ts of sorted) { cpuByTs[ts] = {}; memByTs[ts] = {}; }
    for (const [w, pts] of Object.entries(history.series)) {
      for (const p of pts) {
        if (cpuByTs[p.ts]) cpuByTs[p.ts][w] = p.cpu_m;
        if (memByTs[p.ts]) memByTs[p.ts][w] = p.mem_mi;
      }
    }
    const cpuChartData = sorted.map(ts => ({ ts, ...cpuByTs[ts] }));
    const memChartData = sorted.map(ts => ({ ts, ...memByTs[ts] }));
    return { cpuChartData, memChartData };
  }, [history]);

  const groups = useMemo(() => {
    if (!data) return [];
    const map = new Map<string, PodMetric[]>();
    for (const pod of data.pods) {
      if (!map.has(pod.workload)) map.set(pod.workload, []);
      map.get(pod.workload)!.push(pod);
    }
    return [...map.entries()].sort(([a], [b]) => {
      const ai = WORKLOAD_ORDER.indexOf(a); const bi = WORKLOAD_ORDER.indexOf(b);
      if (ai === -1 && bi === -1) return a.localeCompare(b);
      if (ai === -1) return 1; if (bi === -1) return -1; return ai - bi;
    });
  }, [data]);

  if (error && !data) return (
    <div className="flex flex-col items-center justify-center h-full gap-3">
      <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-5 py-4 max-w-md text-center">
        <p className="font-semibold mb-1">Failed to load metrics</p>
        <p className="text-xs opacity-80">{error}</p>
      </div>
      <button onClick={() => void load()} className="text-xs text-[#FF444F] hover:underline">Try again</button>
    </div>
  );
  if (loading && !data) return (
    <div className="flex flex-col items-center justify-center h-full gap-3 text-slate-400">
      <Loader2 className="w-6 h-6 animate-spin text-[#FF444F]" />
      <span className="text-sm">Fetching pod metrics…</span>
    </div>
  );

  const totalPods = data?.pods.length ?? 0;
  const readyPods = data?.pods.filter(p => p.ready).length ?? 0;
  const totalCpu = data?.pods.reduce((s, p) => s + p.cpu_m_total, 0) ?? 0;
  const totalMem = data?.pods.reduce((s, p) => s + p.mem_mi_total, 0) ?? 0;
  const workloads = history?.workloads ?? [];
  const historyPools = history?.pools ?? [];
  const poolHistory = history?.pool_series ?? {};
  const totalSandboxReady = livePools.reduce((s, p) => s + p.ready, 0);
  const totalSandboxConfig = livePools.reduce((s, p) => s + p.configured, 0);

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div>
          <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
            <Activity className="w-4 h-4 text-[#FF444F]" /> Infrastructure
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">Live metrics — namespace <span className="font-mono">{"{SANDBOX_NAMESPACE}"}</span></p>
        </div>
        <div className="flex items-center gap-3">
          {/* View toggle */}
          <div className="flex items-center bg-slate-100 rounded-lg p-0.5 text-xs font-medium">
            {(["charts", "pods"] as const).map(v => (
              <button key={v} onClick={() => setView(v)}
                className={`px-3 py-1 rounded-md transition-colors capitalize ${view === v ? "bg-white text-slate-800 shadow-sm" : "text-slate-500 hover:text-slate-700"}`}>
                {v}
              </button>
            ))}
          </div>
          {data && <span className="text-[11px] text-slate-400">Refreshing in <span className="font-mono text-slate-600">{countdown}s</span></span>}
          <button onClick={() => void load()} disabled={loading}
            className="flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-700 border border-slate-200 px-3 py-1.5 rounded-lg hover:bg-slate-50 transition-colors disabled:opacity-50">
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      {/* Summary cards */}
      {data && (
        <div className="grid grid-cols-4 gap-3 mb-4 shrink-0">
          {[
            { label: "Pods", value: `${readyPods} / ${totalPods}`, sub: "ready", icon: <Activity className="w-4 h-4 text-emerald-500" /> },
            { label: "Total CPU", value: `${totalCpu}m`, sub: "millicores in use", icon: <Cpu className="w-4 h-4 text-blue-500" /> },
            { label: "Total Memory", value: `${totalMem} MiB`, sub: "in use", icon: <MemoryStick className="w-4 h-4 text-violet-500" /> },
            { label: "Sandbox Pools", value: `${totalSandboxReady} / ${totalSandboxConfig}`, sub: "pods available", icon: <Layers className="w-4 h-4 text-amber-500" /> },
          ].map(({ label, value, sub, icon }) => (
            <div key={label} className="bg-white border border-slate-200 rounded-xl px-4 py-3 flex items-center gap-3">
              <div className="shrink-0">{icon}</div>
              <div>
                <p className="text-base font-bold text-slate-900 leading-none">{value}</p>
                <p className="text-[10px] text-slate-400 mt-0.5">{label} · {sub}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Content area */}
      <div className="flex-1 overflow-y-auto pr-1">
        {data?.error && (
          <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 mb-3">
            ⚠ {data.error}
          </div>
        )}

        {/* ── CHARTS VIEW ── */}
        {view === "charts" && (
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <MultiWorkloadChart
                title="CPU Usage (millicores)" icon={<Cpu className="w-3.5 h-3.5 text-blue-500" />}
                dataKey="cpu_m" unit="m" chartData={cpuChartData} workloads={workloads}
              />
              <MultiWorkloadChart
                title="Memory Usage (MiB)" icon={<MemoryStick className="w-3.5 h-3.5 text-violet-500" />}
                dataKey="mem_mi" unit=" MiB" chartData={memChartData} workloads={workloads}
              />
            </div>

            {/* Sandbox pool utilisation */}
            {(historyPools.length > 0 || livePools.length > 0) && (
              <SandboxPoolsSection
                pools={livePools}
                poolHistory={poolHistory}
                historyPools={historyPools.length > 0 ? historyPools : livePools.map(p => p.name)}
              />
            )}

            {/* Per-workload sparklines */}
            {workloads.length > 0 && (
              <div>
                <p className="text-xs font-semibold text-slate-600 mb-3">Per-workload breakdown</p>
                <div className="grid grid-cols-3 gap-3">
                  {workloads.map((w, i) => {
                    const color = CHART_COLORS[i % CHART_COLORS.length];
                    const pts = history!.series[w] ?? [];
                    const sparkData = pts.map(p => ({ ts: p.ts, cpu_m: p.cpu_m, mem_mi: p.mem_mi }));
                    const lastCpu = pts.at(-1)?.cpu_m ?? 0;
                    const lastMem = pts.at(-1)?.mem_mi ?? 0;
                    return (
                      <div key={w} className="bg-white border border-slate-200 rounded-xl p-3">
                        <div className="flex items-center justify-between mb-1">
                          <span className="text-xs font-semibold text-slate-700 capitalize truncate">{w}</span>
                          <span className="text-[10px] font-mono text-slate-400">{lastCpu}m · {lastMem}Mi</span>
                        </div>
                        <WorkloadChart title={w} dataKey="cpu_m" unit="m" color={color} data={sparkData} workloads={[w]} />
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        )}

        {/* ── PODS VIEW ── */}
        {view === "pods" && (
          <div className="space-y-5">
            {groups.map(([workload, pods]) => (
              <div key={workload}>
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-xs font-semibold text-slate-700 capitalize">{workload}</span>
                  <span className="text-[10px] text-slate-400 bg-slate-100 rounded-full px-2 py-0.5">{pods.length} pod{pods.length !== 1 ? "s" : ""}</span>
                  <div className="flex-1 h-px bg-slate-100" />
                  <span className="text-[10px] text-slate-400 font-mono">{pods.reduce((s, p) => s + p.cpu_m_total, 0)}m CPU · {pods.reduce((s, p) => s + p.mem_mi_total, 0)} MiB</span>
                </div>
                <div className="space-y-2">
                  {pods.map(pod => <PodCard key={pod.name} pod={pod} />)}
                </div>
              </div>
            ))}
            {data && groups.length === 0 && (
              <div className="text-center text-sm text-slate-400 pt-12">No pods found in namespace.</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// MAIN ADMIN PAGE
// ═══════════════════════════════════════════════════════════════════════════════
// USAGE TAB
// ═══════════════════════════════════════════════════════════════════════════════

interface UsageAgg {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  total_tokens: number;
  cost_usd: number;
  cache_savings_usd: number;
  calls: number;
}
interface UsageSourceEntry extends UsageAgg { label: string }
interface UsageAgentEntry extends UsageAgg { agent_id: string; agent_name: string }
interface UsageUserEntry extends UsageAgg { user_email: string }
interface UsageDayEntry extends UsageAgg { date: string }
interface UsageData {
  days: number;
  total: UsageAgg;
  by_source: Record<string, UsageSourceEntry>;
  by_model: Record<string, UsageAgg>;
  by_agent: UsageAgentEntry[];
  by_user: UsageUserEntry[];
  by_day: UsageDayEntry[];
}

const SOURCE_COLORS: Record<string, string> = {
  "Hub Chat": "#3b82f6",
  "Slack Agents": "#FF444F",
  "Automations": "#10b981",
  "Lab Builder": "#8b5cf6",
  "Memory Mining": "#f59e0b",
  "Skill Create": "#6366f1",
  "Skill Publish": "#ec4899",
  "Agent-to-Agent": "#14b8a6",
};

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function UsageTab() {
  const [data, setData] = useState<UsageData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(30);

  const load = useCallback(async (d: number) => {
    setLoading(true); setError(null);
    try {
      const res = await apiFetch(`/api/admin/usage?days=${d}`);
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error((b as { detail?: string }).detail ?? `HTTP ${res.status}`); }
      setData(await res.json() as UsageData);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load usage data.");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { void load(days); }, [days, load]);

  if (error && !data) return (
    <div className="flex flex-col items-center justify-center h-full gap-3">
      <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-5 py-4 max-w-md text-center">
        <p className="font-semibold mb-1">Failed to load usage data</p>
        <p className="text-xs opacity-80">{error}</p>
      </div>
      <button onClick={() => void load(days)} className="text-xs text-[#FF444F] hover:underline">Try again</button>
    </div>
  );

  if (loading && !data) return (
    <div className="flex flex-col items-center justify-center h-full gap-3 text-slate-400">
      <Loader2 className="w-6 h-6 animate-spin text-[#FF444F]" />
      <span className="text-sm">Loading token usage…</span>
    </div>
  );

  const t = data?.total;

  // Build daily chart data
  const dailyData = (data?.by_day ?? []).map(d => ({
    date: d.date.slice(5),  // MM-DD
    input: d.input_tokens,
    output: d.output_tokens,
    cost: d.cost_usd,
  }));

  // Source bar chart data
  const sourceData = Object.entries(data?.by_source ?? {}).map(([, v]) => ({
    name: v.label,
    tokens: v.total_tokens,
    cost: v.cost_usd,
    calls: v.calls,
  })).sort((a, b) => b.tokens - a.tokens);

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div>
          <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
            <TrendingUp className="w-4 h-4 text-[#FF444F]" /> Token Usage
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">LLM tokens consumed across all surfaces</p>
        </div>
        <div className="flex items-center gap-2">
          {/* Period selector */}
          <div className="flex items-center bg-slate-100 rounded-lg p-0.5 text-xs font-medium">
            {([7, 30, 90] as const).map(d => (
              <button key={d} onClick={() => setDays(d)}
                className={`px-3 py-1 rounded-md transition-colors ${days === d ? "bg-white text-slate-800 shadow-sm" : "text-slate-500 hover:text-slate-700"}`}>
                {d}d
              </button>
            ))}
          </div>
          <button onClick={() => void load(days)} disabled={loading}
            className="flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-700 border border-slate-200 px-3 py-1.5 rounded-lg hover:bg-slate-50 transition-colors disabled:opacity-50">
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto pr-1 space-y-4">
        {/* Summary cards */}
        {t && (
          <div className="grid grid-cols-4 gap-3 shrink-0">
            {[
              { label: "Total Tokens", value: fmtTokens(t.total_tokens), sub: `${fmtTokens(t.input_tokens)} in · ${fmtTokens(t.output_tokens)} out`, icon: <BarChart2 className="w-4 h-4 text-blue-500" /> },
              { label: "Estimated Cost", value: `$${t.cost_usd.toFixed(2)}`, sub: `last ${days} days`, icon: <DollarSign className="w-4 h-4 text-emerald-500" /> },
              { label: "LLM Calls", value: t.calls.toLocaleString(), sub: "total invocations", icon: <Zap className="w-4 h-4 text-amber-500" /> },
              { label: "Cache Savings", value: `$${t.cache_savings_usd.toFixed(2)}`, sub: `${fmtTokens(t.cache_read_tokens)} tokens served from cache`, icon: <TrendingUp className="w-4 h-4 text-violet-500" /> },
            ].map(({ label, value, sub, icon }) => (
              <div key={label} className="bg-white border border-slate-200 rounded-xl px-4 py-3 flex items-center gap-3">
                <div className="shrink-0">{icon}</div>
                <div>
                  <p className="text-base font-bold text-slate-900 leading-none">{value}</p>
                  <p className="text-[10px] text-slate-400 mt-0.5">{label} · {sub}</p>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Daily token trend */}
        {dailyData.length >= 2 && (
          <div className="bg-white border border-slate-200 rounded-xl p-4">
            <p className="text-xs font-semibold text-slate-700 mb-3 flex items-center gap-2">
              <TrendingUp className="w-3.5 h-3.5 text-blue-500" /> Daily Token Consumption
            </p>
            <ResponsiveContainer width="100%" height={160}>
              <LineChart data={dailyData} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
                <XAxis dataKey="date" tick={{ fontSize: 9, fill: "#94a3b8" }} tickLine={false} axisLine={false} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 9, fill: "#94a3b8" }} tickLine={false} axisLine={false} tickFormatter={fmtTokens} width={36} />
                <Tooltip contentStyle={{ fontSize: 11, borderRadius: 8, border: "1px solid #e2e8f0" }}
                  formatter={(v: number, name: string) => [fmtTokens(v), name === "input" ? "Input" : "Output"]} />
                <Legend wrapperStyle={{ fontSize: 10 }} />
                <Line type="monotone" dataKey="input" name="input" stroke="#3b82f6" strokeWidth={2} dot={false} isAnimationActive={false} />
                <Line type="monotone" dataKey="output" name="output" stroke="#FF444F" strokeWidth={2} dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}

        <div className="grid grid-cols-2 gap-4">
          {/* By source */}
          {sourceData.length > 0 && (
            <div className="bg-white border border-slate-200 rounded-xl p-4">
              <p className="text-xs font-semibold text-slate-700 mb-3 flex items-center gap-2">
                <BarChart2 className="w-3.5 h-3.5 text-slate-400" /> Tokens by Surface
              </p>
              <div className="space-y-2.5">
                {sourceData.map(s => {
                  const total = sourceData.reduce((a, x) => a + x.tokens, 0);
                  const pct = total > 0 ? (s.tokens / total) * 100 : 0;
                  const color = SOURCE_COLORS[s.name] ?? "#94a3b8";
                  return (
                    <div key={s.name}>
                      <div className="flex items-center justify-between text-[10px] mb-0.5">
                        <span className="flex items-center gap-1.5">
                          <span className="w-2 h-2 rounded-full inline-block" style={{ background: color }} />
                          <span className="text-slate-700 font-medium">{s.name}</span>
                        </span>
                        <span className="text-slate-500 font-mono">{fmtTokens(s.tokens)} · ${s.cost.toFixed(3)}</span>
                      </div>
                      <div className="w-full bg-slate-100 rounded-full h-1.5 overflow-hidden">
                        <div className="h-1.5 rounded-full" style={{ width: `${pct}%`, background: color }} />
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Top agents by cost */}
          {(data?.by_agent?.length ?? 0) > 0 && (
            <div className="bg-white border border-slate-200 rounded-xl p-4">
              <p className="text-xs font-semibold text-slate-700 mb-3 flex items-center gap-2">
                <Zap className="w-3.5 h-3.5 text-amber-500" /> Top Agents by Cost
              </p>
              <div className="space-y-2">
                {(data!.by_agent.slice(0, 8)).map((a, i) => (
                  <div key={a.agent_id} className="flex items-center justify-between text-[11px]">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="text-[10px] text-slate-400 w-4 text-right shrink-0">{i + 1}</span>
                      <span className="text-slate-700 font-medium truncate">{a.agent_name}</span>
                    </div>
                    <div className="flex items-center gap-3 shrink-0">
                      <span className="text-slate-400 font-mono text-[10px]">{fmtTokens(a.total_tokens)}</span>
                      <span className="text-emerald-700 font-semibold font-mono">${a.cost_usd.toFixed(3)}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Top users by cost */}
        {(data?.by_user?.length ?? 0) > 0 && (
          <div className="bg-white border border-slate-200 rounded-xl p-4">
            <p className="text-xs font-semibold text-slate-700 mb-3 flex items-center gap-2">
              <Users className="w-3.5 h-3.5 text-blue-500" /> Token Usage by User
            </p>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-[10px] text-slate-400 border-b border-slate-100">
                    <th className="text-left pb-1.5 font-medium">User</th>
                    <th className="text-right pb-1.5 font-medium">Calls</th>
                    <th className="text-right pb-1.5 font-medium">Input</th>
                    <th className="text-right pb-1.5 font-medium">Output</th>
                    <th className="text-right pb-1.5 font-medium">Total</th>
                    <th className="text-right pb-1.5 font-medium">Cost</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-50">
                  {data!.by_user.map((u, i) => (
                    <tr key={u.user_email} className="text-slate-700">
                      <td className="py-1.5">
                        <div className="flex items-center gap-2 min-w-0">
                          <span className="text-[10px] text-slate-400 w-4 text-right shrink-0">{i + 1}</span>
                          <span className={`font-medium truncate ${u.user_email === "(system)" ? "text-slate-400 italic" : "text-slate-700"}`}>
                            {u.user_email}
                          </span>
                        </div>
                      </td>
                      <td className="py-1.5 text-right">{u.calls.toLocaleString()}</td>
                      <td className="py-1.5 text-right font-mono">{fmtTokens(u.input_tokens)}</td>
                      <td className="py-1.5 text-right font-mono">{fmtTokens(u.output_tokens)}</td>
                      <td className="py-1.5 text-right font-mono">{fmtTokens(u.total_tokens)}</td>
                      <td className="py-1.5 text-right font-semibold text-emerald-700">${u.cost_usd.toFixed(3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Model breakdown */}
        {Object.keys(data?.by_model ?? {}).length > 0 && (
          <div className="bg-white border border-slate-200 rounded-xl p-4">
            <p className="text-xs font-semibold text-slate-700 mb-3">Model Breakdown</p>
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-[10px] text-slate-400 border-b border-slate-100">
                    <th className="text-left pb-1.5 font-medium">Model</th>
                    <th className="text-right pb-1.5 font-medium">Calls</th>
                    <th className="text-right pb-1.5 font-medium">Input</th>
                    <th className="text-right pb-1.5 font-medium">Output</th>
                    <th className="text-right pb-1.5 font-medium">Cache Read</th>
                    <th className="text-right pb-1.5 font-medium">Cost</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-50">
                  {Object.entries(data!.by_model).map(([mdl, v]) => (
                    <tr key={mdl} className="text-slate-700">
                      <td className="py-1.5 font-mono text-[10px] text-slate-500">{mdl}</td>
                      <td className="py-1.5 text-right">{v.calls.toLocaleString()}</td>
                      <td className="py-1.5 text-right font-mono">{fmtTokens(v.input_tokens)}</td>
                      <td className="py-1.5 text-right font-mono">{fmtTokens(v.output_tokens)}</td>
                      <td className="py-1.5 text-right font-mono text-violet-600">{fmtTokens(v.cache_read_tokens)}</td>
                      <td className="py-1.5 text-right font-semibold text-emerald-700">${v.cost_usd.toFixed(3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Empty state */}
        {data && t && t.calls === 0 && (
          <div className="flex flex-col items-center justify-center h-48 gap-3">
            <TrendingUp className="w-8 h-8 text-slate-200" />
            <p className="text-sm text-slate-400">No token usage recorded in the last {days} days.</p>
            <p className="text-xs text-slate-400">Data will appear after the first LLM call is made.</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════

type TopTab = "access" | "agent-access" | "users" | "departments" | "skills" | "infra" | "usage" | "budgets" | "feedback" | "analytics" | "slack-scopes" | "systems";

export default function AdminPage() {
  const branding = useBranding();
  const appName = branding.app_name;
  const [tab, setTab] = useState<TopTab>("access");
  const [dirData, setDirData] = useState<DirectoryData | null>(null);
  const [dirLoading, setDirLoading] = useState(false);
  const [dirError, setDirError] = useState<string | null>(null);

  // Lazy-load directory data only when a directory tab is first visited
  const loadDirectory = useCallback(async () => {
    setDirLoading(true); setDirError(null);
    try {
      const res = await apiFetch("/api/admin/directory");
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { detail?: string }).detail ?? `HTTP ${res.status}`);
      }
      setDirData(await res.json() as DirectoryData);
    } catch (e: unknown) {
      setDirError(e instanceof Error ? e.message : "Failed to load directory.");
    } finally {
      setDirLoading(false);
    }
  }, []);

  useEffect(() => {
    if ((tab === "users" || tab === "departments" || tab === "skills") && !dirData && !dirLoading) {
      void loadDirectory();
    }
  }, [tab, dirData, dirLoading, loadDirectory]);

  const TOP_TABS: { id: TopTab; label: string; icon: ReactNode }[] = [
    { id: "access",       label: "Skill Access",     icon: <Lock className="w-3.5 h-3.5" /> },
    { id: "agent-access", label: "Agent Access",     icon: <Bot className="w-3.5 h-3.5" /> },
    { id: "users",        label: "Users",            icon: <Users className="w-3.5 h-3.5" /> },
    { id: "departments",  label: "Departments",      icon: <Building2 className="w-3.5 h-3.5" /> },
    { id: "skills",       label: "Skills Directory", icon: <Layers className="w-3.5 h-3.5" /> },
    { id: "infra",        label: "Infrastructure",   icon: <Activity className="w-3.5 h-3.5" /> },
    { id: "usage",        label: "Usage",            icon: <TrendingUp className="w-3.5 h-3.5" /> },
    { id: "budgets",      label: "Budgets",          icon: <Wallet className="w-3.5 h-3.5" /> },
    { id: "feedback",     label: "Feedback",         icon: <MessageSquare className="w-3.5 h-3.5" /> },
    { id: "analytics",    label: "Analytics",        icon: <BarChart2 className="w-3.5 h-3.5" /> },
    { id: "slack-scopes", label: "Slack Scopes",     icon: <ShieldCheck className="w-3.5 h-3.5" /> },
    { id: "systems",      label: "Systems",          icon: <Server className="w-3.5 h-3.5" /> },
  ];

  const dirContent = () => {
    if (dirError && !dirData) return (
      <div className="flex flex-col items-center justify-center h-full gap-3">
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-5 py-4 max-w-md text-center">
          <p className="font-semibold mb-1">Failed to load directory</p>
          <p className="text-xs opacity-80">{dirError}</p>
        </div>
        <button onClick={() => void loadDirectory()} className="text-xs text-[#FF444F] hover:underline">Try again</button>
      </div>
    );
    if (dirLoading || !dirData) return (
      <div className="flex flex-col items-center justify-center h-full gap-3 text-slate-400">
        <Loader2 className="w-6 h-6 animate-spin text-[#FF444F]" />
        <span className="text-sm">Loading employee directory…</span>
      </div>
    );
    return null;
  };

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-3 bg-white border-b border-slate-200 shrink-0">
        <div className="flex items-center gap-2">
          <ShieldOff className="w-4 h-4 text-[#FF444F]" />
          <span className="text-sm font-semibold text-slate-700">
            Admin <span className="text-slate-400 font-normal">— Skills Access Control</span>
          </span>
          {tab !== "access" && dirData && (
            <span className="ml-1 text-xs text-slate-400 bg-slate-100 rounded-full px-2 py-0.5">
              {dirData.total} employees · {Object.keys(dirData.skill_meta).length} skills
            </span>
          )}
        </div>
        {(tab === "users" || tab === "departments" || tab === "skills") && dirData && (
          <button onClick={() => void loadDirectory()} disabled={dirLoading}
            className="flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-700 border border-slate-200 px-3 py-1.5 rounded-lg hover:bg-slate-50 transition-colors disabled:opacity-50">
            <RefreshCw className={`w-3.5 h-3.5 ${dirLoading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        )}
        {/* Infra tab has its own refresh button inside the tab */}
      </div>

      {/* Top tab bar */}
      <div className="flex items-center gap-1 px-6 py-2 bg-white border-b border-slate-100 shrink-0">
        {TOP_TABS.map(({ id, label, icon }) => (
          <button key={id} onClick={() => setTab(id)}
            className={`flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-lg transition-colors ${tab === id ? "bg-[#FF444F] text-white" : "text-slate-500 hover:text-slate-700 hover:bg-slate-100"
              }`}>
            {icon} {label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden px-6 py-4">
        {tab === "access" && <AccessRequestsTab />}

        {tab === "agent-access" && <AgentAccessTab />}

        {tab === "users" && (
          dirContent() ?? <UsersTab data={dirData!} />
        )}
        {tab === "departments" && (
          dirContent() ?? <DepartmentsTab data={dirData!} />
        )}
        {tab === "skills" && (
          dirContent() ?? (
            <div>
              <div className="mb-4">
                <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
                  <Layers className="w-4 h-4 text-[#FF444F]" /> Skills Access Directory
                </h2>
                <p className="text-xs text-slate-500 mt-0.5">
                  All skills on {appName} — who has access, at what level, and from which department.
                </p>
              </div>
              <div className="h-[calc(100%-60px)] overflow-hidden">
                <SkillsDirectoryTab data={dirData!} />
              </div>
            </div>
          )
        )}

        {tab === "infra"      && <InfraTab />}
        {tab === "usage"      && <UsageTab />}
        {tab === "budgets"    && <BudgetsTab />}
        {tab === "analytics"  && <AnalyticsTab />}
        {tab === "feedback" && (
          <div className="h-full overflow-hidden">
            <AdminFeedbackList />
          </div>
        )}
        {tab === "slack-scopes" && <SlackScopesTab />}
        {tab === "systems" && <SystemsTab />}
      </div>
    </div>
  );
}

// ── Agent Access Tab ──────────────────────────────────────────────────────────

interface AgentGrant {
  id: string;
  agent_id: string;
  user_email: string;
  user_name: string | null;
  status: string;
  justification: string | null;
  granted_by_email: string | null;
  decided_at: string | null;
  deny_reason: string | null;
  expires_at: string | null;
  created_at: string;
}

interface AgentInfo {
  id: string;
  name: string;
  description: string | null;
}

function AgentAccessTab() {
  const [grants, setGrants] = useState<AgentGrant[]>([]);
  const [pendingOnly, setPendingOnly] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [agentMap, setAgentMap] = useState<Record<string, AgentInfo>>({});
  const [directGrantModal, setDirectGrantModal] = useState<{ agentId: string; agentName: string } | null>(null);
  const [directEmail, setDirectEmail] = useState("");
  const [directDays, setDirectDays] = useState<string>("");
  const [directSubmitting, setDirectSubmitting] = useState(false);
  const [directError, setDirectError] = useState<string | null>(null);

  const loadAll = async () => {
    setLoading(true);
    setError(null);
    try {
      const [grantsRes, agentsRes] = await Promise.all([
        apiFetch("/api/admin/agents/access/grants"),
        apiFetch("/api/agents"),
      ]);
      if (!grantsRes.ok) throw new Error(`Grants fetch failed (${grantsRes.status})`);
      if (!agentsRes.ok) throw new Error(`Agents fetch failed (${agentsRes.status})`);
      const grantsData = await grantsRes.json();
      const agentsData: AgentInfo[] = await agentsRes.json();
      setGrants(grantsData.grants ?? []);
      const map: Record<string, AgentInfo> = {};
      agentsData.forEach(a => { map[a.id] = a; });
      setAgentMap(map);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load agent access data.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void loadAll(); }, []);

  const handleApprove = async (grant: AgentGrant) => {
    try {
      const res = await apiFetch(`/api/admin/agents/${grant.agent_id}/access/${grant.id}/approve`, { method: "POST" });
      if (!res.ok) throw new Error(`Failed (${res.status})`);
      await loadAll();
    } catch (e) {
      alert(e instanceof Error ? e.message : "Failed to approve.");
    }
  };

  const handleDeny = async (grant: AgentGrant) => {
    const reason = prompt("Deny reason (optional):");
    if (reason === null) return;
    try {
      const res = await apiFetch(`/api/admin/agents/${grant.agent_id}/access/${grant.id}/deny`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ deny_reason: reason }),
      });
      if (!res.ok) throw new Error(`Failed (${res.status})`);
      await loadAll();
    } catch (e) {
      alert(e instanceof Error ? e.message : "Failed to deny.");
    }
  };

  const handleRevoke = async (grant: AgentGrant) => {
    const reason = prompt("Revoke reason (optional):");
    if (reason === null) return;
    try {
      const res = await apiFetch(`/api/admin/agents/${grant.agent_id}/access/${grant.id}/revoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revoke_reason: reason }),
      });
      if (!res.ok) throw new Error(`Failed (${res.status})`);
      await loadAll();
    } catch (e) {
      alert(e instanceof Error ? e.message : "Failed to revoke.");
    }
  };

  const handleDirectGrant = async () => {
    if (!directGrantModal) return;
    const emails = directEmail
      .split(",")
      .map(e => e.trim().toLowerCase())
      .filter(Boolean);
    if (emails.length === 0) {
      setDirectError("Enter at least one email address.");
      return;
    }
    const unique = [...new Set(emails)];
    setDirectSubmitting(true);
    setDirectError(null);
    try {
      const res = await apiFetch(`/api/admin/agents/${directGrantModal.agentId}/access/grant`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_emails: unique,
          expires_in_days: directDays ? parseInt(directDays, 10) : null,
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
          : detail?.message ?? `Failed (${res.status})`;
        throw new Error(msg);
      }
      if (body.failed && body.failed > 0 && body.errors?.length) {
        const summary = body.errors.map(e => `${e.email}: ${e.error}`).join("; ");
        setDirectError(
          `Granted ${body.granted ?? 0}, failed ${body.failed}: ${summary}`,
        );
        await loadAll();
        return;
      }
      setDirectGrantModal(null);
      setDirectEmail("");
      setDirectDays("");
      await loadAll();
    } catch (e) {
      setDirectError(e instanceof Error ? e.message : "Failed to grant access.");
    } finally {
      setDirectSubmitting(false);
    }
  };

  const displayed = pendingOnly ? grants.filter(g => g.status === "pending") : grants;

  const statusBadge = (s: string) => {
    const styles: Record<string, string> = {
      approved: "bg-emerald-50 text-emerald-700 border-emerald-200",
      pending: "bg-amber-50 text-amber-700 border-amber-200",
      denied: "bg-red-50 text-red-700 border-red-200",
      revoked: "bg-slate-50 text-slate-500 border-slate-200",
    };
    return (
      <span className={`inline-flex text-[11px] font-medium border rounded-full px-2 py-0.5 ${styles[s] ?? "bg-slate-50 text-slate-500 border-slate-200"}`}>
        {s}
      </span>
    );
  };

  if (loading) return (
    <div className="flex items-center justify-center py-16">
      <Loader2 className="w-5 h-5 animate-spin text-slate-400" />
    </div>
  );

  if (error) return (
    <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-4 py-3">{error}</div>
  );

  const pendingCount = grants.filter(g => g.status === "pending").length;

  return (
    <div className="h-full overflow-y-auto space-y-5">
      {/* Header row */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
            <Bot className="w-4 h-4 text-[#FF444F]" /> Agent Access Management
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Review access requests and grant or revoke individual access to agents.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setPendingOnly(p => !p)}
            className={`text-xs px-3 py-1.5 rounded-lg border font-medium transition-colors ${pendingOnly
                ? "bg-amber-500 text-white border-amber-500"
                : "bg-white text-slate-600 border-slate-200 hover:bg-slate-50"
              }`}
          >
            Pending {pendingCount > 0 && `(${pendingCount})`}
          </button>
          <button
            onClick={() => void loadAll()}
            className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-900 transition-colors border border-slate-200 rounded-lg px-2.5 py-1.5"
          >
            <RefreshCw className="w-3.5 h-3.5" /> Refresh
          </button>
          {/* Direct grant — pick an agent from the loaded map */}
          <select
            className="text-xs border border-slate-200 rounded-lg px-2.5 py-1.5 bg-white text-slate-600 focus:outline-none"
            value=""
            onChange={e => {
              const agentId = e.target.value;
              if (!agentId) return;
              setDirectGrantModal({ agentId, agentName: agentMap[agentId]?.name ?? agentId });
              e.target.value = "";
            }}
          >
            <option value="">Grant direct access…</option>
            {Object.values(agentMap).map(a => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </select>
        </div>
      </div>

      {displayed.length === 0 ? (
        <div className="text-center py-12 text-slate-400 text-sm">
          {pendingOnly ? "No pending access requests." : "No agent access grants yet."}
        </div>
      ) : (
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
          <table className="w-full text-xs">
            <thead className="bg-slate-50 border-b border-slate-200">
              <tr>
                {["Agent", "User", "Status", "Requested", "Expires", "Actions"].map(h => (
                  <th key={h} className="text-left text-[11px] font-semibold text-slate-500 uppercase tracking-wider px-4 py-2.5">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {displayed.map(g => {
                const agentName = agentMap[g.agent_id]?.name ?? g.agent_id.slice(0, 8) + "…";
                return (
                  <tr key={g.id} className="border-b border-slate-100 last:border-b-0 hover:bg-slate-50">
                    <td className="px-4 py-3 font-medium text-slate-900">{agentName}</td>
                    <td className="px-4 py-3">
                      <div className="font-medium text-slate-800">{g.user_name ?? g.user_email}</div>
                      <div className="text-[11px] text-slate-400">{g.user_email}</div>
                    </td>
                    <td className="px-4 py-3">{statusBadge(g.status)}</td>
                    <td className="px-4 py-3 text-slate-500">{relative(g.created_at)}</td>
                    <td className="px-4 py-3 text-slate-500">
                      {g.expires_at ? relative(g.expires_at) : <span className="text-slate-400">Never</span>}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        {g.status === "pending" && (
                          <>
                            <button
                              onClick={() => void handleApprove(g)}
                              className="text-[11px] px-2 py-0.5 bg-emerald-600 text-white rounded hover:bg-emerald-700 transition-colors"
                            >
                              Approve
                            </button>
                            <button
                              onClick={() => void handleDeny(g)}
                              className="text-[11px] px-2 py-0.5 bg-red-600 text-white rounded hover:bg-red-700 transition-colors"
                            >
                              Deny
                            </button>
                          </>
                        )}
                        {g.status === "approved" && (
                          <button
                            onClick={() => void handleRevoke(g)}
                            className="text-[11px] px-2 py-0.5 border border-slate-300 text-slate-600 rounded hover:bg-slate-100 transition-colors"
                          >
                            Revoke
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Direct grant modal */}
      {directGrantModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/40" onClick={() => setDirectGrantModal(null)} />
          <div className="relative bg-white rounded-2xl shadow-2xl border border-slate-200 w-full max-w-md p-6 flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-bold text-slate-900">Grant Agent Access</h3>
              <button onClick={() => setDirectGrantModal(null)} className="text-slate-400 hover:text-slate-700">
                <X className="w-4 h-4" />
              </button>
            </div>
            <p className="text-xs text-slate-500">
              Granting access to <span className="font-semibold text-slate-800">{directGrantModal.agentName}</span>
            </p>
            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-semibold text-slate-700">
                User emails
                <span className="font-normal text-slate-400 ml-1">(comma-separated for multiple)</span>
              </label>
              <textarea
                value={directEmail}
                onChange={e => setDirectEmail(e.target.value)}
                placeholder="user@example.com, colleague@example.com"
                rows={3}
                className="text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-slate-900/10 resize-none"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label className="text-xs font-semibold text-slate-700">
                Expires in days <span className="font-normal text-slate-400">(optional — leave blank for permanent)</span>
              </label>
              <input
                type="number"
                value={directDays}
                onChange={e => setDirectDays(e.target.value)}
                min={1}
                max={3650}
                placeholder="e.g. 90"
                className="text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-slate-900/10"
              />
            </div>
            {directError && (
              <p className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
                {directError}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setDirectGrantModal(null)}
                className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50"
              >
                Cancel
              </button>
              <button
                onClick={() => void handleDirectGrant()}
                disabled={!directEmail.trim() || directSubmitting}
                className="px-4 py-2 text-sm font-semibold bg-slate-900 text-white rounded-lg hover:bg-slate-700 disabled:opacity-50 flex items-center gap-2"
              >
                {directSubmitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                Grant Access
                {directEmail.split(",").filter(e => e.trim()).length > 1
                  ? ` (${directEmail.split(",").filter(e => e.trim()).length})`
                  : ""}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Analytics Tab ─────────────────────────────────────────────────────────────

interface AnalyticsSummaryItem { value: number; delta_pct: number | null; delta_pct_day?: number | null; }
interface AnalyticsRollingActiveItem extends AnalyticsSummaryItem {
  pct:              number | null;
  total_employees:  number;
}
interface AnalyticsInventory {
  users:        number;
  agents:       number;
  automations:  number;
  skills:       number;
}
interface AnalyticsSummary {
  active_users:       AnalyticsSummaryItem;
  active_agents:      AnalyticsSummaryItem;
  active_automations: AnalyticsSummaryItem;
  active_skills:      AnalyticsSummaryItem;
  dau:                AnalyticsRollingActiveItem;
  wau:                AnalyticsRollingActiveItem;
  mau:                AnalyticsRollingActiveItem;
}
interface AnalyticsTimePoint {
  date:             string;
  workspace_chats:  number;
  workspace_messages: number;
  lab_apps:         number;
  automations:      number;
  automation_runs:  number;
}
interface AnalyticsFriction   { event: string; count: number; }
interface AnalyticsFeatureItem { feature: string; count: number; }
interface AnalyticsTopAgent    { name: string; count: number; }
interface AnalyticsTopUser     { email: string; count: number; }
type KpiRankingKey =
  | "active_users"
  | "active_agents"
  | "active_automations"
  | "active_skills"
  | "dau"
  | "wau"
  | "mau";
interface KpiRankingRow {
  label: string;
  sublabel?: string;
  email?: string;
  department?: string;
  count: number;
}
interface KpiRankings {
  active_users:       KpiRankingRow[];
  active_agents:      KpiRankingRow[];
  active_automations: KpiRankingRow[];
  active_skills:      KpiRankingRow[];
  dau?:               KpiRankingRow[];
  wau?:               KpiRankingRow[];
  mau?:               KpiRankingRow[];
}
interface GrowthMilestoneEntry {
  n: number;
  reached: boolean;
  email?: string;
  name?: string;
  department?: string;
  slug?: string;
  at?: string;
}
interface GrowthMilestoneTrack {
  total: number;
  next_milestone: number | null;
  milestones: GrowthMilestoneEntry[];
  latest: {
    n: number;
    email?: string;
    name?: string;
    slug?: string;
    at?: string;
  } | null;
}
interface GrowthMilestones {
  users: GrowthMilestoneTrack;
  agents: GrowthMilestoneTrack;
}
interface ActivityTimelinePoint {
  date: string;
  label: string;
  active_users: number;
  active_agents: number;
  active_automations: number;
  active_skills: number;
}
interface ActivityTimeline {
  days: number;
  start_date: string;
  end_date: string;
  totals: {
    active_users: number;
    active_agents: number;
    active_automations: number;
    active_skills: number;
  };
  series: ActivityTimelinePoint[];
}
interface AdoptionFunnelStep {
  key: string;
  label: string;
  count: number;
  pct_of_cohort: number | null;
  step_conv_pct: number | null;
}
interface AdoptionFunnel {
  id: string;
  title: string;
  subtitle: string;
  scope: "all_time" | "period";
  cohort_count: number;
  activation_rate_pct: number | null;
  stuck_label: string | null;
  stuck_count: number;
  steps: AdoptionFunnelStep[];
}
interface AdoptionFunnels {
  workspace: AdoptionFunnel;
  lab: AdoptionFunnel;
  agents: AdoptionFunnel;
  automations: AdoptionFunnel;
  skills: AdoptionFunnel;
}
interface CreationBreakdownRow {
  label: string;
  skills: number;
  agents: number;
  automations: number;
  total: number;
  department?: string;
  email?: string;
  name?: string;
}
interface ActiveUsersByDepartmentUnknownUser {
  email: string;
  name?: string;
  reason?: string;
}
interface ActiveUsersByDepartmentRow {
  department: string;
  label: string;
  active_users: number;
  headcount?: number | null;
  penetration_pct?: number | null;
  unknown_users?: ActiveUsersByDepartmentUnknownUser[];
  unknown_users_truncated?: number;
}
interface AnalyticsData {
  inventory?:    AnalyticsInventory;
  summary:       AnalyticsSummary;
  time_series:   AnalyticsTimePoint[];
  feature_usage: AnalyticsFeatureItem[];
  top_agents:    AnalyticsTopAgent[];
  top_users:     AnalyticsTopUser[];
  adoption_funnels?: AdoptionFunnels;
  friction:      AnalyticsFriction[];
  mode_split:    { gws: number; skills: number };
  growth_milestones?: GrowthMilestones;
  activity_timeline?: ActivityTimeline;
  creations_by_department?: CreationBreakdownRow[];
  creations_by_user?: CreationBreakdownRow[];
  active_users_by_department?: ActiveUsersByDepartmentRow[];
  kpi_rankings?: KpiRankings;
}

const A_PALETTE = {
  blue:   "#2a78d6",
  aqua:   "#1baf7a",
  yellow: "#eda100",
  green:  "#008300",
  violet: "#4a3aa7",
  red:    "#e34948",
};

function DeltaBadge({ v }: { v: number | null }) {
  if (v === null) return null;
  const up = v >= 0;
  return (
    <span className={`text-[10px] font-semibold px-1.5 py-0.5 rounded-full ml-1 ${up ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-600"}`}>
      {up ? "+" : ""}{v.toFixed(0)}%
    </span>
  );
}

function StatTile({
  label,
  value,
  delta,
  sub,
  onClick,
  active,
}: {
  label: string;
  value: number | string;
  delta: number | null;
  sub?: string;
  onClick?: () => void;
  active?: boolean;
}) {
  const className = [
    "bg-white border rounded-xl px-4 py-3 text-left w-full transition-colors",
    active ? "border-[#FF444F] ring-2 ring-[#FF444F]/15 shadow-sm" : "border-slate-200",
    onClick ? "hover:border-slate-300 hover:bg-slate-50/60 cursor-pointer" : "",
  ].join(" ");

  const content = (
    <>
      <p className="text-[11px] text-slate-400 font-medium uppercase tracking-wider mb-0.5">{label}</p>
      <div className="flex items-baseline gap-1">
        <span className="text-xl font-bold text-slate-900">{typeof value === "number" ? value.toLocaleString() : value}</span>
        <DeltaBadge v={delta} />
      </div>
      {sub && <p className="text-[10px] text-slate-400 mt-0.5">{sub}</p>}
    </>
  );

  if (onClick) {
    return (
      <button type="button" onClick={onClick} className={className} aria-pressed={active}>
        {content}
      </button>
    );
  }

  return <div className={className}>{content}</div>;
}

function periodCompareLabel(period: "daily" | "weekly" | "monthly", days: number): string {
  if (period === "daily") return "vs yesterday";
  return `vs prior ${days} days`;
}

function TotalActiveTile({
  totalLabel,
  total,
  activeLabel,
  activeValue,
  activeDelta,
  activeSub,
  onClick,
  active,
}: {
  totalLabel: string;
  total: number;
  activeLabel: string;
  activeValue: number;
  activeDelta: number | null;
  activeSub?: string;
  onClick?: () => void;
  active?: boolean;
}) {
  const className = [
    "bg-white border rounded-xl px-4 py-3 text-left w-full transition-colors",
    active ? "border-[#FF444F] ring-2 ring-[#FF444F]/15 shadow-sm" : "border-slate-200",
    onClick ? "hover:border-slate-300 hover:bg-slate-50/60 cursor-pointer" : "",
  ].join(" ");

  const content = (
    <>
      <p className="text-[10px] text-slate-400 font-medium uppercase tracking-wider">{totalLabel}</p>
      <p className="text-2xl font-bold text-slate-800 leading-tight mt-0.5">{total.toLocaleString()}</p>
      <div className="mt-2.5 pt-2.5 border-t border-slate-100">
        <p className="text-[10px] text-slate-400 font-medium uppercase tracking-wider">{activeLabel}</p>
        <div className="flex items-baseline gap-1 mt-0.5">
          <span className="text-lg font-bold text-slate-900">{activeValue.toLocaleString()}</span>
          <DeltaBadge v={activeDelta} />
        </div>
        {activeSub && <p className="text-[10px] text-slate-400 mt-0.5">{activeSub}</p>}
      </div>
    </>
  );

  if (onClick) {
    return (
      <button type="button" onClick={onClick} className={className} aria-pressed={active}>
        {content}
      </button>
    );
  }

  return <div className={className}>{content}</div>;
}

const KPI_RANKING_META: Record<KpiRankingKey, { title: string; countLabel: string }> = {
  active_users:       { title: "Active Users",       countLabel: "activity score" },
  active_agents:      { title: "Active Agents",      countLabel: "interactions" },
  active_automations: { title: "Active Automations", countLabel: "runs" },
  active_skills:      { title: "Active Skills",      countLabel: "uses" },
  dau:                { title: "DAU",                countLabel: "activities (UTC today)" },
  wau:                { title: "WAU",                countLabel: "activities (7 UTC days)" },
  mau:                { title: "MAU",                countLabel: "activities (30 UTC days)" },
};

function rollingActiveTileProps(
  metric: AnalyticsRollingActiveItem,
  periodLabel: string,
) {
  return {
    value: metric.pct !== null ? `${metric.pct}%` : metric.value,
    delta: metric.delta_pct,
    sub: metric.total_employees
      ? `${metric.value.toLocaleString()} active · ${metric.total_employees.toLocaleString()} rollout employees`
      : periodLabel,
  };
}

function KpiRankingPanel({
  kpi,
  rows,
  onClose,
}: {
  kpi: KpiRankingKey;
  rows: KpiRankingRow[];
  onClose: () => void;
}) {
  const meta = KPI_RANKING_META[kpi];

  return (
    <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-slate-100 bg-slate-50/80">
        <div>
          <p className="text-sm font-bold text-slate-900">{meta.title} — Ranking</p>
          <p className="text-[11px] text-slate-500 mt-0.5">
            {rows.length.toLocaleString()} shown · sorted by {meta.countLabel}
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-white border border-transparent hover:border-slate-200 transition-colors"
          aria-label="Close ranking"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {rows.length > 0 ? (
        <div className="max-h-72 overflow-y-auto divide-y divide-slate-100">
          {rows.map((row, i) => (
            <div key={`${row.label}-${row.sublabel ?? i}`} className="flex items-center gap-3 px-4 py-2.5">
              <span className="text-[11px] font-bold text-slate-400 w-6 shrink-0 text-right">#{i + 1}</span>
              <div className="min-w-0 flex-1">
                <p className="text-sm font-semibold text-slate-800 truncate">{row.label}</p>
                <p className="text-[11px] text-slate-500 truncate">
                  {[row.sublabel, row.department].filter(Boolean).join(" · ") || "—"}
                </p>
              </div>
              <span className="text-sm font-bold text-slate-900 shrink-0 tabular-nums">
                {row.count.toLocaleString()}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <div className="flex items-center justify-center py-10 text-sm text-slate-400">
          No items to rank in this period.
        </div>
      )}
    </div>
  );
}

function fmtMilestoneDate(iso?: string) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function GrowthMilestoneRail({
  title,
  track,
  kind,
}: {
  title: string;
  track: GrowthMilestoneTrack;
  kind: "users" | "agents";
}) {
  const nextLabel = track.next_milestone ? `#${track.next_milestone}` : "complete";
  const entityLabel = kind === "users" ? "User" : "Agent";

  const milestoneLabel = (m: GrowthMilestoneEntry) => {
    if (!m.reached) return "—";
    return m.name || m.email?.split("@")[0] || "—";
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">{title}</p>
        <p className="text-[10px] text-slate-400">
          <span className="font-semibold text-slate-600">{track.total}</span> total · next {nextLabel}
        </p>
      </div>

      <div className="relative flex items-start justify-between px-1">
        <div className="absolute left-3 right-3 top-[7px] h-0.5 bg-slate-100" aria-hidden />
        {track.milestones.map((m) => (
          <div key={m.n} className="relative z-10 flex flex-col items-center min-w-0 flex-1 px-0.5">
            <div
              className={`w-3.5 h-3.5 rounded-full border-2 shrink-0 ${
                m.reached
                  ? "bg-[#FF444F] border-[#FF444F]"
                  : "bg-white border-slate-300"
              }`}
            />
            <span className="text-[10px] font-bold text-slate-500 mt-1.5">#{m.n}</span>
            <span className="text-[10px] font-medium text-slate-700 mt-0.5 text-center truncate w-full max-w-[88px]">
              {milestoneLabel(m)}
            </span>
            <span className="text-[9px] text-slate-400 mt-0.5">
              {m.reached ? fmtMilestoneDate(m.at) : "—"}
            </span>
          </div>
        ))}
      </div>

      {track.latest && (
        <p className="text-[10px] text-slate-500 border-t border-slate-100 pt-2">
          Latest: {entityLabel} #{track.latest.n}
          {" — "}
          <span className="text-slate-700 font-medium">
            {track.latest.name || track.latest.email || "—"}
          </span>
          {" · "}
          {fmtMilestoneDate(track.latest.at)}
        </p>
      )}
    </div>
  );
}

function GrowthMilestonesCard({ data }: { data: GrowthMilestones }) {
  const branding = useBranding();
  const appName = branding.app_name;
  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4">
      <div className="flex items-center justify-between mb-4">
        <p className="text-xs font-semibold text-slate-700 flex items-center gap-2">
          <Rocket className="w-3.5 h-3.5 text-[#FF444F]" /> Growth Milestones
        </p>
        <span className="text-[10px] font-medium text-slate-400 uppercase tracking-wider">All time</span>
      </div>
      <div className="space-y-5">
        <GrowthMilestoneRail title={`Users on ${appName}`} track={data.users} kind="users" />
        <GrowthMilestoneRail title={`Agents on ${appName}`} track={data.agents} kind="agents" />
      </div>
    </div>
  );
}

function fmtTimelineRange(start: string, end: string) {
  const fmt = (iso: string) => {
    const d = new Date(`${iso}T00:00:00`);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" });
  };
  return `${fmt(start)} — ${fmt(end)}`;
}

function ActivityTimelineCard({
  data,
}: {
  data: ActivityTimeline;
}) {
  const { totals, series } = data;

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between mb-4">
        <div>
          <p className="text-sm font-bold text-slate-900">Activity Timeline</p>
          <p className="text-xs text-slate-500 mt-0.5">
            {data.start_date && data.end_date
              ? fmtTimelineRange(data.start_date, data.end_date)
              : "Daily activity"}
            {data.days > 1 ? ` · ${data.days}d period` : " · today (UTC)"}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
        <div className="rounded-xl bg-violet-50 border border-violet-100 px-4 py-3 text-center">
          <p className="text-2xl font-bold text-[#4a3aa7] leading-none">{totals.active_users.toLocaleString()}</p>
          <p className="text-[11px] text-slate-600 mt-1.5 font-medium">Active Users</p>
        </div>
        <div className="rounded-xl bg-blue-50 border border-blue-100 px-4 py-3 text-center">
          <p className="text-2xl font-bold text-[#2a78d6] leading-none">{totals.active_agents.toLocaleString()}</p>
          <p className="text-[11px] text-slate-600 mt-1.5 font-medium">Active Agents</p>
        </div>
        <div className="rounded-xl bg-red-50 border border-red-100 px-4 py-3 text-center">
          <p className="text-2xl font-bold text-[#e34948] leading-none">{totals.active_automations.toLocaleString()}</p>
          <p className="text-[11px] text-slate-600 mt-1.5 font-medium">Active Automations</p>
        </div>
        <div className="rounded-xl bg-amber-50 border border-amber-100 px-4 py-3 text-center">
          <p className="text-2xl font-bold text-[#eda100] leading-none">{totals.active_skills.toLocaleString()}</p>
          <p className="text-[11px] text-slate-600 mt-1.5 font-medium">Active Skills</p>
        </div>
      </div>

      {series.length > 0 ? (
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={series} margin={{ top: 8, right: 12, left: -8, bottom: 4 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" vertical={false} />
            <XAxis
              dataKey="label"
              tick={{ fontSize: 10, fill: "#94a3b8" }}
              tickLine={false}
              axisLine={false}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={{ fontSize: 10, fill: "#94a3b8" }}
              tickLine={false}
              axisLine={false}
              width={32}
              allowDecimals={false}
            />
            <Tooltip
              contentStyle={{ fontSize: 11, borderRadius: 8, border: "1px solid #e2e8f0" }}
              labelFormatter={(_, payload) => {
                const pt = payload?.[0]?.payload as ActivityTimelinePoint | undefined;
                return pt?.date ?? "";
              }}
            />
            <Legend
              wrapperStyle={{ fontSize: 11, paddingTop: 8 }}
              iconType="circle"
              iconSize={8}
            />
            <Line
              type="monotone"
              dataKey="active_users"
              name="Active Users"
              stroke={A_PALETTE.violet}
              strokeWidth={2}
              dot={{ r: 3, fill: A_PALETTE.violet, strokeWidth: 0 }}
              activeDot={{ r: 4 }}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="active_agents"
              name="Active Agents"
              stroke={A_PALETTE.blue}
              strokeWidth={2}
              dot={{ r: 3, fill: A_PALETTE.blue, strokeWidth: 0 }}
              activeDot={{ r: 4 }}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="active_automations"
              name="Active Automations"
              stroke={A_PALETTE.red}
              strokeWidth={2}
              dot={{ r: 3, fill: A_PALETTE.red, strokeWidth: 0 }}
              activeDot={{ r: 4 }}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="active_skills"
              name="Active Skills"
              stroke={A_PALETTE.yellow}
              strokeWidth={2}
              dot={{ r: 3, fill: A_PALETTE.yellow, strokeWidth: 0 }}
              activeDot={{ r: 4 }}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      ) : (
        <div className="flex items-center justify-center h-40 text-slate-400 text-sm">
          No activity in this period yet.
        </div>
      )}
    </div>
  );
}

function AdoptionFunnelCard({
  data,
  icon,
  compact,
}: {
  data: AdoptionFunnel;
  icon?: ReactNode;
  compact?: boolean;
}) {
  const maxBar = Math.max(
    data.cohort_count,
    ...data.steps.filter((s) => s.key !== "eligible").map((s) => s.count),
    1,
  );
  const scopeLabel = data.scope === "all_time" ? "All time" : "This period";

  return (
    <div className={`bg-white border border-slate-200 rounded-xl p-4 h-full ${compact ? "" : ""}`}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between mb-3">
        <div>
          <p className={`font-bold text-slate-900 flex items-center gap-2 ${compact ? "text-xs" : "text-sm"}`}>
            {icon}
            {data.title}
          </p>
          <p className={`text-slate-500 mt-0.5 ${compact ? "text-[10px]" : "text-xs"}`}>{data.subtitle}</p>
        </div>
        <div className="flex items-center gap-3 text-xs shrink-0">
          {data.cohort_count > 0 && data.activation_rate_pct !== null && (
            <span className="font-semibold text-slate-700">
              Rate:{" "}
              <span className="text-[#FF444F]">{data.activation_rate_pct}%</span>
            </span>
          )}
          <span className="text-slate-400 font-medium uppercase tracking-wider text-[10px]">{scopeLabel}</span>
        </div>
      </div>

      <div className={compact ? "space-y-2" : "space-y-3"}>
        {data.steps.map((step, i) => {
          const barPct = step.key === "eligible"
            ? (data.cohort_count > 0 ? 100 : 0)
            : Math.round((step.count / maxBar) * 100);
          return (
            <div key={step.key}>
              <div className="flex items-center justify-between mb-1 gap-2">
                <span className={`font-medium text-slate-700 shrink-0 ${compact ? "text-[10px] w-24" : "text-[11px] w-28"}`}>
                  {step.label}
                </span>
                <div className="flex items-center gap-2 text-[10px] text-slate-500 shrink-0">
                  {step.step_conv_pct !== null && i > 0 && (
                    <span>{step.step_conv_pct}% of prev</span>
                  )}
                  {step.pct_of_cohort !== null && step.key !== "eligible" && (
                    <span className="text-slate-400">{step.pct_of_cohort}% of cohort</span>
                  )}
                  <span className="font-bold text-slate-800 text-xs w-8 text-right">{step.count.toLocaleString()}</span>
                </div>
              </div>
              <div className={`bg-slate-100 rounded-full overflow-hidden ${compact ? "h-1.5" : "h-2"}`}>
                <div
                  className="h-full rounded-full transition-all"
                  style={{
                    width: `${barPct}%`,
                    backgroundColor: Object.values(A_PALETTE)[i % Object.values(A_PALETTE).length],
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>

      {data.stuck_count > 0 && data.stuck_label && (
        <p className="text-[10px] text-amber-700 mt-3 border-t border-slate-100 pt-2">
          {data.stuck_count} user{data.stuck_count === 1 ? "" : "s"} {data.stuck_label}
        </p>
      )}
    </div>
  );
}

const ADOPTION_FUNNEL_ICONS: Record<keyof AdoptionFunnels, ReactNode> = {
  workspace:   <MessageSquare className="w-4 h-4 text-[#FF444F]" />,
  lab:         <Rocket className="w-4 h-4 text-[#FF444F]" />,
  agents:      <Bot className="w-4 h-4 text-[#FF444F]" />,
  automations: <Zap className="w-4 h-4 text-[#FF444F]" />,
  skills:      <Layers className="w-4 h-4 text-[#FF444F]" />,
};

const FEATURE_FUNNEL_ORDER: (keyof AdoptionFunnels)[] = [
  "workspace",
  "lab",
  "agents",
  "automations",
  "skills",
];

const FRICTION_EVENT_LABELS: Record<string, string> = {
  automation_run_failures: "Automation run failures",
  gws_validation_failures: "Lab sheet validation failures",
  lab_create_abandoned: "Lab app create abandoned",
  lab_app_errors: "Lab app errors",
  lab_build_failures: "Lab build failures",
  skill_review_failures: "Skill review failures",
  skill_preflight_failures: "Skill preflight failures",
  memory_sync_failures: "Memory sync failures",
  skill_access_denied: "Skill access denied",
  agent_access_denied: "Agent access denied",
  agent_scheduled_run_failures: "Agent scheduled run failures",
};

function frictionEventLabel(event: string): string {
  if (event.startsWith("error:")) {
    const detail = event.slice("error:".length);
    return detail === "unknown" ? "Workspace chat errors" : `Workspace: ${detail}`;
  }
  return FRICTION_EVENT_LABELS[event] ?? event.replace(/_/g, " ");
}

function FrictionEventsCard({ items }: { items: AnalyticsFriction[] }) {
  return (
    <div className="bg-white border border-slate-200 rounded-xl p-3 h-full">
      <div className="flex items-center justify-between gap-2 mb-2">
        <p className="text-xs font-bold text-slate-900 flex items-center gap-1.5">
          <AlertCircle className="w-3.5 h-3.5 text-red-500 shrink-0" /> Friction Events
        </p>
        <span className="text-[10px] font-medium text-slate-400 uppercase tracking-wider shrink-0">This period</span>
      </div>

      {items.length > 0 ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-1.5">
          {items.map((f) => (
            <div key={f.event} className="flex items-center gap-2 rounded-md bg-red-50/50 border border-red-100 px-2 py-1">
              <span className="text-[11px] text-slate-700 flex-1 truncate" title={f.event}>
                {frictionEventLabel(f.event)}
              </span>
              <span className="text-[11px] font-bold text-red-600 shrink-0">{f.count.toLocaleString()}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-[11px] text-slate-400 py-1">No friction events in this period.</p>
      )}
    </div>
  );
}

function TopRankList({
  title,
  icon,
  rows,
  valueKey,
  labelKey,
  emptyLabel,
  barColor,
}: {
  title: string;
  icon: ReactNode;
  rows: { count: number; [key: string]: string | number }[];
  valueKey: string;
  labelKey: string;
  emptyLabel: string;
  barColor: string;
}) {
  const max = rows[0]?.count ?? 1;

  return (
    <div className="min-w-0">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 flex items-center gap-1.5 mb-3">
        {icon}
        {title}
      </p>
      {rows.length > 0 ? (
        <ul className="space-y-2">
          {rows.map((row, i) => {
            const label = String(row[labelKey]);
            const count = Number(row[valueKey]);
            const pct = Math.max(8, Math.round((count / max) * 100));
            return (
              <li key={`${label}-${i}`} className="flex items-center gap-2">
                <span className="text-[10px] font-bold text-slate-400 w-4 shrink-0 text-right">{i + 1}</span>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between gap-2 mb-0.5">
                    <span className="text-xs text-slate-700 truncate font-medium" title={String(row.title ?? label)}>
                      {label}
                    </span>
                    <span className="text-xs font-bold text-slate-900 shrink-0">{count.toLocaleString()}</span>
                  </div>
                  <div className="h-1.5 rounded-full bg-slate-100 overflow-hidden">
                    <div className="h-full rounded-full" style={{ width: `${pct}%`, backgroundColor: barColor }} />
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="text-xs text-slate-400 py-6 text-center">{emptyLabel}</p>
      )}
    </div>
  );
}

function TopUsersAndAgentsCard({
  users,
  agents,
}: {
  users: AnalyticsTopUser[];
  agents: AnalyticsTopAgent[];
}) {
  const agentRows = useMemo(
    () => agents.map((a) => ({
      name: a.name,
      title: a.name,
      count: a.count,
    })),
    [agents],
  );
  const userRows = useMemo(
    () => users.map((u) => ({
      email: u.email.split("@")[0],
      title: u.email,
      count: u.count,
    })),
    [users],
  );
  const hasData = users.length > 0 || agents.length > 0;

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:justify-between mb-4">
        <div>
          <p className="text-sm font-bold text-slate-900 flex items-center gap-2">
            <TrendingUp className="w-4 h-4 text-[#FF444F]" /> Top Users & Agents
          </p>
          <p className="text-xs text-slate-500 mt-0.5">
            Most active users by product activity · most used agents by interaction logs
          </p>
        </div>
        <span className="text-[10px] font-medium text-slate-400 uppercase tracking-wider shrink-0">This period</span>
      </div>

      {hasData ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <TopRankList
            title="Top users"
            icon={<Users className="w-3.5 h-3.5 text-[#4a3aa7]" />}
            rows={userRows}
            labelKey="email"
            valueKey="count"
            emptyLabel="No tracked user activity in this period."
            barColor={A_PALETTE.violet}
          />
          <TopRankList
            title="Top agents"
            icon={<Bot className="w-3.5 h-3.5 text-[#2a78d6]" />}
            rows={agentRows}
            labelKey="name"
            valueKey="count"
            emptyLabel="No agent interactions in this period."
            barColor={A_PALETTE.blue}
          />
        </div>
      ) : (
        <div className="flex flex-col items-center justify-center h-32 text-center px-4">
          <TrendingUp className="w-8 h-8 text-slate-300 mb-2" />
          <p className="text-sm text-slate-500">No user or agent activity in this period.</p>
        </div>
      )}
    </div>
  );
}

function ActiveUsersByDepartmentTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload?: ActiveUsersByDepartmentRow; value?: number }>;
}) {
  if (!active || !payload?.length) return null;
  const row = payload[0]?.payload;
  const value = payload[0]?.value;
  if (!row) return null;

  const isUnknown = row.department === "Unknown";
  const users = row.unknown_users ?? [];

  return (
    <div
      className={`rounded-lg border border-slate-200 bg-white px-3 py-2 text-[11px] text-slate-700 shadow-md ${
        isUnknown && users.length > 0 ? "max-w-sm" : ""
      }`}
    >
      <p className="font-semibold text-slate-900 mb-1">{row.department}</p>
      <p className="text-slate-600">
        {typeof value === "number" ? value.toLocaleString() : row.active_users.toLocaleString()} active users
        {row.headcount ? ` · ${row.headcount.toLocaleString()} in department` : ""}
        {row.penetration_pct !== null && row.penetration_pct !== undefined
          ? ` · ${row.penetration_pct}% penetration`
          : ""}
      </p>
      {isUnknown && users.length > 0 && (
        <div className="mt-2 pt-2 border-t border-slate-100">
          <p className="text-[10px] font-medium text-slate-500 uppercase tracking-wide mb-1">
            Users in Unknown
          </p>
          <ul className="space-y-1 max-h-48 overflow-y-auto pr-1">
            {users.map((u) => (
              <li key={u.email} className="leading-snug">
                <span className="font-medium text-slate-800">{u.name || u.email.split("@")[0]}</span>
                <span className="text-slate-500"> · {u.email}</span>
                {u.reason && (
                  <span className="block text-[10px] text-amber-700">{u.reason}</span>
                )}
              </li>
            ))}
          </ul>
          {(row.unknown_users_truncated ?? 0) > 0 && (
            <p className="text-[10px] text-slate-400 mt-1">
              +{row.unknown_users_truncated} more not shown
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function ActiveUsersByDepartmentCard({
  data,
  periodActiveTotal,
}: {
  data: ActiveUsersByDepartmentRow[];
  periodActiveTotal?: number;
}) {
  const rows = useMemo(
    () => [...data].sort((a, b) => b.active_users - a.active_users),
    [data],
  );
  const hasData = rows.some((d) => d.active_users > 0);
  const summedActive = rows.reduce((sum, d) => sum + d.active_users, 0);
  const totalActive = periodActiveTotal ?? summedActive;
  const departmentsActive = rows.filter((d) => d.active_users > 0).length;
  const topDept = rows[0];

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between mb-4">
        <div>
          <p className="text-sm font-bold text-slate-900 flex items-center gap-2">
            <Building2 className="w-4 h-4 text-[#FF444F]" /> Active Users by Department
          </p>
          <p className="text-xs text-slate-500 mt-0.5">
            Distinct active users in this period (same as Active Users KPI), by directory department
          </p>
        </div>
        {hasData && topDept && (
          <div className="text-right shrink-0">
            <p className="text-[10px] font-medium text-slate-400 uppercase tracking-wider">Top department</p>
            <p className="text-sm font-semibold text-slate-800">{topDept.department}</p>
            <p className="text-[11px] text-slate-500">
              {topDept.active_users.toLocaleString()} active user{topDept.active_users === 1 ? "" : "s"}
            </p>
          </div>
        )}
      </div>

      {hasData && (
        <div className="grid grid-cols-2 gap-3 mb-4">
          <div className="rounded-xl bg-violet-50 border border-violet-100 px-4 py-3 text-center">
            <p className="text-2xl font-bold text-[#4a3aa7] leading-none">{totalActive.toLocaleString()}</p>
            <p className="text-[11px] text-slate-600 mt-1.5 font-medium">Active Users</p>
          </div>
          <div className="rounded-xl bg-blue-50 border border-blue-100 px-4 py-3 text-center">
            <p className="text-2xl font-bold text-[#2a78d6] leading-none">{departmentsActive.toLocaleString()}</p>
            <p className="text-[11px] text-slate-600 mt-1.5 font-medium">Departments Active</p>
          </div>
        </div>
      )}

      {hasData ? (
        <ResponsiveContainer width="100%" height={Math.max(220, rows.length * 36)}>
          <BarChart
            data={rows}
            layout="vertical"
            margin={{ top: 0, right: 12, left: 0, bottom: 0 }}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" horizontal={false} />
            <XAxis
              type="number"
              tick={{ fontSize: 9, fill: "#94a3b8" }}
              tickLine={false}
              axisLine={false}
              allowDecimals={false}
            />
            <YAxis
              type="category"
              dataKey="label"
              tick={{ fontSize: 9, fill: "#64748b" }}
              tickLine={false}
              axisLine={false}
              width={96}
            />
            <Tooltip content={<ActiveUsersByDepartmentTooltip />} />
            <Bar
              dataKey="active_users"
              name="Active users"
              fill={A_PALETTE.violet}
              radius={[0, 4, 4, 0]}
              isAnimationActive={false}
            />
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <div className="flex flex-col items-center justify-center h-48 text-center px-4">
          <Building2 className="w-8 h-8 text-slate-300 mb-2" />
          <p className="text-sm text-slate-500">No active users by department in this period.</p>
          <p className="text-[11px] text-slate-400 mt-1">
            Hover the Unknown bar to see which users could not be mapped to a department.
          </p>
        </div>
      )}
    </div>
  );
}

function CreationsStackedChart({
  title,
  subtitle,
  data,
  emptyLabel,
}: {
  title: string;
  subtitle: string;
  data: CreationBreakdownRow[];
  emptyLabel: string;
}) {
  const hasData = data.some((d) => d.skills + d.agents + d.automations > 0);

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-4 h-full">
      <p className="text-sm font-bold text-slate-900">{title}</p>
      <p className="text-xs text-slate-500 mt-0.5 mb-3">{subtitle}</p>

      {hasData ? (
        <ResponsiveContainer width="100%" height={240}>
          <BarChart data={data} margin={{ top: 4, right: 8, left: -12, bottom: 40 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" vertical={false} />
            <XAxis
              dataKey="label"
              tick={{ fontSize: 9, fill: "#94a3b8" }}
              tickLine={false}
              axisLine={false}
              interval={0}
              angle={-30}
              textAnchor="end"
              height={48}
            />
            <YAxis
              tick={{ fontSize: 9, fill: "#94a3b8" }}
              tickLine={false}
              axisLine={false}
              width={28}
              allowDecimals={false}
            />
            <Tooltip
              contentStyle={{ fontSize: 11, borderRadius: 8, border: "1px solid #e2e8f0" }}
            />
            <Legend wrapperStyle={{ fontSize: 10 }} iconType="square" iconSize={8} />
            <Bar dataKey="skills" name="Skills" stackId="creations" fill={A_PALETTE.blue} />
            <Bar dataKey="agents" name="Agents" stackId="creations" fill={A_PALETTE.violet} />
            <Bar
              dataKey="automations"
              name="Automations"
              stackId="creations"
              fill={A_PALETTE.red}
              radius={[4, 4, 0, 0]}
            />
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <div className="flex items-center justify-center h-48 text-slate-400 text-sm">
          {emptyLabel}
        </div>
      )}
    </div>
  );
}

function AnalyticsTab() {
  const [data,    setData]    = useState<AnalyticsData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);
  const [period,  setPeriod]  = useState<"daily" | "weekly" | "monthly">("daily");
  const [days,    setDays]    = useState(1);
  const [selectedKpi, setSelectedKpi] = useState<KpiRankingKey | null>(null);

  const toggleKpi = useCallback((kpi: KpiRankingKey) => {
    setSelectedKpi((prev) => (prev === kpi ? null : kpi));
  }, []);

  const load = useCallback(async (p: string, d: number) => {
    setLoading(true); setError(null);
    try {
      const res = await apiFetch(`/api/admin/analytics?period=${p}&days=${d}`);
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error((b as { detail?: string }).detail ?? `HTTP ${res.status}`); }
      setData(await res.json() as AnalyticsData);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load analytics.");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { void load(period, days); }, [period, days, load]);
  useEffect(() => { setSelectedKpi(null); }, [period, days]);

  if (error && !data) return (
    <div className="flex flex-col items-center justify-center h-full gap-3">
      <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-5 py-4 max-w-md text-center">
        <p className="font-semibold mb-1">Failed to load analytics</p>
        <p className="text-xs opacity-80">{error}</p>
      </div>
      <button onClick={() => void load(period, days)} className="text-xs text-[#FF444F] hover:underline">Try again</button>
    </div>
  );

  if (loading && !data) return (
    <div className="flex flex-col items-center justify-center h-full gap-3 text-slate-400">
      <Loader2 className="w-6 h-6 animate-spin text-[#FF444F]" />
      <span className="text-sm">Loading analytics…</span>
    </div>
  );

  const summary = data?.summary;
  const inventory = data?.inventory;
  const periodCompare = periodCompareLabel(period, days);
  const activeUsersDelta = summary
    ? (period === "daily"
      ? (summary.active_users.delta_pct_day ?? summary.active_users.delta_pct)
      : summary.active_users.delta_pct)
    : null;

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div>
          <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
            <BarChart2 className="w-4 h-4 text-[#FF444F]" /> Analytics
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">User journeys, feature usage, and friction points</p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center bg-slate-100 rounded-lg p-0.5 text-xs font-medium">
            {(["daily", "weekly", "monthly"] as const).map(p => (
              <button key={p} onClick={() => {
                setPeriod(p);
                if (p === "daily")   setDays(1);
                if (p === "weekly")  setDays(7);
                if (p === "monthly") setDays(30);
              }}
                className={`px-3 py-1 rounded-md transition-colors capitalize ${period === p ? "bg-white text-slate-800 shadow-sm" : "text-slate-500 hover:text-slate-700"}`}>
                {p}
              </button>
            ))}
          </div>
          {period !== "daily" && (
            <div className="flex items-center bg-slate-100 rounded-lg p-0.5 text-xs font-medium">
              {(period === "weekly" ? [7, 14, 28] as const : [30, 90, 180] as const).map(d => (
                <button key={d} onClick={() => setDays(d)}
                  className={`px-3 py-1 rounded-md transition-colors ${days === d ? "bg-white text-slate-800 shadow-sm" : "text-slate-500 hover:text-slate-700"}`}>
                  {d}d
                </button>
              ))}
            </div>
          )}
          <button onClick={() => void load(period, days)} disabled={loading}
            className="flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-700 border border-slate-200 px-3 py-1.5 rounded-lg hover:bg-slate-50 transition-colors disabled:opacity-50">
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto pr-1 space-y-4">
        {/* ── KPI tiles ─────────────────────────────────────────────────────── */}
        {summary && inventory && (
          <>
            <div className="space-y-3 shrink-0">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                <TotalActiveTile
                  totalLabel="Total Active Users"
                  total={inventory.users}
                  activeLabel={period === "daily" ? "Active today" : period === "weekly" ? `Active (${days}d)` : `Active (${days}d)`}
                  activeValue={summary.active_users.value}
                  activeDelta={activeUsersDelta}
                  activeSub={periodCompare}
                  onClick={() => toggleKpi("active_users")}
                  active={selectedKpi === "active_users"}
                />
                <TotalActiveTile
                  totalLabel="Total Agents"
                  total={inventory.agents}
                  activeLabel={period === "daily" ? "Active today" : `Active (${days}d)`}
                  activeValue={summary.active_agents.value}
                  activeDelta={summary.active_agents.delta_pct}
                  activeSub={periodCompare}
                  onClick={() => toggleKpi("active_agents")}
                  active={selectedKpi === "active_agents"}
                />
                <TotalActiveTile
                  totalLabel="Total Automations"
                  total={inventory.automations}
                  activeLabel={period === "daily" ? "Ran today" : `Ran (${days}d)`}
                  activeValue={summary.active_automations.value}
                  activeDelta={summary.active_automations.delta_pct}
                  activeSub={periodCompare}
                  onClick={() => toggleKpi("active_automations")}
                  active={selectedKpi === "active_automations"}
                />
                <TotalActiveTile
                  totalLabel="Total Skills"
                  total={inventory.skills}
                  activeLabel={period === "daily" ? "Used today" : `Used (${days}d)`}
                  activeValue={summary.active_skills.value}
                  activeDelta={summary.active_skills.delta_pct}
                  activeSub={periodCompare}
                  onClick={() => toggleKpi("active_skills")}
                  active={selectedKpi === "active_skills"}
                />
              </div>

              <div className="grid grid-cols-3 gap-3">
                <StatTile
                  label="DAU"
                  {...rollingActiveTileProps(summary.dau, "users active today (UTC)")}
                  onClick={() => toggleKpi("dau")}
                  active={selectedKpi === "dau"}
                />
                <StatTile
                  label="WAU"
                  {...rollingActiveTileProps(summary.wau, "users active last 7 UTC days")}
                  onClick={() => toggleKpi("wau")}
                  active={selectedKpi === "wau"}
                />
                <StatTile
                  label="MAU"
                  {...rollingActiveTileProps(summary.mau, "users active last 30 UTC days")}
                  onClick={() => toggleKpi("mau")}
                  active={selectedKpi === "mau"}
                />
              </div>
            </div>

            {selectedKpi && (
              <KpiRankingPanel
                kpi={selectedKpi}
                rows={data?.kpi_rankings?.[selectedKpi] ?? []}
                onClose={() => setSelectedKpi(null)}
              />
            )}
          </>
        )}

        {/* ── Activity timeline ─────────────────────────────────────────────── */}
        {data?.activity_timeline && (
          <ActivityTimelineCard data={data.activity_timeline} />
        )}

        {data?.adoption_funnels && (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {FEATURE_FUNNEL_ORDER.map((key) => {
              const funnel = data.adoption_funnels![key];
              if (!funnel) return null;
              return (
                <AdoptionFunnelCard
                  key={key}
                  data={funnel}
                  icon={ADOPTION_FUNNEL_ICONS[key]}
                  compact
                />
              );
            })}
          </div>
        )}

        <TopUsersAndAgentsCard
          users={data?.top_users ?? []}
          agents={data?.top_agents ?? []}
        />

        {/* ── Active users by department ────────────────────────────────────── */}
        <ActiveUsersByDepartmentCard
          data={data?.active_users_by_department ?? []}
          periodActiveTotal={summary?.active_users.value}
        />

        {/* ── Creations by department + user ────────────────────────────────── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <CreationsStackedChart
            title="Creations by Department"
            subtitle="Skills, agents, and automations created in this period"
            data={data?.creations_by_department ?? []}
            emptyLabel="No creations by department in this period."
          />
          <CreationsStackedChart
            title="Creations by User"
            subtitle="Top contributors in this period"
            data={data?.creations_by_user ?? []}
            emptyLabel="No creations by user in this period."
          />
        </div>

        {/* ── Friction events + feature usage ─────────────────────────────── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <FrictionEventsCard items={data?.friction ?? []} />
          {(data?.feature_usage ?? []).length > 0 ? (
            <div className="bg-white border border-slate-200 rounded-xl p-4 h-full">
              <p className="text-xs font-semibold text-slate-700 mb-3">Feature Usage</p>
              <ResponsiveContainer width="100%" height={180}>
                <BarChart data={data!.feature_usage} layout="vertical" margin={{ top: 0, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" horizontal={false} />
                  <XAxis type="number" tick={{ fontSize: 9, fill: "#94a3b8" }} tickLine={false} axisLine={false} />
                  <YAxis type="category" dataKey="feature" tick={{ fontSize: 9, fill: "#94a3b8" }} tickLine={false} axisLine={false} width={90} />
                  <Tooltip contentStyle={{ fontSize: 11, borderRadius: 8, border: "1px solid #e2e8f0" }} />
                  <Bar dataKey="count" name="Events" radius={[0, 4, 4, 0]} isAnimationActive={false}>
                    {(data!.feature_usage).map((_, i) => (
                      <Cell key={i} fill={Object.values(A_PALETTE)[i % Object.values(A_PALETTE).length]} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : (
            <div className="bg-white border border-slate-200 rounded-xl p-4 h-full flex items-center justify-center text-slate-400 text-sm">
              No feature usage in this period.
            </div>
          )}
        </div>

        {/* ── Growth milestones ─────────────────────────────────────────────── */}
        {data?.growth_milestones && (
          <GrowthMilestonesCard data={data.growth_milestones} />
        )}

        {!data && !loading && (
          <div className="flex items-center justify-center h-32 text-slate-400 text-sm">No data yet.</div>
        )}
      </div>
    </div>
  );
}

// Re-export for backward compat
export { AdminPage };

// ── Slack Scopes Tab ──────────────────────────────────────────────────────────

const BASE_SCOPES = [
  "app_mentions:read", "chat:write", "channels:read", "channels:history",
  "groups:read", "groups:history", "users:read", "users:read.email",
];

const CONDITIONAL_SCOPES = [
  { scope: "im:read",                description: "List DM conversations" },
  { scope: "im:history",             description: "Read DM message history" },
  { scope: "im:write",               description: "Open a DM with a user" },
  { scope: "channels:join",          description: "Self-join public channels" },
  { scope: "channels:manage",        description: "Create/archive/rename public channels" },
  { scope: "groups:manage",          description: "Create/archive/rename private channels" },
  { scope: "mpim:read",              description: "List multi-person DMs" },
  { scope: "mpim:history",           description: "Read multi-person DM history" },
  { scope: "files:read",             description: "Download shared files" },
  { scope: "files:write",            description: "Upload and share files" },
  { scope: "search:read",            description: "Workspace-wide search" },
  { scope: "team:read",              description: "Read workspace metadata" },
  { scope: "reactions:read",         description: "Read emoji reactions" },
  { scope: "reactions:write",        description: "Add emoji reactions" },
  { scope: "pins:write",             description: "Pin messages in channels" },
  { scope: "bookmarks:write",        description: "Add bookmarks to channels" },
  { scope: "reminders:write",        description: "Create reminders for users" },
  { scope: "usergroups:read",        description: "Read user groups" },
  { scope: "usergroups:write",       description: "Create and manage user groups" },
  { scope: "workflow.steps:execute", description: "Run Workflow Builder steps" },
];

interface AgentScopeRow {
  id: string;
  name: string;
  avatar_url: string | null;
  slack_app_id: string;
  creation_method: string;
  bot_scopes: string[] | null;
  pending_scopes: string[];
}

function SlackScopesTab() {
  const [agents, setAgents] = useState<AgentScopeRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Record<string, string[]>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<Record<string, string>>({});
  const [search, setSearch] = useState("");
  const [agentSearch, setAgentSearch] = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const baseSet = new Set(BASE_SCOPES);

  const fetchAgents = async (silent = false) => {
    if (!silent) setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/api/agents");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setAgents(data.map((a: AgentScopeRow) => ({
        id: a.id,
        name: a.name,
        avatar_url: a.avatar_url,
        slack_app_id: a.slack_app_id,
        creation_method: (a as { creation_method?: string }).creation_method ?? "manual",
        bot_scopes: a.bot_scopes,
        pending_scopes: (a as { pending_scopes?: string[] }).pending_scopes ?? [],
      })));
    } catch (e) {
      if (!silent) setError(e instanceof Error ? e.message : "Failed to load agents.");
    } finally {
      if (!silent) setLoading(false);
    }
  };

  // Poll silently while any agent has pending scopes.
  useEffect(() => {
    const hasPending = agents.some(a => a.pending_scopes.length > 0);
    if (hasPending && !pollRef.current) {
      pollRef.current = setInterval(() => { void fetchAgents(true); }, 4000);
    } else if (!hasPending && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents]);

  useEffect(() => { void fetchAgents(); }, []);

  const enterEdit = (agent: AgentScopeRow) => {
    setEditing(prev => ({ ...prev, [agent.id]: [...(agent.bot_scopes ?? BASE_SCOPES)] }));
    setSearch("");
    setSaveError(prev => ({ ...prev, [agent.id]: "" }));
  };

  const cancelEdit = (agentId: string) => {
    setEditing(prev => { const n = { ...prev }; delete n[agentId]; return n; });
    setSearch("");
  };

  const stageRemove = (agentId: string, scope: string) => {
    setEditing(prev => ({ ...prev, [agentId]: prev[agentId].filter(s => s !== scope) }));
  };

  const stageAdd = (agentId: string, scope: string) => {
    setEditing(prev => {
      if (prev[agentId]?.includes(scope)) return prev;
      return { ...prev, [agentId]: [...prev[agentId], scope] };
    });
    setSearch("");
  };

  const save = async (agentId: string) => {
    const scopes = editing[agentId];
    if (!scopes) return;
    setSaving(agentId);
    setSaveError(prev => ({ ...prev, [agentId]: "" }));
    try {
      const res = await apiFetch(`/api/admin/agents/${agentId}/scopes`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scopes }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { detail?: string }).detail || `HTTP ${res.status}`);
      }
      const updated = await res.json();
      setAgents(prev => prev.map(a => a.id === agentId ? {
        ...a,
        bot_scopes: updated.bot_scopes,
        pending_scopes: updated.pending_scopes ?? [],
      } : a));
      cancelEdit(agentId);
      // Auto-open Slack reinstall URL so the new scopes can be activated.
      if (updated.install_url) {
        window.open(updated.install_url, "_blank", "noopener,noreferrer");
      }
    } catch (e) {
      setSaveError(prev => ({ ...prev, [agentId]: e instanceof Error ? e.message : "Save failed." }));
    } finally {
      setSaving(null);
    }
  };

  if (loading) return (
    <div className="flex items-center justify-center py-20">
      <Loader2 className="w-5 h-5 animate-spin text-slate-400" />
    </div>
  );

  if (error) return (
    <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-5 py-4 m-6">{error}</div>
  );

  const agentQuery = agentSearch.trim().toLowerCase();
  const visibleAgents = agentQuery
    ? agents.filter(a =>
        a.name.toLowerCase().includes(agentQuery) ||
        (a.slack_app_id ?? "").toLowerCase().includes(agentQuery)
      )
    : agents;

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Header + agent search (fixed) */}
      <div className="shrink-0 pb-3 space-y-3">
        <div>
          <h2 className="text-base font-semibold text-slate-900">Slack Scopes</h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Manage Slack permission scopes per agent. Base scopes (<Lock className="w-3 h-3 inline mb-0.5" />) are locked by default but admins can remove them.
          </p>
        </div>

        {agents.length > 0 && (
          <div className="flex items-center gap-2 border border-slate-200 rounded-lg px-3 py-2 bg-white focus-within:border-slate-400 transition-colors">
            <Search className="w-3.5 h-3.5 text-slate-400 shrink-0" />
            <input
              type="text"
              value={agentSearch}
              onChange={e => setAgentSearch(e.target.value)}
              placeholder="Search agents by name or Slack app ID…"
              className="flex-1 bg-transparent text-xs text-slate-700 placeholder:text-slate-400 focus:outline-none"
            />
            {agentSearch && (
              <button onClick={() => setAgentSearch("")}
                className="text-slate-400 hover:text-slate-600 transition-colors" title="Clear">
                <X className="w-3.5 h-3.5" />
              </button>
            )}
            <span className="text-[11px] text-slate-400 shrink-0 tabular-nums">
              {visibleAgents.length}/{agents.length}
            </span>
          </div>
        )}
      </div>

      {agents.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-center border border-dashed border-slate-200 rounded-xl">
          <Bot className="w-8 h-8 text-slate-300 mb-2" />
          <p className="text-sm font-semibold text-slate-700">No agents found</p>
        </div>
      ) : visibleAgents.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-center border border-dashed border-slate-200 rounded-xl">
          <Search className="w-8 h-8 text-slate-300 mb-2" />
          <p className="text-sm font-semibold text-slate-700">No agents match "{agentSearch}"</p>
        </div>
      ) : (
        <div className="flex-1 min-h-0 overflow-y-auto space-y-3 pr-1 pb-4">
          {visibleAgents.map(agent => {
            const isEditing = agent.id in editing;
            const scopes = isEditing ? editing[agent.id] : (agent.bot_scopes ?? BASE_SCOPES);
            const pendingSet = new Set(agent.pending_scopes ?? []);
            const addable = CONDITIONAL_SCOPES.filter(
              c => !scopes.includes(c.scope) &&
                (search === "" || c.scope.includes(search.toLowerCase()) || c.description.toLowerCase().includes(search.toLowerCase()))
            );
            const isSaving = saving === agent.id;

            return (
              <div
                key={agent.id}
                className={`border rounded-xl bg-white overflow-hidden transition-colors ${isEditing ? "border-slate-400 ring-1 ring-slate-300" : "border-slate-200"}`}
              >
                {/* Agent header */}
                <div className="flex items-center gap-3 px-4 py-3 border-b border-slate-100 bg-slate-50">
                  <div className="w-7 h-7 rounded-lg border border-slate-200 bg-slate-100 overflow-hidden shrink-0 flex items-center justify-center">
                    {agent.avatar_url
                      ? <img src={agent.avatar_url} alt={agent.name} className="w-full h-full object-cover" />
                      : <Bot className="w-3.5 h-3.5 text-slate-400" />}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-semibold text-slate-900 truncate">{agent.name}</p>
                    <p className="text-[11px] font-mono text-slate-400 truncate">{agent.slack_app_id}</p>
                  </div>
                  {!agent.bot_scopes && !isEditing && (
                    <span className="text-[11px] text-slate-400 italic">default scopes</span>
                  )}
                  {isEditing ? (
                    <div className="flex items-center gap-2">
                      <button onClick={() => cancelEdit(agent.id)} disabled={isSaving}
                        className="text-xs px-2.5 py-1 border border-slate-200 text-slate-500 rounded-lg hover:bg-slate-100 transition-colors">
                        Cancel
                      </button>
                      <button onClick={() => save(agent.id)} disabled={isSaving}
                        className="flex items-center gap-1 text-xs px-2.5 py-1 bg-slate-900 text-white rounded-lg hover:bg-slate-700 transition-colors font-semibold disabled:opacity-50">
                        {isSaving ? <Loader2 className="w-3 h-3 animate-spin" /> : <CheckCircle2 className="w-3 h-3" />}
                        Save
                      </button>
                    </div>
                  ) : (
                    <button onClick={() => enterEdit(agent)}
                      className="flex items-center gap-1 text-xs px-2.5 py-1 border border-slate-300 text-slate-600 rounded-lg hover:bg-slate-100 transition-colors font-medium">
                      <Pencil className="w-3 h-3" /> Edit
                    </button>
                  )}
                </div>

                {/* Search bar — always visible in edit mode */}
                {isEditing && (
                  <div className="border-b border-slate-100">
                    <div className="flex items-center gap-2 px-3 py-2">
                      <Search className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                      <input
                        autoFocus
                        type="text"
                        value={search}
                        onChange={e => setSearch(e.target.value)}
                        placeholder="Search scopes to add…"
                        className="flex-1 bg-transparent text-xs text-slate-700 placeholder:text-slate-400 focus:outline-none"
                      />
                    </div>
                    {search && (
                      <div className="max-h-44 overflow-y-auto divide-y divide-slate-50 border-t border-slate-100">
                        {addable.length === 0
                          ? <p className="px-4 py-3 text-xs text-slate-400 text-center">No matching scopes to add.</p>
                          : addable.map(c => (
                            <button key={c.scope} onClick={() => stageAdd(agent.id, c.scope)}
                              className="w-full flex items-center gap-3 px-4 py-2 hover:bg-slate-50 transition-colors text-left">
                              <Plus className="w-3 h-3 text-slate-400 shrink-0" />
                              <span className="font-mono text-xs text-slate-700">{c.scope}</span>
                              <span className="text-[11px] text-slate-400 ml-auto truncate max-w-[200px]">{c.description}</span>
                            </button>
                          ))}
                      </div>
                    )}
                  </div>
                )}

                {/* Scope pills */}
                <div className="px-4 py-3 flex flex-wrap gap-2">
                  {scopes.map(scope => {
                    const isBase = baseSet.has(scope);
                    const isPending = !isEditing && pendingSet.has(scope);
                    return (
                      <span key={scope}
                        className={`inline-flex items-center gap-1 text-xs font-mono px-2.5 py-1 rounded-lg border ${
                          isPending
                            ? "bg-amber-50 border-amber-300 text-amber-700"
                            : isBase
                            ? "bg-slate-50 border-slate-200 text-slate-600"
                            : "bg-violet-50 border-violet-200 text-violet-700"
                        }`}
                      >
                        {isBase && !isEditing && <Lock className="w-3 h-3 shrink-0 opacity-50" />}
                        {scope}
                        {isPending && (
                          <span className="ml-1 text-[10px] font-sans font-semibold text-amber-600 bg-amber-100 border border-amber-200 rounded px-1 leading-4">
                            pending
                          </span>
                        )}
                        {isEditing && (
                          <button onClick={() => stageRemove(agent.id, scope)}
                            className="ml-0.5 text-slate-400 hover:text-red-500 transition-colors" title="Remove">
                            <X className="w-3 h-3 shrink-0" />
                          </button>
                        )}
                      </span>
                    );
                  })}
                </div>

                {/* Reinstall banner — shown while scopes are pending */}
                {!isEditing && pendingSet.size > 0 && (
                  <div className="mx-4 mb-3 flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
                    <Loader2 className="w-3.5 h-3.5 text-amber-500 mt-0.5 shrink-0 animate-spin" />
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-semibold text-amber-800">Reinstall in progress</p>
                      <p className="text-[11px] text-amber-700 mt-0.5 leading-relaxed">
                        A Slack reinstall tab was opened. Once the workspace admin approves and the OAuth flow completes, the pending scopes will become active automatically.
                      </p>
                    </div>
                  </div>
                )}

                {saveError[agent.id] && (
                  <p className="px-4 pb-3 text-xs text-red-600">{saveError[agent.id]}</p>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════════════
// SYSTEMS TAB
// ═══════════════════════════════════════════════════════════════════════════════

interface SystemSkillRow {
  slug: string;
  name: string;
  risk_level: string | null;
  integration_tags: string[];
  directions: string[];
  match_confidence: string | null;
  sources: string[];
}

interface SystemAgentRow {
  id: string;
  name: string;
  avatar_url: string | null;
  status: string;
  via_skills: string[];
}

interface SystemRow {
  system_name: string;
  risk_level: string | null;
  system_purpose?: string | null;
  sso_enabled?: boolean | null;
  mfa_enabled?: boolean | null;
  system_status?: string | null;
  skills: SystemSkillRow[];
  agents: SystemAgentRow[];
  skill_count: number;
  agent_count: number;
}

interface SystemsData {
  systems: SystemRow[];
  unmatched_tags: { integration_tag: string; skills: string[] }[];
  stale_links: { system_name: string; skills: string[] }[];
  totals: { systems: number; connected_systems: number; skills: number; agents: number };
  registry_available: boolean;
  fetched_at: string;
}

type SystemsRiskFilter = "all" | "critical" | "high" | "medium" | "low";

/** Loose bucketing — Risk levels are free text ("High Risk", "Low risk", …). */
function riskBucket(riskLevel: string | null | undefined): SystemsRiskFilter | "unknown" {
  const v = (riskLevel ?? "").toLowerCase();
  if (!v) return "unknown";
  if (v.includes("critical")) return "critical";
  if (v.includes("high")) return "high";
  if (v.includes("medium") || v.includes("elevated")) return "medium";
  if (v.includes("low")) return "low";
  return "unknown";
}

function DirectionBadge({ direction }: { direction: string }) {
  const label = direction === "read_write" ? "read/write" : direction;
  const colors =
    direction === "read"
      ? "bg-sky-50 text-sky-700 border-sky-200"
      : "bg-orange-50 text-orange-700 border-orange-200";
  return (
    <span className={`px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider border ${colors}`}>
      {label}
    </span>
  );
}

function SystemCard({
  system,
  expanded,
  onToggle,
  onOpenSkill,
  onOpenAgent,
}: {
  system: SystemRow;
  expanded: boolean;
  onToggle: () => void;
  onOpenSkill: (slug: string) => void;
  onOpenAgent: (id: string) => void;
}) {
  const connected = system.skill_count > 0;
  return (
    <div className={`bg-white border rounded-xl overflow-hidden ${connected ? "border-slate-200" : "border-slate-100"}`}>
      <button
        onClick={onToggle}
        disabled={!connected}
        className={`w-full flex items-center gap-3 px-4 py-3 text-left ${connected ? "hover:bg-slate-50 cursor-pointer" : "cursor-default"}`}
      >
        {connected ? (
          expanded ? <ChevronUp className="w-3.5 h-3.5 text-slate-400 shrink-0" />
            : <ChevronDown className="w-3.5 h-3.5 text-slate-400 shrink-0" />
        ) : (
          <span className="w-3.5 shrink-0" />
        )}

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`text-sm font-semibold truncate ${connected ? "text-slate-900" : "text-slate-500"}`}>
              {system.system_name}
            </span>
            <SystemCriticalityBadge riskLevel={system.risk_level} />
            {system.system_status && system.system_status.toLowerCase() !== "active" && (
              <span className="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider border bg-slate-100 text-slate-500 border-slate-200">
                {system.system_status}
              </span>
            )}
            {system.sso_enabled === true && (
              <span className="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider border bg-violet-50 text-violet-700 border-violet-200">
                SSO
              </span>
            )}
          </div>
          {system.system_purpose && (
            <p className="text-[11px] text-slate-400 mt-0.5 truncate">{system.system_purpose}</p>
          )}
        </div>

        <div className="flex items-center gap-4 shrink-0 text-xs">
          <span className={`flex items-center gap-1 ${connected ? "text-slate-600" : "text-slate-300"}`}>
            <Layers className="w-3.5 h-3.5" /> {system.skill_count}
          </span>
          <span className={`flex items-center gap-1 ${system.agent_count ? "text-slate-600" : "text-slate-300"}`}>
            <Bot className="w-3.5 h-3.5" /> {system.agent_count}
          </span>
        </div>
      </button>

      {expanded && connected && (
        <div className="border-t border-slate-100 px-4 py-3 grid grid-cols-1 lg:grid-cols-2 gap-4 bg-slate-50/60">
          {/* Skills integrated with this system */}
          <div>
            <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400 mb-2 flex items-center gap-1">
              <Layers className="w-3 h-3" /> Skills ({system.skills.length})
            </p>
            <div className="space-y-1.5">
              {system.skills.map(s => (
                <div key={s.slug} className="bg-white border border-slate-200 rounded-lg px-2.5 py-2">
                  <div className="flex items-center gap-2 flex-wrap">
                    <button
                      onClick={() => onOpenSkill(s.slug)}
                      className="text-xs font-semibold text-slate-800 hover:text-[#FF444F] hover:underline truncate"
                    >
                      {s.name}
                    </button>
                    {s.directions.map(d => <DirectionBadge key={d} direction={d} />)}
                    {s.match_confidence && s.match_confidence !== "exact" && (
                      <span
                        title="How confidently this skill's integration tag was matched to the catalogued system"
                        className="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider border bg-amber-50 text-amber-700 border-amber-200"
                      >
                        {s.match_confidence}
                      </span>
                    )}
                  </div>
                  {s.integration_tags.length > 0 && (
                    <p className="text-[10px] text-slate-400 mt-1 font-mono truncate">
                      {s.integration_tags.join(", ")}
                    </p>
                  )}
                </div>
              ))}
            </div>
          </div>

          {/* Agents connected via those skills */}
          <div>
            <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400 mb-2 flex items-center gap-1">
              <Bot className="w-3 h-3" /> Agents ({system.agents.length})
            </p>
            {system.agents.length === 0 ? (
              <p className="text-xs text-slate-400 bg-white border border-dashed border-slate-200 rounded-lg px-2.5 py-3">
                No agent has any of these skills attached yet.
              </p>
            ) : (
              <div className="space-y-1.5">
                {system.agents.map(a => (
                  <div key={a.id} className="bg-white border border-slate-200 rounded-lg px-2.5 py-2 flex items-start gap-2">
                    <div className="w-6 h-6 rounded-md bg-slate-100 border border-slate-200 shrink-0 overflow-hidden flex items-center justify-center">
                      {a.avatar_url
                        ? <img src={a.avatar_url} alt="" className="w-full h-full object-cover" />
                        : <Bot className="w-3 h-3 text-slate-400" />}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <button
                          onClick={() => onOpenAgent(a.id)}
                          className="text-xs font-semibold text-slate-800 hover:text-[#FF444F] hover:underline truncate"
                        >
                          {a.name}
                        </button>
                        <span className="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider border bg-slate-100 text-slate-500 border-slate-200">
                          {a.status}
                        </span>
                      </div>
                      <p className="text-[10px] text-slate-400 mt-0.5 truncate">
                        via {a.via_skills.join(", ")}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function SystemsTab() {
  const navigate = useNavigate();
  const [data, setData] = useState<SystemsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [risk, setRisk] = useState<SystemsRiskFilter>("all");
  const [connectedOnly, setConnectedOnly] = useState(true);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const res = await apiFetch("/api/admin/systems");
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { detail?: string }).detail ?? `HTTP ${res.status}`);
      }
      setData(await res.json() as SystemsData);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load systems.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = search.trim().toLowerCase();
    return data.systems.filter(s => {
      if (connectedOnly && s.skill_count === 0) return false;
      if (risk !== "all" && riskBucket(s.risk_level) !== risk) return false;
      if (!q) return true;
      return (
        s.system_name.toLowerCase().includes(q) ||
        (s.system_purpose ?? "").toLowerCase().includes(q) ||
        s.skills.some(sk =>
          sk.name.toLowerCase().includes(q) ||
          sk.slug.toLowerCase().includes(q) ||
          sk.integration_tags.some(t => t.toLowerCase().includes(q))
        ) ||
        s.agents.some(a => a.name.toLowerCase().includes(q))
      );
    });
  }, [data, search, risk, connectedOnly]);

  if (loading) return (
    <div className="flex flex-col items-center justify-center h-full gap-3 text-slate-400">
      <Loader2 className="w-6 h-6 animate-spin text-[#FF444F]" />
      <span className="text-sm">Loading connected systems…</span>
    </div>
  );

  if (error && !data) return (
    <div className="flex flex-col items-center justify-center h-full gap-3">
      <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-5 py-4 max-w-md text-center">
        <p className="font-semibold mb-1">Failed to load systems</p>
        <p className="text-xs opacity-80">{error}</p>
      </div>
      <button onClick={() => void load()} className="text-xs text-[#FF444F] hover:underline">Try again</button>
    </div>
  );

  if (!data) return null;

  return (
    <div className="flex flex-col gap-4 h-full overflow-hidden">
      {/* Header + stats */}
      <div className="shrink-0">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
              <Server className="w-4 h-4 text-[#FF444F]" /> Systems
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Active catalogued systems (inactive ones are excluded). Skills declare the systems they
              touch; an agent is connected to a system when it has one of those skills attached.
            </p>
          </div>
          <button onClick={() => void load()}
            className="flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-700 border border-slate-200 px-3 py-1.5 rounded-lg hover:bg-slate-50 transition-colors shrink-0">
            <RefreshCw className="w-3.5 h-3.5" /> Refresh
          </button>
        </div>

        {!data.registry_available && (
          <div className="mt-3 flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2">
            <AlertCircle className="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
            <p className="text-xs text-amber-800">
              The system catalogue is unavailable right now — only systems already referenced by a
              skill link are listed, and their risk levels and active status may be stale.
            </p>
          </div>
        )}

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-3">
          {([
            { label: "Active systems", value: data.totals.systems, icon: <Server className="w-3.5 h-3.5" /> },
            { label: "With integrations", value: data.totals.connected_systems, icon: <Zap className="w-3.5 h-3.5" /> },
            { label: "Skills connected", value: data.totals.skills, icon: <Layers className="w-3.5 h-3.5" /> },
            { label: "Agents connected", value: data.totals.agents, icon: <Bot className="w-3.5 h-3.5" /> },
          ]).map(stat => (
            <div key={stat.label} className="bg-white border border-slate-200 rounded-xl px-3 py-2">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-1">
                {stat.icon} {stat.label}
              </p>
              <p className="text-lg font-bold text-slate-900 mt-0.5">{stat.value}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-3 flex-wrap shrink-0">
        <div className="relative flex-1 min-w-[220px]">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
          <input
            className="w-full pl-8 pr-8 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-[#FF444F]/30 focus:border-[#FF444F]"
            placeholder="Search systems, skills, integration tags, agents…"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
          {search && (
            <button className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600" onClick={() => setSearch("")}>
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
        <select
          className="px-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-[#FF444F]/30 focus:border-[#FF444F] bg-white"
          value={risk}
          onChange={e => setRisk(e.target.value as SystemsRiskFilter)}
        >
          <option value="all">All criticality</option>
          <option value="critical">Critical</option>
          <option value="high">High</option>
          <option value="medium">Medium</option>
          <option value="low">Low</option>
        </select>
        <label className="flex items-center gap-1.5 text-xs text-slate-500 cursor-pointer select-none">
          <input type="checkbox" checked={connectedOnly} onChange={e => setConnectedOnly(e.target.checked)} className="rounded" />
          Only systems with skills
        </label>
        <span className="text-xs text-slate-400 shrink-0">
          {filtered.length} system{filtered.length !== 1 ? "s" : ""}
        </span>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto space-y-2 pr-1">
        {filtered.length === 0 ? (
          <div className="text-sm text-slate-400 text-center py-12">No systems match the current filters.</div>
        ) : (
          filtered.map(s => (
            <SystemCard
              key={s.system_name}
              system={s}
              expanded={!!expanded[s.system_name]}
              onToggle={() => setExpanded(prev => ({ ...prev, [s.system_name]: !prev[s.system_name] }))}
              onOpenSkill={slug => navigate(`/skills/${slug}`)}
              onOpenAgent={id => navigate(`/agents/${id}`)}
            />
          ))
        )}

        {/* Links pointing at systems the catalogue no longer lists as active */}
        {data.stale_links.length > 0 && (
          <div className="mt-4 bg-amber-50 border border-amber-200 rounded-xl px-4 py-3">
            <p className="text-[10px] font-bold uppercase tracking-wider text-amber-700 flex items-center gap-1 mb-2">
              <AlertCircle className="w-3 h-3" /> Stale system links ({data.stale_links.length})
            </p>
            <p className="text-[11px] text-amber-800 mb-2">
              These skills are linked to systems the catalogue now marks inactive (or has removed), so the
              systems are not listed above. Re-run <span className="font-mono">scripts/backfill_skill_systems.py</span> to clear them.
            </p>
            <div className="flex flex-wrap gap-1.5">
              {data.stale_links.map(t => (
                <span
                  key={t.system_name}
                  title={`Linked by: ${t.skills.join(", ")}`}
                  className="px-2 py-0.5 rounded-full text-[10px] bg-white text-amber-800 border border-amber-200"
                >
                  {t.system_name} · {t.skills.length} skill{t.skills.length !== 1 ? "s" : ""}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Integration tags that matched no catalogued system */}
        {data.unmatched_tags.length > 0 && (
          <div className="mt-4 bg-white border border-dashed border-slate-200 rounded-xl px-4 py-3">
            <p className="text-[10px] font-bold uppercase tracking-wider text-slate-400 flex items-center gap-1 mb-2">
              <AlertCircle className="w-3 h-3" /> Unmatched integration tags ({data.unmatched_tags.length})
            </p>
            <p className="text-[11px] text-slate-400 mb-2">
              Declared by a skill but not resolvable to a catalogued system — either not in the registry, or
              named differently there.
            </p>
            <div className="flex flex-wrap gap-1.5">
              {data.unmatched_tags.map(t => (
                <span
                  key={t.integration_tag}
                  title={`Used by: ${t.skills.join(", ")}`}
                  className="px-2 py-0.5 rounded-full text-[10px] font-mono bg-slate-100 text-slate-600 border border-slate-200"
                >
                  {t.integration_tag} · {t.skills.length}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
