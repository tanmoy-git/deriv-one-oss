import { type FormEvent, useCallback, useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, FlaskConical, Loader2, Play, Plus, Trash2, X, XCircle } from "lucide-react";
import { apiFetch, readApiErrorResponse } from "@/lib/api";
import {
  GRADER_OPTIONS,
  type EvalCase,
  type EvalCaseInput,
  type EvalOverview,
  type EvalRun,
  type GraderName,
  type Tone,
  emptyCaseDraft,
  graderLabel,
  isRunInProgress,
  passRatePercent,
  resultTone,
  runSummary,
  runTone,
  shortHash,
  triggerLabel,
  validateCaseDraft,
} from "./format";

const POLL_MS = 3000;

const TONE_CLASS: Record<Tone, string> = {
  good: "bg-emerald-50 text-emerald-700 ring-emerald-100",
  bad: "bg-red-50 text-red-700 ring-red-100",
  warn: "bg-amber-50 text-amber-700 ring-amber-100",
  neutral: "bg-slate-100 text-slate-600 ring-slate-200",
};

function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function ResultIcon({ status }: { status: string }) {
  if (status === "pass") return <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />;
  if (status === "fail") return <XCircle className="w-4 h-4 text-red-600 shrink-0" />;
  return <AlertCircle className="w-4 h-4 text-amber-600 shrink-0" />;
}

function CaseForm({ onAdded, onCancel, slug }: { slug: string; onAdded: () => void; onCancel: () => void }) {
  const [draft, setDraft] = useState<EvalCaseInput>(emptyCaseDraft);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const grader = GRADER_OPTIONS.find(g => g.value === draft.grader) ?? GRADER_OPTIONS[0];

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const problem = validateCaseDraft(draft);
    if (problem) {
      setError(problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/skills/${slug}/evals/cases`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...draft, name: draft.name.trim() }),
      });
      if (!res.ok) throw new Error(await readApiErrorResponse(res, "Could not add the case."));
      onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add the case.");
    } finally {
      setSaving(false);
    }
  };

  const inputCls =
    "w-full bg-white border border-slate-200 rounded-lg px-3 py-2 text-xs text-slate-800 focus:outline-none focus:ring-2 focus:ring-[#FF444F]/30 focus:border-[#FF444F]/50";

  return (
    <form onSubmit={submit} className="rounded-xl border border-slate-200 bg-slate-50/60 p-4 space-y-3">
      <div className="grid sm:grid-cols-3 gap-3">
        <label className="sm:col-span-2 space-y-1">
          <span className="text-xs font-semibold text-slate-600">Name</span>
          <input
            className={inputCls}
            value={draft.name}
            maxLength={120}
            onChange={e => setDraft({ ...draft, name: e.target.value })}
            placeholder="Asks for the account ID"
          />
        </label>
        <label className="space-y-1">
          <span className="text-xs font-semibold text-slate-600">Grader</span>
          <select
            className={inputCls}
            value={draft.grader}
            onChange={e => setDraft({ ...draft, grader: e.target.value as GraderName })}
          >
            {GRADER_OPTIONS.map(g => (
              <option key={g.value} value={g.value}>{g.label}</option>
            ))}
          </select>
        </label>
      </div>
      <label className="block space-y-1">
        <span className="text-xs font-semibold text-slate-600">Input prompt</span>
        <textarea
          className={`${inputCls} min-h-[4.5rem] resize-y`}
          value={draft.input_prompt}
          onChange={e => setDraft({ ...draft, input_prompt: e.target.value })}
          placeholder="What the user says to the skill"
        />
      </label>
      <label className="block space-y-1">
        <span className="text-xs font-semibold text-slate-600">{grader.expectedLabel}</span>
        <textarea
          className={`${inputCls} min-h-[3rem] resize-y ${draft.grader === "regex" ? "font-mono" : ""}`}
          value={draft.expected}
          onChange={e => setDraft({ ...draft, expected: e.target.value })}
          placeholder={grader.placeholder}
        />
      </label>
      {error && <p className="text-xs text-red-700 bg-red-50 border border-red-100 rounded-lg px-3 py-2">{error}</p>}
      <div className="flex items-center justify-end gap-2">
        <button type="button" onClick={onCancel} className="px-2.5 py-1 text-xs font-semibold text-slate-600 hover:text-slate-900">
          Cancel
        </button>
        <button
          type="submit"
          disabled={saving}
          className="inline-flex items-center gap-1.5 px-3 py-1 text-xs font-semibold rounded-md bg-[#FF444F] text-white hover:bg-[#e63d47] disabled:opacity-50"
        >
          {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Plus className="w-3.5 h-3.5" />}
          Add case
        </button>
      </div>
    </form>
  );
}

function RunResults({ run, cases }: { run: EvalRun; cases: EvalCase[] }) {
  const pct = passRatePercent(run);
  const caseNames = new Map(cases.map(c => [c.id, c.name]));
  const results = run.results ?? [];
  return (
    <div className="rounded-xl border border-slate-200 overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 px-4 py-3 bg-slate-50 border-b border-slate-200">
        <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ring-1 ${TONE_CLASS[runTone(run)]}`}>
          {runSummary(run)}
          {pct !== null && run.status === "completed" ? ` (${pct}%)` : ""}
        </span>
        <span className="text-xs text-slate-500">
          {triggerLabel(run.trigger)} · {formatWhen(run.started_at)} · {run.model || "no model"} · text {shortHash(run.content_hash)}
        </span>
      </div>
      {results.length > 0 && (
        <ul className="divide-y divide-slate-100">
          {results.map(r => (
            <li key={r.case_id} className="px-4 py-3 space-y-1.5">
              <div className="flex items-start gap-2">
                <ResultIcon status={r.status} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-semibold text-slate-800">{caseNames.get(r.case_id) ?? r.name}</span>
                    <span className={`text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded ring-1 ${TONE_CLASS[resultTone(r.status)]}`}>
                      {r.status}
                    </span>
                    <span className="text-[11px] text-slate-400">{graderLabel(r.grader)} · {(r.duration_ms / 1000).toFixed(1)}s</span>
                  </div>
                  <p className="text-xs text-slate-600 mt-0.5">{r.reason}</p>
                  {r.output_excerpt && (
                    <pre className="mt-1.5 bg-[#F8F8F9] border border-slate-200 rounded-lg p-2 text-[11px] font-mono text-slate-700 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">
                      {r.output_excerpt}
                    </pre>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function SkillEvalsTab({ slug }: { slug: string }) {
  const [overview, setOverview] = useState<EvalOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [starting, setStarting] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/skills/${slug}/evals`);
      if (!res.ok) throw new Error(await readApiErrorResponse(res, "Could not load evals."));
      setOverview((await res.json()) as EvalOverview);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load evals.");
    } finally {
      setLoading(false);
    }
  }, [slug]);

  useEffect(() => {
    void load();
  }, [load]);

  const running = isRunInProgress(overview?.last_run);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => { void load(); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [running, load]);

  const startRun = async () => {
    setStarting(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/skills/${slug}/evals/runs`, { method: "POST" });
      if (!res.ok) throw new Error(await readApiErrorResponse(res, "Could not start the run."));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the run.");
    } finally {
      setStarting(false);
    }
  };

  const deleteCase = async (c: EvalCase) => {
    if (!window.confirm(`Delete eval case "${c.name}"?`)) return;
    setDeleting(c.id);
    setError(null);
    try {
      const res = await apiFetch(`/api/skills/${slug}/evals/cases/${c.id}`, { method: "DELETE" });
      if (!res.ok) throw new Error(await readApiErrorResponse(res, "Could not delete the case."));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete the case.");
    } finally {
      setDeleting(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20 text-slate-400">
        <Loader2 className="w-5 h-5 animate-spin" />
      </div>
    );
  }

  const cases = overview?.cases ?? [];
  const lastRun = overview?.last_run ?? null;
  const atLimit = overview ? cases.length >= overview.max_cases_per_skill : false;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
            <FlaskConical className="w-4 h-4 text-[#FF444F]" />
            Eval cases
          </h3>
          <p className="text-xs text-slate-500 max-w-prose">
            Each case sends an input to the model with this skill as the system prompt and grades the reply.
            Every publish of a new version records a run.
            {overview?.gate_enabled
              ? " The publish gate is on: a version that passes fewer cases than the published one is not published."
              : " The publish gate is off, so runs are recorded but never block publishing."}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setAdding(true)}
            disabled={adding || atLimit}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-md border border-slate-200 text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            <Plus className="w-3.5 h-3.5" />
            Add case
          </button>
          <button
            type="button"
            onClick={() => void startRun()}
            disabled={starting || running || cases.length === 0}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-md bg-[#FF444F] text-white hover:bg-[#e63d47] disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {starting || running ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />}
            {running ? "Running" : "Run evals"}
          </button>
        </div>
      </div>

      {error && (
        <div className="flex items-start gap-2 text-xs text-red-700 bg-red-50 border border-red-100 rounded-lg px-3 py-2">
          <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
          <span className="flex-1">{error}</span>
          <button type="button" onClick={() => setError(null)} aria-label="Dismiss">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {adding && (
        <CaseForm
          slug={slug}
          onCancel={() => setAdding(false)}
          onAdded={() => { setAdding(false); void load(); }}
        />
      )}

      {cases.length === 0 && !adding ? (
        <div className="rounded-xl border border-dashed border-slate-200 px-6 py-10 text-center text-xs text-slate-500">
          No eval cases yet. Add a few typical requests and what a good reply must contain.
        </div>
      ) : (
        <ul className="rounded-xl border border-slate-200 divide-y divide-slate-100">
          {cases.map(c => (
            <li key={c.id} className="flex items-start gap-3 px-4 py-3">
              <div className="min-w-0 flex-1 space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs font-semibold text-slate-800">{c.name}</span>
                  <span className="text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">
                    {graderLabel(c.grader)}
                  </span>
                </div>
                <p className="text-xs text-slate-600 line-clamp-2"><span className="text-slate-400">Input:</span> {c.input_prompt}</p>
                <p className={`text-xs text-slate-600 line-clamp-2 ${c.grader === "regex" ? "font-mono" : ""}`}>
                  <span className="text-slate-400 font-sans">{c.grader === "llm_judge" ? "Rubric:" : "Expected:"}</span> {c.expected}
                </p>
              </div>
              <button
                type="button"
                onClick={() => void deleteCase(c)}
                disabled={deleting === c.id}
                className="p-1 text-slate-400 hover:text-red-600 disabled:opacity-50"
                aria-label={`Delete ${c.name}`}
              >
                {deleting === c.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
              </button>
            </li>
          ))}
        </ul>
      )}

      <section className="space-y-2">
        <h3 className="text-sm font-semibold text-slate-900">Last run</h3>
        {lastRun ? (
          <>
            <RunResults run={lastRun} cases={cases} />
            {overview && lastRun.content_hash !== overview.live_content_hash && (
              <p className="text-xs text-slate-500">
                This run graded a different version of the skill text than the one published now.
              </p>
            )}
          </>
        ) : (
          <p className="text-xs text-slate-500">No runs yet.</p>
        )}
      </section>
    </div>
  );
}
