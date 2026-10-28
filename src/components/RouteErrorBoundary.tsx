import { Component, type ErrorInfo, type ReactNode } from "react";
import { useLocation } from "react-router-dom";

interface BoundaryProps {
  /** Changing this clears a caught error, so navigating away recovers. */
  resetKey: string;
  children: ReactNode;
}

interface BoundaryState {
  error: Error | null;
}

/** A failed lazy chunk usually means a new deploy replaced the old bundle. */
function isChunkLoadError(error: Error): boolean {
  return /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i.test(
    error.message,
  );
}

export class RouteErrorBoundaryInner extends Component<BoundaryProps, BoundaryState> {
  state: BoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): BoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Route failed to render", error, info.componentStack);
  }

  componentDidUpdate(prev: BoundaryProps) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null });
    }
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    const stale = isChunkLoadError(error);
    return (
      <div role="alert" className="flex-1 flex items-center justify-center py-32 px-6">
        <div className="max-w-md text-center">
          <p className="text-sm font-semibold text-slate-800">
            {stale ? "A new version of this app is available." : "This page failed to load."}
          </p>
          <p className="mt-1 text-sm text-slate-500">
            {stale ? "Reload to get it." : "Reload to try again, or go back to another page."}
          </p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="mt-4 text-xs font-bold bg-[#FF444F] hover:bg-[#e63d48] text-white px-3 py-1.5 rounded-md transition-colors"
          >
            Reload
          </button>
        </div>
      </div>
    );
  }
}

/** Catches errors from lazy route chunks and route components; resets on navigation. */
export default function RouteErrorBoundary({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  return <RouteErrorBoundaryInner resetKey={pathname}>{children}</RouteErrorBoundaryInner>;
}
