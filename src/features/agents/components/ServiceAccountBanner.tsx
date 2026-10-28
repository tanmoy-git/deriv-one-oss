import { useState } from "react";
import { AlertCircle, Check, ExternalLink, Loader2 } from "lucide-react";
import { apiFetch } from "@/lib/api";
import { useBranding } from "@/lib/branding";
import { AgentRecord } from "@/data/agents";

export function ServiceAccountBanner({
  agent,
  onVerified,
}: {
  agent: AgentRecord;
  onVerified: () => void;
}) {
  const branding = useBranding();
  const collaborator = branding.slack_config_collaborator || "the platform collaborator";
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showHelp, setShowHelp] = useState(false);

  const handleVerify = async () => {
    if (!agent.slack_app_id) return;
    setVerifying(true);
    setError(null);
    try {
      const res = await apiFetch("/api/agents/verify-service-account", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slack_app_id: agent.slack_app_id, agent_id: agent.id }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data?.verified) {
        onVerified();
      } else {
        setError("This hasn't been added yet — add it in Slack, then click Done again.");
      }
    } catch {
      setError("Could not check right now — try again.");
    } finally {
      setVerifying(false);
    }
  };

  return (
    <div className="flex items-start gap-3 bg-amber-50 border border-amber-200 rounded-xl px-4 py-4">
      <AlertCircle className="w-4 h-4 text-amber-600 mt-0.5 shrink-0" />
      <div className="flex-1 space-y-2">
        <div className="flex items-start justify-between gap-2">
          <p className="text-sm font-semibold text-amber-900">
            Service account not yet added
          </p>
          <button
            type="button"
            onClick={() => setShowHelp(v => !v)}
            className="shrink-0 text-[11px] font-semibold text-amber-800 hover:text-amber-950 underline underline-offset-2"
          >
            {showHelp ? "Hide help" : "How do I do this?"}
          </button>
        </div>
        <p className="text-xs text-amber-800 leading-relaxed">
          This agent's Slack app was self-created and doesn't yet have{" "}
          <strong>{collaborator}</strong> added as a collaborator, so {branding.app_name} can't
          update its name, description, or photo in Slack. Add it as a collaborator, then click
          Done.
        </p>

        {showHelp && (
          <ol className="space-y-2 text-xs text-amber-800 border-t border-amber-200 pt-2">
            <li>
              1. Open{" "}
              <a
                href="https://api.slack.com/apps"
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-amber-900 underline underline-offset-2 hover:text-amber-950"
              >
                api.slack.com/apps
                <ExternalLink className="w-3 h-3" />
              </a>
              {" "}and click into this agent's Slack app.
            </li>
            <li>2. Go to <strong>Basic Information</strong> → <strong>Collaborators</strong>.</li>
            <li>3. Click <strong>Add a collaborator</strong>, search for <strong>{collaborator}</strong>, and add it.</li>
            <li>4. Come back here and click <strong>Done</strong>.</li>
          </ol>
        )}

        {error && <p className="text-xs text-red-600">{error}</p>}

        <button
          type="button"
          onClick={handleVerify}
          disabled={verifying}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold bg-amber-700 hover:bg-amber-800 text-white rounded-lg transition-colors disabled:opacity-50"
        >
          {verifying ? (
            <>
              <Loader2 className="w-3 h-3 animate-spin" /> Checking…
            </>
          ) : (
            <>
              <Check className="w-3 h-3" /> Done
            </>
          )}
        </button>
      </div>
    </div>
  );
}
