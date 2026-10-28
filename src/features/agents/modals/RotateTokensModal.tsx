import { useState } from "react";
import { AlertCircle, Loader2, ShieldCheck, Trash2 } from "lucide-react";
import { apiFetch } from "@/lib/api";

export const XOXB_RE = /^xoxb-[A-Za-z0-9-]{10,512}$/;
export const SIGNING_SECRET_RE = /^[a-f0-9]{8,}$/i;

export function RotateTokensModal({
  agentId,
  onClose,
  onRotated,
}: {
  agentId: string;
  onClose: () => void;
  onRotated: () => void;
}) {
  const [xoxb, setXoxb] = useState("");
  const [signingSecret, setSigningSecret] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!XOXB_RE.test(xoxb)) {
      setError("Bot token (xoxb) looks malformed.");
      return;
    }
    if (!SIGNING_SECRET_RE.test(signingSecret)) {
      setError("Signing secret looks malformed — expected a hex string (e.g. xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx).");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/rotate-tokens`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ xoxb, signing_secret: signingSecret }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const msg = typeof body?.detail === "string"
          ? body.detail
          : Array.isArray(body?.detail)
            ? body.detail.map((e: { msg?: string }) => e.msg ?? String(e)).join("; ")
            : `Rotation failed (HTTP ${res.status}).`;
        throw new Error(msg);
      }
      setXoxb("");
      setSigningSecret("");
      onRotated();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Rotation failed.");
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
            Rotate Slack tokens
          </h2>
          <p className="text-xs text-slate-500 mt-1 leading-relaxed">
            Paste the new bot token and signing secret. The previous secret
            containers in the vault are deleted after the new ones are saved.
          </p>
        </div>

        <div className="space-y-3">
          <div>
            <label className="block text-[11px] font-semibold text-slate-500 uppercase tracking-wider mb-1">Bot token (xoxb)</label>
            <input
              type="password"
              autoComplete="new-password"
              spellCheck={false}
              placeholder="xoxb-..."
              value={xoxb}
              onChange={e => setXoxb(e.target.value)}
              className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg font-mono focus:outline-none focus:border-slate-400"
            />
          </div>
          <div>
            <label className="block text-[11px] font-semibold text-slate-500 uppercase tracking-wider mb-1">Signing secret</label>
            <input
              type="password"
              autoComplete="new-password"
              spellCheck={false}
              placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
              value={signingSecret}
              onChange={e => setSigningSecret(e.target.value.trim())}
              className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg font-mono focus:outline-none focus:border-slate-400"
            />
            <p className="text-[11px] text-slate-400 mt-1">Basic Information → App Credentials → Signing Secret</p>
          </div>
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
            onClick={submit}
            disabled={submitting}
            className="flex-1 px-4 py-2 text-sm font-bold bg-slate-900 text-white rounded-lg hover:bg-slate-700 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
          >
            {submitting && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
            Rotate
          </button>
        </div>

        <p className="text-[10px] text-slate-400 leading-relaxed pt-1 border-t border-slate-100">
          <Trash2 className="w-2.5 h-2.5 inline -mt-0.5 mr-1" />
          Old secret containers are deleted after the new ones land.
        </p>
      </div>
    </div>
  );
}
