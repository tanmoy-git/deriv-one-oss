import { useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { AlertCircle, ChevronLeft, ChevronRight, Loader2, Search } from "lucide-react";
import { apiFetch } from "@/lib/api";
import { InteractionDetailModal } from "@/components/InteractionDetailModal";
import { AgentInteractionListResponse, AgentInteractionRecord } from "@/data/agents";
import { formatInteractionWhen, interactionTriggerLabel } from "../format";

// ── Interaction log tab ───────────────────────────────────────────────────

export const INTERACTIONS_PAGE_SIZE = 25;

export function InteractionLogTab({
  agentId,
  initialInteractionId,
  onTotalChange,
  onFailureTotalChange,
}: {
  agentId: string;
  initialInteractionId: string | null;
  onTotalChange: (total: number) => void;
  onFailureTotalChange: (total: number) => void;
}) {
  const [items, setItems] = useState<AgentInteractionRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");
  const [triggerFilter, setTriggerFilter] = useState("");
  const [failuresOnly, setFailuresOnly] = useState(false);
  const [selectedItem, setSelectedItem] = useState<AgentInteractionRecord | null>(null);
  const [highlightId, setHighlightId] = useState<string | null>(initialInteractionId);
  const deepLinkRowRef = useRef<HTMLTableRowElement | null>(null);

  useEffect(() => {
    if (!initialInteractionId) return;
    setHighlightId(initialInteractionId);
  }, [initialInteractionId]);

  useEffect(() => {
    if (!initialInteractionId || loading || items.length === 0) return;
    const match = items.find(row => row.id === initialInteractionId);
    if (match) {
      setSelectedItem(match);
    }
  }, [initialInteractionId, loading, items]);

  useEffect(() => {
    if (!highlightId || !deepLinkRowRef.current) return;
    const timer = window.setTimeout(() => {
      deepLinkRowRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [highlightId, loading, items]);

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(searchInput.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [searchInput]);

  useEffect(() => {
    setPage(1);
  }, [query, triggerFilter, failuresOnly]);

  useEffect(() => {
    let cancelled = false;
    const loadFailureTotal = async () => {
      try {
        const res = await apiFetch(
          `/api/agents/${agentId}/interactions?failures_only=true&page=1&limit=1`
        );
        if (!res.ok) return;
        const payload: AgentInteractionListResponse = await res.json();
        if (!cancelled) {
          onFailureTotalChange(typeof payload.total === "number" ? payload.total : 0);
        }
      } catch {
        if (!cancelled) onFailureTotalChange(0);
      }
    };
    void loadFailureTotal();
    return () => {
      cancelled = true;
    };
  }, [agentId, onFailureTotalChange]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams({
          page: String(page),
          limit: String(INTERACTIONS_PAGE_SIZE),
        });
        if (query) params.set("q", query);
        if (triggerFilter) params.set("trigger_type", triggerFilter);
        if (failuresOnly) params.set("failures_only", "true");

        const res = await apiFetch(
          `/api/agents/${agentId}/interactions?${params.toString()}`
        );
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const payload: AgentInteractionListResponse = await res.json();
        if (cancelled) return;
        const nextItems = Array.isArray(payload.items) ? payload.items : [];
        const nextTotal = typeof payload.total === "number" ? payload.total : 0;
        setItems(nextItems);
        setTotal(nextTotal);
        onTotalChange(nextTotal);
      } catch {
        if (!cancelled) {
          setError("Could not load interaction log.");
          setItems([]);
          setTotal(0);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [agentId, page, query, triggerFilter, failuresOnly, onTotalChange, initialInteractionId]);

  const totalPages = Math.max(1, Math.ceil(total / INTERACTIONS_PAGE_SIZE));
  const rangeStart = total === 0 ? 0 : (page - 1) * INTERACTIONS_PAGE_SIZE + 1;
  const rangeEnd = Math.min(page * INTERACTIONS_PAGE_SIZE, total);

  return (
    <div className="space-y-4">
      <div className="bg-white border border-slate-200 rounded-xl p-4 space-y-3">
        <div>
          <h2 className="text-sm font-bold text-slate-900">Interaction log</h2>
          <p className="text-xs text-slate-500 mt-1">
            Queries and responses from Slack and agent-to-agent calls. Click a row to
            open the full run details, tool timeline, and failure diagnostics.
          </p>
        </div>

        <div className="flex flex-col sm:flex-row gap-2 sm:items-center">
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
            <input
              type="search"
              value={searchInput}
              onChange={e => setSearchInput(e.target.value)}
              placeholder="Search user, query, or response…"
              className="w-full text-sm pl-9 pr-3 py-2 border border-slate-200 rounded-lg focus:outline-none focus:border-slate-400"
            />
          </div>
          <select
            value={triggerFilter}
            onChange={e => setTriggerFilter(e.target.value)}
            className="text-sm px-3 py-2 border border-slate-200 rounded-lg bg-white focus:outline-none focus:border-slate-400"
          >
            <option value="">All types</option>
            <option value="mention">Mention</option>
            <option value="dm">DM</option>
            <option value="a2a">Agent call</option>
          </select>
          <label className="inline-flex items-center gap-2 text-sm text-slate-700 whitespace-nowrap px-1">
            <input
              type="checkbox"
              checked={failuresOnly}
              onChange={e => setFailuresOnly(e.target.checked)}
              className="rounded border-slate-300 text-slate-900 focus:ring-slate-400"
            />
            Failures only
          </label>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-4 py-3 flex items-start gap-2">
          <AlertCircle className="w-4 h-4 mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-16 bg-white border border-slate-200 rounded-xl">
          <Loader2 className="w-5 h-5 animate-spin text-slate-400" />
        </div>
      ) : items.length === 0 ? (
        <div className="text-center py-16 bg-white border border-slate-200 rounded-xl text-slate-500 text-sm">
          {query || triggerFilter || failuresOnly
            ? "No interactions match your filters."
            : "No interactions recorded yet."}
        </div>
      ) : (
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm min-w-[720px]">
              <thead className="bg-slate-50 border-b border-slate-200">
                <tr>
                  <InteractionTh>Time</InteractionTh>
                  <InteractionTh>User</InteractionTh>
                  <InteractionTh>Type</InteractionTh>
                  <InteractionTh>Duration</InteractionTh>
                  <InteractionTh>Query</InteractionTh>
                  <InteractionTh className="w-10" />
                </tr>
              </thead>
              <tbody>
                {items.map(item => {
                  const isHighlighted = highlightId === item.id;
                  return (
                    <InteractionRow
                      key={item.id}
                      item={item}
                      highlighted={isHighlighted}
                      rowRef={isHighlighted ? deepLinkRowRef : undefined}
                      onOpen={() => setSelectedItem(item)}
                    />
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 px-4 py-3 border-t border-slate-200 bg-slate-50">
            <p className="text-xs text-slate-500">
              Showing {rangeStart}–{rangeEnd} of {total.toLocaleString()}
            </p>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page <= 1}
                className="inline-flex items-center gap-1 px-3 py-1.5 text-xs font-medium border border-slate-200 rounded-lg bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <ChevronLeft className="w-3.5 h-3.5" />
                Previous
              </button>
              <span className="text-xs text-slate-500 tabular-nums">
                Page {page} of {totalPages}
              </span>
              <button
                type="button"
                onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                disabled={page >= totalPages}
                className="inline-flex items-center gap-1 px-3 py-1.5 text-xs font-medium border border-slate-200 rounded-lg bg-white text-slate-700 hover:bg-slate-50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Next
                <ChevronRight className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        </div>
      )}

      {selectedItem && (
        <InteractionDetailModal
          agentId={agentId}
          item={selectedItem}
          onClose={() => setSelectedItem(null)}
        />
      )}
    </div>
  );
}

export function InteractionTh({
  children,
  className = "",
}: {
  children?: ReactNode;
  className?: string;
}) {
  return (
    <th
      className={`text-left text-[11px] font-semibold text-slate-500 uppercase tracking-wider px-4 py-2.5 ${className}`}
    >
      {children}
    </th>
  );
}

export function InteractionRow({
  item,
  onOpen,
  highlighted,
  rowRef,
}: {
  item: AgentInteractionRecord;
  onOpen: () => void;
  highlighted?: boolean;
  rowRef?: RefObject<HTMLTableRowElement | null>;
}) {
  const preview =
    item.user_message.length > 120
      ? `${item.user_message.slice(0, 120)}…`
      : item.user_message;
  const toolSummary = item.tool_summary;
  const hasFailure = Boolean(item.failure_kind);

  return (
    <tr
      ref={rowRef}
      className={`border-b border-slate-100 hover:bg-slate-50 cursor-pointer ${
        hasFailure ? "bg-red-50/40" : ""
      } ${highlighted ? "ring-2 ring-inset ring-slate-300 bg-slate-50" : ""}`}
      onClick={onOpen}
    >
      <td className="px-4 py-3 text-xs text-slate-600 whitespace-nowrap">
        {formatInteractionWhen(item.created_at)}
      </td>
      <td className="px-4 py-3 text-xs text-slate-700 max-w-[180px] truncate" title={item.invoker_email}>
        {item.invoker_email}
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="inline-flex text-[10px] font-semibold uppercase tracking-wide rounded-full bg-slate-100 text-slate-600 px-2 py-0.5">
            {interactionTriggerLabel(item.trigger_type)}
          </span>
          {hasFailure && (
            <span className="inline-flex text-[10px] font-semibold uppercase tracking-wide rounded-full bg-red-100 text-red-700 px-2 py-0.5">
              Failed
            </span>
          )}
          {toolSummary && toolSummary.total > 0 && (
            <span className="inline-flex text-[10px] font-medium rounded-full bg-slate-50 text-slate-500 border border-slate-200 px-2 py-0.5 tabular-nums">
              {toolSummary.total} tools
              {toolSummary.failed > 0 ? ` · ${toolSummary.failed} err` : ""}
            </span>
          )}
        </div>
      </td>
      <td className="px-4 py-3 text-xs text-slate-500 tabular-nums whitespace-nowrap">
        {item.duration_ms != null ? `${(item.duration_ms / 1000).toFixed(1)}s` : "—"}
      </td>
      <td className="px-4 py-3 text-sm text-slate-700 max-w-md">
        <span className="line-clamp-2 whitespace-pre-wrap">{preview}</span>
      </td>
      <td className="px-4 py-3 text-slate-400">
        <ChevronRight className="w-4 h-4" />
      </td>
    </tr>
  );
}
