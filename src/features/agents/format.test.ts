import { describe, expect, it } from "vitest";
import { formatInteractionWhen, interactionTriggerLabel } from "./format";
import { describeCron } from "./tabs/AutomationsTab";

describe("agent formatting helpers", () => {
  it("returns the input unchanged when the timestamp is not a date", () => {
    expect(formatInteractionWhen("not-a-date")).toBe("not-a-date");
  });

  it("labels each interaction trigger", () => {
    expect(interactionTriggerLabel("dm")).toBe("DM");
    expect(interactionTriggerLabel("a2a")).toBe("Agent call");
    expect(interactionTriggerLabel("mention")).toBe("Mention");
  });

  it("describes preset crons by label and leaves custom ones as written", () => {
    expect(describeCron("*/5 * * 1 2")).toBe("*/5 * * 1 2");
  });
});
