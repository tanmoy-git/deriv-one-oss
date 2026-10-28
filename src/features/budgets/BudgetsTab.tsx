import { useCallback, useEffect, useState, type FormEvent } from "react";
import { AlertTriangle, Loader2, Plus, RefreshCw, Trash2, Wallet } from "lucide-react";
import { apiFetch } from "@/lib/api";
import { barWidth, forecastText, formatUsd, SCOPE_LABEL, STATE_LABEL } from "./format";
import type { Budget, BudgetList, BudgetScope, BudgetState } from "./types";

interface AgentOption {
  id: string;
  name: string;
}

const STATE_STYLE: Record<BudgetState, { bar: string; pill: string }> = {
  ok: { bar: "bg-emerald-500", pill: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  soft: { bar: "bg-amber-500", pill: "bg-amber-50 text-amber-700 border-amber-200" },
  exceeded: { bar: "bg-red-500", pill: "bg-red-50 text-red-700 border-red-200" },
};

async function errorDetail(res: Response): Promise<string> {
  const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) return "Check the limit and alert threshold.";
  return `HTTP ${res.status}`;
}

export default function BudgetsTab() {
  const [data, setData] = useState<BudgetList | null>(null);
  const [agents, setAgents] = useState<AgentOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/api/admin/budgets");
      if (!res.ok) throw new Error(await errorDetail(res));
      setData((await res.json()) as BudgetList);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to load budgets.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    apiFetch("/api/agents")
      .then((r) => (r.ok ? r.json() : []))
      .then((list: AgentOption[]) => setAgents(list.map(({ id, name }) => ({ id, name }))))
      .catch(() => {});
  }, [load]);

  const remove = async (budget: Budget) => {
    if (!window.confirm(`Delete the ${SCOPE_LABEL[budget.scope].toLowerCase()} budget for ${budget.label}?`)) return;
    const res = await apiFetch(`/api/admin/budgets/${budget.id}`, { method: "DELETE" });
    if (!res.ok) {
      setError(await errorDetail(res));
      return;
    }
    void load();
  };

  const toggleEnforce = async (budget: Budget) => {
    const res = await apiFetch(`/api/admin/budgets/${budget.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enforce: !budget.enforce }),
    });
    if (!res.ok) {
      setError(await errorDetail(res));
      return;
    }
    void load();
  };

  const periodEnd = data
    ? new Date(data.period_end).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" })
    : null;

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div>
          <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
            <Wallet className="w-4 h-4 text-[#FF444F]" /> Spend Budgets
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Monthly limits on model spend for the platform, an agent or a user.
            {periodEnd && ` Resets on ${periodEnd} (UTC).`}
          </p>
        </div>
        <button
          onClick={() => void load()}
          disabled={loading}
          className="flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-700 border border-slate-200 px-3 py-1.5 rounded-lg hover:bg-slate-50 transition-colors disabled:opacity-50"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      <div className="flex-1 overflow-y-auto pr-1 space-y-4">
        {error && (
          <div role="alert" className="bg-red-50 border border-red-200 text-red-700 text-xs rounded-xl px-4 py-3 flex items-center gap-2">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0" /> {error}
          </div>
        )}

        <NewBudgetForm agents={agents} onSaved={() => void load()} onError={setError} />

        {loading && !data && (
          <div className="flex items-center gap-2 text-xs text-slate-400 py-6 justify-center">
            <Loader2 className="w-4 h-4 animate-spin" /> Loading budgets…
          </div>
        )}

        {data && data.budgets.length === 0 && (
          <p className="text-xs text-slate-500 bg-white border border-dashed border-slate-200 rounded-xl px-4 py-6 text-center">
            No budgets yet. Spend is tracked on the Usage tab but nothing limits it until you add one.
          </p>
        )}

        {data?.budgets.map((budget) => (
          <BudgetRow
            key={budget.id}
            budget={budget}
            onDelete={() => void remove(budget)}
            onToggleEnforce={() => void toggleEnforce(budget)}
          />
        ))}
      </div>
    </div>
  );
}

function BudgetRow({
  budget,
  onDelete,
  onToggleEnforce,
}: {
  budget: Budget;
  onDelete: () => void;
  onToggleEnforce: () => void;
}) {
  const { status } = budget;
  const style = STATE_STYLE[status.state];
  return (
    <div className="bg-white border border-slate-200 rounded-xl px-4 py-3" data-testid="budget-row">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-slate-900 truncate">{budget.label}</p>
          <p className="text-[11px] text-slate-400">
            {SCOPE_LABEL[budget.scope]} · alert at {budget.soft_limit_pct}% ·{" "}
            {budget.enforce ? "blocks at the limit" : "alert only"}
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <span className={`text-[11px] font-medium border rounded-full px-2 py-0.5 ${style.pill}`}>
            {STATE_LABEL[status.state]}
          </span>
          <button
            onClick={onToggleEnforce}
            className="text-[11px] text-slate-500 hover:text-slate-700 border border-slate-200 rounded-lg px-2 py-1 hover:bg-slate-50"
          >
            {budget.enforce ? "Switch to alert only" : "Enforce limit"}
          </button>
          <button
            onClick={onDelete}
            aria-label={`Delete budget for ${budget.label}`}
            className="text-slate-400 hover:text-red-600 p-1"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
      <div className="mt-3 flex items-baseline justify-between text-xs">
        <span className="font-semibold text-slate-800">
          {formatUsd(status.spent_usd)} <span className="font-normal text-slate-400">of {formatUsd(status.limit_usd)}</span>
        </span>
        <span className="text-slate-400">{status.used_pct}% used</span>
      </div>
      <div className="mt-1.5 h-1.5 rounded-full bg-slate-100 overflow-hidden" aria-hidden>
        <div className={`h-full rounded-full ${style.bar}`} style={{ width: barWidth(status.used_pct) }} />
      </div>
      <p className="mt-2 text-[11px] text-slate-500">{forecastText(budget)}</p>
    </div>
  );
}

function NewBudgetForm({
  agents,
  onSaved,
  onError,
}: {
  agents: AgentOption[];
  onSaved: () => void;
  onError: (message: string | null) => void;
}) {
  const [scope, setScope] = useState<BudgetScope>("agent");
  const [scopeId, setScopeId] = useState("");
  const [limit, setLimit] = useState("");
  const [softPct, setSoftPct] = useState("80");
  const [enforce, setEnforce] = useState(true);
  const [saving, setSaving] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSaving(true);
    onError(null);
    try {
      const res = await apiFetch("/api/admin/budgets", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scope,
          scope_id: scope === "org" ? null : scopeId,
          monthly_limit_usd: Number(limit),
          soft_limit_pct: Number(softPct),
          enforce,
        }),
      });
      if (!res.ok) throw new Error(await errorDetail(res));
      setScopeId("");
      setLimit("");
      onSaved();
    } catch (err: unknown) {
      onError(err instanceof Error ? err.message : "Failed to save the budget.");
    } finally {
      setSaving(false);
    }
  };

  const input = "text-xs border border-slate-200 rounded-lg px-2.5 py-1.5 bg-white focus:outline-none focus:ring-1 focus:ring-[#FF444F]";

  return (
    <form onSubmit={(e) => void submit(e)} className="bg-slate-50 border border-slate-200 rounded-xl px-4 py-3 flex flex-wrap items-end gap-3">
      <label className="flex flex-col gap-1 text-[11px] font-medium text-slate-600">
        Applies to
        <select aria-label="Applies to" value={scope} onChange={(e) => setScope(e.target.value as BudgetScope)} className={input}>
          <option value="org">Whole platform</option>
          <option value="agent">One agent</option>
          <option value="user">One user</option>
        </select>
      </label>
      {scope === "agent" && (
        <label className="flex flex-col gap-1 text-[11px] font-medium text-slate-600">
          Agent
          <select aria-label="Agent" required value={scopeId} onChange={(e) => setScopeId(e.target.value)} className={`${input} min-w-[12rem]`}>
            <option value="">Choose an agent</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{a.name}</option>
            ))}
          </select>
        </label>
      )}
      {scope === "user" && (
        <label className="flex flex-col gap-1 text-[11px] font-medium text-slate-600">
          User email
          <input aria-label="User email" required type="email" value={scopeId} onChange={(e) => setScopeId(e.target.value)} placeholder="name@company.com" className={`${input} min-w-[14rem]`} />
        </label>
      )}
      <label className="flex flex-col gap-1 text-[11px] font-medium text-slate-600">
        Monthly limit (US dollars)
        <input aria-label="Monthly limit (US dollars)" required type="number" min="0.01" step="0.01" value={limit} onChange={(e) => setLimit(e.target.value)} className={`${input} w-32`} />
      </label>
      <label className="flex flex-col gap-1 text-[11px] font-medium text-slate-600">
        Alert at (%)
        <input aria-label="Alert at (%)" required type="number" min="1" max="100" value={softPct} onChange={(e) => setSoftPct(e.target.value)} className={`${input} w-20`} />
      </label>
      <label className="flex items-center gap-1.5 text-[11px] font-medium text-slate-600 pb-1.5">
        <input type="checkbox" checked={enforce} onChange={(e) => setEnforce(e.target.checked)} />
        Block calls at the limit
      </label>
      <button
        type="submit"
        disabled={saving}
        className="flex items-center gap-1.5 text-xs font-semibold text-white bg-[#FF444F] hover:bg-[#e63e48] px-3 py-1.5 rounded-lg disabled:opacity-50"
      >
        {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
        Save budget
      </button>
    </form>
  );
}
