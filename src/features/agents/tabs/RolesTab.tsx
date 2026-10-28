import { useEffect, useState } from "react";
import EmployeePicker, { type EmployeeResult } from "@/components/EmployeePicker";
import {
  AlertCircle,
  ChevronDown,
  Loader2,
  Pencil,
  Plus,
  Tag,
  Trash2,
  UserPlus,
} from "lucide-react";
import { apiFetch } from "@/lib/api";

// ── Roles tab ──────────────────────────────────────────────────────────────────

export interface AgentRoleRecord {
  id: string;
  agent_id: string;
  name: string;
  description: string | null;
  skill_slugs: string[];
  created_by_email: string | null;
  archived_at: string | null;
  full_count: number;
  partial_count: number;
}

export interface RoleMemberGrant {
  skill_slug: string;
  grant_id: string;
  expires_at: string | null;
}

export interface RoleMember {
  email: string;
  name: string | null;
  grants: RoleMemberGrant[];
  status: "full" | "partial";
  missing_skill_slugs: string[];
  has_agent_access: boolean;
}

export function RolesTab({ agentId }: { agentId: string }) {
  const [roles, setRoles] = useState<AgentRoleRecord[]>([]);
  const [grantableSkills, setGrantableSkills] = useState<{ slug: string; name: string; grantable: boolean }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [formMode, setFormMode] = useState<"create" | "edit" | null>(null);
  const [editingRoleId, setEditingRoleId] = useState<string | null>(null);
  const [formName, setFormName] = useState("");
  const [formDescription, setFormDescription] = useState("");
  const [formSlugs, setFormSlugs] = useState<Set<string>>(new Set());
  const [formError, setFormError] = useState<string | null>(null);
  const [formSubmitting, setFormSubmitting] = useState(false);

  const [expandedRoleId, setExpandedRoleId] = useState<string | null>(null);
  const [members, setMembers] = useState<Record<string, RoleMember[]>>({});
  const [membersLoading, setMembersLoading] = useState<string | null>(null);

  const [assigningRoleId, setAssigningRoleId] = useState<string | null>(null);
  const [assignEmployees, setAssignEmployees] = useState<EmployeeResult[]>([]);
  const [assignJustification, setAssignJustification] = useState("");
  const [assignGrantType, setAssignGrantType] = useState<"time_limited" | "permanent" | "restricted">("time_limited");
  const [assignExpiresDays, setAssignExpiresDays] = useState("90");
  const [assignError, setAssignError] = useState<string | null>(null);
  const [assignSubmitting, setAssignSubmitting] = useState(false);

  const [actionBusyId, setActionBusyId] = useState<string | null>(null);

  const grantableOnly = grantableSkills.filter(s => s.grantable);
  const skillName = (slug: string) => grantableSkills.find(s => s.slug === slug)?.name || slug;

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const [rolesRes, skillsRes] = await Promise.all([
        apiFetch(`/api/agents/${agentId}/roles`),
        apiFetch(`/api/agents/${agentId}/skills/grantable`),
      ]);
      if (!rolesRes.ok) throw new Error(`HTTP ${rolesRes.status}`);
      if (!skillsRes.ok) throw new Error(`HTTP ${skillsRes.status}`);
      const rolesData = await rolesRes.json();
      const skillsData = await skillsRes.json();
      setRoles(rolesData.roles ?? []);
      setGrantableSkills(skillsData.skills ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load roles.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, [agentId]);

  const loadMembers = async (roleId: string) => {
    setMembersLoading(roleId);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/roles/${roleId}/members`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setMembers(prev => ({ ...prev, [roleId]: data.members ?? [] }));
    } catch {
      setError("Could not load role members.");
    } finally {
      setMembersLoading(null);
    }
  };

  const toggleExpand = (roleId: string) => {
    if (expandedRoleId === roleId) {
      setExpandedRoleId(null);
      return;
    }
    setExpandedRoleId(roleId);
    if (!members[roleId]) void loadMembers(roleId);
  };

  const openCreateForm = () => {
    setFormMode("create");
    setEditingRoleId(null);
    setFormName("");
    setFormDescription("");
    setFormSlugs(new Set());
    setFormError(null);
  };

  const openEditForm = (role: AgentRoleRecord) => {
    setFormMode("edit");
    setEditingRoleId(role.id);
    setFormName(role.name);
    setFormDescription(role.description ?? "");
    setFormSlugs(new Set(role.skill_slugs));
    setFormError(null);
  };

  const closeForm = () => {
    setFormMode(null);
    setEditingRoleId(null);
    setFormError(null);
  };

  const toggleFormSlug = (slug: string) => {
    setFormSlugs(prev => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug); else next.add(slug);
      return next;
    });
  };

  const submitForm = async () => {
    const name = formName.trim();
    if (!name) { setFormError("Name is required."); return; }
    if (formSlugs.size === 0) { setFormError("Select at least one skill for this role."); return; }

    setFormSubmitting(true);
    setFormError(null);
    try {
      const body = {
        name,
        description: formDescription.trim() || null,
        skill_slugs: Array.from(formSlugs),
      };
      const url =
        formMode === "edit" && editingRoleId
          ? `/api/agents/${agentId}/roles/${editingRoleId}`
          : `/api/agents/${agentId}/roles`;
      const res = await apiFetch(url, {
        method: formMode === "edit" ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error((detail as { detail?: string }).detail || `HTTP ${res.status}`);
      }
      closeForm();
      await load();
    } catch (e) {
      setFormError(e instanceof Error ? e.message : "Failed to save role.");
    } finally {
      setFormSubmitting(false);
    }
  };

  const deleteRole = async (role: AgentRoleRecord) => {
    if (!window.confirm(
      `Delete role "${role.name}"? This revokes the skill access and agent access it granted for everyone who has it. Access they hold directly, or via another role, is not affected.`
    )) return;
    setActionBusyId(role.id);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/roles/${role.id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await load();
    } catch {
      setError("Failed to delete role.");
    } finally {
      setActionBusyId(null);
    }
  };

  const openAssignForm = (roleId: string) => {
    setAssigningRoleId(roleId);
    setAssignEmployees([]);
    setAssignJustification("");
    setAssignGrantType("time_limited");
    setAssignExpiresDays("90");
    setAssignError(null);
  };

  const submitAssign = async (roleId: string) => {
    const unique = Array.from(new Set(assignEmployees.map(e => e.email)));
    if (unique.length === 0) { setAssignError("Add at least one employee."); return; }
    if (assignJustification.trim().length < 10) { setAssignError("Justification must be at least 10 characters."); return; }

    setAssignSubmitting(true);
    setAssignError(null);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/roles/${roleId}/assign`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_emails: unique,
          grant_type: assignGrantType,
          expires_in_days: assignGrantType === "permanent" ? null : parseInt(assignExpiresDays, 10) || 90,
          justification: assignJustification.trim(),
        }),
      });
      const body = await res.json().catch(() => ({})) as {
        detail?: string | { message?: string; errors?: Array<{ email: string; error: string }> };
        granted?: number; failed?: number; errors?: Array<{ email: string; error: string }>;
      };
      if (!res.ok) {
        const detail = body.detail;
        const msg = typeof detail === "string" ? detail : detail?.message ?? `HTTP ${res.status}`;
        throw new Error(msg);
      }
      if (body.failed && body.failed > 0 && body.errors?.length) {
        const summary = body.errors.map(e => `${e.email}: ${e.error}`).join("; ");
        setAssignError(`Granted ${body.granted ?? 0}, failed ${body.failed}: ${summary}`);
        await load();
        if (members[roleId]) await loadMembers(roleId);
        return;
      }
      setAssigningRoleId(null);
      await load();
      if (members[roleId]) await loadMembers(roleId);
    } catch (e) {
      setAssignError(e instanceof Error ? e.message : "Failed to assign role.");
    } finally {
      setAssignSubmitting(false);
    }
  };

  const revokeMember = async (roleId: string, member: RoleMember) => {
    if (!window.confirm(`Revoke this role from ${member.email}?`)) return;
    setActionBusyId(`${roleId}:${member.email}`);
    try {
      const res = await apiFetch(
        `/api/agents/${agentId}/roles/${roleId}/members/${encodeURIComponent(member.email)}/revoke`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) }
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({})) as { detail?: string };
        throw new Error(body.detail || `HTTP ${res.status}`);
      }
      await load();
      await loadMembers(roleId);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to revoke role from user.");
    } finally {
      setActionBusyId(null);
    }
  };

  const memberStatusBadge = (status: "full" | "partial") => (
    <span
      className={`inline-flex text-[11px] font-medium border rounded-full px-2 py-0.5 ${
        status === "full"
          ? "bg-emerald-50 text-emerald-700 border-emerald-200"
          : "bg-amber-50 text-amber-700 border-amber-200"
      }`}
    >
      {status === "full" ? "Full" : "Partial"}
    </span>
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">Roles</h3>
          <p className="text-xs text-slate-500 mt-0.5">
            Named bundles of skills (e.g. "CRI User") you can assign to people in one action, for cleaner auditing.
          </p>
        </div>
        {formMode === null && (
          <button
            type="button"
            onClick={openCreateForm}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 bg-slate-900 text-white rounded-lg hover:bg-slate-700 transition-colors font-semibold shrink-0"
          >
            <Plus className="w-3.5 h-3.5" /> New Role
          </button>
        )}
      </div>

      {formMode !== null && (
        <div className="border border-slate-200 rounded-xl bg-white p-4 space-y-3">
          <p className="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
            <Tag className="w-3.5 h-3.5 text-indigo-500" /> {formMode === "edit" ? "Edit role" : "New role"}
          </p>
          <input
            autoFocus
            value={formName}
            onChange={e => setFormName(e.target.value)}
            placeholder="e.g. CRI User"
            maxLength={120}
            className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-400 placeholder:text-slate-400"
          />
          <textarea
            value={formDescription}
            onChange={e => setFormDescription(e.target.value)}
            placeholder="What is this role for? (optional)"
            rows={2}
            maxLength={2000}
            className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-200 focus:border-indigo-400 placeholder:text-slate-400 resize-none"
          />
          <div>
            <p className="text-xs font-semibold text-slate-700 mb-2">Skills in this role</p>
            <div className="flex flex-wrap gap-2">
              {grantableOnly.map(skill => (
                <label
                  key={skill.slug}
                  className={`inline-flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg border cursor-pointer transition-colors ${
                    formSlugs.has(skill.slug)
                      ? "bg-indigo-50 border-indigo-300 text-indigo-800"
                      : "bg-white border-slate-200 text-slate-600 hover:bg-slate-50"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={formSlugs.has(skill.slug)}
                    onChange={() => toggleFormSlug(skill.slug)}
                    className="rounded border-slate-300 text-indigo-600 focus:ring-indigo-200"
                  />
                  {skill.name || skill.slug}
                </label>
              ))}
            </div>
            {grantableOnly.length === 0 && (
              <p className="text-[11px] text-slate-400 mt-2">This agent has no grantable skills to bundle into a role.</p>
            )}
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => void submitForm()}
              disabled={formSubmitting || !formName.trim()}
              className="flex items-center gap-1.5 px-4 py-2 text-xs font-semibold bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition-colors"
            >
              {formSubmitting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
              {formMode === "edit" ? "Save" : "Create"}
            </button>
            <button
              type="button"
              onClick={closeForm}
              className="px-3 py-2 text-xs text-slate-500 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
            >
              Cancel
            </button>
          </div>
          {formError && (
            <p className="text-[11px] text-red-600 flex items-center gap-1">
              <AlertCircle className="w-3 h-3 shrink-0" /> {formError}
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
        <div className="flex items-center justify-center py-10"><Loader2 className="w-4 h-4 animate-spin text-slate-400" /></div>
      ) : roles.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-center border border-dashed border-slate-200 rounded-xl">
          <Tag className="w-8 h-8 text-slate-300 mb-2" />
          <p className="text-sm font-semibold text-slate-700">No roles yet</p>
          <p className="text-xs text-slate-400 mt-1 max-w-xs">
            Click "New Role" to bundle a set of skills under a reusable name.
          </p>
        </div>
      ) : (
        <div className="divide-y divide-slate-100 border border-slate-200 rounded-xl overflow-hidden">
          {roles.map(role => (
            <div key={role.id} className="bg-white">
              <div className="flex items-center gap-3 px-4 py-3 hover:bg-slate-50 transition-colors group">
                <button
                  type="button"
                  onClick={() => toggleExpand(role.id)}
                  className="flex items-center gap-2 flex-1 min-w-0 text-left"
                >
                  <ChevronDown
                    className={`w-3.5 h-3.5 text-slate-400 shrink-0 transition-transform ${expandedRoleId === role.id ? "" : "-rotate-90"}`}
                  />
                  <div className="min-w-0">
                    <p className="text-xs font-semibold text-slate-900 truncate">{role.name}</p>
                    {role.description && (
                      <p className="text-xs text-slate-500 truncate">{role.description}</p>
                    )}
                    <div className="flex flex-wrap gap-1 mt-1">
                      {role.skill_slugs.map(slug => (
                        <span key={slug} className="text-[10px] px-1.5 py-0.5 bg-slate-100 text-slate-500 rounded">
                          {skillName(slug)}
                        </span>
                      ))}
                    </div>
                  </div>
                </button>
                <div className="flex items-center gap-2 shrink-0">
                  {role.full_count > 0 && (
                    <span className="inline-flex text-[11px] font-medium border rounded-full px-2 py-0.5 bg-emerald-50 text-emerald-700 border-emerald-200">
                      {role.full_count} full
                    </span>
                  )}
                  {role.partial_count > 0 && (
                    <span className="inline-flex text-[11px] font-medium border rounded-full px-2 py-0.5 bg-amber-50 text-amber-700 border-amber-200">
                      {role.partial_count} partial
                    </span>
                  )}
                  <button
                    type="button"
                    onClick={() => openAssignForm(role.id)}
                    className="text-[11px] font-semibold px-2 py-1 bg-slate-900 text-white rounded-lg hover:bg-slate-700"
                  >
                    Assign
                  </button>
                  <>
                    <button
                      type="button"
                      onClick={() => openEditForm(role)}
                      className="opacity-0 group-hover:opacity-100 text-slate-400 hover:text-slate-700 transition-all"
                      title="Edit role"
                    >
                      <Pencil className="w-3.5 h-3.5" />
                    </button>
                    <button
                      type="button"
                      onClick={() => void deleteRole(role)}
                      disabled={actionBusyId === role.id}
                      className="opacity-0 group-hover:opacity-100 text-slate-400 hover:text-red-500 transition-all disabled:opacity-50"
                      title="Delete role"
                    >
                      {actionBusyId === role.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
                    </button>
                  </>
                </div>
              </div>

              {assigningRoleId === role.id && (
                <div className="mx-4 mb-3 border border-slate-200 rounded-xl bg-slate-50 p-4 space-y-3">
                  <p className="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
                    <UserPlus className="w-3.5 h-3.5 text-emerald-500" /> Assign "{role.name}"
                  </p>
                  <EmployeePicker selected={assignEmployees} onChange={setAssignEmployees} disabled={assignSubmitting} />
                  <textarea
                    value={assignJustification}
                    onChange={e => setAssignJustification(e.target.value)}
                    placeholder="Why do these users need this role? (min 10 characters)"
                    rows={2}
                    maxLength={2000}
                    className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-200 focus:border-emerald-400 placeholder:text-slate-400 resize-none bg-white"
                  />
                  <div className="flex flex-wrap items-center gap-3">
                    <select
                      value={assignGrantType}
                      onChange={e => setAssignGrantType(e.target.value as typeof assignGrantType)}
                      className="text-sm px-3 py-1.5 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-200 bg-white"
                    >
                      <option value="time_limited">Time-limited</option>
                      <option value="permanent">Permanent</option>
                      <option value="restricted">Restricted</option>
                    </select>
                    {assignGrantType !== "permanent" && (
                      <>
                        <label className="text-xs text-slate-600">Expires in (days)</label>
                        <input
                          type="number"
                          min={1}
                          max={365}
                          value={assignExpiresDays}
                          onChange={e => setAssignExpiresDays(e.target.value)}
                          className="w-20 text-sm px-3 py-1.5 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-200 bg-white"
                        />
                      </>
                    )}
                  </div>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => void submitAssign(role.id)}
                      disabled={assignSubmitting || assignEmployees.length === 0 || assignJustification.trim().length < 10}
                      className="flex items-center gap-1.5 px-4 py-2 text-xs font-semibold bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 disabled:opacity-50 transition-colors"
                    >
                      {assignSubmitting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />} Assign
                    </button>
                    <button
                      type="button"
                      onClick={() => setAssigningRoleId(null)}
                      className="px-3 py-2 text-xs text-slate-500 border border-slate-200 rounded-lg hover:bg-slate-100 transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                  {assignError && (
                    <p className="text-[11px] text-red-600 flex items-center gap-1">
                      <AlertCircle className="w-3 h-3 shrink-0" /> {assignError}
                    </p>
                  )}
                </div>
              )}

              {expandedRoleId === role.id && (
                <div className="px-4 pb-3">
                  {membersLoading === role.id ? (
                    <div className="flex items-center justify-center py-6">
                      <Loader2 className="w-4 h-4 animate-spin text-slate-400" />
                    </div>
                  ) : (members[role.id]?.length ?? 0) === 0 ? (
                    <p className="text-xs text-slate-400 py-2">No one holds this role yet.</p>
                  ) : (
                    <div className="divide-y divide-slate-100 border border-slate-200 rounded-lg overflow-hidden">
                      {members[role.id].map(member => (
                        <div key={member.email} className="flex items-center gap-3 px-3 py-2 bg-white hover:bg-slate-50 transition-colors group/member">
                          <div className="flex-1 min-w-0">
                            <p className="text-xs font-semibold text-slate-900 truncate">{member.name ?? member.email}</p>
                            <p className="text-[11px] text-slate-500 truncate">{member.email}</p>
                            {member.status === "partial" && (
                              <p className="text-[11px] text-amber-600 mt-0.5">
                                Missing:{" "}
                                {[
                                  ...member.missing_skill_slugs.map(skillName),
                                  ...(member.has_agent_access ? [] : ["agent access"]),
                                ].join(", ")}
                              </p>
                            )}
                          </div>
                          <div className="flex items-center gap-2 shrink-0">
                            {memberStatusBadge(member.status)}
                            <button
                              type="button"
                              onClick={() => void revokeMember(role.id, member)}
                              disabled={actionBusyId === `${role.id}:${member.email}`}
                              className="opacity-0 group-hover/member:opacity-100 text-[11px] font-semibold px-2 py-1 text-red-600 border border-red-200 rounded-lg hover:bg-red-50 disabled:opacity-50 transition-all"
                            >
                              {actionBusyId === `${role.id}:${member.email}` ? <Loader2 className="w-3 h-3 animate-spin" /> : "Revoke"}
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
