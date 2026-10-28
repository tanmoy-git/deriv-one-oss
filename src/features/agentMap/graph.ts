import type { components } from "@/src/api/schema";

export type AgentMap = components["schemas"]["AgentMapResponse"];
export type MapNode = components["schemas"]["AgentMapNode"];
export type MapEdge = components["schemas"]["AgentMapEdge"];
export type MapNodeType = MapNode["type"];

// Channels sit left of agents because they hang off agents, the same way
// systems and secrets hang off skills on the right.
export const COLUMN_ORDER: MapNodeType[] = ["channel", "agent", "skill", "system", "secret"];

export const COLUMN_LABELS: Record<MapNodeType, string> = {
  channel: "Slack channels",
  agent: "Agents",
  skill: "Skills",
  system: "Systems",
  secret: "Secrets",
};

export const COLUMN_WIDTH = 300;
export const ROW_HEIGHT = 64;

export interface Point {
  x: number;
  y: number;
}

/** One column per node type, each column vertically centred on the tallest. */
export function layoutColumns(nodes: MapNode[]): Map<string, Point> {
  const columns = new Map<MapNodeType, MapNode[]>(COLUMN_ORDER.map(t => [t, []]));
  for (const node of nodes) columns.get(node.type)?.push(node);

  const tallest = Math.max(0, ...[...columns.values()].map(c => c.length));
  const positions = new Map<string, Point>();
  COLUMN_ORDER.forEach((type, col) => {
    const members = columns.get(type) ?? [];
    const offset = ((tallest - members.length) * ROW_HEIGHT) / 2;
    members.forEach((node, row) => {
      positions.set(node.id, { x: col * COLUMN_WIDTH, y: offset + row * ROW_HEIGHT });
    });
  });
  return positions;
}

/**
 * The node plus everything strictly upstream and downstream of it. Mirrors
 * backend/agent_map.connected_ids: following edges in one direction at a time
 * keeps a system's highlight to the skills and agents that reach it, instead
 * of every other system those agents also touch.
 */
export function connectedIds(focusId: string, edges: Pick<MapEdge, "source" | "target">[]): Set<string> {
  const down = new Map<string, string[]>();
  const up = new Map<string, string[]>();
  for (const e of edges) {
    down.set(e.source, [...(down.get(e.source) ?? []), e.target]);
    up.set(e.target, [...(up.get(e.target) ?? []), e.source]);
  }
  const keep = new Set([focusId]);
  for (const adjacency of [down, up]) {
    const queue = [focusId];
    const seen = new Set([focusId]);
    while (queue.length > 0) {
      const current = queue.shift() as string;
      for (const next of adjacency.get(current) ?? []) {
        if (!seen.has(next)) {
          seen.add(next);
          keep.add(next);
          queue.push(next);
        }
      }
    }
  }
  return keep;
}

/** Nodes whose label or id contains the query, best matches (prefix) first. */
export function searchNodes(nodes: MapNode[], query: string, limit = 8): MapNode[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const scored: { node: MapNode; score: number }[] = [];
  for (const node of nodes) {
    const label = node.label.toLowerCase();
    const id = node.id.toLowerCase();
    if (label.startsWith(q) || label.startsWith(`#${q}`)) scored.push({ node, score: 0 });
    else if (label.includes(q)) scored.push({ node, score: 1 });
    else if (id.includes(q)) scored.push({ node, score: 2 });
  }
  scored.sort((a, b) => a.score - b.score || a.node.label.localeCompare(b.node.label));
  return scored.slice(0, limit).map(s => s.node);
}

export function directionLabel(direction: string | null | undefined): string | undefined {
  if (direction === "read") return "read";
  if (direction === "write") return "write";
  if (direction === "read_write") return "read + write";
  return undefined;
}
