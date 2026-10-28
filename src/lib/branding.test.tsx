import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BrandingProvider, useBranding } from "./branding";

function SetupState() {
  const { setup_complete } = useBranding();
  return <span data-testid="setup">{String(setup_complete)}</span>;
}

describe("BrandingProvider", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("does not report setup as unfinished before the config loads", async () => {
    let answer: (value: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { answer = resolve; })));

    render(
      <BrandingProvider>
        <SetupState />
      </BrandingProvider>,
    );

    // Before the response: unknown, so SetupAdminGate does not redirect.
    expect(screen.getByTestId("setup")).toHaveTextContent("undefined");

    answer(new Response(JSON.stringify({ setup_complete: true })));
    await waitFor(() => expect(screen.getByTestId("setup")).toHaveTextContent("true"));
  });
});
