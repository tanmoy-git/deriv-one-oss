import { useEffect, useState } from "react";
import {
  AlertCircle,
  Clock,
  KeyRound,
  Loader2,
  MessageSquare,
  Route,
  User,
  Wrench,
  X,
} from "lucide-react";
import { apiFetch } from "@/lib/api";
import TracePanel from "@/src/features/agentTrace/TracePanel";
import {
  AgentInteractionDetail,
  AgentInteractionRecord,
  AgentToolAuditActivateSkill,
  AgentToolAuditCallArtifact,
  AgentToolAuditEntry,
  AgentToolAuditRunPython,
} from "@/data/agents";

function failureKindLabel(kind: string): string {
  const labels: Record<string, string> = {
    timeout: "Timeout",
    format_failure: "Format failure",
    dispatch_error: "Dispatch error",
    tool_error: "Tool error",
  };
  return labels[kind] ?? kind.replace(/_/g, " ");
}

function triggerLabel(trigger: AgentInteractionRecord["trigger_type"]): string {
  if (trigger === "mention") return "Mention";
  if (trigger === "dm") return "DM";
  if (trigger === "a2a") return "Agent call";
  return trigger;
}

function formatWhen(iso: string): string {
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(iso));
  } catch {
    return iso;
  }
}

function toolTitle(entry: AgentToolAuditEntry, index: number): string {
  const tool = typeof entry.tool === "string" ? entry.tool : "unknown";
  if (tool === "run_python") return `Step ${index + 1}: Run Python`;
  if (tool === "call_artifact") {
    const artifact = "artifact" in entry ? entry.artifact : undefined;
    const version = "version" in entry ? entry.version : undefined;
    return `Step ${index + 1}: Call artifact${artifact ? ` · ${artifact}` : ""}${version != null ? ` v${version}` : ""}`;
  }
  if (tool === "activate_skill") {
    const slug = "slug" in entry ? entry.slug : undefined;
    return `Step ${index + 1}: Activate skill${slug ? ` · ${slug}` : ""}`;
  }
  return `Step ${index + 1}: ${tool}`;
}

function WrappedTextBlock({ label, content }: { label: string; content?: string }) {
  if (!content) return null;
  return (
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500 mb-1.5">
        {label}
      </p>
      <div className="text-xs font-mono bg-slate-50 border border-slate-200 rounded-xl p-4 text-slate-800 leading-relaxed whitespace-pre-wrap break-words overflow-y-auto max-h-72">
        {content}
      </div>
    </div>
  );
}

function ToolStepCard({ entry, index }: { entry: AgentToolAuditEntry; index: number }) {
  const failed = "ok" in entry && entry.ok === false;
  const succeeded = "ok" in entry && entry.ok === true;

  return (
    <li className="relative pl-8 pb-6 last:pb-0">
      <span
        className={`absolute left-0 top-1 flex h-6 w-6 items-center justify-center rounded-full text-[10px] font-bold ${
          failed
            ? "bg-red-100 text-red-700 ring-4 ring-red-50"
            : succeeded
              ? "bg-emerald-100 text-emerald-700 ring-4 ring-emerald-50"
              : "bg-slate-100 text-slate-600 ring-4 ring-slate-50"
        }`}
      >
        {index + 1}
      </span>
      {index > 0 && (
        <span className="absolute left-[11px] top-8 bottom-0 w-px bg-slate-200" aria-hidden />
      )}

      <div className="rounded-xl border border-slate-200 bg-white p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <h4 className="text-sm font-semibold text-slate-900">{toolTitle(entry, index)}</h4>
          {"ok" in entry && entry.ok != null && (
            <span
              className={`inline-flex text-[10px] font-semibold uppercase tracking-wide rounded-full px-2 py-0.5 ${
                entry.ok
                  ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
                  : "bg-red-50 text-red-700 border border-red-200"
              }`}
            >
              {entry.ok ? "Success" : "Error"}
            </span>
          )}
        </div>

        {entry.tool === "run_python" && (() => {
          const run = entry as AgentToolAuditRunPython;
          return (
            <div className="space-y-3">
              <WrappedTextBlock label="Code" content={run.code_redacted} />
              <WrappedTextBlock label="Output" content={run.output_redacted} />
            </div>
          );
        })()}

        {entry.tool === "call_artifact" && (
          <WrappedTextBlock
            label="Output"
            content={(entry as AgentToolAuditCallArtifact).output_redacted}
          />
        )}

        {entry.tool === "activate_skill" && (() => {
          const skill = entry as AgentToolAuditActivateSkill;
          return (
            <div className="space-y-2 text-sm text-slate-700">
              {skill.error && (
                <p className="text-red-700 whitespace-pre-wrap break-words rounded-lg bg-red-50 border border-red-100 px-3 py-2">
                  {skill.error}
                </p>
              )}
              {skill.secret_keys && skill.secret_keys.length > 0 && (
                <div>
                  <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-500 mb-1.5">
                    Secret keys
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {skill.secret_keys.map(key => (
                      <span
                        key={key}
                        className="inline-flex rounded-full bg-slate-100 text-slate-600 px-2.5 py-1 font-mono text-[10px]"
                      >
                        {key}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {skill.artifact_names && skill.artifact_names.length > 0 && (
                <p className="whitespace-pre-wrap break-words">
                  <span className="font-semibold text-slate-800">Artifacts:</span>{" "}
                  {skill.artifact_names.join(", ")}
                </p>
              )}
            </div>
          );
        })()}
      </div>
    </li>
  );
}

export function InteractionDetailModal({
  agentId,
  item,
  onClose,
}: {
  agentId: string;
  item: AgentInteractionRecord;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<AgentInteractionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await apiFetch(`/api/agents/${agentId}/interactions/${item.id}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const payload: AgentInteractionDetail = await res.json();
        if (!cancelled) setDetail(payload);
      } catch {
        if (!cancelled) {
          setError("Could not load interaction details.");
          setDetail(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [agentId, item.id]);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const failureKind = item.failure_kind ?? detail?.failure_kind;
  const failureDetail = item.failure_detail ?? detail?.failure_detail;
  const resolvedModel = item.resolved_model ?? detail?.resolved_model;
  const routing = detail?.routing_metadata;
  const tools = Array.isArray(detail?.tools_called) ? detail.tools_called : [];
  const secrets = Array.isArray(detail?.secrets_loaded) ? detail.secrets_loaded : [];
  const toolSummary = item.tool_summary ?? detail?.tool_summary;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
      <div
        className="absolute inset-0"
        onClick={onClose}
        aria-hidden
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="interaction-detail-title"
        className="relative bg-white rounded-2xl shadow-2xl w-full max-w-5xl max-h-[92vh] flex flex-col border border-slate-200"
      >
        <div className="flex items-start justify-between gap-4 px-5 py-4 border-b border-slate-100 shrink-0">
          <div className="min-w-0 space-y-2">
            <h2 id="interaction-detail-title" className="text-lg font-bold text-slate-900">
              Interaction details
            </h2>
            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-600">
              <span className="inline-flex items-center gap-1">
                <Clock className="w-3.5 h-3.5" />
                {formatWhen(item.created_at)}
              </span>
              <span className="inline-flex items-center gap-1">
                <User className="w-3.5 h-3.5" />
                {item.invoker_email}
              </span>
              <span className="rounded-full bg-slate-100 px-2 py-0.5 font-semibold uppercase tracking-wide text-[10px] text-slate-600">
                {triggerLabel(item.trigger_type)}
              </span>
              {item.duration_ms != null && (
                <span className="tabular-nums">{(item.duration_ms / 1000).toFixed(1)}s</span>
              )}
              {toolSummary && toolSummary.total > 0 && (
                <span className="inline-flex items-center gap-1">
                  <Wrench className="w-3.5 h-3.5" />
                  {toolSummary.total} tools
                  {toolSummary.failed > 0 ? ` · ${toolSummary.failed} failed` : ""}
                </span>
              )}
            </div>
            <div className="flex flex-wrap gap-2 text-[11px] text-slate-500">
              {item.channel_id && <span>Channel: {item.channel_id}</span>}
              {item.thread_ts && <span>Thread: {item.thread_ts}</span>}
              {item.source_agent_name && <span>Via: {item.source_agent_name}</span>}
              {resolvedModel && <span>Model: {resolvedModel}</span>}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-2 text-slate-400 hover:text-slate-700 rounded-lg shrink-0"
            aria-label="Close"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="overflow-y-auto flex-1 p-5 space-y-5">
          {failureKind && (
            <div className="bg-red-50 border border-red-200 rounded-xl px-4 py-3 text-sm text-red-800">
              <div className="flex items-start gap-2">
                <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
                <div className="min-w-0">
                  <p className="font-semibold">{failureKindLabel(failureKind)}</p>
                  {failureDetail && (
                    <p className="mt-1 text-red-700 whitespace-pre-wrap break-words">
                      {failureDetail}
                    </p>
                  )}
                </div>
              </div>
            </div>
          )}

          <div className="grid md:grid-cols-2 gap-4">
            <section className="rounded-xl border border-slate-200 bg-slate-50/60 p-4">
              <div className="flex items-center gap-2 mb-2">
                <User className="w-4 h-4 text-slate-500" />
                <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                  User message
                </h3>
              </div>
              <p className="text-sm text-slate-800 leading-relaxed whitespace-pre-wrap break-words">
                {item.user_message}
              </p>
            </section>
            <section className="rounded-xl border border-slate-200 bg-slate-50/60 p-4">
              <div className="flex items-center gap-2 mb-2">
                <MessageSquare className="w-4 h-4 text-slate-500" />
                <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                  Agent response
                </h3>
              </div>
              <p className="text-sm text-slate-800 leading-relaxed whitespace-pre-wrap break-words">
                {item.assistant_message}
              </p>
            </section>
          </div>

          {loading && (
            <div className="flex items-center justify-center gap-2 py-10 text-sm text-slate-500">
              <Loader2 className="w-4 h-4 animate-spin" />
              Loading tool timeline…
            </div>
          )}

          {error && (
            <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-4 py-3">
              {error}
            </div>
          )}

          {!loading && !error && detail && <TracePanel agentId={agentId} interactionId={item.id} />}

          {!loading && !error && detail && tools.length > 0 && (
            <section>
              <div className="flex items-center gap-2 mb-3">
                <Wrench className="w-4 h-4 text-slate-500" />
                <h3 className="text-sm font-bold text-slate-900">
                  Tool timeline ({tools.length})
                </h3>
              </div>
              <ol className="space-y-0">{tools.map((entry, index) => (
                <ToolStepCard key={`${index}-${typeof entry.tool === "string" ? entry.tool : "tool"}`} entry={entry} index={index} />
              ))}</ol>
            </section>
          )}

          {!loading && !error && detail && routing && Object.keys(routing).length > 0 && (
            <section className="rounded-xl border border-slate-200 p-4 space-y-2">
              <div className="flex items-center gap-2 mb-1">
                <Route className="w-4 h-4 text-slate-500" />
                <h3 className="text-sm font-bold text-slate-900">Skill routing</h3>
              </div>
              <div className="text-sm text-slate-700 space-y-2">
                {routing.mode && (
                  <p className="whitespace-pre-wrap break-words">
                    <span className="font-semibold">Mode:</span> {routing.mode}
                  </p>
                )}
                {routing.confidence != null && (
                  <p>
                    <span className="font-semibold">Confidence:</span> {routing.confidence}
                  </p>
                )}
                {routing.selected_slugs && routing.selected_slugs.length > 0 && (
                  <p className="whitespace-pre-wrap break-words">
                    <span className="font-semibold">Selected:</span>{" "}
                    <span className="font-mono text-xs">{routing.selected_slugs.join(", ")}</span>
                  </p>
                )}
                {routing.candidate_slugs && routing.candidate_slugs.length > 0 && (
                  <p className="whitespace-pre-wrap break-words">
                    <span className="font-semibold">Candidates:</span>{" "}
                    <span className="font-mono text-xs">{routing.candidate_slugs.join(", ")}</span>
                  </p>
                )}
                {routing.fallback && (
                  <p className="text-amber-700 whitespace-pre-wrap break-words">
                    Fallback{routing.reason ? `: ${routing.reason}` : ""}
                  </p>
                )}
                {routing.active_skill_hint && (
                  <p className="whitespace-pre-wrap break-words">
                    <span className="font-semibold">Hint:</span> {routing.active_skill_hint}
                  </p>
                )}
              </div>
            </section>
          )}

          {!loading && !error && detail && secrets.length > 0 && (
            <section className="rounded-xl border border-slate-200 p-4">
              <div className="flex items-center gap-2 mb-2">
                <KeyRound className="w-4 h-4 text-slate-500" />
                <h3 className="text-sm font-bold text-slate-900">Secrets loaded</h3>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {secrets.map(key => (
                  <span
                    key={key}
                    className="inline-flex rounded-full bg-slate-100 border border-slate-200 text-slate-600 px-2.5 py-1 font-mono text-[10px]"
                  >
                    {key}
                  </span>
                ))}
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
