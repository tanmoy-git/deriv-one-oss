import { describe, expect, it } from "vitest";
import { barWidth, forecastText, formatUsd } from "./format";
import type { Budget } from "./types";

function budget(status: Partial<Budget["status"]>, enforce = true): Budget {
  return {
    id: "b1",
    scope: "agent",
    scope_id: "a1",
    label: "Support bot",
    monthly_limit_usd: 100,
    soft_limit_pct: 80,
    enforce,
    updated_at: "2026-10-01T00:00:00",
    status: {
      spent_usd: 10,
      limit_usd: 100,
      soft_limit_usd: 80,
      used_pct: 10,
      state: "ok",
      forecast_usd: 20,
      forecast_exceed_at: null,
      ...status,
    },
  };
}

describe("budget formatting", () => {
  it("formats dollars with two decimals and thousands separators", () => {
    expect(formatUsd(1234.5)).toBe("$1,234.50");
  });

  it("caps the progress bar between 0 and 100 percent", () => {
    expect(barWidth(140)).toBe("100%");
    expect(barWidth(-5)).toBe("0%");
    expect(barWidth(42.5)).toBe("42.5%");
  });

  it("projects the month when the limit will not be reached", () => {
    expect(forecastText(budget({}))).toBe("On pace for $20.00 this month.");
  });

  it("names the day the limit will be reached", () => {
    const text = forecastText(budget({ forecast_usd: 150, forecast_exceed_at: "2026-10-21T00:00:00Z" }));
    expect(text).toBe("At the current rate it reaches the limit on Oct 21.");
  });

  it("says whether an exceeded budget blocks calls", () => {
    expect(forecastText(budget({ state: "exceeded" }))).toMatch(/Model calls are blocked/);
    expect(forecastText(budget({ state: "exceeded" }, false))).toMatch(/Alert only/);
  });
});
