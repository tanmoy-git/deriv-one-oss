import { useEffect, useState, type ReactNode } from "react";
import {
  AlertCircle,
  Brain,
  ChevronLeft,
  ChevronRight,
  Loader2,
  Pencil,
  Plus,
  Search,
  Trash2,
} from "lucide-react";
import { apiFetch } from "@/lib/api";
import { AgentMemoryListResponse, AgentMemoryRecord } from "@/data/agents";

// ── Memory tab ───────────────────────────────────────────────────────────────

export const MEMORY_PAGE_SIZE = 50;

export function MemoryTab({
  agentId,
  onTotalChange,
}: {
  agentId: string;
  onTotalChange: (n: number) => void;
}) {
  const [items, setItems] = useState<AgentMemoryRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [scopeFilter, setScopeFilter] = useState<"" | "agent" | "user">("");
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");

  // Inline editing state
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editKey, setEditKey] = useState("");
  const [editValue, setEditValue] = useState("");
  const [saving, setSaving] = useState(false);

  // Add-new row state
  const [adding, setAdding] = useState(false);
  const [newScope, setNewScope] = useState<"agent" | "user">("agent");
  const [newKey, setNewKey] = useState("");
  const [newValue, setNewValue] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const [addError, setAddError] = useState<string | null>(null);

  useEffect(() => {
    const t = window.setTimeout(() => setQuery(searchInput.trim()), 300);
    return () => window.clearTimeout(t);
  }, [searchInput]);

  useEffect(() => { setPage(1); }, [query, scopeFilter]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({ page: String(page), limit: String(MEMORY_PAGE_SIZE) });
        if (scopeFilter) params.set("scope", scopeFilter);
        if (query) params.set("search", query);
        const res = await apiFetch(`/api/agents/${agentId}/memories?${params}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: AgentMemoryListResponse = await res.json();
        if (cancelled) return;
        const nextItems = Array.isArray(data.items) ? data.items : [];
        const nextTotal = typeof data.total === "number" ? data.total : 0;
        setItems(nextItems);
        setTotal(nextTotal);
        onTotalChange(nextTotal);
      } catch {
        if (!cancelled) setError("Could not load memory.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => { cancelled = true; };
  }, [agentId, page, query, scopeFilter, onTotalChange]);

  const reload = () => {
    setPage(1);
    setQuery(searchInput.trim());
  };

  const startEdit = (item: AgentMemoryRecord) => {
    setEditingId(item.id);
    setEditKey(item.key);
    setEditValue(item.value);
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditKey("");
    setEditValue("");
  };

  const saveEdit = async (item: AgentMemoryRecord) => {
    setSaving(true);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/memories/${item.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: editKey.trim(), value: editValue.trim() }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const updated: AgentMemoryRecord = await res.json();
      setItems(prev => prev.map(m => (m.id === updated.id ? updated : m)));
      setEditingId(null);
    } catch {
      // leave editing open so user can retry
    } finally {
      setSaving(false);
    }
  };

  const deleteOne = async (id: string) => {
    if (!window.confirm("Delete this memory?")) return;
    try {
      await apiFetch(`/api/agents/${agentId}/memories/${id}`, { method: "DELETE" });
      setItems(prev => {
        const next = prev.filter(m => m.id !== id);
        const nextTotal = total - 1;
        setTotal(nextTotal);
        onTotalChange(nextTotal);
        return next;
      });
    } catch {
      setError("Delete failed.");
    }
  };

  const clearAll = async () => {
    const msg = scopeFilter
      ? `Clear all ${scopeFilter}-scope memories for this agent?`
      : "Clear ALL memories for this agent? This cannot be undone.";
    if (!window.confirm(msg)) return;
    try {
      const params = new URLSearchParams();
      if (scopeFilter) params.set("scope", scopeFilter);
      await apiFetch(`/api/agents/${agentId}/memories?${params}`, { method: "DELETE" });
      setItems([]);
      setTotal(0);
      onTotalChange(0);
    } catch {
      setError("Clear failed.");
    }
  };

  const submitAdd = async () => {
    setAddError(null);
    if (!newKey.trim() || !newValue.trim()) {
      setAddError("Key and value are required.");
      return;
    }
    if (newScope === "user" && !newEmail.trim()) {
      setAddError("User email is required for user-scope memories.");
      return;
    }
    setSaving(true);
    try {
      const body: Record<string, string> = {
        scope: newScope,
        key: newKey.trim(),
        value: newValue.trim(),
      };
      if (newScope === "user") body.user_email = newEmail.trim().toLowerCase();
      const res = await apiFetch(`/api/agents/${agentId}/memories`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail?.detail ?? `HTTP ${res.status}`);
      }
      setAdding(false);
      setNewKey("");
      setNewValue("");
      setNewEmail("");
      setNewScope("agent");
      reload();
    } catch (err) {
      setAddError(err instanceof Error ? err.message : "Save failed.");
    } finally {
      setSaving(false);
    }
  };

  const totalPages = Math.max(1, Math.ceil(total / MEMORY_PAGE_SIZE));

  return (
    <div className="space-y-4">
      <div className="bg-white border border-slate-200 rounded-xl p-4 space-y-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-bold text-slate-900">Agent memory</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Facts the agent remembers across conversations. Auto-extracted after each interaction or added manually.
            </p>
          </div>
          <div className="flex gap-2 shrink-0">
            <button
              type="button"
              onClick={() => setAdding(a => !a)}
              className="inline-flex items-center gap-1 px-3 py-1.5 text-xs font-semibold border border-slate-200 rounded-lg bg-white text-slate-700 hover:bg-slate-50 transition-colors"
            >
              <Plus className="w-3.5 h-3.5" />
              Add
            </button>
            {total > 0 && (
              <button
                type="button"
                onClick={clearAll}
                className="inline-flex items-center gap-1 px-3 py-1.5 text-xs font-semibold border border-red-200 rounded-lg bg-white text-red-600 hover:bg-red-50 transition-colors"
              >
                <Trash2 className="w-3.5 h-3.5" />
                Clear{scopeFilter ? ` ${scopeFilter}` : " all"}
              </button>
            )}
          </div>
        </div>

        {adding && (
          <div className="border border-slate-200 rounded-lg p-3 bg-slate-50 space-y-2">
            <p className="text-xs font-semibold text-slate-700">New memory</p>
            <div className="flex flex-col sm:flex-row gap-2">
              <select
                value={newScope}
                onChange={e => setNewScope(e.target.value as "agent" | "user")}
                className="text-xs px-2 py-1.5 border border-slate-200 rounded-lg bg-white"
              >
                <option value="agent">Agent scope</option>
                <option value="user">User scope</option>
              </select>
              <input
                type="text"
                placeholder="Key (e.g. prefers_bullets)"
                value={newKey}
                onChange={e => setNewKey(e.target.value)}
                className="flex-1 text-xs px-2 py-1.5 border border-slate-200 rounded-lg focus:outline-none focus:border-slate-400"
              />
              <input
                type="text"
                placeholder="Value"
                value={newValue}
                onChange={e => setNewValue(e.target.value)}
                className="flex-1 text-xs px-2 py-1.5 border border-slate-200 rounded-lg focus:outline-none focus:border-slate-400"
              />
              {newScope === "user" && (
                <input
                  type="email"
                  placeholder="user@example.com"
                  value={newEmail}
                  onChange={e => setNewEmail(e.target.value)}
                  className="flex-1 text-xs px-2 py-1.5 border border-slate-200 rounded-lg focus:outline-none focus:border-slate-400"
                />
              )}
            </div>
            {addError && <p className="text-[11px] text-red-600">{addError}</p>}
            <div className="flex gap-2">
              <button
                type="button"
                onClick={submitAdd}
                disabled={saving}
                className="px-3 py-1.5 text-xs font-semibold bg-slate-900 text-white rounded-lg hover:bg-slate-700 disabled:opacity-50"
              >
                {saving ? <Loader2 className="w-3 h-3 animate-spin inline" /> : "Save"}
              </button>
              <button
                type="button"
                onClick={() => { setAdding(false); setAddError(null); }}
                className="px-3 py-1.5 text-xs text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50"
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        <div className="flex flex-col sm:flex-row gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
            <input
              type="search"
              value={searchInput}
              onChange={e => setSearchInput(e.target.value)}
              placeholder="Search key or value…"
              className="w-full text-sm pl-9 pr-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:border-slate-400"
            />
          </div>
          <select
            value={scopeFilter}
            onChange={e => setScopeFilter(e.target.value as "" | "agent" | "user")}
            className="text-sm px-3 py-2 border border-slate-200 rounded-lg bg-white focus:outline-none focus:border-slate-400"
          >
            <option value="">All scopes</option>
            <option value="agent">Agent</option>
            <option value="user">User</option>
          </select>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-4 py-3 flex items-start gap-2">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-16 bg-white border border-slate-200 rounded-xl">
          <Loader2 className="w-5 h-5 animate-spin text-slate-400" />
        </div>
      ) : items.length === 0 ? (
        <div className="text-center py-16 bg-white border border-slate-200 rounded-xl">
          <Brain className="w-8 h-8 text-slate-300 mx-auto mb-3" />
          <p className="text-sm text-slate-500">
            {query || scopeFilter ? "No memories match your filters." : "No memories stored yet. They'll be auto-extracted as the agent converses."}
          </p>
        </div>
      ) : (
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm min-w-[640px]">
              <thead className="bg-slate-50 border-b border-slate-200">
                <tr>
                  <MemTh>Scope</MemTh>
                  <MemTh>Key</MemTh>
                  <MemTh>Value</MemTh>
                  <MemTh>User</MemTh>
                  <MemTh>Source</MemTh>
                  <MemTh>Updated</MemTh>
                  <MemTh className="w-16" />
                </tr>
              </thead>
              <tbody>
                {items.map(item => {
                  const isEditing = editingId === item.id;
                  return (
                    <tr key={item.id} className="border-b border-slate-100 last:border-b-0 hover:bg-slate-50 group">
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex text-[10px] font-semibold uppercase tracking-wide rounded-full px-2 py-0.5 ${
                            item.scope === "agent"
                              ? "bg-indigo-50 text-indigo-700"
                              : "bg-emerald-50 text-emerald-700"
                          }`}
                        >
                          {item.scope}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        {isEditing ? (
                          <input
                            autoFocus
                            type="text"
                            value={editKey}
                            onChange={e => setEditKey(e.target.value)}
                            className="w-full text-xs px-2 py-1 border border-slate-300 rounded focus:outline-none focus:border-slate-500"
                          />
                        ) : (
                          <span className="text-xs font-mono text-slate-700">{item.key}</span>
                        )}
                      </td>
                      <td className="px-4 py-3 max-w-xs">
                        {isEditing ? (
                          <input
                            type="text"
                            value={editValue}
                            onChange={e => setEditValue(e.target.value)}
                            onKeyDown={e => {
                              if (e.key === "Enter") void saveEdit(item);
                              if (e.key === "Escape") cancelEdit();
                            }}
                            className="w-full text-xs px-2 py-1 border border-slate-300 rounded focus:outline-none focus:border-slate-500"
                          />
                        ) : (
                          <span className="text-xs text-slate-800 line-clamp-2 whitespace-pre-wrap">{item.value}</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-xs text-slate-500 max-w-[160px] truncate" title={item.user_email ?? ""}>
                        {item.user_email ?? <span className="text-slate-300">—</span>}
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex text-[10px] font-semibold uppercase tracking-wide rounded-full px-2 py-0.5 ${
                            item.source === "manual"
                              ? "bg-amber-50 text-amber-700"
                              : "bg-slate-100 text-slate-500"
                          }`}
                        >
                          {item.source}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-xs text-slate-400 whitespace-nowrap">
                        {new Date(item.updated_at).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}
                      </td>
                      <td className="px-4 py-3">
                        {isEditing ? (
                          <div className="flex items-center gap-1">
                            <button
                              type="button"
                              onClick={() => void saveEdit(item)}
                              disabled={saving}
                              className="px-2 py-0.5 text-[10px] font-bold bg-slate-900 text-white rounded hover:bg-slate-700 disabled:opacity-50"
                            >
                              {saving ? "…" : "Save"}
                            </button>
                            <button
                              type="button"
                              onClick={cancelEdit}
                              className="px-2 py-0.5 text-[10px] text-slate-500 border border-slate-200 rounded hover:bg-slate-50"
                            >
                              Cancel
                            </button>
                          </div>
                        ) : (
                          <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                            <button
                              type="button"
                              onClick={() => startEdit(item)}
                              className="p-1 text-slate-400 hover:text-slate-700"
                              title="Edit"
                            >
                              <Pencil className="w-3.5 h-3.5" />
                            </button>
                            <button
                              type="button"
                              onClick={() => void deleteOne(item.id)}
                              className="p-1 text-slate-400 hover:text-red-600"
                              title="Delete"
                            >
                              <Trash2 className="w-3.5 h-3.5" />
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {totalPages > 1 && (
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 px-4 py-3 border-t border-slate-200 bg-slate-50">
              <p className="text-xs text-slate-500">
                Showing {total === 0 ? 0 : (page - 1) * MEMORY_PAGE_SIZE + 1}–{Math.min(page * MEMORY_PAGE_SIZE, total)} of {total.toLocaleString()}
              </p>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="inline-flex items-center gap-1 px-3 py-1.5 text-xs font-medium border border-slate-200 rounded-lg bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                >
                  <ChevronLeft className="w-3.5 h-3.5" /> Previous
                </button>
                <span className="text-xs text-slate-500 tabular-nums">Page {page} of {totalPages}</span>
                <button
                  type="button"
                  onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  className="inline-flex items-center gap-1 px-3 py-1.5 text-xs font-medium border border-slate-200 rounded-lg bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-40"
                >
                  Next <ChevronRight className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function MemTh({ children, className = "" }: { children?: ReactNode; className?: string }) {
  return (
    <th className={`text-left text-[11px] font-semibold text-slate-500 uppercase tracking-wider px-4 py-2.5 ${className}`}>
      {children}
    </th>
  );
}
