import { type FormEvent, useState, useEffect, useRef } from "react";
import { useParams, Link, useNavigate } from "react-router-dom";
import { ArrowLeft, ShieldCheck, Star, Terminal, Copy, Check, Lock, AlertCircle, Clock, CheckCircle2, Pencil, Trash2, KeyRound, Eye, EyeOff, X, UserCheck, UserPlus, Plus, Loader2, Sparkles } from "lucide-react";
import { useSkillList, useSkillAccess as useSkillAccessContext } from "@/lib/SkillAccessContext";
import { apiFetch } from "@/lib/api";
import {
  DepartmentPicker,
  IntegrationTags,
  RISK_LEVEL_OPTIONS,
  CROSS_DEPT_POLICY_OPTIONS,
  DEFAULT_GRANT_OPTIONS,
  type RiskLevel,
  type CrossDeptPolicy,
  type CrossDeptDefaultGrant,
} from "@/components/SkillFormFields";
import { AdminGrantModal } from "@/components/AdminGrantModal";
import { SystemCriticalityBadge } from "@/components/SystemCriticalityBadge";
import { SystemLinksConfirmModal } from "@/components/SystemLinksConfirmModal";
import { SkillAiEditPanel, type AiEditPreview } from "@/components/SkillAiEditPanel";
import { SkillMarkdownDiff } from "@/components/SkillMarkdownDiff";
import { SkillVersionHistoryTab } from "@/components/SkillVersionHistoryTab";
import { SkillEvalsTab } from "../features/skillEvals/SkillEvalsTab";
import { SkillArtifactCreateModal } from "@/components/SkillArtifactCreateModal";
import { SkillArtifactViewModal } from "@/components/SkillArtifactViewModal";
import { SkillArtifactSidebarList } from "@/components/SkillArtifactSidebarList";
import type { ArtifactSummary } from "@/lib/skillArtifactsApi";

interface SkillAccess {
  slug: string;
  accessible: boolean;
  tier1: boolean;
  tier2: boolean;
  can_request: boolean;
  pending_request_id: string | null;
  expires_at: string | null;
  risk_level: string;
  cross_dept_policy: string;
  departments: string[];
  integrations: string[];
  owning_department: string;
  system_links: {
    integration_tag: string;
    system_name: string | null;
    risk_level: string | null;
    match_confidence: "exact" | "fuzzy" | "content" | "unmatched";
    source: "integration_tag" | "skill_content";
    direction: "read" | "write" | "read_write" | null;
  }[];
  max_system_risk_level: string | null;
}

function useSkillAccess(slug: string | undefined) {
  const [access, setAccess] = useState<SkillAccess | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!slug) { setLoading(false); return; }
    let cancelled = false;
    apiFetch("/api/skills")
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (cancelled) return;
        const found = d?.skills?.find((s: SkillAccess) => s.slug === slug) ?? null;
        setAccess(found);
        setLoading(false);
      })
      .catch(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [slug]);

  return { access, loading };
}

function stripFrontmatter(raw: string): string {
  if (!raw.startsWith("---")) return raw;
  const end = raw.indexOf("\n---", 3);
  if (end === -1) return raw;
  return raw.slice(end + 4).trimStart();
}

function renderMarkdown(rawText: string): string {
  // Escape raw HTML before any transform below. Published skill content is
  // authored by ACL-approved employees, but this is the same class of bug as
  // the confirmed workspace-chat stored XSS — a literal `<img onerror=...>`
  // must never reach the DOM unescaped via dangerouslySetInnerHTML. Only
  // `<`/`&` need escaping; none of the regexes below key off those characters
  // (fences use ```, blockquote uses a literal `>` we deliberately don't
  // escape, since `>` alone can't open a tag).
  const text = rawText.replace(/&/g, "&amp;").replace(/</g, "&lt;");
  let html = text
    // Fenced code blocks — light brand-themed, not dark
    .replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
      const langBadge = lang
        ? `<span class="inline-block text-[10px] font-mono font-bold text-[#FF444F] uppercase tracking-wider mb-2 select-none">${lang}</span><br/>`
        : "";
      return `<pre class="bg-[#F8F8F9] border border-slate-200 rounded-lg p-4 overflow-x-auto text-sm font-mono my-4 text-slate-800 leading-relaxed">${langBadge}<code>${code.trimEnd()}</code></pre>`;
    })
    // Inline code
    .replace(/`([^`]+)`/g, '<code class="bg-[#FFF0F0] text-[#FF444F] px-1.5 py-0.5 rounded text-[0.85em] font-mono border border-[#FF444F]/15">$1</code>')
    // Tables — robust: handles |---| and |:---:| separators
    .replace(/^([ \t]*\|[^\n]+\|[ \t]*\n)([ \t]*\|[ \t]*[-:| \t]+[ \t]*\|[ \t]*\n)((?:[ \t]*\|[^\n]+\|[ \t]*\n?)+)/gm, (_, header, _sep, body) => {
      const parseRow = (row: string) =>
        row.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const headers = parseRow(header);
      const rows = body.trim().split("\n").filter(r => r.trim()).map(parseRow);
      const ths = headers.map(h =>
        `<th class="px-4 py-2.5 text-left text-[11px] font-bold text-[#FF444F] uppercase tracking-wider border-b-2 border-[#FF444F]/20 bg-[#FFF0F0]/60 whitespace-nowrap">${h}</th>`
      ).join("");
      const trs = rows.map((cols, ri) =>
        `<tr class="${ri % 2 === 0 ? "bg-white" : "bg-slate-50/60"} hover:bg-[#FFF8F8] transition-colors border-b border-slate-100">${
          cols.map(c => `<td class="px-4 py-2.5 text-sm text-slate-700 align-top">${c}</td>`).join("")
        }</tr>`
      ).join("");
      return `<div class="overflow-x-auto my-4 rounded-xl border border-slate-200 shadow-sm"><table class="w-full border-collapse"><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table></div>`;
    })
    // Blockquote
    .replace(/^> (.+)$/gm, '<blockquote class="border-l-4 border-[#FF444F] bg-[#FFF0F0]/50 pl-4 py-2 pr-4 my-3 text-sm text-slate-700 rounded-r-lg italic">$1</blockquote>')
    // Headings
    .replace(/^#### (.+)$/gm, '<h4 class="text-sm font-bold text-slate-700 mt-4 mb-1.5 uppercase tracking-wide">$1</h4>')
    .replace(/^### (.+)$/gm, '<h3 class="text-base font-bold text-[#0E0E0E] mt-6 mb-2">$1</h3>')
    .replace(/^## (.+)$/gm, '<h2 class="text-lg font-bold text-[#0E0E0E] mt-8 mb-3 pb-2 border-b-2 border-[#FF444F]/20">$1</h2>')
    .replace(/^# (.+)$/gm, '<h1 class="text-2xl font-bold text-[#0E0E0E] mt-2 mb-4">$1</h1>')
    // Bold / italic
    .replace(/\*\*(.+?)\*\*/g, '<strong class="font-semibold text-[#0E0E0E]">$1</strong>')
    .replace(/\*(.+?)\*/g, '<em class="italic text-slate-600">$1</em>')
    // Horizontal rule
    .replace(/^---$/gm, '<hr class="border-[#FF444F]/20 my-6" />')
    // Unordered list items
    .replace(/^[-*] (.+)$/gm, '<li class="ml-4 text-slate-600 text-sm flex items-start gap-2"><span class="mt-1.5 w-1.5 h-1.5 rounded-full bg-[#FF444F]/60 shrink-0 inline-block"></span><span>$1</span></li>')
    // Wrap consecutive <li> in <ul>
    .replace(/(<li[^>]*>[\s\S]*?<\/li>\n?)+/g, match => `<ul class="my-3 space-y-1.5">${match}</ul>`)
    // Paragraphs — blank-line separated blocks not already wrapped in a tag
    .replace(/^(?!<[a-z])(.*\S.*)$/gm, line => `<p class="text-sm text-slate-600 leading-relaxed my-1.5">${line}</p>`);

  return html;
}

/** Convert a slug like "iam-backoffice" into "IAM Backoffice". */
function humanizeSlug(slug: string): string {
  return slug
    .split("-")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

export default function SkillDetailPage() {
  const { slug } = useParams();
  const navigate = useNavigate();
  const skillList = useSkillList();
  const staticSkill = skillList.find(s => s.slug === slug);
  const { access, loading: accessLoading } = useSkillAccess(slug);
  const { isAdmin, isPlatformAdmin, bySlug, refresh: refreshAccess } = useSkillAccessContext();
  const accessInfo = bySlug.get(slug ?? "");
  const isOwner = !!(accessInfo?.is_owner || isAdmin);
  const canManage = !!(accessInfo?.can_manage || accessInfo?.is_owner || accessInfo?.is_collaborator || isAdmin);
  const canEditArtifacts = !!(
    accessInfo?.is_owner
    || accessInfo?.is_collaborator
    || isPlatformAdmin
  );
  const canDelete = !!(accessInfo?.is_owner || isAdmin);

  // For runtime-published skills not in the static bundle, fetch the skill.md.
  // A skill from useSkillList() with empty markdown is a runtime-published skill
  // that the context knows about but whose content must still be fetched from the API.
  const hasStaticMarkdown = !!(staticSkill?.markdown);
  const [apiMarkdown, setApiMarkdown] = useState<string | null>(null);
  const [apiMarkdownLoading, setApiMarkdownLoading] = useState(!hasStaticMarkdown);
  const [contentOverride, setContentOverride] = useState<string | null>(null);
  const [contentBaseHash, setContentBaseHash] = useState<string | null>(null);

  const refreshSkillContent = async () => {
    if (!slug) return;
    try {
      const res = await apiFetch(`/api/skills/${slug}/content`);
      if (res.ok) {
        const d = await res.json();
        setContentOverride(d?.content ?? null);
        setApiMarkdown(d?.content ?? null);
        if (typeof d?.content_hash === "string") setContentBaseHash(d.content_hash);
      }
    } catch {
      /* ignore */
    }
  };

  useEffect(() => {
    if (hasStaticMarkdown || !slug) { setApiMarkdownLoading(false); return; }
    let cancelled = false;
    apiFetch(`/api/skills/${slug}/content`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (!cancelled) {
          setApiMarkdown(d?.content ?? null);
          if (typeof d?.content_hash === "string") setContentBaseHash(d.content_hash);
          setApiMarkdownLoading(false);
        }
      })
      .catch(() => { if (!cancelled) setApiMarkdownLoading(false); });
    return () => { cancelled = true; };
  }, [slug, hasStaticMarkdown]);

  // Build a synthetic skill object from API data when not in MOCK_SKILLS
  const baseSkill = (hasStaticMarkdown ? staticSkill : null) ?? (access ? {
    id: access.slug,
    name: accessInfo?.name || humanizeSlug(access.slug),
    slug: access.slug,
    description: accessInfo?.description || access.slug,
    publisher: access.owning_department || "community",
    version: "1.0.0",
    scope: "internal" as const,
    tags: [],
    verified: false,
    markdown: apiMarkdown ?? "",
  } : null);
  const skill = baseSkill
    ? { ...baseSkill, name: accessInfo?.name || baseSkill.name }
    : null;

  const [copied, setCopied] = useState(false);
  const [activeTab, setActiveTab] = useState<"readme" | "raw" | "diff" | "edit" | "history" | "evals">("readme");
  const [aiPreview, setAiPreview] = useState<AiEditPreview | null>(null);
  const [historyRefreshKey, setHistoryRefreshKey] = useState(0);
  const [manualDraft, setManualDraft] = useState("");
  const [manualSaving, setManualSaving] = useState(false);
  const [manualError, setManualError] = useState<string | null>(null);
  const [showManualSaveDialog, setShowManualSaveDialog] = useState(false);
  const [manualReason, setManualReason] = useState("");
  const [manualReasonError, setManualReasonError] = useState<string | null>(null);

  useEffect(() => {
    if (!aiPreview && activeTab === "diff") {
      setActiveTab("readme");
    }
  }, [aiPreview, activeTab]);
  const [requesting, setRequesting] = useState(false);
  const [requestJustification, setRequestJustification] = useState("");
  const [requestError, setRequestError] = useState<string | null>(null);
  const [requestSuccess, setRequestSuccess] = useState(false);

  // Management state
  const [showEditModal, setShowEditModal] = useState(false);
  const [showCreateArtifactModal, setShowCreateArtifactModal] = useState(false);
  const [artifactReviewJobId, setArtifactReviewJobId] = useState<string | null>(null);
  const [viewArtifact, setViewArtifact] = useState<{ name: string; version: number } | null>(null);
  const [artifactListTick, setArtifactListTick] = useState(0);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [showGrantModal, setShowGrantModal] = useState(false);
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState(skill?.name ?? "");
  const [savingName, setSavingName] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);
  const [localName, setLocalName] = useState(skill?.name ?? "");

  useEffect(() => {
    if (!editingName && skill?.name) {
      setNameDraft(skill.name);
      setLocalName(skill.name);
    }
  }, [skill?.name, editingName]);

  // While waiting for API data for a runtime skill, show a loading state
  if (!skill && (accessLoading || apiMarkdownLoading)) {
    return (
      <div className="flex-1 flex items-center justify-center py-32 text-slate-400">
        <div className="w-6 h-6 border-2 border-slate-300 border-t-slate-600 rounded-full animate-spin" />
      </div>
    );
  }

  if (!skill) {
    return (
      <div className="min-h-screen pt-32 px-4 text-center">
        <h1 className="text-2xl font-bold">Skill Not Found</h1>
        <Link to="/skills" className="text-slate-900 font-medium hover:underline mt-4 inline-block">Return to registry</Link>
      </div>
    );
  }

  // While access check is loading, render a skeleton instead of the full page
  // so we don't flash the content then lock it.
  const isLocked = !accessLoading && access !== null && !access.accessible;

  const saveSkillName = async () => {
    const trimmed = nameDraft.trim();
    if (!trimmed || !slug) {
      setNameError("Name is required.");
      setNameDraft(localName);
      setEditingName(false);
      return;
    }
    if (trimmed === localName) {
      setEditingName(false);
      return;
    }
    setSavingName(true);
    setNameError(null);
    try {
      const res = await apiFetch(`/api/skills/${slug}/name`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: trimmed }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d?.detail || `Update failed (${res.status})`);
      }
      const data = await res.json();
      setLocalName(data.name ?? trimmed);
      setNameDraft(data.name ?? trimmed);
      setEditingName(false);
      refreshAccess();
    } catch (err) {
      setNameError(err instanceof Error ? err.message : "Could not update name.");
      setNameDraft(localName);
    } finally {
      setSavingName(false);
    }
  };

  const handleRequestAccess = async (e: FormEvent) => {
    e.preventDefault();
    if (requestJustification.trim().length < 20) {
      setRequestError("Please write at least 20 characters.");
      return;
    }
    setRequesting(true);
    setRequestError(null);
    try {
      const res = await apiFetch("/api/access/request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill_slug: slug, justification: requestJustification.trim() }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d?.detail || `Error ${res.status}`);
      }
      setRequestSuccess(true);
    } catch (err) {
      setRequestError(err instanceof Error ? err.message : "Could not submit request.");
    } finally {
      setRequesting(false);
    }
  };

  const installCmd = `skills install ${skill.slug}`;
  const effectiveMarkdown = contentOverride ?? skill.markdown;
  const markdownBody = stripFrontmatter(effectiveMarkdown);

  const copyToClipboard = () => {
    navigator.clipboard.writeText(installCmd);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const copyRaw = () => {
    navigator.clipboard.writeText(effectiveMarkdown);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const bumpHistory = () => setHistoryRefreshKey(k => k + 1);

  const openEditTab = () => {
    setManualDraft(effectiveMarkdown);
    setManualError(null);
    setActiveTab("edit");
    if (!contentBaseHash) void refreshSkillContent();
  };

  const openManualSaveDialog = () => {
    if (!slug || manualDraft === effectiveMarkdown) return;
    setManualReason("");
    setManualReasonError(null);
    setShowManualSaveDialog(true);
  };

  const handleManualSave = async () => {
    const reason = manualReason.trim();
    if (reason.length < 10) {
      setManualReasonError("Please provide at least 10 characters explaining why you made this change.");
      return;
    }
    if (!slug || manualDraft === effectiveMarkdown) return;
    if (!contentBaseHash) {
      setManualReasonError("Missing content hash. Reload the skill and try again.");
      return;
    }
    setManualSaving(true);
    setManualError(null);
    setManualReasonError(null);
    try {
      const res = await apiFetch(`/api/skills/${slug}/content`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: manualDraft, reason, base_hash: contentBaseHash }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = d?.detail;
        if (res.status === 409 && detail && typeof detail === "object" && detail.code === "skill_conflict") {
          await refreshSkillContent();
          throw new Error(detail.message || "Skill was updated by someone else. Reloaded — review and save again.");
        }
        throw new Error(typeof detail === "string" ? detail : detail?.message || `Save failed (${res.status})`);
      }
      if (typeof d?.content_hash === "string") setContentBaseHash(d.content_hash);
      setShowManualSaveDialog(false);
      setManualReason("");
      setAiPreview(null);
      bumpHistory();
      await refreshSkillContent();
      setActiveTab("readme");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not save.";
      setManualReasonError(message);
    } finally {
      setManualSaving(false);
    }
  };

  return (
    <div className="flex-1 bg-white">

      {/* Header */}
      <div className="border-b border-slate-200 pt-12 pb-12 bg-[#F2F3F4]">
        <div className="max-w-4xl mx-auto px-4 sm:px-6">
          <Link to="/skills" className="inline-flex items-center gap-2 text-sm font-medium text-slate-500 hover:text-slate-900 mb-8 transition-colors">
            <ArrowLeft className="w-4 h-4" /> Back to search
          </Link>

          <div className="flex flex-col md:flex-row md:items-start justify-between gap-8">
            <div className="flex-1">
              <div className="flex items-center gap-3 mb-3">
                <span className="font-mono text-xs text-slate-500 flex items-center gap-1 font-medium">
                  @{skill.publisher}
                  {skill.verified && <ShieldCheck className="w-3.5 h-3.5 text-blue-500" />}
                </span>
                <span className="px-2 py-0.5 rounded bg-slate-200/50 text-[10px] font-mono font-bold text-slate-600 border border-slate-200 uppercase tracking-wider">
                  v{skill.version}
                </span>
              </div>
              <h1 className="text-3xl md:text-4xl font-bold tracking-tight text-slate-900 mb-4">
                {canManage && editingName ? (
                  <span className="flex items-center gap-2 flex-wrap">
                    <input
                      type="text"
                      value={nameDraft}
                      onChange={e => setNameDraft(e.target.value)}
                      onKeyDown={e => {
                        if (e.key === "Enter") void saveSkillName();
                        if (e.key === "Escape") {
                          setNameDraft(localName);
                          setEditingName(false);
                        }
                      }}
                      onBlur={() => void saveSkillName()}
                      maxLength={120}
                      autoFocus
                      disabled={savingName}
                      className="flex-1 min-w-0 text-3xl md:text-4xl font-bold text-slate-900 px-2 py-1 border border-slate-300 rounded-lg focus:outline-none focus:border-slate-500"
                    />
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-2 min-w-0">
                    <span className="truncate">{localName}</span>
                    {canManage && (
                      <button
                        type="button"
                        onClick={() => {
                          setNameDraft(localName);
                          setEditingName(true);
                        }}
                        className="shrink-0 p-1 text-slate-400 hover:text-slate-700 rounded"
                        title="Edit name"
                      >
                        <Pencil className="w-4 h-4" />
                      </button>
                    )}
                  </span>
                )}
              </h1>
              {nameError && (
                <p className="text-sm text-red-600 mb-2">{nameError}</p>
              )}
              <p className="text-lg text-slate-600 leading-relaxed max-w-2xl">{skill.description}</p>
              {canManage && (
                <div className="flex items-center gap-2 mt-4 flex-wrap">
                  <button
                    onClick={() => setShowEditModal(true)}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 transition-colors"
                  >
                    <Pencil className="w-3.5 h-3.5" /> Edit Skill
                  </button>
                  {canEditArtifacts && (
                    <button
                      type="button"
                      onClick={() => setShowCreateArtifactModal(true)}
                      className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 transition-colors"
                    >
                      <Sparkles className="w-3.5 h-3.5" /> Create artifact
                    </button>
                  )}
                  {canDelete && (
                  <button
                    onClick={() => setShowDeleteModal(true)}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg border border-red-200 bg-white text-red-600 hover:bg-red-50 transition-colors"
                  >
                    <Trash2 className="w-3.5 h-3.5" /> Delete
                  </button>
                  )}
                  {isAdmin && (
                    <button
                      onClick={() => setShowGrantModal(true)}
                      className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg border border-[#FF444F]/30 bg-[#FF444F]/5 text-[#FF444F] hover:bg-[#FF444F]/10 transition-colors"
                    >
                      <UserCheck className="w-3.5 h-3.5" /> Grant Access
                    </button>
                  )}
                </div>
              )}
            </div>

            {/* (install box removed) */}
          </div>
        </div>
      </div>

      {/* Content */}
      <div className={`mx-auto px-4 sm:px-6 py-12 ${activeTab === "history" ? "max-w-[96rem]" : "max-w-4xl"}`}>
        <div className={activeTab === "history" ? "space-y-8" : "grid md:grid-cols-3 gap-12"}>

          <div className={activeTab === "history" ? "space-y-8" : "md:col-span-2 space-y-8"}>

            {/* Skill file tab bar — locked if no access */}
            <section>
              {accessLoading ? (
                <div className="h-48 bg-slate-50 rounded-xl border border-slate-200 animate-pulse" />
              ) : isLocked ? (
                <LockedSkillContent
                  access={access}
                  slug={slug!}
                  justification={requestJustification}
                  onJustificationChange={setRequestJustification}
                  onSubmit={handleRequestAccess}
                  submitting={requesting}
                  error={requestError}
                  success={requestSuccess}
                />
              ) : (
                <>
                  <div className="flex items-center justify-between mb-4 border-b border-slate-100 pb-0">
                    <div className="flex items-center gap-1">
                      {([
                        { id: "readme" as const, label: "Skill" },
                        ...(aiPreview ? [{ id: "diff" as const, label: "Diff" }] : []),
                        { id: "raw" as const, label: "Raw" },
                        ...(canManage ? [{ id: "edit" as const, label: "Edit" }] : []),
                        ...(canManage ? [{ id: "history" as const, label: "Audit" }] : []),
                        ...(canManage ? [{ id: "evals" as const, label: "Evals" }] : []),
                      ]).map(tab => (
                        <button
                          key={tab.id}
                          onClick={() => {
                            if (tab.id === "edit") openEditTab();
                            else setActiveTab(tab.id);
                          }}
                          className={`px-4 py-2 text-sm font-semibold rounded-t-md transition-colors border-b-2 -mb-px ${
                            activeTab === tab.id
                              ? "text-[#FF444F] border-[#FF444F]"
                              : "text-slate-500 border-transparent hover:text-slate-700"
                          }`}
                        >
                          {tab.label}
                          {tab.id === "diff" && aiPreview && !aiPreview.canApply && (
                            <span className="ml-1.5 text-[10px] font-bold text-amber-600">!</span>
                          )}
                        </button>
                      ))}
                    </div>
                    {activeTab === "raw" && (
                      <button
                        onClick={copyRaw}
                        className="flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-900 transition-colors mb-1"
                      >
                        {copied ? <Check className="w-3.5 h-3.5 text-emerald-600" /> : <Copy className="w-3.5 h-3.5" />}
                        Copy
                      </button>
                    )}
                    {activeTab === "edit" && canManage && (
                      <div className="flex items-center gap-2 mb-1">
                        <button
                          type="button"
                          onClick={() => { setManualError(null); setActiveTab("raw"); }}
                          disabled={manualSaving}
                          className="px-2.5 py-1 text-xs font-semibold text-slate-600 hover:text-slate-900 disabled:opacity-50"
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          onClick={openManualSaveDialog}
                          disabled={manualSaving || manualDraft === effectiveMarkdown}
                          className="inline-flex items-center gap-1.5 px-3 py-1 text-xs font-semibold rounded-md bg-[#FF444F] text-white hover:bg-[#e63d47] disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          <Check className="w-3.5 h-3.5" />
                          Save
                        </button>
                      </div>
                    )}
                  </div>

                  {activeTab === "readme" ? (
                    <div
                      className="min-w-0"
                      dangerouslySetInnerHTML={{ __html: renderMarkdown(markdownBody) }}
                    />
                  ) : activeTab === "diff" && aiPreview ? (
                    <SkillMarkdownDiff
                      current={effectiveMarkdown}
                      proposed={aiPreview.proposedContent}
                      summary={aiPreview.summary}
                      verdict={aiPreview.verdict}
                    />
                  ) : activeTab === "edit" && canManage ? (
                    <div className="space-y-2">
                      {manualError && (
                        <p className="text-xs text-red-700 bg-red-50 border border-red-100 rounded-lg px-3 py-2">{manualError}</p>
                      )}
                      <textarea
                        value={manualDraft}
                        onChange={e => setManualDraft(e.target.value)}
                        className="w-full min-h-[32rem] bg-[#F8F8F9] border border-slate-200 text-slate-800 rounded-xl p-4 text-xs font-mono leading-relaxed focus:outline-none focus:ring-2 focus:ring-[#FF444F]/30 focus:border-[#FF444F]/50 resize-y"
                        spellCheck={false}
                      />
                    </div>
                  ) : activeTab === "evals" && canManage && slug ? (
                    <SkillEvalsTab slug={slug} />
                  ) : activeTab === "history" && canManage && slug ? (
                    <SkillVersionHistoryTab
                      key={historyRefreshKey}
                      slug={slug}
                      baseHash={contentBaseHash}
                      onReverted={() => {
                        bumpHistory();
                        setAiPreview(null);
                        void refreshSkillContent();
                        setActiveTab("readme");
                      }}
                    />
                  ) : (
                    <pre className="bg-[#F8F8F9] border border-slate-200 text-slate-800 rounded-xl p-6 overflow-x-auto text-xs font-mono leading-relaxed whitespace-pre-wrap break-words">
                      {activeTab === "diff" && aiPreview ? aiPreview.proposedContent : effectiveMarkdown}
                    </pre>
                  )}
                </>
              )}
            </section>
          </div>

          {activeTab !== "history" && (
          <div className="space-y-8">
            {canEditArtifacts && slug && (
              <section>
                <h3 className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-3">
                  Artifacts
                </h3>
                <SkillArtifactSidebarList
                  key={artifactListTick}
                  slug={slug}
                  onSelect={(a: ArtifactSummary) =>
                    setViewArtifact({ name: a.name, version: a.version })
                  }
                />
              </section>
            )}

            <section>
              <h3 className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-3">Tags</h3>
              <div className="flex flex-wrap gap-2">
                {skill.tags.map(tag => (
                  <span key={tag} className="text-xs px-2.5 py-1 rounded bg-slate-100 text-slate-600 font-medium">
                    {tag}
                  </span>
                ))}
              </div>
            </section>

            {accessInfo?.system_links && accessInfo.system_links.length > 0 && (
              <section>
                <h3 className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-3">
                  Connected Systems
                </h3>
                <div className="space-y-2">
                  {accessInfo.system_links.map(link => (
                    <div
                      key={link.integration_tag}
                      className="border border-slate-200 rounded-lg px-3 py-2 text-xs"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-semibold text-slate-900 truncate flex items-center gap-1.5">
                          {link.system_name ?? link.integration_tag}
                          {link.direction && (
                            <span className="text-[9px] font-bold px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 uppercase tracking-wider">
                              {link.direction === "read_write" ? "Read & Write" : link.direction}
                            </span>
                          )}
                        </span>
                        <SystemCriticalityBadge riskLevel={link.risk_level} />
                      </div>
                      {link.match_confidence === "unmatched" ? (
                        <div className="text-slate-400 mt-1">
                          Not found in the system catalogue — declared as "{link.integration_tag}"
                        </div>
                      ) : (
                        <div className="text-slate-500 mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
                          {link.match_confidence === "fuzzy" && (
                            <span className="text-amber-600">Best-effort match</span>
                          )}
                          <span className="text-slate-400">
                            {link.source === "skill_content" ? "Detected in SKILL.md" : "Declared integration"}
                          </span>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </section>
            )}

            {canManage && accessInfo?.has_secrets && (
              <SecretsPanel slug={slug!} />
            )}

            {canManage && slug && (
              <SkillCollaboratorsPanel slug={slug} isOwner={isOwner} onChanged={refreshAccess} />
            )}

            {canManage && slug && (
              <SkillAiEditPanel
                slug={slug}
                onApplied={() => {
                  setAiPreview(null);
                  bumpHistory();
                  setActiveTab("readme");
                  void refreshSkillContent();
                }}
                onPreviewChange={preview => {
                  setAiPreview(preview);
                  if (preview) setActiveTab("diff");
                }}
              />
            )}
          </div>
          )}

        </div>
      </div>

      {showEditModal && slug && (
        <EditSkillModal
          slug={slug}
          onClose={() => setShowEditModal(false)}
          onSubmitted={() => { setShowEditModal(false); refreshAccess(); }}
        />
      )}

      {showDeleteModal && slug && (
        <DeleteSkillModal
          slug={slug}
          onClose={() => setShowDeleteModal(false)}
          onDeleted={() => { navigate("/skills"); refreshAccess(); }}
        />
      )}

      {showGrantModal && slug && (
        <AdminGrantModal
          skillSlug={slug}
          skillName={localName}
          onClose={() => setShowGrantModal(false)}
          onGranted={() => { setShowGrantModal(false); refreshAccess(); }}
        />
      )}

      {showCreateArtifactModal && slug && (
        <SkillArtifactCreateModal
          slug={slug}
          initialReviewJobId={artifactReviewJobId}
          onClose={() => {
            setShowCreateArtifactModal(false);
            setArtifactReviewJobId(null);
          }}
          onCreated={() => setArtifactListTick(t => t + 1)}
        />
      )}

      {viewArtifact && slug && (
        <SkillArtifactViewModal
          slug={slug}
          name={viewArtifact.name}
          version={viewArtifact.version}
          canManage={canEditArtifacts}
          onClose={() => setViewArtifact(null)}
          onChanged={() => setArtifactListTick(t => t + 1)}
          onStartAiEdit={jobId => {
            setViewArtifact(null);
            setArtifactReviewJobId(jobId);
            setShowCreateArtifactModal(true);
          }}
        />
      )}

      {showManualSaveDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md border border-slate-200">
            <div className="px-5 pt-5 pb-3 border-b border-slate-100 flex items-start justify-between">
              <div>
                <h2 className="text-base font-bold text-slate-900">Save changes</h2>
                <p className="text-xs text-slate-500 mt-0.5">Briefly explain why you edited this skill.</p>
              </div>
              <button
                type="button"
                onClick={() => { if (!manualSaving) { setShowManualSaveDialog(false); setManualReasonError(null); } }}
                disabled={manualSaving}
                className="text-slate-400 hover:text-slate-700 text-lg leading-none disabled:opacity-50"
              >
                ✕
              </button>
            </div>
            <div className="px-5 py-4">
              <label className="block text-xs font-semibold text-slate-600 mb-1.5">Reason for edit</label>
              <textarea
                value={manualReason}
                onChange={e => { setManualReason(e.target.value); setManualReasonError(null); }}
                placeholder="e.g. Fixed outdated API endpoint and added timeout troubleshooting steps"
                rows={4}
                maxLength={500}
                disabled={manualSaving}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-[#FF444F]/30 focus:border-[#FF444F]/50 disabled:opacity-60 resize-none"
                autoFocus
              />
              <div className="flex items-center justify-between mt-1.5">
                <p className="text-[10px] text-slate-400">Minimum 10 characters</p>
                <p className="text-[10px] text-slate-400">{manualReason.trim().length}/500</p>
              </div>
              {manualReasonError && (
                <p className="mt-2 text-xs text-red-600">{manualReasonError}</p>
              )}
            </div>
            <div className="px-5 pb-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => { setShowManualSaveDialog(false); setManualReasonError(null); }}
                disabled={manualSaving}
                className="px-4 py-2 text-sm font-semibold text-slate-600 hover:text-slate-900 disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void handleManualSave()}
                disabled={manualSaving || manualReason.trim().length < 10}
                className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-semibold rounded-lg bg-[#FF444F] text-white hover:bg-[#e63d47] disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {manualSaving && <Loader2 className="w-4 h-4 animate-spin" />}
                Confirm save
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}

// ── Edit skill modal ──────────────────────────────────────────────────────────

type EditReviewStatus = "pending" | "reviewing" | "awaiting_confirmation" | "pass" | "fail" | "modify";

function EditSkillModal({ slug, onClose, onSubmitted }: { slug: string; onClose: () => void; onSubmitted: () => void }) {
  const [skillFile, setSkillFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submissionId, setSubmissionId] = useState<string | null>(null);
  const [reviewStatus, setReviewStatus] = useState<EditReviewStatus | null>(null);
  const [reviewReason, setReviewReason] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // Editable metadata fields — pre-populated from current ACL
  const [aclLoaded, setAclLoaded] = useState(false);
  const [departments, setDepartments] = useState<string[]>([]);
  const [integrations, setIntegrations] = useState<string[]>([]);
  const [riskLevel, setRiskLevel] = useState<RiskLevel>("standard");
  const [crossDeptPolicy, setCrossDeptPolicy] = useState<CrossDeptPolicy>("requestable");
  const [crossDeptGrant, setCrossDeptGrant] = useState<CrossDeptDefaultGrant>("time_limited_30d");
  const [crossDeptApprovers, setCrossDeptApprovers] = useState<string[]>([]);
  const [description, setDescription] = useState("");
  const [skillName, setSkillName] = useState("");

  // Secrets — existing keys shown read-only; new entries submitted on save
  const [existingSecretKeys, setExistingSecretKeys] = useState<string[]>([]);
  const [newSecrets, setNewSecrets] = useState<{ key: string; value: string }[]>([]);
  const [secretKeyInput, setSecretKeyInput] = useState("");
  const [secretValInput, setSecretValInput] = useState("");
  const [showSecretVal, setShowSecretVal] = useState(false);

  useEffect(() => {
    Promise.all([
      apiFetch("/api/skills").then(r => r.ok ? r.json() : null),
      apiFetch(`/api/skills/${slug}/secrets`).then(r => r.ok ? r.json() : []),
    ]).then(([skillsData, secretsData]) => {
      const entry = skillsData?.skills?.find((s: any) => s.slug === slug);
      if (entry) {
        setDepartments(entry.departments ?? []);
        setIntegrations(entry.integrations ?? []);
        setRiskLevel(entry.risk_level ?? "standard");
        setCrossDeptPolicy(entry.cross_dept_policy ?? "requestable");
        setCrossDeptGrant(entry.cross_dept_default_grant ?? "time_limited_30d");
        setCrossDeptApprovers(entry.cross_dept_approvers ?? []);
        setDescription(entry.description ?? "");
        setSkillName(entry.name ?? "");
      }
      const keys = Array.isArray(secretsData) ? secretsData.map((s: any) => s.env_key) : [];
      setExistingSecretKeys(keys);
      setAclLoaded(true);
    }).catch(() => setAclLoaded(true));
  }, [slug]);

  // Poll the submission's review status after PUT ack, until it either needs
  // human system-access confirmation or reaches a terminal AI-review verdict.
  useEffect(() => {
    if (!submissionId) return;
    const TERMINAL: EditReviewStatus[] = ["awaiting_confirmation", "pass", "fail", "modify"];
    if (reviewStatus && TERMINAL.includes(reviewStatus)) return;

    const timer = setInterval(async () => {
      try {
        const res = await apiFetch(`/api/skills/submissions/${submissionId}`);
        if (!res.ok) return;
        const data = await res.json();
        setReviewStatus(data.review_status as EditReviewStatus);
        setReviewReason(data.review_reason ?? null);
      } catch {
        // silently retry next tick
      }
    }, 2500);
    return () => clearInterval(timer);
  }, [submissionId, reviewStatus]);

  const addNewSecret = () => {
    const key = secretKeyInput.trim().toUpperCase().replace(/[^A-Z0-9_]/g, "_");
    const value = secretValInput.trim();
    if (!key || !value) return;
    if (newSecrets.some(s => s.key === key) || existingSecretKeys.includes(key)) {
      setError(`Secret key "${key}" already exists.`);
      return;
    }
    setNewSecrets(prev => [...prev, { key, value }]);
    setSecretKeyInput("");
    setSecretValInput("");
    setShowSecretVal(false);
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (departments.length === 0) { setError("Select at least one department."); return; }
    if (!skillName.trim()) { setError("Skill name is required."); return; }
    if (!description.trim()) { setError("Description is required."); return; }
    setSubmitting(true);
    setError(null);
    const form = new FormData();
    form.append("departments", JSON.stringify(departments));
    form.append("integrations", JSON.stringify(integrations));
    form.append("risk_level", riskLevel);
    form.append("cross_dept_policy", crossDeptPolicy);
    form.append("cross_dept_default_grant", crossDeptGrant);
    form.append("cross_dept_approvers", JSON.stringify(crossDeptApprovers.filter(d => d !== "*")));
    form.append("name", skillName.trim());
    form.append("description", description.trim());
    // Only send secrets if new ones were added — existing secrets are always preserved
    form.append("secrets", newSecrets.length > 0 ? JSON.stringify(newSecrets) : "[]");
    if (skillFile) {
      form.append("skill_file", skillFile, skillFile.name);
    }
    try {
      const res = await apiFetch(`/api/skills/${slug}`, { method: "PUT", body: form });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d?.detail || `Edit failed (${res.status})`);
      }
      const data = await res.json();
      setSubmissionId(data.id);
      setReviewStatus(data.review_status as EditReviewStatus);
      setReviewReason(data.review_reason ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Edit failed.");
    } finally {
      setSubmitting(false);
    }
  };

  if (submissionId && reviewStatus === "awaiting_confirmation") {
    return (
      <SystemLinksConfirmModal
        submissionId={submissionId}
        slug={slug}
        onClose={onClose}
        onDone={onSubmitted}
      />
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl border border-slate-200 flex flex-col max-h-[90vh]">

        {/* Header */}
        <div className="px-6 pt-5 pb-4 border-b border-slate-100 flex items-start justify-between shrink-0">
          <div>
            <h2 className="text-base font-bold text-slate-900">Edit Skill</h2>
            <p className="text-xs text-slate-500 mt-0.5">
              Changes go through AI security review before going live.
              The current version of <code className="font-mono">{slug}</code> stays active until the new one passes.
            </p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700 text-lg leading-none ml-4 shrink-0">✕</button>
        </div>

        {submissionId && (reviewStatus === "pending" || reviewStatus === "reviewing") ? (
          <div className="px-6 py-10 text-center">
            <Loader2 className="w-10 h-10 text-blue-500 mx-auto mb-3 animate-spin" />
            <p className="font-bold text-slate-900">Under AI security review…</p>
            <p className="text-sm text-slate-500 mt-1">
              We're scanning your changes for hardcoded secrets and unsafe instructions.
              The current version stays live until this finishes.
            </p>
          </div>
        ) : submissionId && reviewStatus === "pass" ? (
          <div className="px-6 py-10 text-center">
            <CheckCircle2 className="w-10 h-10 text-emerald-500 mx-auto mb-3" />
            <p className="font-bold text-slate-900">Published</p>
            <p className="text-sm text-slate-500 mt-1">Your changes passed review and are now live.</p>
            <button onClick={onSubmitted} className="mt-5 px-4 py-2 bg-slate-900 text-white text-sm font-bold rounded-lg hover:bg-slate-800">Done</button>
          </div>
        ) : submissionId && (reviewStatus === "fail" || reviewStatus === "modify") ? (
          <div className="px-6 py-10 text-center">
            <AlertCircle className="w-10 h-10 text-amber-500 mx-auto mb-3" />
            <p className="font-bold text-slate-900">
              {reviewStatus === "fail" ? "Changes rejected" : "Changes need modification"}
            </p>
            {reviewReason && (
              <p className="text-sm text-slate-500 mt-1 whitespace-pre-wrap">{reviewReason}</p>
            )}
            <button onClick={onClose} className="mt-5 px-4 py-2 bg-slate-900 text-white text-sm font-bold rounded-lg hover:bg-slate-800">Close</button>
          </div>
        ) : !aclLoaded ? (
          <div className="px-6 py-10 space-y-3">
            {[1,2,3].map(i => <div key={i} className="h-10 bg-slate-100 rounded-lg animate-pulse" />)}
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="overflow-y-auto flex-1">
            <div className="px-6 py-5 space-y-6">

              {/* Skill name */}
              <div>
                <label className="block text-sm font-semibold text-slate-700 mb-1">
                  Skill name <span className="text-red-500">*</span>
                </label>
                <input
                  type="text"
                  value={skillName}
                  onChange={e => setSkillName(e.target.value)}
                  maxLength={120}
                  placeholder="Treasury Analysis"
                  className="w-full bg-slate-50 border border-slate-200 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-slate-900 placeholder:text-slate-400"
                />
              </div>

              {/* Description */}
              <div>
                <label className="block text-sm font-semibold text-slate-700 mb-1">
                  Description <span className="text-red-500">*</span>
                </label>
                <textarea
                  value={description}
                  onChange={e => setDescription(e.target.value)}
                  rows={3}
                  maxLength={2000}
                  placeholder="What does this skill do? When should the AI use it?"
                  className="w-full bg-slate-50 border border-slate-200 rounded-md px-3 py-2 text-sm resize-none focus:outline-none focus:ring-1 focus:ring-slate-900 placeholder:text-slate-400"
                />
                <p className="text-xs text-slate-400 mt-0.5 text-right">{description.length}/2000</p>
              </div>

              {/* Department access */}
              <div>
                <label className="block text-sm font-semibold text-slate-700 mb-1">
                  Department Access <span className="text-red-500">*</span>
                </label>
                <p className="text-xs text-slate-500 mb-2">
                  Which departments have Tier 1 (automatic) access to this skill?
                </p>
                <DepartmentPicker selected={departments} onChange={setDepartments} />
              </div>

              {/* Integrations */}
              <div>
                <label className="block text-sm font-semibold text-slate-700 mb-1">Integrations</label>
                <p className="text-xs text-slate-500 mb-2">
                  External systems or APIs this skill connects to (e.g. "Data Warehouse (read)").
                </p>
                <IntegrationTags tags={integrations} onChange={setIntegrations} />
              </div>

              {/* Risk level */}
              <div>
                <label className="block text-sm font-semibold text-slate-700 mb-2">Risk Level</label>
                <div className="grid sm:grid-cols-3 gap-2">
                  {RISK_LEVEL_OPTIONS.map(opt => (
                    <label
                      key={opt.value}
                      className={`flex flex-col gap-0.5 border rounded-lg p-3 cursor-pointer transition-colors ${
                        riskLevel === opt.value
                          ? "border-slate-900 bg-slate-50"
                          : "border-slate-200 hover:border-slate-300"
                      }`}
                    >
                      <div className="flex items-center gap-2">
                        <input
                          type="radio"
                          name="edit_risk_level"
                          value={opt.value}
                          checked={riskLevel === opt.value}
                          onChange={() => setRiskLevel(opt.value)}
                          className="accent-slate-900"
                        />
                        <span className="text-sm font-semibold text-slate-800">{opt.label}</span>
                      </div>
                      <span className="text-xs text-slate-500 ml-5">{opt.hint}</span>
                    </label>
                  ))}
                </div>
              </div>

              {/* Cross-dept policy + grant */}
              <div className="grid sm:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-semibold text-slate-700 mb-1">Cross-dept Policy</label>
                  <select
                    value={crossDeptPolicy}
                    onChange={e => setCrossDeptPolicy(e.target.value as CrossDeptPolicy)}
                    className="w-full bg-slate-50 border border-slate-200 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-slate-900"
                  >
                    {CROSS_DEPT_POLICY_OPTIONS.map(opt => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label} — {opt.hint}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-semibold text-slate-700 mb-1">Default Grant Duration</label>
                  <select
                    value={crossDeptGrant}
                    onChange={e => setCrossDeptGrant(e.target.value as CrossDeptDefaultGrant)}
                    className="w-full bg-slate-50 border border-slate-200 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-slate-900"
                    disabled={crossDeptPolicy === "blocked"}
                  >
                    {DEFAULT_GRANT_OPTIONS.map(opt => (
                      <option key={opt.value} value={opt.value}>{opt.label}</option>
                    ))}
                  </select>
                </div>
              </div>

              {/* Cross-dept approvers */}
              {crossDeptPolicy !== "blocked" && (
                <div>
                  <label className="block text-sm font-semibold text-slate-700 mb-1">
                    Cross-dept Approvers <span className="font-normal text-slate-400">(optional)</span>
                  </label>
                  <p className="text-xs text-slate-500 mb-2">
                    Departments whose managers can approve cross-dept access requests for this skill.
                  </p>
                  <DepartmentPicker
                    selected={crossDeptApprovers}
                    onChange={depts => setCrossDeptApprovers(depts.filter(d => d !== "*"))}
                  />
                </div>
              )}

              {/* Secrets */}
              <div>
                <label className="block text-sm font-semibold text-slate-700 mb-1">
                  Secrets <span className="font-normal text-slate-400">(optional)</span>
                </label>
                <p className="text-xs text-slate-500 mb-3">
                  Existing secrets are always preserved. Add new keys here — rotate or delete existing ones from the skill detail sidebar.
                </p>

                {/* Existing keys (read-only) */}
                {existingSecretKeys.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 mb-3">
                    {existingSecretKeys.map(k => (
                      <span key={k} className="inline-flex items-center gap-1.5 bg-emerald-50 border border-emerald-200 text-emerald-800 text-xs font-mono px-2.5 py-1 rounded-full">
                        <KeyRound className="w-3 h-3" /> {k} <span className="text-emerald-500 font-sans font-normal">stored</span>
                      </span>
                    ))}
                  </div>
                )}

                {/* New secrets staged for this edit */}
                {newSecrets.length > 0 && (
                  <div className="flex flex-wrap gap-1.5 mb-3">
                    {newSecrets.map(s => (
                      <span key={s.key} className="inline-flex items-center gap-1.5 bg-amber-50 border border-amber-200 text-amber-800 text-xs font-mono px-2.5 py-1 rounded-full">
                        <KeyRound className="w-3 h-3" /> {s.key} <span className="text-amber-500 font-sans font-normal">new</span>
                        <button type="button" onClick={() => setNewSecrets(p => p.filter(x => x.key !== s.key))} className="ml-0.5 text-amber-400 hover:text-amber-700">
                          <X className="w-3 h-3" />
                        </button>
                      </span>
                    ))}
                  </div>
                )}

                {/* Add new secret row */}
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={secretKeyInput}
                    onChange={e => setSecretKeyInput(e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, "_"))}
                    placeholder="ENV_VAR_NAME"
                    className="w-36 bg-slate-50 border border-slate-200 rounded-md px-2.5 py-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-slate-900 placeholder:text-slate-400"
                  />
                  <div className="relative flex-1">
                    <input
                      type={showSecretVal ? "text" : "password"}
                      value={secretValInput}
                      onChange={e => setSecretValInput(e.target.value)}
                      placeholder="Secret value"
                      className="w-full bg-slate-50 border border-slate-200 rounded-md px-2.5 py-2 pr-8 text-xs focus:outline-none focus:ring-1 focus:ring-slate-900 placeholder:text-slate-400"
                    />
                    <button type="button" onClick={() => setShowSecretVal(v => !v)} className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600">
                      {showSecretVal ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
                    </button>
                  </div>
                  <button
                    type="button"
                    onClick={addNewSecret}
                    disabled={!secretKeyInput.trim() || !secretValInput.trim()}
                    className="px-3 py-2 bg-slate-900 text-white text-xs font-semibold rounded-md hover:bg-slate-700 disabled:opacity-40 whitespace-nowrap"
                  >
                    Add
                  </button>
                </div>
              </div>

              {/* Skill file (optional) */}
              <div className="border border-dashed border-slate-200 rounded-lg p-4 bg-slate-50">
                <label className="block text-sm font-semibold text-slate-700 mb-1">
                  New skill.md <span className="font-normal text-slate-400">(optional)</span>
                </label>
                <p className="text-xs text-slate-500 mb-2">
                  Leave empty to keep the current skill file and only update the settings above.
                </p>
                <input
                  ref={fileRef}
                  type="file"
                  accept=".md"
                  onChange={e => setSkillFile(e.target.files?.[0] ?? null)}
                  className="block w-full text-sm text-slate-700 file:mr-3 file:py-1.5 file:px-3 file:rounded-md file:border file:border-slate-200 file:bg-white file:text-xs file:font-semibold file:text-slate-700 hover:file:bg-slate-100"
                />
                {skillFile && (
                  <p className="text-xs text-slate-500 mt-1.5">
                    {skillFile.name} — {(skillFile.size / 1024).toFixed(1)} KB
                  </p>
                )}
              </div>

            </div>

            {/* Footer */}
            <div className="sticky bottom-0 bg-white border-t border-slate-100 px-6 py-4 flex gap-2">
              {error && (
                <div className="flex items-start gap-2 text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2 flex-1">
                  <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" /> {error}
                </div>
              )}
              {!error && <div className="flex-1" />}
              <button type="button" onClick={onClose} className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 font-medium whitespace-nowrap">Cancel</button>
              <button
                type="submit"
                disabled={submitting}
                className="px-4 py-2 text-sm font-bold bg-slate-900 text-white rounded-lg hover:bg-slate-700 disabled:opacity-50 flex items-center gap-1.5 whitespace-nowrap"
              >
                {submitting ? "Submitting…" : "Submit for Review"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}


// ── Delete skill modal ────────────────────────────────────────────────────────

function DeleteSkillModal({ slug, onClose, onDeleted }: { slug: string; onClose: () => void; onDeleted: () => void }) {
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState("");

  const handleDelete = async () => {
    setDeleting(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/skills/${slug}`, { method: "DELETE" });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d?.detail || `Delete failed (${res.status})`);
      }
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed.");
      setDeleting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md border border-slate-200">
        <div className="px-6 pt-5 pb-4 border-b border-slate-100 flex items-start justify-between">
          <h2 className="text-base font-bold text-red-700 flex items-center gap-2"><Trash2 className="w-4 h-4" /> Delete Skill</h2>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700 text-lg leading-none">✕</button>
        </div>
        <div className="px-6 py-4 space-y-4">
          <p className="text-sm text-slate-700">
            This will permanently remove <strong>{slug}</strong>, its ACL entry, and all stored secrets. This cannot be undone.
          </p>
          <div>
            <label className="block text-xs font-bold text-slate-700 mb-1.5">Type <code className="font-mono bg-slate-100 px-1 py-0.5 rounded">{slug}</code> to confirm</label>
            <input
              type="text"
              value={confirm}
              onChange={e => setConfirm(e.target.value)}
              className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:border-red-400 font-mono"
              placeholder={slug}
            />
          </div>
          {error && (
            <div className="flex items-start gap-2 text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
              <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" /> {error}
            </div>
          )}
          <div className="flex gap-2 pt-1">
            <button type="button" onClick={onClose} className="flex-1 px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 font-medium">Cancel</button>
            <button
              onClick={handleDelete}
              disabled={deleting || confirm !== slug}
              className="flex-1 px-4 py-2 text-sm font-bold bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-40 flex items-center justify-center gap-1.5"
            >
              {deleting ? "Deleting…" : "Delete Permanently"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}


// ── Secrets panel (sidebar) ───────────────────────────────────────────────────

interface SecretRow { env_key: string; created_at: string | null; }

function SecretsPanel({ slug }: { slug: string }) {
  const [secrets, setSecrets] = useState<SecretRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const [showValue, setShowValue] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadSecrets = () => {
    setLoading(true);
    apiFetch(`/api/skills/${slug}/secrets`)
      .then(r => r.ok ? r.json() : [])
      .then(data => { setSecrets(Array.isArray(data) ? data : []); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(() => { loadSecrets(); }, [slug]);

  const handleUpdate = async (env_key: string) => {
    if (!editValue.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/skills/${slug}/secrets/${env_key}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: editValue }),
      });
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d?.detail || "Update failed."); }
      setEditing(null);
      setEditValue("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Update failed.");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (env_key: string) => {
    setDeleting(env_key);
    setError(null);
    try {
      const res = await apiFetch(`/api/skills/${slug}/secrets/${env_key}`, { method: "DELETE" });
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d?.detail || "Delete failed."); }
      loadSecrets();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed.");
    } finally {
      setDeleting(null);
    }
  };

  if (loading) return (
    <section>
      <h3 className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-3 flex items-center gap-1.5"><KeyRound className="w-3 h-3" /> Secrets</h3>
      <div className="h-16 bg-slate-50 rounded-lg animate-pulse" />
    </section>
  );

  if (secrets.length === 0) return null;

  return (
    <section>
      <h3 className="text-[10px] font-bold text-slate-400 uppercase tracking-widest mb-3 flex items-center gap-1.5">
        <KeyRound className="w-3 h-3" /> Secrets
      </h3>
      <div className="space-y-2">
        {error && (
          <div className="text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg px-2.5 py-1.5 flex items-start gap-1.5">
            <AlertCircle className="w-3 h-3 mt-0.5 shrink-0" /> {error}
          </div>
        )}
        {secrets.map(s => (
          <div key={s.env_key} className="border border-slate-200 rounded-lg overflow-hidden">
            <div className="flex items-center justify-between px-3 py-2 bg-slate-50">
              <code className="text-xs font-mono text-slate-700">{s.env_key}</code>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => { setEditing(editing === s.env_key ? null : s.env_key); setEditValue(""); setShowValue(false); }}
                  className="p-1 rounded text-slate-400 hover:text-slate-700 hover:bg-slate-200 transition-colors"
                  title="Update value"
                >
                  <Pencil className="w-3 h-3" />
                </button>
                <button
                  onClick={() => handleDelete(s.env_key)}
                  disabled={deleting === s.env_key}
                  className="p-1 rounded text-slate-400 hover:text-red-600 hover:bg-red-50 transition-colors disabled:opacity-40"
                  title="Delete secret"
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              </div>
            </div>
            {editing === s.env_key && (
              <div className="px-3 py-2 border-t border-slate-200 bg-white space-y-2">
                <div className="relative">
                  <input
                    type={showValue ? "text" : "password"}
                    value={editValue}
                    onChange={e => setEditValue(e.target.value)}
                    placeholder="New value…"
                    className="w-full text-xs font-mono px-2.5 py-1.5 pr-8 border border-slate-200 rounded focus:outline-none focus:border-slate-400"
                  />
                  <button
                    type="button"
                    onClick={() => setShowValue(v => !v)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-700"
                  >
                    {showValue ? <EyeOff className="w-3 h-3" /> : <Eye className="w-3 h-3" />}
                  </button>
                </div>
                <div className="flex gap-1.5">
                  <button onClick={() => setEditing(null)} className="flex-1 py-1 text-xs font-medium text-slate-600 border border-slate-200 rounded hover:bg-slate-50">Cancel</button>
                  <button
                    onClick={() => handleUpdate(s.env_key)}
                    disabled={saving || !editValue.trim()}
                    className="flex-1 py-1 text-xs font-bold bg-slate-900 text-white rounded hover:bg-slate-700 disabled:opacity-40"
                  >
                    {saving ? "Saving…" : "Save"}
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}


const COLLAB_EMAIL_RE = /^[^\s@]{1,64}@[^\s@]{1,255}\.[^\s@]{1,63}$/;

interface SkillCollaborator {
  user_email: string;
  user_name?: string | null;
  added_by_email?: string;
  created_at?: string | null;
}

function SkillCollaboratorsPanel({
  slug,
  isOwner,
  onChanged,
}: {
  slug: string;
  isOwner: boolean;
  onChanged: () => void;
}) {
  const [collaborators, setCollaborators] = useState<SkillCollaborator[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [removingEmail, setRemovingEmail] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch(`/api/skills/${slug}/collaborators`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setCollaborators(data.collaborators ?? []);
    } catch {
      setError("Could not load collaborators.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, [slug]);

  const addCollaborator = async () => {
    const email = newEmail.trim().toLowerCase();
    if (!COLLAB_EMAIL_RE.test(email)) {
      setAddError("Enter a valid email address.");
      return;
    }
    setSubmitting(true);
    setAddError(null);
    try {
      const res = await apiFetch(`/api/skills/${slug}/collaborators`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { detail?: string }).detail || `HTTP ${res.status}`);
      }
      setNewEmail("");
      setAdding(false);
      await load();
      onChanged();
    } catch (e) {
      setAddError(e instanceof Error ? e.message : "Failed to add collaborator.");
    } finally {
      setSubmitting(false);
    }
  };

  const removeCollaborator = async (email: string) => {
    if (!window.confirm(`Remove ${email} as a collaborator?`)) return;
    setRemovingEmail(email);
    setError(null);
    try {
      const res = await apiFetch(
        `/api/skills/${slug}/collaborators/${encodeURIComponent(email)}`,
        { method: "DELETE" },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setCollaborators(prev => prev.filter(c => c.user_email !== email));
      onChanged();
    } catch {
      setError("Failed to remove collaborator.");
    } finally {
      setRemovingEmail(null);
    }
  };

  return (
    <section>
      <div className="flex items-center justify-between gap-2 mb-3">
        <h3 className="text-[10px] font-bold text-slate-400 uppercase tracking-widest">Collaborators</h3>
        {isOwner && !adding && (
          <button
            type="button"
            onClick={() => { setAdding(true); setAddError(null); setNewEmail(""); }}
            className="inline-flex items-center gap-1 text-[10px] font-bold text-slate-700 hover:text-slate-900"
          >
            <UserPlus className="w-3 h-3" /> Add
          </button>
        )}
      </div>
      <p className="text-xs text-slate-500 mb-3">
        Co-managers can edit this skill and manage secrets. Only the owner can delete the skill or add collaborators.
      </p>

      {adding && (
        <div className="border border-slate-200 rounded-xl bg-white p-3 mb-3 space-y-2">
          <div className="flex flex-col gap-2">
            <input
              autoFocus
              type="email"
              value={newEmail}
              onChange={e => setNewEmail(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); void addCollaborator(); } }}
              placeholder="colleague@example.com"
              maxLength={320}
              className="w-full text-xs px-2.5 py-2 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-200"
            />
            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => { setAdding(false); setAddError(null); }}
                className="px-3 py-2 text-xs text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void addCollaborator()}
                disabled={submitting || !newEmail.trim()}
                className="inline-flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-semibold bg-slate-900 text-white rounded-lg disabled:opacity-50 transition-colors"
              >
                {submitting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : "Add"}
              </button>
            </div>
          </div>
          {addError && (
            <p className="text-[11px] text-red-600 flex items-center gap-1">
              <AlertCircle className="w-3 h-3 shrink-0" /> {addError}
            </p>
          )}
        </div>
      )}

      {error && (
        <p className="text-xs text-red-600 mb-2 flex items-center gap-1">
          <AlertCircle className="w-3.5 h-3.5" /> {error}
        </p>
      )}

      {loading ? (
        <div className="flex justify-center py-6">
          <Loader2 className="w-4 h-4 animate-spin text-slate-400" />
        </div>
      ) : collaborators.length === 0 ? (
        <div className="text-xs text-slate-400 border border-dashed border-slate-200 rounded-xl px-4 py-6 text-center">
          {isOwner ? "No collaborators yet. Click Add to invite a co-manager." : "No collaborators assigned."}
        </div>
      ) : (
        <div className="divide-y divide-slate-100 border border-slate-200 rounded-xl overflow-hidden">
          {collaborators.map(c => (
            <div key={c.user_email} className="flex items-center gap-2 px-3 py-2.5 bg-white text-xs group">
              <div className="w-7 h-7 rounded-full bg-indigo-100 text-indigo-700 flex items-center justify-center font-bold shrink-0">
                {(c.user_name ?? c.user_email).slice(0, 1).toUpperCase()}
              </div>
              <div className="flex-1 min-w-0">
                {c.user_name && <p className="font-semibold text-slate-900 truncate">{c.user_name}</p>}
                <p className="text-slate-500 truncate">{c.user_email}</p>
              </div>
              {isOwner && (
                <button
                  type="button"
                  onClick={() => void removeCollaborator(c.user_email)}
                  disabled={removingEmail === c.user_email}
                  className="opacity-0 group-hover:opacity-100 text-slate-400 hover:text-red-500 disabled:opacity-50"
                  title="Remove collaborator"
                >
                  {removingEmail === c.user_email ? (
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  ) : (
                    <X className="w-3.5 h-3.5" />
                  )}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}


// ── Locked skill content ──────────────────────────────────────────────────────

function LockedSkillContent({
  access,
  slug,
  justification,
  onJustificationChange,
  onSubmit,
  submitting,
  error,
  success,
}: {
  access: SkillAccess | null;
  slug: string;
  justification: string;
  onJustificationChange: (v: string) => void;
  onSubmit: (e: FormEvent) => void;
  submitting: boolean;
  error: string | null;
  success: boolean;
}) {
  const isPending = !!access?.pending_request_id;
  const isBlocked = access?.cross_dept_policy === "blocked";
  const canRequest = access?.can_request ?? false;

  const riskColors: Record<string, string> = {
    standard: "text-emerald-700 bg-emerald-50 border-emerald-200",
    elevated: "text-amber-700 bg-amber-50 border-amber-200",
    critical: "text-red-700 bg-red-50 border-red-200",
  };

  return (
    <div className="border border-slate-200 rounded-2xl overflow-hidden">
      {/* Lock banner */}
      <div className="bg-slate-950 px-6 py-8 text-center flex flex-col items-center gap-3">
        <div className="w-12 h-12 rounded-full bg-white/10 flex items-center justify-center">
          <Lock className="w-6 h-6 text-white" />
        </div>
        <h3 className="text-white font-bold text-lg">Full Skill Locked</h3>
        <p className="text-slate-400 text-sm max-w-sm leading-relaxed">
          {isBlocked
            ? "This skill is restricted to its home department only and cannot be requested."
            : isPending
            ? "Your access request is pending approval. You'll be notified when it's reviewed."
            : "Request access to unlock the full skill — queries, prompts, and integration details."}
        </p>
      </div>

      {/* Info strip */}
      <div className="bg-white border-t border-slate-200 px-6 py-4 flex flex-wrap gap-4 text-xs text-slate-600">
        {access?.owning_department && (
          <div><span className="font-semibold text-slate-900">Owned by</span> {access.owning_department}</div>
        )}
        {access?.risk_level && (
          <div>
            <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border font-bold uppercase tracking-wider text-[9px] ${riskColors[access.risk_level]}`}>
              {access.risk_level} risk
            </span>
          </div>
        )}
        {access?.integrations && access.integrations.length > 0 && (
          <div>
            <span className="font-semibold text-slate-900">Accesses: </span>
            {access.integrations.join(", ")}
          </div>
        )}
        {access?.max_system_risk_level && (
          <div className="flex items-center gap-1.5">
            <span className="font-semibold text-slate-900">System criticality:</span>
            <SystemCriticalityBadge riskLevel={access.max_system_risk_level} />
          </div>
        )}
      </div>

      {/* CTA */}
      <div className="bg-slate-50 border-t border-slate-200 px-6 py-6">
        {isPending ? (
          <div className="flex items-center gap-3 text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-xl px-4 py-3">
            <Clock className="w-4 h-4 shrink-0" />
            <span>Your request is <strong>pending approval</strong>. You'll be notified when it's decided.</span>
          </div>
        ) : isBlocked ? (
          <div className="flex items-center gap-3 text-sm text-red-700 bg-red-50 border border-red-200 rounded-xl px-4 py-3">
            <Lock className="w-4 h-4 shrink-0" />
            <span>Cross-department access is <strong>not permitted</strong> for this skill.</span>
          </div>
        ) : success ? (
          <div className="flex items-center gap-3 text-sm text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-xl px-4 py-3">
            <CheckCircle2 className="w-4 h-4 shrink-0" />
            <span><strong>Request submitted!</strong> The skill owner has been notified and will review your request.</span>
          </div>
        ) : canRequest ? (
          <form onSubmit={onSubmit} className="space-y-3">
            <div>
              <label className="block text-xs font-bold text-slate-700 mb-1.5">
                Why do you need access? <span className="text-red-500">*</span>
              </label>
              <textarea
                rows={4}
                value={justification}
                onChange={e => onJustificationChange(e.target.value)}
                placeholder="Explain the business need — include any relevant project or ticket reference. Minimum 20 characters."
                className="w-full text-sm px-3 py-2 border border-slate-200 rounded-lg resize-none focus:outline-none focus:border-slate-400 bg-white placeholder:text-slate-400"
                maxLength={2000}
              />
              <div className="text-[10px] text-right text-slate-400 mt-1">{justification.length}/2000</div>
            </div>
            {error && (
              <div className="flex items-start gap-2 text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
                <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" /> {error}
              </div>
            )}
            <button
              type="submit"
              disabled={submitting}
              className="w-full py-2.5 text-sm font-bold bg-slate-900 text-white rounded-xl hover:bg-slate-700 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
            >
              {submitting ? "Submitting…" : "Request Access to This Skill"}
            </button>
          </form>
        ) : null}
      </div>
    </div>
  );
}
