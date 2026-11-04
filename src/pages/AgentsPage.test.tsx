import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { AgentRecord } from "@/data/agents";
import { AgentCard } from "./AgentsPage";

// What /api/agents returns for an agent the viewer has no access to
// (agent_to_public_dict): no skill_ids or channel_ids.
const publicPlatformAgent = {
  id: "a1",
  name: "Platform",
  description: "The platform-level Slack agent.",
  avatar_url: null,
  status: "active",
  agent_type: "platform",
  trigger_mode: "mention",
  creation_method: "manual",
  is_platform_agent: true,
  created_at: "2026-10-28T14:54:30",
  updated_at: "2026-10-28T14:54:30",
  has_signing_secret: false,
  has_gateway_api_key: false,
  my_access: "none",
} as unknown as AgentRecord;

function renderCard(agent: AgentRecord) {
  return render(
    <MemoryRouter>
      <AgentCard
        agent={agent}
        manifestSyncAvailable={false}
        appName="Platform"
        slackCollaborator=""
        onRequestAccess={vi.fn()}
        onClick={vi.fn()}
      />
    </MemoryRouter>,
  );
}

describe("AgentCard", () => {
  it("renders an agent the viewer has no access to", () => {
    renderCard(publicPlatformAgent);
    expect(screen.getByText("Platform")).toBeInTheDocument();
    expect(screen.queryByText(/skill/)).not.toBeInTheDocument();
  });

  it("shows skill and channel counts when they are present", () => {
    renderCard({ ...publicPlatformAgent, skill_ids: ["a", "b"], channel_ids: ["#ops", "#eng"] });
    expect(screen.getByText("2 skills")).toBeInTheDocument();
    expect(screen.getByText(/ops/)).toHaveTextContent("ops +1");
  });
});
