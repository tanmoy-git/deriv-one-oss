import { useEffect, useState } from "react";
import { Activity, AlertTriangle, Bot, Loader2, Play, Route, Wrench } from "lucide-react";
import { apiFetch } from "@/lib/api";
import { formatCost, formatMs, formatTokens, stepDetail, stepTitle } from "./format";
import type { InteractionTrace, ReplayResult, TraceStep } from "./types";

const STEP_ICON = {
  routing: <Route className="w-3.5 h-3.5 text-violet-500" />,
  model: <Bot className="w-3.5 h-3.5 text-blue-500" />,
  tool: <Wrench className="w-3.5 h-3.5 text-amber-500" />,
};

async function errorDetail(res: Response): Promise<string> {
  const body = (await res.json().catch(() => ({}))) as { detail?: unknown };
  return typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`;
}

export default function TracePanel({ agentId, interactionId }: { agentId: string; interactionId: string }) {
  const [trace, setTrace] = useState<InteractionTrace | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/agents/${agentId}/interactions/${interactionId}/trace`)
      .then(async (res) => {
        if (!res.ok) throw new Error(await errorDetail(res));
        return (await res.json()) as InteractionTrace;
      })
      .then((t) => { if (!cancelled) setTrace(t); })
      .catch((e: unknown) => { if (!cancelled) setError(e instanceof Error ? e.message : "Could not load the trace."); });
    return () => { cancelled = true; };
  }, [agentId, interactionId]);

  if (error) return <p className="text-xs text-red-600">{error}</p>;
  if (!trace) {
    return (
      <p className="flex items-center gap-2 text-xs text-slate-400">
        <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading trace…
      </p>
    );
  }

  const { totals } = trace;
  return (
    <section className="rounded-xl border border-slate-200 p-4 space-y-3" aria-label="Trace">
      <div className="flex items-center gap-2">
        <Activity className="w-4 h-4 text-slate-500" />
        <h3 className="text-sm font-bold text-slate-900">Trace</h3>
      </div>

      <dl className="grid grid-cols-3 gap-2 text-[11px]">
        {[
          ["Duration", formatMs(totals.duration_ms) || "unknown"],
          ["Model calls", String(totals.model_calls)],
          ["Cost", formatCost(totals.cost_usd)],
          ["Tokens", `${formatTokens(totals.input_tokens)} in · ${formatTokens(totals.output_tokens)} out`],
          ["Tool calls", String(totals.tool_calls)],
          ["Tool errors", String(totals.tool_errors)],
        ].map(([label, value]) => (
          <div key={label} className="bg-slate-50 border border-slate-200 rounded-lg px-2.5 py-1.5">
            <dt className="text-slate-400">{label}</dt>
            <dd className="font-semibold text-slate-800">{value}</dd>
          </div>
        ))}
      </dl>

      {!trace.timed && (
        <p className="text-[11px] text-slate-500">
          Recorded before traces were kept: tool calls are shown in order, without timings or model calls.
        </p>
      )}

      {trace.steps.length > 0 ? (
        <ol className="space-y-1.5">
          {trace.steps.map((step: TraceStep, i) => (
            <li key={i} className="flex items-start gap-2.5 text-xs">
              <span className="w-14 shrink-0 text-right font-mono text-[10px] text-slate-400 pt-0.5">
                {formatMs(step.t_ms)}
              </span>
              <span className="pt-0.5">{STEP_ICON[step.kind]}</span>
              <span className="min-w-0">
                <span className={`font-medium ${step.kind === "tool" && step.ok === false ? "text-red-600" : "text-slate-800"}`}>
                  {stepTitle(step)}
                </span>
                {stepDetail(step) && <span className="block text-[11px] text-slate-500">{stepDetail(step)}</span>}
              </span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-[11px] text-slate-500">No steps were recorded for this interaction.</p>
      )}

      {trace.replayable && <ReplayBox agentId={agentId} interactionId={interactionId} currentModel={trace.resolved_model} />}
    </section>
  );
}

function ReplayBox({
  agentId,
  interactionId,
  currentModel,
}: {
  agentId: string;
  interactionId: string;
  currentModel: string | null;
}) {
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ReplayResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiFetch("/api/models")
      .then((r) => (r.ok ? r.json() : { models: [] }))
      .then((body: { models?: string[] }) => {
        const list = body.models ?? [];
        setModels(list);
        setModel(list.find((m) => m !== currentModel) ?? list[0] ?? "");
      })
      .catch(() => {});
  }, [currentModel]);

  const run = async () => {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const res = await apiFetch(`/api/agents/${agentId}/interactions/${interactionId}/replay`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model }),
      });
      if (!res.ok) throw new Error(await errorDetail(res));
      setResult((await res.json()) as ReplayResult);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Replay failed.");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="border-t border-slate-100 pt-3 space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold text-slate-700">Replay on another model</span>
        <select
          aria-label="Replay model"
          value={model}
          onChange={(e) => setModel(e.target.value)}
          className="text-xs border border-slate-200 rounded-lg px-2 py-1 bg-white"
        >
          {models.map((m) => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
        <button
          onClick={() => void run()}
          disabled={running || !model}
          className="flex items-center gap-1.5 text-xs font-semibold text-white bg-[#FF444F] hover:bg-[#e63e48] px-3 py-1 rounded-lg disabled:opacity-50"
        >
          {running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
          Run dry replay
        </button>
      </div>
      <p className="text-[11px] text-slate-500">
        Sends the same system prompt and message with no tools, so nothing outside the model runs. It is billed like any model call.
      </p>
      {error && (
        <p role="alert" className="flex items-center gap-1.5 text-xs text-red-600">
          <AlertTriangle className="w-3.5 h-3.5" /> {error}
        </p>
      )}
      {result && (
        <div className="grid grid-cols-2 gap-2">
          {[
            { title: `Original · ${result.original.model ?? "unknown model"}`, text: result.original.output, meta: "with tools" },
            {
              title: `Replay · ${result.model}`,
              text: result.output,
              meta: `${formatTokens(result.input_tokens)} in · ${formatTokens(result.output_tokens)} out · ${formatCost(result.cost_usd)} · ${formatMs(result.duration_ms)}`,
            },
          ].map(({ title, text, meta }) => (
            <div key={title} className="bg-slate-50 border border-slate-200 rounded-lg p-2.5 min-w-0">
              <p className="text-[11px] font-semibold text-slate-700 truncate">{title}</p>
              <p className="text-[10px] text-slate-400 mb-1.5">{meta}</p>
              <p className="text-xs text-slate-800 whitespace-pre-wrap break-words max-h-64 overflow-y-auto">{text}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
