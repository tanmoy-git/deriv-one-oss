import { describe, expect, it } from "vitest";
import {
  COLUMN_WIDTH,
  ROW_HEIGHT,
  connectedIds,
  directionLabel,
  layoutColumns,
  searchNodes,
  type MapNode,
} from "./graph";

const node = (id: string, type: MapNode["type"], label: string): MapNode => ({ id, type, label });

const nodes: MapNode[] = [
  node("agent:a1", "agent", "Sales Bot"),
  node("agent:a2", "agent", "Ops Bot"),
  node("skill:sf", "skill", "Salesforce Read"),
  node("skill:jira", "skill", "Jira"),
  node("system:salesforce", "system", "Salesforce"),
  node("system:jira", "system", "Jira"),
  node("secret:SF_TOKEN", "secret", "SF_TOKEN"),
  node("channel:C1", "channel", "#sales"),
];

const edges = [
  { source: "agent:a1", target: "skill:sf" },
  { source: "agent:a1", target: "skill:jira" },
  { source: "agent:a2", target: "skill:jira" },
  { source: "agent:a1", target: "channel:C1" },
  { source: "skill:sf", target: "system:salesforce" },
  { source: "skill:sf", target: "secret:SF_TOKEN" },
  { source: "skill:jira", target: "system:jira" },
];

describe("layoutColumns", () => {
  it("places each node type in its own column and centres shorter columns", () => {
    const pos = layoutColumns(nodes);
    expect(pos.get("channel:C1")).toEqual({ x: 0, y: ROW_HEIGHT / 2 });
    expect(pos.get("agent:a1")).toEqual({ x: COLUMN_WIDTH, y: 0 });
    expect(pos.get("agent:a2")).toEqual({ x: COLUMN_WIDTH, y: ROW_HEIGHT });
    expect(pos.get("skill:sf")?.x).toBe(2 * COLUMN_WIDTH);
    expect(pos.get("system:jira")?.x).toBe(3 * COLUMN_WIDTH);
    expect(pos.get("secret:SF_TOKEN")?.x).toBe(4 * COLUMN_WIDTH);
  });

  it("handles an empty graph", () => {
    expect(layoutColumns([]).size).toBe(0);
  });
});

describe("connectedIds", () => {
  it("answers what can touch a system without pulling in the agents' other systems", () => {
    expect(connectedIds("system:salesforce", edges)).toEqual(
      new Set(["system:salesforce", "skill:sf", "agent:a1"]),
    );
  });

  it("answers what breaks when a secret is rotated", () => {
    expect(connectedIds("secret:SF_TOKEN", edges)).toEqual(new Set(["secret:SF_TOKEN", "skill:sf", "agent:a1"]));
  });

  it("returns an agent's full reach downstream", () => {
    expect(connectedIds("agent:a2", edges)).toEqual(new Set(["agent:a2", "skill:jira", "system:jira"]));
  });
});

describe("searchNodes", () => {
  it("ranks prefix matches before substring and id matches", () => {
    expect(searchNodes(nodes, "sales").map(n => n.id)).toEqual([
      "channel:C1",
      "agent:a1",
      "system:salesforce",
      "skill:sf",
    ]);
  });

  it("returns nothing for a blank query and respects the limit", () => {
    expect(searchNodes(nodes, "  ")).toEqual([]);
    expect(searchNodes(nodes, "a", 2)).toHaveLength(2);
  });
});

describe("directionLabel", () => {
  it("labels known directions only", () => {
    expect(directionLabel("read_write")).toBe("read + write");
    expect(directionLabel(null)).toBeUndefined();
  });
});
