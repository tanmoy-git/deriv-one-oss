import { useState } from "react";
import { AlertCircle, ShieldCheck } from "lucide-react";
import { apiFetch } from "@/lib/api";

// ── Rotate-tokens modal ────────────────────────────────────────────────────

export function GatewayKeyModal({
  agentId,
  onClose,
  onSaved,
}: {
  agentId: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [apiKey, setApiKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    const trimmed = apiKey.trim();
    if (trimmed.length < 8 || /\s/.test(trimmed)) {
      setError("gateway API key looks malformed.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/gateway-key`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: trimmed }),
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
      setApiKey("");
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Save failed.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 px-4">
      <div className="bg-white rounded-xl border border-slate-200 shadow-2xl w-full max-w-md p-6 space-y-4">
        <div>
          <h2 className="text-base font-bold text-slate-900 flex items-center gap-2">
            <ShieldCheck className="w-4 h-4 text-emerald-600" />
            gateway API key
          </h2>
          <p className="text-xs text-slate-500 mt-1 leading-relaxed">
            Bring your own gateway key for this agent. Stored in the vault — never written to the database.
            Leave the platform key alone for agents that should share quota.
          </p>
        </div>

        <div>
          <label className="block text-[11px] font-semibold text-slate-500 uppercase tracking-wider mb-1">
            API key
          </label>
          <input
            type="password"
            autoComplete="new-password"
            spellCheck={false}
            placeholder="sk-…"
            value={apiKey}
            onChange={e => setApiKey(e.target.value.trim())}
            className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg font-mono focus:outline-none focus:border-slate-400"
          />
        </div>

        {error && (
          <div className="bg-red-50 border border-red-200 text-red-700 text-xs rounded-lg px-3 py-2 flex items-start gap-2">
            <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        <div className="flex gap-2 pt-2">
          <button
            onClick={onClose}
            disabled={submitting}
            className="flex-1 px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={() => void submit()}
            disabled={submitting || apiKey.trim().length < 8}
            className="flex-1 px-4 py-2 text-sm font-semibold text-white bg-slate-900 rounded-lg hover:bg-slate-800 transition-colors disabled:opacity-50"
          >
            {submitting ? "Saving…" : "Save key"}
          </button>
        </div>
      </div>
    </div>
  );
}
