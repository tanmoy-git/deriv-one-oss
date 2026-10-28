import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RouteErrorBoundaryInner } from "./RouteErrorBoundary";

function Boom({ message }: { message: string }): never {
  throw new Error(message);
}

describe("RouteErrorBoundary", () => {
  it("renders children when nothing throws", () => {
    render(
      <RouteErrorBoundaryInner resetKey="/a">
        <p>page body</p>
      </RouteErrorBoundaryInner>,
    );
    expect(screen.getByText("page body")).toBeInTheDocument();
  });

  it("shows a reload prompt instead of a blank screen when a route throws", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <RouteErrorBoundaryInner resetKey="/a">
        <Boom message="render failed" />
      </RouteErrorBoundaryInner>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("This page failed to load.");
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });

  it("says a new version is available when a lazy chunk fails to load", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <RouteErrorBoundaryInner resetKey="/a">
        <Boom message="Failed to fetch dynamically imported module: /assets/AgentsPage-abc.js" />
      </RouteErrorBoundaryInner>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("A new version of this app is available.");
  });

  it("recovers when the route changes", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const { rerender } = render(
      <RouteErrorBoundaryInner resetKey="/broken">
        <Boom message="render failed" />
      </RouteErrorBoundaryInner>,
    );
    rerender(
      <RouteErrorBoundaryInner resetKey="/fine">
        <p>other page</p>
      </RouteErrorBoundaryInner>,
    );
    expect(screen.getByText("other page")).toBeInTheDocument();
  });
});
