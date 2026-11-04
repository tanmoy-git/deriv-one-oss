import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { AlertCircle, Bot, Hash, KeyRound, Layers, Loader2, Network, RefreshCw, Search, Server, X } from "lucide-react";
import { apiFetch } from "@/lib/api";
import {
  COLUMN_LABELS,
  COLUMN_ORDER,
  COLUMN_WIDTH,
  connectedIds,
  directionLabel,
  layoutColumns,
  searchNodes,
  type AgentMap,
  type MapNode,
  type MapNodeType,
} from "./graph";

const NODE_W = 220;

const TYPE_STYLE: Record<MapNodeType, { border: string; icon: ReactNode; minimap: string }> = {
  channel: { border: "border-sky-300", icon: <Hash className="w-3.5 h-3.5 text-sky-500" />, minimap: "#7dd3fc" },
  agent: { border: "border-indigo-300", icon: <Bot className="w-3.5 h-3.5 text-indigo-500" />, minimap: "#a5b4fc" },
  skill: { border: "border-emerald-300", icon: <Layers className="w-3.5 h-3.5 text-emerald-600" />, minimap: "#6ee7b7" },
  system: { border: "border-amber-300", icon: <Server className="w-3.5 h-3.5 text-amber-600" />, minimap: "#fcd34d" },
  secret: { border: "border-rose-300", icon: <KeyRound className="w-3.5 h-3.5 text-rose-500" />, minimap: "#fda4af" },
};

function riskChip(risk: string | null | undefined): string {
  switch ((risk ?? "").toLowerCase()) {
    case "critical": return "bg-red-50 text-red-700 border-red-200";
    case "high":
    case "elevated": return "bg-orange-50 text-orange-700 border-orange-200";
    case "medium": return "bg-amber-50 text-amber-700 border-amber-200";
    default: return "bg-slate-50 text-slate-600 border-slate-200";
  }
}

type MapNodeData = { node: MapNode; dimmed: boolean; selected: boolean };
type MapFlowNode = Node<MapNodeData, "mapNode">;
type HeaderFlowNode = Node<{ label: string }, "columnHeader">;

function MapNodeCard({ data }: NodeProps<MapFlowNode>) {
  const { node, dimmed, selected } = data;
  const style = TYPE_STYLE[node.type];
  const showRisk = node.risk_level && node.risk_level !== "standard";
  return (
    <div
      className={`bg-white border-2 rounded-xl shadow-sm px-3 py-2 cursor-pointer transition-opacity ${style.border} ${
        selected ? "ring-2 ring-[#FF444F] ring-offset-1" : ""
      } ${dimmed ? "opacity-25" : ""}`}
      style={{ width: NODE_W }}
      title={node.detail ?? node.id}
    >
      <Handle type="target" position={Position.Left} className="!w-2 !h-2 !bg-slate-300 !border-0" />
      <div className="flex items-center gap-1.5 min-w-0">
        {style.icon}
        <span className="text-xs font-semibold text-slate-800 truncate">{node.label}</span>
      </div>
      {(showRisk || node.status || node.detail) && (
        <div className="flex items-center gap-1 mt-1 min-w-0">
          {showRisk && (
            <span className={`text-[10px] font-medium border rounded-full px-1.5 py-0.5 ${riskChip(node.risk_level)}`}>
              {node.risk_level}
            </span>
          )}
          {node.status && <span className="text-[10px] text-slate-400 shrink-0">{node.status}</span>}
          {node.detail && <span className="text-[10px] text-slate-400 truncate">{node.detail}</span>}
        </div>
      )}
      <Handle type="source" position={Position.Right} className="!w-2 !h-2 !bg-slate-300 !border-0" />
    </div>
  );
}

function ColumnHeader({ data }: NodeProps<HeaderFlowNode>) {
  return (
    <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-400" style={{ width: NODE_W }}>
      {data.label}
    </div>
  );
}

const nodeTypes = { mapNode: MapNodeCard, columnHeader: ColumnHeader };

function AgentMapCanvas({ data }: { data: AgentMap }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [hideUnrelated, setHideUnrelated] = useState(false);

  const highlighted = useMemo(
    () => (selected ? connectedIds(selected, data.edges) : null),
    [selected, data.edges],
  );
  const matches = useMemo(() => searchNodes(data.nodes, query), [data.nodes, query]);

  const visibleNodes = useMemo(
    () => (hideUnrelated && highlighted ? data.nodes.filter(n => highlighted.has(n.id)) : data.nodes),
    [data.nodes, hideUnrelated, highlighted],
  );

  const flowNodes = useMemo<(MapFlowNode | HeaderFlowNode)[]>(() => {
    const positions = layoutColumns(visibleNodes);
    const present = new Set(visibleNodes.map(n => n.type));
    const headers: HeaderFlowNode[] = COLUMN_ORDER.filter(t => present.has(t)).map(t => ({
      id: `header:${t}`,
      type: "columnHeader",
      position: { x: COLUMN_ORDER.indexOf(t) * COLUMN_WIDTH, y: -40 },
      data: { label: COLUMN_LABELS[t] },
      draggable: false,
      selectable: false,
    }));
    const cards: MapFlowNode[] = visibleNodes.map(n => ({
      id: n.id,
      type: "mapNode",
      position: positions.get(n.id) ?? { x: 0, y: 0 },
      data: { node: n, selected: n.id === selected, dimmed: highlighted !== null && !highlighted.has(n.id) },
    }));
    return [...headers, ...cards];
  }, [visibleNodes, selected, highlighted]);

  const flowEdges = useMemo<Edge[]>(() => {
    const visible = new Set(visibleNodes.map(n => n.id));
    return data.edges
      .filter(e => visible.has(e.source) && visible.has(e.target))
      .map(e => {
        const lit = highlighted !== null && highlighted.has(e.source) && highlighted.has(e.target);
        const writes = e.direction === "write" || e.direction === "read_write";
        // Channels are drawn left of agents, so draw that edge right-to-left
        // into the agent's left handle instead of looping back from its right.
        const flip = e.kind === "in_channel";
        return {
          id: e.id,
          source: flip ? e.target : e.source,
          target: flip ? e.source : e.target,
          label: directionLabel(e.direction),
          labelStyle: { fontSize: 10, fill: writes ? "#be123c" : "#64748b" },
          animated: lit,
          style: {
            stroke: lit ? "#FF444F" : writes ? "#fda4af" : "#cbd5e1",
            strokeWidth: lit ? 2 : 1.25,
            opacity: highlighted !== null && !lit ? 0.15 : 1,
          },
        };
      });
  }, [data.edges, visibleNodes, highlighted]);

  const selectedNode = selected ? data.nodes.find(n => n.id === selected) : undefined;
  const connectedCounts = useMemo(() => {
    if (!highlighted || !selected) return null;
    const counts = Object.fromEntries(COLUMN_ORDER.map(t => [t, 0])) as Record<MapNodeType, number>;
    for (const n of data.nodes) if (n.id !== selected && highlighted.has(n.id)) counts[n.type] += 1;
    return counts;
  }, [data.nodes, highlighted, selected]);

  const pick = useCallback((id: string) => {
    setSelected(id);
    setQuery("");
  }, []);

  return (
    <div className="flex flex-col gap-3 h-full overflow-hidden">
      <div className="flex items-center gap-3 flex-wrap shrink-0">
        <div className="relative flex-1 min-w-[240px] max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
          <input
            className="w-full pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-[#FF444F]/30 focus:border-[#FF444F]"
            placeholder="Find an agent, skill, system, secret or channel…"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={e => {
              const first = matches[0];
              if (e.key === "Enter" && first) pick(first.id);
            }}
          />
          {matches.length > 0 && (
            <ul className="absolute z-20 mt-1 w-full bg-white border border-slate-200 rounded-xl shadow-lg overflow-hidden">
              {matches.map(m => (
                <li key={m.id}>
                  <button
                    onClick={() => pick(m.id)}
                    className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-left hover:bg-slate-50"
                  >
                    {TYPE_STYLE[m.type].icon}
                    <span className="font-medium text-slate-800 truncate">{m.label}</span>
                    <span className="ml-auto text-[10px] text-slate-400">{COLUMN_LABELS[m.type]}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <label className="flex items-center gap-1.5 text-xs text-slate-600 select-none">
          <input
            type="checkbox"
            checked={hideUnrelated}
            onChange={e => setHideUnrelated(e.target.checked)}
            disabled={!selected}
            className="accent-[#FF444F]"
          />
          Hide unrelated
        </label>
        {selectedNode && connectedCounts && (
          <div className="flex items-center gap-2 bg-white border border-slate-200 rounded-xl px-3 py-1.5 text-xs">
            {TYPE_STYLE[selectedNode.type].icon}
            <span className="font-semibold text-slate-800">{selectedNode.label}</span>
            <span className="text-slate-400">
              {COLUMN_ORDER.filter(t => connectedCounts[t] > 0)
                .map(t => `${connectedCounts[t]} ${COLUMN_LABELS[t].toLowerCase()}`)
                .join(" · ") || "no connections"}
            </span>
            <button
              onClick={() => { setSelected(null); setHideUnrelated(false); }}
              className="text-slate-400 hover:text-slate-700"
              aria-label="Clear selection"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        )}
      </div>

      <div className="flex-1 min-h-0 bg-white border border-slate-200 rounded-xl overflow-hidden">
        {data.nodes.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-2 text-slate-400">
            <Network className="w-6 h-6" />
            <span className="text-sm">No agents yet, so there is nothing to map.</span>
          </div>
        ) : (
          <ReactFlow
            nodes={flowNodes}
            edges={flowEdges}
            nodeTypes={nodeTypes}
            onNodeClick={(_, n) => { if (n.type === "mapNode") setSelected(prev => (prev === n.id ? null : n.id)); }}
            onPaneClick={() => setSelected(null)}
            nodesConnectable={false}
            fitView
            fitViewOptions={{ padding: 0.15 }}
            minZoom={0.1}
            maxZoom={2}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={20} size={1} color="#e2e8f0" />
            <Controls showInteractive={false} className="!shadow-none !border !border-slate-200 !rounded-xl overflow-hidden" />
          </ReactFlow>
        )}
      </div>
    </div>
  );
}

export function AgentMapTab() {
  const [data, setData] = useState<AgentMap | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/api/admin/agent-map");
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { detail?: string }).detail ?? `HTTP ${res.status}`);
      }
      setData((await res.json()) as AgentMap);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load the agent map.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  if (loading && !data) return (
    <div className="flex flex-col items-center justify-center h-full gap-3 text-slate-400">
      <Loader2 className="w-6 h-6 animate-spin text-[#FF444F]" />
      <span className="text-sm">Loading agent map…</span>
    </div>
  );

  if (error && !data) return (
    <div className="flex flex-col items-center justify-center h-full gap-3">
      <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-5 py-4 max-w-md text-center">
        <p className="font-semibold mb-1 flex items-center justify-center gap-1.5">
          <AlertCircle className="w-4 h-4" /> Failed to load the agent map
        </p>
        <p className="text-xs opacity-80">{error}</p>
      </div>
      <button onClick={() => void load()} className="text-xs text-[#FF444F] hover:underline">Try again</button>
    </div>
  );

  if (!data) return null;

  return (
    <div className="flex flex-col gap-3 h-full overflow-hidden">
      <div className="flex items-start justify-between gap-3 shrink-0">
        <div>
          <h2 className="text-sm font-bold text-slate-900 flex items-center gap-2">
            <Network className="w-4 h-4 text-[#FF444F]" /> Agent map
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">
            Which agents carry which skills, and which systems, secrets and channels those reach. Click a
            node to see everything upstream and downstream of it. Secrets are shown by name only.
          </p>
          <p className="text-[11px] text-slate-400 mt-1">
            {COLUMN_ORDER.map(t => `${data.totals[t] ?? 0} ${COLUMN_LABELS[t].toLowerCase()}`).join(" · ")}
          </p>
        </div>
        <button
          onClick={() => void load()}
          disabled={loading}
          className="flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-700 border border-slate-200 px-3 py-1.5 rounded-lg hover:bg-slate-50 transition-colors shrink-0 disabled:opacity-50"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} /> Refresh
        </button>
      </div>
      <div className="flex-1 min-h-0">
        <ReactFlowProvider>
          <AgentMapCanvas data={data} />
        </ReactFlowProvider>
      </div>
    </div>
  );
}
