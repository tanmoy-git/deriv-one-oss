import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  CalendarClock,
  Clock,
  Hash,
  Loader2,
  Pause,
  Pencil,
  Play,
  Plus,
  Trash2,
  Zap,
} from "lucide-react";
import { apiFetch } from "@/lib/api";
import { ScheduledTask } from "../shared";

// ── Automations tab ───────────────────────────────────────────────────────────

export const CRON_PRESETS = [
  { label: "Every day at 9 AM", value: "0 9 * * *" },
  { label: "Every day at 6 PM", value: "0 18 * * *" },
  { label: "Every Monday at 9 AM", value: "0 9 * * 1" },
  { label: "Every weekday at 9 AM", value: "0 9 * * 1-5" },
  { label: "Every hour", value: "0 * * * *" },
  { label: "Custom…", value: "" },
];

export const TIMEZONES = [
  "Asia/Dubai",
  "UTC",
  "Europe/London",
  "Europe/Berlin",
  "Asia/Singapore",
  "Asia/Kolkata",
  "America/New_York",
  "America/Los_Angeles",
];

export function describeCron(cron: string): string {
  const presetMatch = CRON_PRESETS.find((p) => p.value === cron);
  if (presetMatch && presetMatch.value) return presetMatch.label;
  return cron;
}

export interface AutomationsTabProps {
  agentId: string;
  tasks: ScheduledTask[];
  onTasksChange: (tasks: ScheduledTask[]) => void;
  readonly?: boolean;
}

export function AutomationsTab({ agentId, tasks, onTasksChange, readonly = false }: AutomationsTabProps) {
  const [showForm, setShowForm] = useState(false);
  const [editingTask, setEditingTask] = useState<ScheduledTask | null>(null);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [runningTaskId, setRunningTaskId] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  const defaultReportPrompt =
    tasks[0]?.default_report_prompt ??
    "You are a data analyst. Write a concise, well-formatted markdown report based on the query results.";

  const emptyForm = {
    prompt: "",
    schedule: "0 9 * * *",
    customCron: "",
    useCustom: false,
    timezone: "Asia/Dubai",
    output_channel: "",
    is_active: true,
    report_enabled: false,
    report_prompt: defaultReportPrompt,
  };
  const [form, setForm] = useState(emptyForm);

  function openNew() {
    setEditingTask(null);
    setForm({ ...emptyForm, report_prompt: defaultReportPrompt });
    setFormError(null);
    setShowForm(true);
  }

  function openEdit(t: ScheduledTask) {
    const presetMatch = CRON_PRESETS.find((p) => p.value === t.schedule && p.value !== "");
    setEditingTask(t);
    setForm({
      prompt: t.prompt,
      schedule: presetMatch ? t.schedule : "custom",
      customCron: presetMatch ? "" : t.schedule,
      useCustom: !presetMatch,
      timezone: t.timezone,
      output_channel: t.output_channel,
      is_active: t.is_active,
      report_enabled: t.report_enabled,
      report_prompt: t.report_prompt ?? t.default_report_prompt,
    });
    setFormError(null);
    setShowForm(true);
  }

  function cancelForm() {
    setShowForm(false);
    setEditingTask(null);
    setFormError(null);
  }

  async function saveTask() {
    const resolvedCron = form.useCustom ? form.customCron.trim() : form.schedule;
    if (!form.prompt.trim()) { setFormError("Prompt is required."); return; }
    if (!resolvedCron) { setFormError("Schedule is required."); return; }
    if (!form.output_channel.trim()) { setFormError("Output channel is required."); return; }

    setBusy(true);
    setFormError(null);
    try {
      const body = {
        prompt: form.prompt.trim(),
        schedule: resolvedCron,
        timezone: form.timezone,
        output_channel: form.output_channel.trim().replace(/^#/, ""),
        is_active: form.is_active,
        report_enabled: form.report_enabled,
        report_prompt: form.report_enabled ? form.report_prompt.trim() || null : null,
      };
      const url = editingTask
        ? `/api/agents/${agentId}/scheduled-tasks/${editingTask.id}`
        : `/api/agents/${agentId}/scheduled-tasks`;
      const res = await apiFetch(url, {
        method: editingTask ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to save automation.");
      }
      const saved: ScheduledTask = await res.json();
      if (editingTask) {
        onTasksChange(tasks.map((t) => (t.id === saved.id ? saved : t)));
      } else {
        onTasksChange([...tasks, saved]);
      }
      cancelForm();
    } catch (e) {
      setFormError(e instanceof Error ? e.message : "Failed to save automation.");
    } finally {
      setBusy(false);
    }
  }

  async function toggleActive(task: ScheduledTask) {
    try {
      const res = await apiFetch(`/api/agents/${agentId}/scheduled-tasks/${task.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: !task.is_active }),
      });
      if (res.ok) {
        const updated: ScheduledTask = await res.json();
        onTasksChange(tasks.map((t) => (t.id === updated.id ? updated : t)));
      }
    } catch {
      // silent
    }
  }

  async function deleteTask(task: ScheduledTask) {
    if (!window.confirm(`Delete automation "${task.prompt.slice(0, 60)}…"?`)) return;
    try {
      const res = await apiFetch(`/api/agents/${agentId}/scheduled-tasks/${task.id}`, { method: "DELETE" });
      if (res.ok || res.status === 204) {
        onTasksChange(tasks.filter((t) => t.id !== task.id));
      }
    } catch {
      // silent
    }
  }

  async function runTaskNow(task: ScheduledTask) {
    if (runningTaskId) return;
    setRunningTaskId(task.id);
    const startedAt = task.last_run_at;

    try {
      const res = await apiFetch(`/api/agents/${agentId}/scheduled-tasks/${task.id}/run`, {
        method: "POST",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "Failed to trigger run.");
      }
    } catch (e) {
      setRunningTaskId(null);
      window.alert(e instanceof Error ? e.message : "Failed to trigger run.");
      return;
    }

    let polls = 0;
    pollRef.current = setInterval(async () => {
      polls += 1;
      try {
        const res = await apiFetch(`/api/agents/${agentId}/scheduled-tasks`);
        if (!res.ok) return;
        const updatedTasks: ScheduledTask[] = await res.json();
        onTasksChange(updatedTasks);
        const updated = updatedTasks.find((t) => t.id === task.id);
        const runFinished =
          updated &&
          updated.last_run_at &&
          updated.last_run_at !== startedAt &&
          updated.last_run_status;
        if (runFinished || polls >= 90) {
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
          setRunningTaskId(null);
        }
      } catch {
        if (pollRef.current) clearInterval(pollRef.current);
        pollRef.current = null;
        setRunningTaskId(null);
      }
    }, 2000);
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-slate-900">Scheduled Automations</h3>
          <p className="text-xs text-slate-500 mt-0.5">
            Run prompts on a schedule — the agent posts results to a Slack channel using its connected bot token.
          </p>
        </div>
        {!showForm && (
          <button
            onClick={openNew}
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold bg-slate-900 text-white rounded-lg hover:bg-slate-700 transition-colors"
          >
            <Plus className="w-3.5 h-3.5" />
            New automation
          </button>
        )}
      </div>

      {/* Create / edit form */}
      {showForm && (
        <div className="bg-white border border-slate-200 rounded-xl p-5 space-y-4">
          <h4 className="text-sm font-semibold text-slate-900">
            {editingTask ? "Edit automation" : "New automation"}
          </h4>

          {/* Prompt */}
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-slate-700">Prompt</label>
            <textarea
              rows={3}
              value={form.prompt}
              onChange={(e) => setForm({ ...form, prompt: e.target.value })}
              placeholder="e.g. Summarise open P2P disputes from the last 24 hours and post a bullet-point report."
              className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 resize-none focus:outline-none focus:ring-2 focus:ring-slate-900/20"
            />
          </div>

          {/* Schedule */}
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-slate-700">Schedule</label>
              <select
                value={form.useCustom ? "custom" : form.schedule}
                onChange={(e) => {
                  const v = e.target.value;
                  if (v === "custom") {
                    setForm({ ...form, useCustom: true, schedule: "custom" });
                  } else {
                    setForm({ ...form, useCustom: false, schedule: v, customCron: "" });
                  }
                }}
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-slate-900/20 bg-white"
              >
                {CRON_PRESETS.map((p) => (
                  <option key={p.label} value={p.value || "custom"}>{p.label}</option>
                ))}
              </select>
              {form.useCustom && (
                <input
                  type="text"
                  value={form.customCron}
                  onChange={(e) => setForm({ ...form, customCron: e.target.value })}
                  placeholder="0 9 * * 1"
                  className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 font-mono focus:outline-none focus:ring-2 focus:ring-slate-900/20"
                />
              )}
            </div>

            <div className="space-y-1.5">
              <label className="text-xs font-medium text-slate-700">Timezone</label>
              <select
                value={form.timezone}
                onChange={(e) => setForm({ ...form, timezone: e.target.value })}
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-slate-900/20 bg-white"
              >
                {TIMEZONES.map((tz) => (
                  <option key={tz} value={tz}>{tz}</option>
                ))}
              </select>
            </div>
          </div>

          {/* Output channel */}
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-slate-700">Output Slack channel</label>
            <div className="relative">
              <Hash className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
              <input
                type="text"
                value={form.output_channel}
                onChange={(e) => setForm({ ...form, output_channel: e.target.value })}
                placeholder="general"
                className="w-full text-sm border border-slate-200 rounded-lg pl-8 pr-3 py-2 focus:outline-none focus:ring-2 focus:ring-slate-900/20"
              />
            </div>
            <p className="text-[11px] text-slate-400">The agent must be a member of this channel.</p>
          </div>

          {/* Report formatting (per automation) */}
          <div className="border border-slate-100 rounded-lg p-4 space-y-3 bg-slate-50/50">
            <div>
              <p className="text-xs font-medium text-slate-700">Report formatting</p>
              <p className="text-[11px] text-slate-400 mt-0.5">
                When enabled, posts a formatted summary to the output channel after the run.
                When disabled, only in-run tool actions run (e.g. thread replies) — no summary post.
              </p>
            </div>
            <label className={`flex items-center gap-2 select-none ${readonly ? "cursor-default" : "cursor-pointer"}`}>
              <input
                type="checkbox"
                checked={form.report_enabled}
                disabled={readonly}
                onChange={(e) => setForm({ ...form, report_enabled: e.target.checked })}
                className="w-4 h-4 rounded accent-slate-900"
              />
              <span className="text-xs text-slate-700">Enable report formatting step</span>
            </label>
            {form.report_enabled && (
              <div className="space-y-1.5">
                <div className="flex items-center justify-between gap-2">
                  <label className="text-xs font-medium text-slate-700">Report system prompt</label>
                  {!readonly && (
                    <button
                      type="button"
                      onClick={() => setForm({ ...form, report_prompt: defaultReportPrompt })}
                      className="text-[11px] text-slate-500 hover:text-slate-800 underline"
                    >
                      Reset to default
                    </button>
                  )}
                </div>
                <textarea
                  rows={4}
                  value={form.report_prompt}
                  disabled={readonly}
                  onChange={(e) => setForm({ ...form, report_prompt: e.target.value })}
                  className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 resize-y focus:outline-none focus:ring-2 focus:ring-slate-900/20 disabled:bg-slate-50 disabled:text-slate-500 font-mono text-[12px] leading-relaxed bg-white"
                />
              </div>
            )}
          </div>

          {/* Active toggle */}
          <label className="flex items-center gap-2 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={form.is_active}
              onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
              className="w-4 h-4 rounded accent-slate-900"
            />
            <span className="text-xs text-slate-700">Active — run on schedule</span>
          </label>

          {formError && (
            <p className="text-xs text-red-600 flex items-center gap-1">
              <AlertCircle className="w-3.5 h-3.5" />
              {formError}
            </p>
          )}

          <div className="flex gap-2 pt-1">
            <button
              onClick={cancelForm}
              disabled={busy}
              className="px-4 py-2 text-xs text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={saveTask}
              disabled={busy}
              className="flex items-center gap-1.5 px-5 py-2 text-xs font-semibold bg-slate-900 text-white rounded-lg hover:bg-slate-700 transition-colors disabled:opacity-50"
            >
              {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
              {editingTask ? "Save changes" : "Create automation"}
            </button>
          </div>
        </div>
      )}

      {/* Task list */}
      {tasks.length === 0 && !showForm ? (
        <div className="bg-white border border-slate-200 rounded-xl py-12 flex flex-col items-center gap-3 text-slate-400">
          <CalendarClock className="w-8 h-8 opacity-40" />
          <p className="text-sm font-medium">No automations yet</p>
          <p className="text-xs text-center max-w-xs">
            Create a scheduled automation to have this agent run a prompt on a recurring schedule and post results to Slack.
          </p>
        </div>
      ) : (
        tasks.map((task) => (
          <div
            key={task.id}
            className={`bg-white border rounded-xl p-4 space-y-3 ${task.is_active ? "border-slate-200" : "border-slate-100 opacity-60"}`}
          >
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <p className="text-sm text-slate-800 leading-snug line-clamp-2">{task.prompt}</p>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <button
                  onClick={() => runTaskNow(task)}
                  disabled={runningTaskId === task.id}
                  title="Run now"
                  className="p-1.5 rounded-lg text-slate-400 hover:text-emerald-600 hover:bg-emerald-50 transition-colors disabled:opacity-50"
                >
                  {runningTaskId === task.id ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  ) : (
                    <Zap className="w-3.5 h-3.5" />
                  )}
                </button>
                <button
                  onClick={() => toggleActive(task)}
                  title={task.is_active ? "Pause schedule" : "Resume schedule"}
                  className="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-50 transition-colors"
                >
                  {task.is_active ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
                </button>
                <button
                  onClick={() => openEdit(task)}
                  className="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-50 transition-colors"
                >
                  <Pencil className="w-3.5 h-3.5" />
                </button>
                <button
                  onClick={() => deleteTask(task)}
                  className="p-1.5 rounded-lg text-slate-400 hover:text-red-600 hover:bg-red-50 transition-colors"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>

            <div className="flex flex-wrap gap-2">
              <span className="inline-flex items-center gap-1 text-[11px] bg-slate-100 text-slate-600 rounded-md px-2 py-0.5 font-mono">
                <Clock className="w-3 h-3" />
                {describeCron(task.schedule)}
              </span>
              <span className="inline-flex items-center gap-1 text-[11px] bg-slate-100 text-slate-600 rounded-md px-2 py-0.5">
                {task.timezone}
              </span>
              <span className="inline-flex items-center gap-1 text-[11px] bg-slate-100 text-slate-600 rounded-md px-2 py-0.5">
                <Hash className="w-3 h-3" />
                {task.output_channel}
              </span>
              {task.report_enabled && (
                <span className="inline-flex items-center gap-1 text-[11px] bg-indigo-50 text-indigo-700 rounded-md px-2 py-0.5">
                  Report step
                </span>
              )}
              {!task.is_active && (
                <span className="inline-flex items-center gap-1 text-[11px] bg-amber-50 text-amber-600 rounded-md px-2 py-0.5">
                  Schedule paused
                </span>
              )}
              {runningTaskId === task.id && (
                <span className="inline-flex items-center gap-1 text-[11px] bg-emerald-50 text-emerald-600 rounded-md px-2 py-0.5">
                  Running…
                </span>
              )}
            </div>

            {(task.last_run_at || task.run_count > 0) && (
              <div className="flex items-center gap-3 text-[11px] text-slate-400 border-t border-slate-100 pt-2">
                {task.run_count > 0 && (
                  <span>{task.run_count} run{task.run_count !== 1 ? "s" : ""}</span>
                )}
                {task.last_run_at && (
                  <span>Last run: {new Date(task.last_run_at).toLocaleString()}</span>
                )}
                {task.last_run_status && (
                  <span
                    className={`font-medium ${
                      task.last_run_status === "success" ? "text-green-600" : "text-red-500"
                    }`}
                  >
                    {task.last_run_status}
                  </span>
                )}
              </div>
            )}
          </div>
        ))
      )}
    </div>
  );
}
