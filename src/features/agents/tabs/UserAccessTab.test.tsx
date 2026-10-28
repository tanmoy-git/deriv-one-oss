import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiFetch = vi.fn();
vi.mock("@/lib/api", () => ({ apiFetch: (...args: unknown[]) => apiFetch(...args) }));

import { UserAccessTab } from "./UserAccessTab";

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }));
}

describe("UserAccessTab (moved from AgentDetailPage)", () => {
  beforeEach(() => apiFetch.mockReset());

  it("loads and lists the agent's grants", async () => {
    apiFetch.mockImplementation(() =>
      json({
        grants: [
          {
            id: "g1",
            agent_id: "a1",
            user_email: "pat@example.com",
            user_name: "Pat Lee",
            status: "active",
            justification: null,
          },
        ],
      }),
    );
    render(<UserAccessTab agentId="a1" />);
    expect(await screen.findByText("pat@example.com")).toBeInTheDocument();
    expect(screen.getByText("Pat Lee")).toBeInTheDocument();
    expect(apiFetch).toHaveBeenCalledWith("/api/agents/a1/access/grants");
  });

  it("shows the empty state when there are no grants", async () => {
    apiFetch.mockImplementation(() => json({ grants: [] }));
    render(<UserAccessTab agentId="a1" />);
    expect(await screen.findByText("No user access grants yet")).toBeInTheDocument();
  });
});
