import {
  useState, useRef, useEffect, useCallback, useMemo,
  type MouseEvent, type ChangeEvent, type SetStateAction,
} from "react";
import { useLocation } from "react-router-dom";
import {
  Plus, X, Columns2, Square, ChevronDown,
  Loader2, FileText, Zap, Trash2,
  Brain, CheckCircle2, XCircle, Download, Eye,
  Search, PanelLeftClose, PanelLeftOpen, MessageSquare, ArrowDown, Copy, Check, Pencil, AlertCircle, ArrowUpRight, BookOpen, Clock,
} from "lucide-react";
import { apiFetch } from "@/lib/api";
import { useSkillList } from "@/lib/SkillAccessContext";
import { useSkillAccess } from "@/lib/SkillAccessContext";
import { readWorkspaceDraft, type WorkspaceDraftState } from "@/src/lib/workspaceDraft";
import { trackEvent } from "@/lib/analytics";
import { getInitials } from "@/lib/user";
import {
  hydrateMessageContent, AssistantMessage,
  type ToolStep,
} from "@/src/lib/chatMarkdown";
import { ProposalCard } from "@/src/components/workspace/ProposalCard";
import { ChatComposer, type ChatComposerHandle } from "@/src/components/composer/ChatComposer";
import type { MentionKind } from "@/src/components/composer/mention/types";
import {
  useWorkspaceChat,
  type Message,
  type PendingAttachment,
  type Tab,
} from "@/src/lib/workspaceChat";
import { useBranding } from "@/lib/branding";

import "./workspace-chat.css";

// ── Spinner verbs ──────────────────────────────────────────────────────────────

// Keep in sync with ATTACHMENT_MAX_BYTES / ATTACHMENT_MAX_COUNT in backend/main.py
const CHAT_ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024;
const CHAT_ATTACHMENT_MAX_COUNT = 3;
const CHAT_ATTACHMENT_MAX_MB = CHAT_ATTACHMENT_MAX_BYTES / (1024 * 1024);

function formatFileSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Keep in sync with MIN_QUERY_CHARS in backend/chat_search.py — below this the
// server returns nothing, so there is no point spending a round trip.
const CHAT_SEARCH_MIN_CHARS = 2;
const CHAT_SEARCH_DEBOUNCE_MS = 250;

/** Only persisted hub chats sync to SQLite; boot failures use local-* tabs. */

/** One hit from GET /api/chats/search. */
interface ChatSearchResult {
  id: string;
  title: string;
  active_skill_id: string | null;
  updated_at: string | null;
  match_count: number;
  matched_in: "title" | "message" | "both";
  snippet: string | null;
}

/** A row rendered in the sidebar while a search is active. */
interface ChatSearchRow {
  id: string;
  title: string;
  activeSkillId: string | null;
  snippet: string | null;
}

interface PdfPanel {
  url: string;
  name: string;
  docType?: "pdf" | "docx";  // defaults to "pdf" for backwards compat
}

// ── Tool activity trail (Cursor-style cascade) ────────────────────────────────

function summarizeToolSteps(steps: ToolStep[]): string {
  const errorCount = steps.filter(s => s.status === "error").length;
  const n = steps.length;
  const base = `${n} tool${n === 1 ? "" : "s"}`;
  return errorCount > 0 ? `${base} · ${errorCount} failed` : base;
}

function ToolStepsTrail({
  steps,
  isStreaming = false,
}: {
  steps: ToolStep[];
  isStreaming?: boolean;
}) {
  const running = isStreaming ? steps.find(s => s.status === "running") : undefined;
  const interrupted = !isStreaming && steps.some(s => s.status === "running");
  const failed = steps.some(s => s.status === "error");
  // Collapsed by default (Cursor-style): header shows the live tool or a summary;
  // expand on click to see the full cascade.
  const [expanded, setExpanded] = useState(false);

  if (steps.length === 0) return null;

  const totalMs = steps.reduce((acc, s) => acc + (s.durationMs ?? 0), 0);
  const headerLabel = running ? running.label : interrupted ? "Response stopped" : summarizeToolSteps(steps);
  // While collapsed during a run, the header IS the live tool — no need to
  // duplicate it in the list when expanded either beyond the cascade.

  return (
    <div className={`space-y-1 ${isStreaming || running ? "mb-2" : "mb-3"}`}>
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        className="group w-full flex items-center gap-2 text-left text-xs text-slate-500 hover:text-slate-700 transition-colors select-none"
        aria-expanded={expanded}
      >
        {running ? (
          <Loader2 className="w-3.5 h-3.5 animate-spin text-slate-400 shrink-0" />
        ) : interrupted ? (
          <Square className="w-3.5 h-3.5 text-slate-400 shrink-0" />
        ) : failed ? (
          <XCircle className="w-3.5 h-3.5 text-red-400 shrink-0" />
        ) : (
          <CheckCircle2 className="w-3.5 h-3.5 text-slate-400 shrink-0" />
        )}
        <span
          className={`flex-1 min-w-0 truncate ${running ? "tool-shimmer-text font-medium" : "text-slate-500"}`}
        >
          {headerLabel}
        </span>
        {!running && totalMs > 0 && (
          <span className="text-slate-400 tabular-nums shrink-0">
            {(totalMs / 1000).toFixed(1)}s
          </span>
        )}
        {steps.length > 1 && (
          <ChevronDown
            className={`w-3.5 h-3.5 text-slate-400 shrink-0 transition-transform ${expanded ? "rotate-180" : ""}`}
          />
        )}
      </button>

      {expanded && steps.length > 0 && (
        <div className="pl-1 space-y-1.5 border-l border-slate-200 ml-1.5">
          {steps.map((step) => {
            const isRun = isStreaming && step.status === "running";
            return (
              <div key={step.id} className="pl-2.5 py-0.5 space-y-1">
                <div className="flex items-center gap-2 text-xs">
                  {isRun ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin text-slate-400 shrink-0" />
                  ) : step.status === "done" ? (
                    <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500 shrink-0" />
                  ) : (
                    <XCircle className="w-3.5 h-3.5 text-red-400 shrink-0" />
                  )}
                  <span
                    className={`flex-1 min-w-0 truncate ${
                      isRun ? "tool-shimmer-text font-medium" : "text-slate-500"
                    }`}
                  >
                    {step.label}
                  </span>
                  {step.durationMs !== undefined && (
                    <span className="text-slate-400 tabular-nums shrink-0">
                      {(step.durationMs / 1000).toFixed(1)}s
                    </span>
                  )}
                </div>
                {step.output && (
                  <pre className="text-[11px] leading-relaxed text-slate-500 bg-slate-900/[0.04] rounded-lg px-2.5 py-2 overflow-x-auto max-h-48 whitespace-pre-wrap break-words font-mono">
                    {step.output.length > 4000 ? `${step.output.slice(0, 4000)}\n…` : step.output}
                  </pre>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── PDF Viewer ────────────────────────────────────────────────────────────────

function DocViewer({ panel, onClose }: { panel: PdfPanel; onClose: () => void }) {
  const isDocx = panel.docType === "docx";
  const [docHtml, setDocHtml] = useState<string | null>(null);
  const [docError, setDocError] = useState<string | null>(null);

  useEffect(() => {
    if (!isDocx) return;
    setDocHtml(null);
    setDocError(null);
    apiFetch(panel.url)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.arrayBuffer();
      })
      .then(buf => import("mammoth").then(m => m.default.convertToHtml({ arrayBuffer: buf })))
      .then(result => setDocHtml(result.value))
      .catch(err => setDocError(String(err)));
  }, [panel.url, isDocx]);

  return (
    <div className="flex flex-col h-full overflow-hidden bg-surface">
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-slate-200 bg-slate-50 shrink-0">
        <FileText className={`w-4 h-4 shrink-0 ${isDocx ? "text-blue-500" : "text-red-500"}`} />
        <span className="text-xs font-semibold text-slate-700 flex-1 truncate">{panel.name}</span>
        <a
          href={panel.url}
          download={panel.name}
          className="text-xs text-slate-500 hover:text-slate-900 transition-colors px-2 py-1 rounded hover:bg-slate-200"
        >
          Download
        </a>
        <button
          onClick={onClose}
          className="text-slate-400 hover:text-slate-700 transition-colors p-1 rounded hover:bg-slate-200"
          title="Close viewer"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      {isDocx ? (
        docError ? (
          <div className="flex-1 flex items-center justify-center p-8 text-sm text-red-500">
            Failed to render document: {docError}
          </div>
        ) : docHtml === null ? (
          <div className="flex-1 flex items-center justify-center gap-2 text-slate-400">
            <Loader2 className="w-4 h-4 animate-spin" />
            <span className="text-sm">Rendering document…</span>
          </div>
        ) : (
          <div className="flex-1 overflow-y-auto">
            <div
              className="mx-auto max-w-3xl px-12 py-10 text-sm leading-relaxed text-slate-800
                [&_h1]:text-2xl [&_h1]:font-bold [&_h1]:mt-6 [&_h1]:mb-3
                [&_h2]:text-xl [&_h2]:font-semibold [&_h2]:mt-5 [&_h2]:mb-2
                [&_h3]:text-base [&_h3]:font-semibold [&_h3]:mt-4 [&_h3]:mb-1.5
                [&_p]:mb-3 [&_p]:leading-relaxed
                [&_ul]:list-disc [&_ul]:pl-6 [&_ul]:mb-3 [&_ul>li]:mb-1
                [&_ol]:list-decimal [&_ol]:pl-6 [&_ol]:mb-3 [&_ol>li]:mb-1
                [&_table]:w-full [&_table]:border-collapse [&_table]:mb-4
                [&_td]:border [&_td]:border-slate-200 [&_td]:px-3 [&_td]:py-2
                [&_th]:border [&_th]:border-slate-200 [&_th]:px-3 [&_th]:py-2 [&_th]:bg-slate-50 [&_th]:font-semibold
                [&_strong]:font-semibold [&_em]:italic
                [&_hr]:border-slate-200 [&_hr]:my-4"
              dangerouslySetInnerHTML={{ __html: docHtml }}
            />
          </div>
        )
      ) : (
        <embed
          src={panel.url}
          type="application/pdf"
          className="flex-1 w-full"
          title={panel.name}
        />
      )}
    </div>
  );
}

// Keep backward-compat alias used in JSX below
const PdfViewer = DocViewer;

// ── ChatPane ──────────────────────────────────────────────────────────────────

// Keep uploads and pending choices with their conversation when switching chats.
function useChatState<T>(chatId: string, initial: T) {
  const [values, setValues] = useState<Record<string, T>>({});
  const update = useCallback((next: SetStateAction<T>) => {
    setValues(prev => ({ ...prev, [chatId]: typeof next === "function" ? (next as (value: T) => T)(prev[chatId] ?? initial) : next }));
  }, [chatId]);
  return [values[chatId] ?? initial, update] as const;
}

function ChatPane({
  tab,
  onUploadPdf,
  availableModels,
  defaultModel,
  initialDraft,
  onDraftConsumed,
}: {
  tab: Tab;
  onUploadPdf: (url: string, name: string, docType?: "pdf" | "docx") => void;
  availableModels: string[];
  defaultModel: string;
  initialDraft?: WorkspaceDraftState | null;
  onDraftConsumed?: () => void;
}) {
  const branding = useBranding();
  const appName = branding.app_name;
  const logoUrl = branding.app_logo_url;
  const {
    updateMessages,
    setSkill,
    setModel,
    sendMessage: sendChatMessage,
    stopStream,
    answerNow,
    loadTabMessages,
    isTabStreaming,
  } = useWorkspaceChat();
  const [isUploading, setIsUploading] = useChatState(tab.id, false);
  const [showJump, setShowJump] = useState(false);
  const [copiedMessage, setCopiedMessage] = useState<number | null>(null);
  const [copyError, setCopyError] = useState(false);
  const [thinkingEnabled, setThinkingEnabled] = useState(false);
  const [pendingAttachments, setPendingAttachments] = useChatState<PendingAttachment[]>(tab.id, []);
  const [skillDisambiguation, setSkillDisambiguation] = useChatState<{
    candidates: { slug: string; confidence: number; reason: string }[];
    pending: { newMessages: Message[]; sentAttachments: PendingAttachment[] };
  } | null>(tab.id, null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesScrollRef = useRef<HTMLDivElement>(null);
  // Auto-follow streaming only while the user is near the bottom; scrolling up pauses it.
  const stickToBottomRef = useRef(true);
  const { employee } = useSkillAccess();
  const userInitials = employee ? getInitials(employee.name, employee.email) : "??";
  const attachInputRef = useRef<HTMLInputElement>(null);
  const chatComposerRef = useRef<ChatComposerHandle>(null);
  const currentTabIdRef = useRef(tab.id);
  currentTabIdRef.current = tab.id;
  const skillList = useSkillList();
  const streaming = isTabStreaming(tab.id);

  const stopStreamLocal = useCallback(async () => {
    await stopStream(tab.id);
  }, [stopStream, tab.id]);

  const answerNowLocal = useCallback(async () => {
    await answerNow(tab.id);
  }, [answerNow, tab.id]);
  const activeSkill = skillList.find(s => s.id === tab.activeSkillId) ?? null;
  const effectiveModel = tab.selectedModel || defaultModel;
  const isAutoModel = effectiveModel === "auto-workhorse";
  // For Claude-specific UI (thinking toggle) check the resolved model when auto is active
  const resolvedDisplayModel = isAutoModel ? (tab.resolvedModel ?? "") : effectiveModel;
  const isClaudeModel = resolvedDisplayModel.startsWith("claude-");

  const isNearBottom = useCallback((el: HTMLDivElement, thresholdPx = 96) => {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= thresholdPx;
  }, []);

  const handleMessagesScroll = useCallback(() => {
    const el = messagesScrollRef.current;
    if (!el) return;
    stickToBottomRef.current = isNearBottom(el);
    setShowJump(!stickToBottomRef.current);
  }, [isNearBottom]);

  useEffect(() => {
    if (!stickToBottomRef.current) return;
    const el = messagesScrollRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    } else {
      messagesEndRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
    }
  }, [tab.messages, tab.id]);

  useEffect(() => { stickToBottomRef.current = true; setShowJump(false); setCopiedMessage(null); setCopyError(false); }, [tab.id]);

  const handleAttachmentChange = useCallback(async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []) as File[];
    e.target.value = "";
    if (!files.length) return;
    const available = CHAT_ATTACHMENT_MAX_COUNT - pendingAttachments.length;
    const toUpload = files.slice(0, available);
    const oversized = toUpload.filter(file => file.size > CHAT_ATTACHMENT_MAX_BYTES);
    if (oversized.length > 0) {
      const names = oversized.map(f => `"${f.name}" (${formatFileSize(f.size)})`).join(", ");
      updateMessages(tab.id, [
        ...tab.messages,
        {
          role: "model",
          content: `**Attachment error:** ${names} exceed${oversized.length === 1 ? "s" : ""} the ${CHAT_ATTACHMENT_MAX_MB} MB per-file limit. Compress the file or split it into smaller parts.`,
        },
      ]);
      if (toUpload.length === oversized.length) return;
    }
    const validFiles = toUpload.filter(file => file.size <= CHAT_ATTACHMENT_MAX_BYTES);
    setIsUploading(true);
    try {
      for (const file of validFiles) {
        const previewUrl = file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined;
        const form = new FormData();
        form.append("file", file);
        const res = await apiFetch("/api/chat/upload", { method: "POST", body: form });
        if (!res.ok) {
          const err = await res.json().catch(() => ({ detail: "Upload failed" }));
          throw new Error(err.detail ?? "Upload failed");
        }
        const { id, mime } = await res.json();
        setPendingAttachments(prev => [...prev, { id, mime, name: file.name, previewUrl }]);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Upload failed";
      updateMessages(tab.id, [
        ...tab.messages,
        { role: "model", content: `**Attachment error:** ${msg}` },
      ]);
    } finally {
      setIsUploading(false);
    }
  }, [tab, pendingAttachments.length, updateMessages]);

  const removeAttachment = useCallback((idx: number) => {
    setPendingAttachments(prev => {
      const att = prev[idx];
      if (att?.previewUrl) URL.revokeObjectURL(att.previewUrl);
      return prev.filter((_, i) => i !== idx);
    });
  }, []);

  const sendMessage = useCallback(async (
    textOrRetry?: string | {
      skipUserBubble?: boolean;
      pending: { newMessages: Message[]; sentAttachments: PendingAttachment[] };
      skillId?: string | null;
      noSkill?: boolean;
    },
    options?: { skillId?: string | null },
    createKind?: MentionKind | null,
  ) => {
    stickToBottomRef.current = true;
    setShowJump(false);
    await sendChatMessage(tab.id, {
      textOrRetry,
      options,
      createKind,
      thinkingEnabled,
      pendingAttachments,
      onClearPendingAttachments: () => setPendingAttachments([]),
      onSkillDisambiguation: setSkillDisambiguation,
      isFocusedTab: () => currentTabIdRef.current === tab.id,
      onClearPendingCreate: () => chatComposerRef.current?.clearPendingCreate(),
    });
  }, [sendChatMessage, tab.id, thinkingEnabled, pendingAttachments]);

  // Consume the draft carried over from Home (Ask bar / template cards). Must
  // live below sendMessage — it depends on it, and a dep-array reference from
  // above sendMessage's `const` would hit its TDZ during render.
  //
  // `autoSend` (the default whenever a prompt is present, see
  // lib/workspaceDraft.ts) sends straight away so Home's Enter key behaves like
  // Enter in the composer, rather than making the user press it a second time.
  // The ref guard keeps that to exactly one send: onDraftConsumed only clears
  // the draft on the parent's next render, so without it a sendMessage identity
  // change in between would re-fire this effect and double-send.
  const draftHandledRef = useRef(false);
  useEffect(() => {
    if (!initialDraft || draftHandledRef.current) return;
    draftHandledRef.current = true;

    const prompt = initialDraft.prompt?.trim();
    const skillId = initialDraft.skillId ?? null;
    if (skillId) setSkill(tab.id, skillId);

    if (prompt && initialDraft.autoSend) {
      // Pass skillId explicitly: onSetSkill's state update hasn't reached
      // tab.activeSkillId yet, so sendMessage would otherwise send with no skill.
      void sendMessage(prompt, { skillId: skillId ?? tab.activeSkillId ?? null });
    } else if (prompt) {
      chatComposerRef.current?.setText(prompt);
    }
    onDraftConsumed?.();
  }, [initialDraft, onDraftConsumed, setSkill, sendMessage, tab.id, tab.activeSkillId]);

  const resolveSkillDisambiguation = useCallback((choice: { skillId: string | null; noSkill: boolean }) => {
    if (!skillDisambiguation) return;
    const pending = skillDisambiguation.pending;
    setSkillDisambiguation(null);
    if (choice.noSkill) {
      setSkill(tab.id, null, false);
      void sendMessage({ skipUserBubble: true, pending, noSkill: true });
      return;
    }
    if (choice.skillId) {
      setSkill(tab.id, choice.skillId, false);
      void sendMessage({ skipUserBubble: true, pending, skillId: choice.skillId });
    }
  }, [skillDisambiguation, setSkill, sendMessage, tab.id]);

  return (
    <div className="workspace-pane flex flex-col h-full min-h-0 relative">
      {skillDisambiguation && (
        <div className="absolute inset-0 z-40 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
          <div
            className="bg-surface rounded-2xl shadow-2xl border border-slate-200 w-full max-w-lg"
            role="dialog"
            aria-labelledby="skill-disambiguation-title"
          >
            <div className="px-6 pt-5 pb-3 border-b border-slate-100">
              <h2 id="skill-disambiguation-title" className="text-base font-bold text-slate-900">
                Which skill fits this message?
              </h2>
              <p className="text-sm text-slate-500 mt-1">
                We weren&apos;t confident enough to pick automatically. Choose a skill or continue without one.
              </p>
            </div>
            <div className="px-4 py-3 space-y-2 max-h-[min(50vh,320px)] overflow-y-auto">
              {skillDisambiguation.candidates.map(c => {
                const skill = skillList.find(s => s.id === c.slug || s.slug === c.slug);
                const label = skill?.name ?? c.slug;
                const pct = Math.round(c.confidence * 100);
                return (
                  <button
                    key={c.slug}
                    type="button"
                    onClick={() => resolveSkillDisambiguation({ skillId: c.slug, noSkill: false })}
                    className="w-full text-left rounded-xl border border-slate-200 hover:border-[#FF444F]/40 hover:bg-brand-tint px-4 py-3 transition-colors"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-semibold text-slate-900">{label}</span>
                      <span className="text-xs font-mono text-slate-500 shrink-0">{pct}% match</span>
                    </div>
                    {c.reason && (
                      <p className="text-xs text-slate-500 mt-1 line-clamp-2">{c.reason}</p>
                    )}
                  </button>
                );
              })}
            </div>
            <div className="px-4 pb-5 pt-2 flex flex-col gap-2 border-t border-slate-100">
              <button
                type="button"
                onClick={() => resolveSkillDisambiguation({ skillId: null, noSkill: true })}
                className="w-full px-4 py-2.5 text-sm font-semibold text-slate-700 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
              >
                No skill — general assistant
              </button>
            </div>
          </div>
        </div>
      )}
      {/* Hidden file input for chat attachments */}
      <input
        ref={attachInputRef}
        type="file"
        accept="image/jpeg,image/png,image/gif,image/webp,application/pdf,.pdf"
        multiple
        className="hidden"
        onChange={handleAttachmentChange}
      />

      {/* Active skill banner.
          The neutral ramp is inverted under `.dark` (see index.css), so the
          light-mode `bg-slate-900 / text-slate-50` pair would flip to a white
          strip — the dark: overrides pin it to real surface tokens instead. */}
      {activeSkill && (
        <div className="flex items-center gap-2 px-4 py-2 bg-slate-900 text-slate-50 dark:bg-surface-muted dark:text-foreground dark:border-b dark:border-slate-200 text-xs font-medium shrink-0">
          <Zap className="w-3 h-3 text-amber-400" />
          <span className="font-mono text-white/70 dark:text-slate-500">Skill active:</span>
          <span className="font-semibold">{activeSkill.name}</span>
          {tab.activeSkillAuto && (
            <span className="px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 text-[10px] font-semibold uppercase tracking-wide">
              Auto
            </span>
          )}
          <button
            onClick={() => setSkill(tab.id, null)}
            className="ml-auto text-slate-400 hover:text-white dark:hover:text-slate-900 transition-colors"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {!tab.messagesLoaded ? (
        <div className="workspace-loading" role="status"><Loader2 size={22} className="animate-spin" /><span>Opening your conversation…</span></div>
      ) : tab.messagesLoadError ? (
        <div className="workspace-loading" role="alert"><AlertCircle size={24} /><strong>We couldn’t load this conversation.</strong><span>Your saved messages haven’t been changed.</span><button className="workspace-header-new" onClick={() => loadTabMessages(tab.id)}>Try loading again</button></div>
      ) : tab.messages.length === 0 ? (
        /* ── EMPTY STATE: centered in available space ── */
        <div className="workspace-welcome">
          <div className="workspace-welcome-inner">
          <div className="workspace-greeting">
            <div className="workspace-eyebrow"><span className="workspace-agent-mark"><img src={logoUrl} alt="" /></span> YOUR WORK, A LITTLE LIGHTER</div>
            <h1>{activeSkill ? `Let’s work with ${activeSkill.name}.` : <>Big ideas. Small tasks.<br /><span>Let’s get it done.</span></>}</h1>
            <p>{activeSkill ? "Tell me what you need. I’ll use this skill to help." : `I’m ${appName}, your partner for the workday. Ask a question, untangle an idea, or turn a to-do into done.`}</p>
          </div>

          {/* Centered input */}
          <div className="w-full" data-tour="workspace-composer">
            <ChatComposer
              key={tab.id}
              ref={chatComposerRef}
              variant="empty"
              onSend={(text, createKind) => { void sendMessage(text, undefined, createKind); }}
              isStreaming={streaming}
              onStop={stopStreamLocal}
              onAnswerNow={answerNowLocal}
              activeSkill={activeSkill}
              activeSkillAuto={tab.activeSkillAuto}
              activeSkillId={tab.activeSkillId}
              onSelectSkill={id => setSkill(tab.id, id, false)}
              onRemoveSkill={() => setSkill(tab.id, null)}
              pendingAttachments={pendingAttachments}
              onRemoveAttachment={removeAttachment}
              isUploading={isUploading}
              onAttachFile={() => attachInputRef.current?.click()}
              thinkingEnabled={thinkingEnabled}
              onToggleThinking={() => setThinkingEnabled(v => !v)}
              isClaudeModel={isClaudeModel}
              availableModels={availableModels}
              defaultModel={defaultModel}
              selectedModel={tab.selectedModel}
              onSetModel={model => setModel(tab.id, model)}
              isAutoModel={isAutoModel}
              resolvedModel={tab.resolvedModel}
            />
          </div>
          <div className="workspace-starters">
            <div className="workspace-section-label">A LITTLE INSPIRATION <span>Pick one, then make it yours</span></div>
            <div className="workspace-starter-grid">
              {[
                { icon: BookOpen, title: "Make sense of something", text: "A document, a decision, a complex idea.", prompt: "Help me understand a topic. Start by asking what I’m working on, then explain it in plain language with an example.", tone: "mint" },
                { icon: Pencil, title: "Find the right words", text: "From rough notes to a ready-to-share draft.", prompt: "Help me turn my rough notes into a clear update for my team. Ask me for the notes and who it’s for before drafting.", tone: "peach" },
                { icon: Clock, title: "Take a task off my plate", text: "Find a simpler way to do repeat work.", prompt: "Help me simplify a repetitive task. Ask what I do, how often, and which tools I use. Then suggest a practical next step.", tone: "lavender" },
              ].map(item => <button key={item.title} className="workspace-starter" onClick={() => chatComposerRef.current?.setText(item.prompt)}>
                <span className={`workspace-starter-icon ${item.tone}`}><item.icon size={18} /></span>
                <ArrowUpRight className="workspace-starter-arrow" size={16} />
                <strong>{item.title}</strong><span>{item.text}</span>
              </button>)}
            </div>
          </div>
          </div>
        </div>
      ) : (
        /* ── ACTIVE CONVERSATION ── */
        <>
          {/* Messages */}
          <div
            ref={messagesScrollRef}
            onScroll={handleMessagesScroll}
            className="workspace-messages flex-1 overflow-y-auto py-6"
            role="region" aria-label="Conversation"
          >
            <div className="workspace-reading-column mx-auto space-y-8">
              {tab.messages.map((msg, i) => (
                <div key={i} className={`flex gap-3 ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
                  {msg.role === "model" && (
                    <div className="w-7 h-7 rounded-lg bg-[#FF444F] flex items-center justify-center shrink-0 mt-0.5 overflow-hidden">
                      <img src={logoUrl} alt="" className="w-7 h-7 object-contain" />
                    </div>
                  )}
                  <div
                    className={`text-sm leading-relaxed ${
                      msg.role === "user"
                        ? "workspace-user-message max-w-[85%] text-slate-900 px-5 py-3 whitespace-pre-wrap break-words"
                        : "workspace-agent-message min-w-0 flex-1 text-slate-700 px-0.5 py-0.5"
                    }`}
                  >
                    {msg.role === "model" ? (() => {
                      // Always parse GENFILE markers from content so file cards
                      // survive page reloads (markers are persisted in the DB).
                      const hydrated = hydrateMessageContent(msg.content);
                      const displayContent = hydrated.content;
                      const parsedFiles = hydrated.files;
                      const parsedProposal = hydrated.proposal;
                      const parsedSteps = hydrated.steps;
                      // Merge: live SSE files/proposal/steps (during streaming) take precedence;
                      // fall back to DB-parsed markers after reload.
                      const resolvedFiles = (msg.files && msg.files.length > 0) ? msg.files : parsedFiles;
                      const resolvedProposal = msg.proposal ?? parsedProposal;
                      const resolvedSteps = (msg.steps && msg.steps.length > 0) ? msg.steps : parsedSteps;
                      const isLiveMessage = streaming && i === tab.messages.length - 1;

                      const isError = /^\*\*(?:Error|Attachment error):\*\*/i.test(displayContent) || displayContent.startsWith("I couldn’t complete that request.");
                      if (isError) {
                        const attachmentError = /^\*\*Attachment error/i.test(displayContent);
                        const notConfigured = /MODEL_GATEWAY|not configured|API key/i.test(displayContent);
                        // Spend budgets (Admin > Budgets): show the reason, it tells the user what to do.
                        const overBudget = /monthly spend limit/i.test(displayContent);
                        return <div className="workspace-service-error" role="alert">
                          <AlertCircle size={18} />
                          <div><strong>{attachmentError ? "That file couldn’t be attached" : overBudget ? "Monthly spend limit reached" : notConfigured ? "The agent isn’t connected yet" : "That request didn’t go through"}</strong>
                            <p>{attachmentError ? displayContent.replace(/^\*\*Attachment error:\*\*\s*/i, "") : overBudget ? displayContent.replace(/^\*\*Error:\*\*\s*/i, "") : notConfigured ? `This workspace needs an AI connection before ${appName} can respond. Ask your workspace administrator to connect it. Your message is still here.` : "Your conversation is still here. Review your request and try again when you’re ready."}</p>
                            {!attachmentError && !overBudget && <details><summary>Technical details</summary><p>{displayContent.replace(/\*\*/g, "")}</p></details>}
                            <button disabled={streaming} onClick={() => { const lastUser = [...tab.messages].reverse().find(m => m.role === "user"); if (lastUser) chatComposerRef.current?.setText(lastUser.content); }}><Pencil size={14} />Revise my request</button>
                          </div>
                        </div>;
                      }
                      return displayContent === "" && !msg.thinking && !resolvedSteps.length && resolvedFiles.length === 0 && !resolvedProposal ? (
                        <span className="workspace-working" role="status">{isLiveMessage && <span className="workspace-thinking-dots"><i /><i /><i /></span>}{isLiveMessage ? (tab.streamingStatus || "Thinking it through…") : "Response stopped. You can send a follow-up whenever you’re ready."}</span>
                      ) : (
                        <>
                          {msg.thinking && (
                            <details className="mb-3 group" open={!displayContent}>
                              <summary className="cursor-pointer flex items-center gap-1.5 text-xs text-slate-400 hover:text-violet-700 font-medium select-none list-none transition-colors">
                                <Brain className="w-3 h-3 shrink-0" />
                                <span>Thinking</span>
                                <ChevronDown className="w-3 h-3 ml-auto transition-transform group-open:rotate-180" />
                              </summary>
                              <div className="mt-2 pl-3 border-l-2 border-violet-100 text-xs text-slate-500 whitespace-pre-wrap leading-relaxed max-h-64 overflow-y-auto">
                                {msg.thinking}
                              </div>
                            </details>
                          )}
                          {(resolvedSteps.length > 0 || (msg.steps && msg.steps.length > 0)) && (
                            <ToolStepsTrail steps={resolvedSteps.length > 0 ? resolvedSteps : (msg.steps ?? [])} isStreaming={isLiveMessage} />
                          )}
                          {displayContent && <>
                            <div className="workspace-message-author">{appName}</div>
                            <AssistantMessage content={displayContent} onOpenPdf={onUploadPdf} />
                            {!isLiveMessage && <div className="workspace-message-actions">
                              <button aria-label="Copy response" onClick={async () => {
                                try { await navigator.clipboard.writeText(displayContent); setCopiedMessage(i); setCopyError(false); }
                                catch { setCopyError(true); }
                              }}>{copiedMessage === i ? <Check size={14} /> : <Copy size={14} />}{copiedMessage === i ? "Copied" : "Copy"}</button>
                              {i === tab.messages.length - 1 && <button disabled={streaming} onClick={() => {
                                const lastUser = [...tab.messages].reverse().find(m => m.role === "user");
                                if (lastUser) chatComposerRef.current?.setText(lastUser.content);
                              }}><Pencil size={14} />Revise my request</button>}
                              {copyError && <span role="status">Couldn’t copy. Select the response to copy it manually.</span>}
                            </div>}
                          </>}
                          {resolvedFiles.length > 0 && (
                            <div className={`${displayContent ? "mt-3" : ""} space-y-2`}>
                              {resolvedFiles.map((f) => {
                                const ext = f.filename.split(".").pop()?.toLowerCase() ?? "";
                                const isViewable = ext === "pdf" || ext === "docx";
                                const docType = ext === "docx" ? "docx" : "pdf";
                                return (
                                  <div
                                    key={f.filename}
                                    className="flex items-center gap-2.5 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm"
                                  >
                                    <div className="w-8 h-8 rounded-md bg-blue-50 border border-blue-100 flex items-center justify-center shrink-0">
                                      <FileText className="w-4 h-4 text-blue-500" />
                                    </div>
                                    <div className="flex-1 min-w-0">
                                      <div className="font-medium text-slate-700 truncate">{f.filename}</div>
                                      <div className="text-xs text-slate-400">
                                        {f.sizeBytes > 0 ? `${(f.sizeBytes / 1024).toFixed(1)} KB` : ""}
                                      </div>
                                    </div>
                                    <div className="flex items-center gap-1 shrink-0">
                                      {isViewable && (
                                        <button
                                          onClick={() => onUploadPdf(f.url, f.filename, docType as "pdf" | "docx")}
                                          className="flex items-center gap-1 text-xs text-blue-700 hover:text-blue-800 px-2 py-1.5 rounded hover:bg-blue-50 transition-colors font-medium"
                                          title="View in panel"
                                        >
                                          <Eye className="w-3.5 h-3.5" />
                                          View
                                        </button>
                                      )}
                                      <a
                                        href={f.url}
                                        download={f.filename}
                                        className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-800 px-2 py-1.5 rounded hover:bg-slate-200 transition-colors font-medium"
                                        title="Download"
                                      >
                                        <Download className="w-3.5 h-3.5" />
                                        Download
                                      </a>
                                    </div>
                                  </div>
                                );
                              })}
                            </div>
                          )}
                          {resolvedProposal && (
                            <div className={displayContent || resolvedFiles.length > 0 ? "mt-3" : ""}>
                              <ProposalCard proposalId={resolvedProposal.id} initialKind={resolvedProposal.kind} />
                            </div>
                          )}
                        </>
                      );
                    })() : (
                      msg.content
                    )}
                  </div>
                  {msg.role === "user" && (
                    <div className="w-7 h-7 rounded-full bg-[#FF444F]/10 border border-[#FF444F]/30 flex items-center justify-center shrink-0 mt-0.5 text-xs font-bold text-[#FF444F]">
                      {userInitials}
                    </div>
                  )}
                </div>
              ))}
              {/* Streaming status — controls live in the input bar only */}
              {streaming && tab.streamingStatus && tab.messages.at(-1)?.content && !tab.messages.at(-1)?.steps?.some(s => s.status === "running") && (
                <div className="flex items-center gap-2 pl-10">
                  <div className="flex items-center gap-2 text-xs text-slate-500 px-1 py-1">
                    <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse shrink-0" />
                    <span className="italic">{tab.streamingStatus}</span>
                  </div>
                </div>
              )}
              <div ref={messagesEndRef} />
            </div>
          </div>

          {/* Bottom input */}
          <div className="workspace-composer-dock shrink-0">
            {showJump && <button className="workspace-jump" onClick={() => { const el = messagesScrollRef.current; if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" }); stickToBottomRef.current = true; setShowJump(false); }}><ArrowDown size={14} />Latest message</button>}
            <div className="workspace-reading-column mx-auto" data-tour="workspace-composer">
              <ChatComposer
                key={tab.id}
              ref={chatComposerRef}
                variant="docked"
                onSend={(text, createKind) => { void sendMessage(text, undefined, createKind); }}
                isStreaming={streaming}
                onStop={stopStreamLocal}
                onAnswerNow={answerNowLocal}
                activeSkill={activeSkill}
                activeSkillAuto={tab.activeSkillAuto}
                activeSkillId={tab.activeSkillId}
                onSelectSkill={id => setSkill(tab.id, id, false)}
                onRemoveSkill={() => setSkill(tab.id, null)}
                pendingAttachments={pendingAttachments}
                onRemoveAttachment={removeAttachment}
                isUploading={isUploading}
                onAttachFile={() => attachInputRef.current?.click()}
                thinkingEnabled={thinkingEnabled}
                onToggleThinking={() => setThinkingEnabled(v => !v)}
                isClaudeModel={isClaudeModel}
                availableModels={availableModels}
                defaultModel={defaultModel}
                selectedModel={tab.selectedModel}
                onSetModel={model => setModel(tab.id, model)}
                isAutoModel={isAutoModel}
                resolvedModel={tab.resolvedModel}
              />
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// ── WorkspacePage ─────────────────────────────────────────────────────────────

export default function WorkspacePage() {
  const branding = useBranding();
  const appName = branding.app_name;
  const skillList = useSkillList();
  const location = useLocation();
  const {
    tabs,
    activeTabId,
    booting,
    availableModels,
    defaultModel,
    setActiveTabId,
    addTab,
    deleteTab,
    loadTabMessages,
    adoptChatTab,
    isTabStreaming,
  } = useWorkspaceChat();
  const [splitMode, setSplitMode] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [chatSearch, setChatSearch] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [rightTabId, setRightTabId] = useState<string | null>(null);
  const [pdfPanel, setPdfPanel] = useState<PdfPanel | null>(null);
  const [searchResults, setSearchResults] = useState<ChatSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const pendingDraftRef = useRef<WorkspaceDraftState | null>(readWorkspaceDraft(location.state));
  const [workspaceDraft, setWorkspaceDraft] = useState<WorkspaceDraftState | null>(null);

  const consumeWorkspaceDraft = useCallback(() => {
    setWorkspaceDraft(null);
  }, []);

  const handleTabSelect = useCallback((tabId: string) => {
    setActiveTabId(tabId);
    const tab = tabs.find(t => t.id === tabId);
    if (tab && !tab.messagesLoaded) loadTabMessages(tabId);
  }, [tabs, loadTabMessages, setActiveTabId]);

  // ── Chat history search ──────────────────────────────────────────────────
  //
  // Titles are already in memory (boot loads every chat), so those match as the
  // user types. Message bodies live only in the DB, so they come from a
  // debounced /api/chats/search call and are merged in behind the title hits.

  useEffect(() => {
    const q = chatSearch.trim();
    if (q.length < CHAT_SEARCH_MIN_CHARS) {
      setSearchResults([]);
      setSearching(false);
      return;
    }
    setSearching(true);
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const res = await apiFetch(
            `/api/chats/search?q=${encodeURIComponent(q)}`,
            { signal: controller.signal },
          );
          if (!res.ok) throw new Error("search failed");
          const data = await res.json();
          const results: ChatSearchResult[] = Array.isArray(data.results) ? data.results : [];
          setSearchResults(results);
          trackEvent("workspace.history_searched", { has_results: results.length > 0 });
        } catch {
          if (!controller.signal.aborted) setSearchResults([]);
        } finally {
          if (!controller.signal.aborted) setSearching(false);
        }
      })();
    }, CHAT_SEARCH_DEBOUNCE_MS);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [chatSearch]);

  const trimmedQuery = chatSearch.trim();
  const hasQuery = trimmedQuery.length > 0;

  const searchRows = useMemo<ChatSearchRow[]>(() => {
    if (!hasQuery) return [];
    const q = trimmedQuery.toLowerCase();
    const rows: ChatSearchRow[] = [];
    const seen = new Set<string>();
    for (const tab of tabs) {
      if (!tab.title.toLowerCase().includes(q)) continue;
      rows.push({ id: tab.id, title: tab.title, activeSkillId: tab.activeSkillId, snippet: null });
      seen.add(tab.id);
    }
    for (const result of searchResults) {
      if (seen.has(result.id)) {
        const row = rows.find(r => r.id === result.id);
        if (row && !row.snippet) row.snippet = result.snippet;
        continue;
      }
      seen.add(result.id);
      rows.push({
        id: result.id,
        title: result.title,
        activeSkillId: result.active_skill_id,
        snippet: result.snippet,
      });
    }
    return rows;
  }, [hasQuery, trimmedQuery, tabs, searchResults]);

  const openSearchResult = useCallback((row: ChatSearchRow) => {
    if (tabs.some(t => t.id === row.id)) {
      handleTabSelect(row.id);
      return;
    }
    adoptChatTab(row);
  }, [tabs, handleTabSelect, adoptChatTab]);

  const handleAddTab = useCallback(async () => {
    const newId = await addTab();
    if (splitMode && !rightTabId && !pdfPanel && newId) setRightTabId(newId);
  }, [addTab, splitMode, rightTabId, pdfPanel]);

  // ── Fresh chat on arrival (sidebar "Workspace" click, Home Ask/templates) ──
  //
  // react-router assigns a new `location.key` per navigation, so keying off it
  // fires on every arrival rather than only first mount — while still not
  // re-firing when unrelated state changes re-run this effect. Home drafts
  // land here too: create the tab first, then release the draft so ChatPane
  // auto-sends it into that new chat rather than an existing thread.
  const handledNewChatKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (booting) return;
    const wantsNewChat =
      Boolean((location.state as { newChat?: boolean } | null)?.newChat) ||
      Boolean(pendingDraftRef.current);
    if (!wantsNewChat) return;
    if (handledNewChatKeyRef.current === location.key) return;
    handledNewChatKeyRef.current = location.key;
    const draft = pendingDraftRef.current;
    pendingDraftRef.current = null;
    void (async () => {
      await addTab();
      if (draft) setWorkspaceDraft(draft);
    })();
  }, [location, booting, addTab]);

  // ── Close tab (hide only — chat persists in DB) ──────────────────────────

  const closeTab = useCallback(async (id: string, e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    const remaining = tabs.filter(t => t.id !== id);
    if (remaining.length === 0) {
      await addTab();
    } else if (activeTabId === id) {
      const idx = tabs.findIndex(t => t.id === id);
      setActiveTabId(remaining[Math.max(0, idx - 1)].id);
    }
    if (rightTabId === id) setRightTabId(null);
  }, [tabs, activeTabId, rightTabId, addTab, setActiveTabId]);

  const handleDeleteTab = useCallback(async (id: string, e: MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    const tab = tabs.find(t => t.id === id);
    if (!confirm(`Delete "${tab?.title ?? "this chat"}" permanently? This cannot be undone.`)) return;
    setHistoryError("");
    const ok = await deleteTab(id);
    if (!ok) setHistoryError("Couldn’t delete that conversation. It’s still in your history. Please try again.");
    if (rightTabId === id) setRightTabId(null);
  }, [tabs, deleteTab, rightTabId]);

  const handleUploadPdf = useCallback((url: string, name: string, docType?: "pdf" | "docx") => {
    setPdfPanel({ url, name, docType: docType ?? "pdf" });
    setSplitMode(true);
  }, []);

  const closePdf = useCallback(() => {
    setPdfPanel(null);
    if (!rightTabId) setSplitMode(false);
  }, [rightTabId]);

  const activeTab = tabs.find(t => t.id === activeTabId) ?? tabs[0] ?? null;
  const rightTab = tabs.find(t => t.id === rightTabId) ?? null;

  const toggleSplit = async () => {
    if (splitMode && !pdfPanel) {
      setSplitMode(false);
      setRightTabId(null);
    } else if (!splitMode) {
      setSplitMode(true);
      if (!pdfPanel) {
        const other = tabs.find(t => t.id !== activeTabId);
        if (other) {
          setRightTabId(other.id);
          if (!other.messagesLoaded) void loadTabMessages(other.id);
        } else {
          try {
            const newTabId = await addTab();
            setRightTabId(newTabId);
          } catch { setSplitMode(false); }
        }
      }
    }
  };

  const showRightPane = splitMode && (pdfPanel !== null || rightTab !== null);

  if (booting) return (
    <div className="flex items-center justify-center h-full">
      <Loader2 className="w-5 h-5 animate-spin text-slate-400" />
    </div>
  );

  if (!activeTab) return null;

  return (
    <div className="workspace-chat flex h-full w-full overflow-hidden">
      {historyOpen && <aside className="workspace-history" aria-label="Chat history">
        <div className="workspace-history-heading"><span>Your conversations</span><button aria-label="Close history" onClick={() => setHistoryOpen(false)}><PanelLeftClose size={17} /></button></div>
        <button className="workspace-new-chat" onClick={handleAddTab}><Plus size={16} />New conversation</button>
        <label className="workspace-history-search">
          <Search size={15} />
          <input
            value={chatSearch}
            onChange={e => setChatSearch(e.target.value)}
            placeholder="Search conversations"
            aria-label="Search conversations"
          />
          {searching && <Loader2 size={13} className="animate-spin" />}
        </label>
        {historyError && <p role="alert" className="workspace-history-empty">{historyError}</p>}
        <div className="workspace-history-list">
          <div className="workspace-section-label">CONVERSATIONS <span>{tabs.length}</span></div>
          {hasQuery ? (
            searchRows.length === 0 ? (
              <p className="workspace-history-empty">
                {trimmedQuery.length < CHAT_SEARCH_MIN_CHARS
                  ? "Keep typing to search message text\u2026"
                  : searching
                    ? "Searching\u2026"
                    : `No conversations match \u201c${trimmedQuery}\u201d`}
              </p>
            ) : searchRows.map(row => (
              <div key={row.id} className={`workspace-history-row ${activeTabId === row.id ? "is-active" : ""}`}>
                <button
                  className="workspace-history-select"
                  aria-current={activeTabId === row.id ? "true" : undefined}
                  onClick={() => openSearchResult(row)}
                  title={row.snippet ?? undefined}
                >
                  <MessageSquare size={15} />
                  <span>{row.title === "New Chat" ? "New conversation" : row.title}</span>
                  {isTabStreaming(row.id) && <Loader2 size={13} className="animate-spin" />}
                </button>
                <button className="workspace-history-delete" aria-label={`Delete ${row.title}`} onClick={e => handleDeleteTab(row.id, e)}><Trash2 size={13} /></button>
              </div>
            ))
          ) : tabs.map(t => (
            <div key={t.id} className={`workspace-history-row ${activeTabId === t.id ? "is-active" : ""}`}>
              <button className="workspace-history-select" aria-current={activeTabId === t.id ? "true" : undefined} onClick={() => handleTabSelect(t.id)}><MessageSquare size={15} /><span>{t.title === "New Chat" ? "New conversation" : t.title}</span>{isTabStreaming(t.id) && <Loader2 size={13} className="animate-spin" />}</button>
              <button className="workspace-history-delete" aria-label={`Delete ${t.title}`} onClick={e => handleDeleteTab(t.id, e)}><Trash2 size={13} /></button>
            </div>
          ))}
        </div>
        <div className="workspace-history-note"><MessageSquare size={15} /><span>A place for every idea.<br />Pick up right where you left off.</span></div>
      </aside>}

      {/* ── Main chat area ── */}
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
        {/* Slim top bar: just split-pane controls */}
        <header className="workspace-header">
          <div className="workspace-header-left">
            <button className="workspace-icon-button" aria-label={historyOpen ? "Hide conversations" : "Show conversations"} title="Conversation history" aria-expanded={historyOpen} onClick={() => setHistoryOpen(v => !v)}><PanelLeftOpen size={18} /></button>
            <span className="workspace-header-divider" />
            <div className="workspace-header-identity"><strong>{appName}</strong><span>Your workspace agent</span></div>
          </div>
          <div className="workspace-header-actions">
            <button className="workspace-header-new" aria-label="New conversation" title="New conversation" onClick={handleAddTab}><Plus size={15} /><span>New conversation</span></button>
          <div className="workspace-layout-controls flex items-center gap-1">
            <button
              onClick={() => { setSplitMode(false); setRightTabId(null); setPdfPanel(null); }}
              className={`p-1.5 rounded-md transition-colors ${!splitMode ? "bg-slate-100 text-slate-900" : "text-slate-400 hover:text-slate-700 hover:bg-slate-50"}`}
              title="Single pane"
              aria-label="Single pane"
            >
              <Square className="w-3.5 h-3.5" />
            </button>
              <button
                onClick={toggleSplit}
                className={`p-1.5 rounded-md transition-colors ${splitMode ? "bg-slate-100 text-slate-900" : "text-slate-400 hover:text-slate-700 hover:bg-slate-50"}`}
                title="Split panes"
                aria-label="Split panes"
              >
              <Columns2 className="w-3.5 h-3.5" />
            </button>
          </div>
          </div>
        </header>

        {/* Panes */}
        <div className="flex flex-1 overflow-hidden">
          <div className={`${showRightPane ? "w-1/2 border-r border-slate-200" : "flex-1"} flex flex-col overflow-hidden bg-slate-50`}>
            <ChatPane
              tab={activeTab}
              onUploadPdf={handleUploadPdf}
              availableModels={availableModels}
              defaultModel={defaultModel}
              initialDraft={workspaceDraft}
              onDraftConsumed={consumeWorkspaceDraft}
            />
          </div>

          {showRightPane && (
            <div className="w-1/2 flex flex-col overflow-hidden bg-slate-50">
              {pdfPanel ? (
                <PdfViewer panel={pdfPanel} onClose={closePdf} />
              ) : rightTab ? (
                <>
                  <div className="flex items-center gap-1 px-3 py-2 border-b border-slate-100 bg-surface overflow-x-auto shrink-0">
                    {tabs.map(t => (
                      <button
                        key={t.id}
                        onClick={() => { setRightTabId(t.id); if (!t.messagesLoaded) void loadTabMessages(t.id); }}
                        className={`flex items-center gap-1.5 px-2.5 py-1 rounded text-xs font-medium whitespace-nowrap transition-colors ${
                          rightTabId === t.id
                            ? "bg-blue-100 text-blue-700 border border-blue-200"
                            : "text-slate-500 hover:bg-slate-50 hover:text-slate-700"
                        }`}
                      >
                        <FileText className="w-3 h-3" />
                        {t.title}
                      </button>
                    ))}
                  </div>
                  <ChatPane
                    tab={rightTab}
                    onUploadPdf={handleUploadPdf}
                    availableModels={availableModels}
                    defaultModel={defaultModel}
                  />
                </>
              ) : null}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
