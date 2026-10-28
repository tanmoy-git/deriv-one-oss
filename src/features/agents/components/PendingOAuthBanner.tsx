import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Loader2, RefreshCw } from "lucide-react";
import { apiFetch } from "@/lib/api";

export function PendingOAuthBanner({ agentId }: { agentId: string }) {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleResume = async () => {
    setLoading(true);
    setError(null);
    try {
      // resume-agent creates a fresh 30-min session using credentials stored on the
      // agent row — works even if the original session has already expired.
      const res = await apiFetch(`/api/slack-oauth/resume-agent/${agentId}`, {
        method: "POST",
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const detail: string = body?.detail ?? "Could not resume Slack setup.";
        setError(detail);
        return;
      }
      const { session_id, redirect_url } = await res.json();
      // Navigate to the pending page; it will open the Slack install URL in a new tab.
      navigate(`/agents/oauth-pending?session=${session_id}`, {
        state: { slack_install_url: redirect_url },
      });
    } catch {
      setError("An unexpected error occurred. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex items-start gap-3 bg-violet-50 border border-violet-200 rounded-xl px-4 py-4">
      <Loader2 className="w-4 h-4 text-violet-600 mt-0.5 shrink-0 animate-spin" />
      <div className="flex-1 space-y-2">
        <p className="text-sm font-semibold text-violet-900">
          Slack install pending
        </p>
        <p className="text-xs text-violet-700 leading-relaxed">
          This agent is saved as a draft. Complete the Slack install to activate it.
          If your workspace required admin approval, click below once approved.
        </p>
        {error ? (
          <p className="text-xs text-red-600">{error}</p>
        ) : null}
        <button
          type="button"
          onClick={handleResume}
          disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold bg-violet-700 hover:bg-violet-800 text-white rounded-lg transition-colors disabled:opacity-50"
        >
          {loading ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
          Resume Slack setup
        </button>
      </div>
    </div>
  );
}
