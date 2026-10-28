import { useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, Loader2, Plus, ShieldCheck, X } from "lucide-react";
import { apiFetch } from "@/lib/api";

// ── Guardrails ────────────────────────────────────────────────────────────────

export interface GuardrailPreset {
  id: string;
  label: string;
  description: string;
  type: "preset";
  enabled: boolean;
}

export interface GuardrailCustom {
  id: string;
  type: "custom";
  text: string;
  enabled: boolean;
}

export type GuardrailSavePayload =
  | { id: string; type: "preset"; enabled: boolean }
  | { id: string; type: "custom"; text: string; enabled: boolean };

export function GuardrailsTab({
  agentId,
  readonly,
}: {
  agentId: string;
  readonly: boolean;
}) {
  const [presets, setPresets] = useState<GuardrailPreset[]>([]);
  const [custom, setCustom] = useState<GuardrailCustom[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [newRule, setNewRule] = useState("");

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/guardrails`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setPresets(data.presets ?? []);
      setCustom(data.custom ?? []);
    } catch {
      setError("Could not load guardrails.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [agentId]);

  const togglePreset = (id: string) => {
    if (readonly) return;
    setPresets(prev =>
      prev.map(p => (p.id === id ? { ...p, enabled: !p.enabled } : p))
    );
  };

  const addCustomRule = () => {
    const text = newRule.trim();
    if (!text || text.length > 500) return;
    setCustom(prev => [
      ...prev,
      { id: `custom_${Date.now()}`, type: "custom", text, enabled: true },
    ]);
    setNewRule("");
  };

  const removeCustomRule = (id: string) => {
    setCustom(prev => prev.filter(c => c.id !== id));
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    setSuccess(false);
    try {
      const payload: GuardrailSavePayload[] = [
        ...presets.map(p => ({ id: p.id, type: "preset" as const, enabled: p.enabled })),
        ...custom,
      ];
      const res = await apiFetch(`/api/agents/${agentId}/guardrails`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ guardrails: payload }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { detail?: string }).detail || `HTTP ${res.status}`);
      }
      setSuccess(true);
      setTimeout(() => setSuccess(false), 3000);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save guardrails.");
    } finally {
      setSaving(false);
    }
  };

  const activeCount = [...presets.filter(p => p.enabled), ...custom.filter(c => c.enabled)].length;

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-slate-400">
        <Loader2 className="w-5 h-5 animate-spin mr-2" />
        Loading guardrails…
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">Guardrails</h3>
          <p className="text-xs text-slate-500 mt-0.5">
            Rules injected into this agent's system prompt on every invocation.
            {activeCount > 0 && (
              <span className="ml-1 inline-flex items-center gap-1 bg-emerald-50 text-emerald-700 border border-emerald-200 rounded-full px-2 py-0.5 text-[10px] font-semibold">
                <ShieldCheck className="w-3 h-3" />
                {activeCount} active
              </span>
            )}
          </p>
        </div>
        {!readonly && (
          <button
            type="button"
            onClick={save}
            disabled={saving}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold bg-slate-900 text-white rounded-lg hover:bg-slate-700 disabled:opacity-50 transition-colors"
          >
            {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <CheckCircle2 className="w-3.5 h-3.5" />}
            {saving ? "Saving…" : "Save"}
          </button>
        )}
      </div>

      {error && (
        <div className="flex items-center gap-2 text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
          <AlertCircle className="w-4 h-4 flex-shrink-0" />
          {error}
        </div>
      )}
      {success && (
        <div className="flex items-center gap-2 text-xs text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-lg px-3 py-2">
          <CheckCircle2 className="w-4 h-4 flex-shrink-0" />
          Guardrails saved successfully.
        </div>
      )}

      {/* Preset rules */}
      <div>
        <p className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-3">Preset rules</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {presets.map(preset => (
            <button
              key={preset.id}
              type="button"
              onClick={() => togglePreset(preset.id)}
              disabled={readonly}
              className={`text-left p-3.5 rounded-xl border transition-all ${
                preset.enabled
                  ? "border-slate-900 bg-slate-900 text-white"
                  : "border-slate-200 bg-white text-slate-700 hover:border-slate-300"
              } ${readonly ? "cursor-default" : "cursor-pointer"}`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 min-w-0">
                  <p className={`text-xs font-semibold ${preset.enabled ? "text-white" : "text-slate-900"}`}>
                    {preset.label}
                  </p>
                  <p className={`text-[11px] mt-0.5 leading-relaxed ${preset.enabled ? "text-slate-300" : "text-slate-500"}`}>
                    {preset.description}
                  </p>
                </div>
                <div
                  className={`flex-shrink-0 w-8 rounded-full border transition-colors mt-0.5 ${
                    preset.enabled
                      ? "bg-emerald-400 border-emerald-400"
                      : "bg-slate-200 border-slate-300"
                  }`}
                  style={{ height: "18px" }}
                >
                  <div
                    className={`w-3.5 h-3.5 rounded-full bg-white shadow-sm transform transition-transform mt-[1px] ${
                      preset.enabled ? "translate-x-[18px]" : "translate-x-[1px]"
                    }`}
                  />
                </div>
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* Custom rules */}
      <div>
        <p className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-3">Custom rules</p>
        {custom.length === 0 && readonly && (
          <p className="text-xs text-slate-400 italic">No custom rules configured.</p>
        )}
        <div className="space-y-2">
          {custom.map(rule => (
            <div
              key={rule.id}
              className="flex items-start gap-2 p-3 bg-slate-50 border border-slate-200 rounded-lg"
            >
              <ShieldCheck className="w-3.5 h-3.5 text-slate-400 flex-shrink-0 mt-0.5" />
              <p className="flex-1 text-xs text-slate-700">{rule.text}</p>
              {!readonly && (
                <button
                  type="button"
                  onClick={() => removeCustomRule(rule.id)}
                  className="flex-shrink-0 text-slate-400 hover:text-red-500 transition-colors"
                  aria-label="Remove rule"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              )}
            </div>
          ))}
        </div>
        {!readonly && (
          <div className="flex gap-2 mt-2">
            <input
              type="text"
              value={newRule}
              onChange={e => setNewRule(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); addCustomRule(); }}}
              placeholder="Add a custom rule… (max 500 chars)"
              maxLength={500}
              className="flex-1 text-xs border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-slate-300 placeholder:text-slate-400"
            />
            <button
              type="button"
              onClick={addCustomRule}
              disabled={!newRule.trim()}
              className="flex items-center gap-1 px-3 py-2 text-xs font-semibold bg-slate-100 text-slate-700 rounded-lg hover:bg-slate-200 disabled:opacity-40 transition-colors"
            >
              <Plus className="w-3.5 h-3.5" />
              Add
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
