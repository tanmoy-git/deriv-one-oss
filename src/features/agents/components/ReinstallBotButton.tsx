import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Loader2, RefreshCw } from "lucide-react";
import { apiFetch } from "@/lib/api";

// ── Pending OAuth banner ──────────────────────────────────────────────────────

export function ReinstallBotButton({ agentId }: { agentId: string }) {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleReinstall = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/slack-oauth/reinstall-agent/${agentId}`, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body?.detail ?? "Failed to start reinstall.");
        return;
      }
      const { session_id, redirect_url } = await res.json();
      navigate(`/agents/oauth-pending?session=${session_id}`, {
        state: { slack_install_url: redirect_url },
      });
    } catch {
      setError("An unexpected error occurred.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-1.5">
      <p className="text-xs text-slate-500 leading-relaxed">
        If the bot is missing Slack permissions (e.g. can't read user emails), reinstall it to refresh the OAuth scopes.
      </p>
      {error && <p className="text-xs text-red-600">{error}</p>}
      <button
        type="button"
        onClick={handleReinstall}
        disabled={loading}
        className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold bg-slate-700 hover:bg-slate-800 text-white rounded-lg transition-colors disabled:opacity-50"
      >
        {loading ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
        Reinstall bot
      </button>
    </div>
  );
}
