import { describe, expect, it } from "vitest";
import { formatCost, formatMs, stepDetail, stepTitle } from "./format";

describe("trace formatting", () => {
  it("formats durations in milliseconds and seconds", () => {
    expect(formatMs(null)).toBe("");
    expect(formatMs(850)).toBe("850 ms");
    expect(formatMs(2350)).toBe("2.4 s");
  });

  it("keeps small costs readable", () => {
    expect(formatCost(0)).toBe("$0");
    expect(formatCost(0.00123)).toBe("$0.0012");
    expect(formatCost(1.5)).toBe("$1.50");
  });

  it("titles each kind of step", () => {
    expect(stepTitle({ kind: "routing", t_ms: 0, selected: ["tickets"] })).toBe("Skill routing: tickets");
    expect(stepTitle({ kind: "routing", t_ms: 0, selected: [] })).toBe("Skill routing: no skill");
    expect(
      stepTitle({ kind: "model", t_ms: 5, turn: 2, model: "m", input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cost_usd: 0 }),
    ).toBe("Model call 2");
    expect(stepTitle({ kind: "tool", t_ms: 1, tool: "run_python" })).toBe("Tool: run Python");
    expect(stepTitle({ kind: "tool", t_ms: 1, tool: "send_slack_message" })).toBe("Tool: send slack message");
  });

  it("describes model tokens and tool failures", () => {
    expect(
      stepDetail({ kind: "model", t_ms: 5, turn: 1, model: "m", input_tokens: 1500, output_tokens: 20, cache_read_tokens: 1000, cost_usd: 0.004 }),
    ).toBe("1.5K in (1.0K from cache) · 20 out · $0.0040");
    expect(stepDetail({ kind: "tool", t_ms: 1, tool: "run_python", ok: false, code_len: 40, output_len: 12 })).toBe(
      "failed · 40 characters of code · 12 characters of output",
    );
  });
});
