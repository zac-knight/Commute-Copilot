import { Component, type ErrorInfo, type ReactNode } from "react";

/** A render error in one page shows a message (and a way out) instead of a blank screen. */
export class ErrorBoundary extends Component<{ children: ReactNode; resetKey?: string }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error("UI render error", error, info.componentStack); }
  componentDidUpdate(prev: { resetKey?: string }) { if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null }); }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div role="alert" className="mono m-5 border border-bad bg-bad-bg p-4 text-sm text-bad">
        <p className="font-bold">This page hit an error and could not render.</p>
        <p className="mt-1 break-words text-xs">{this.state.error.message}</p>
        <p className="mt-2 text-xs text-muted">If you just updated the code, stop the backend (Ctrl-C) and run ./start.sh again, then reload.</p>
      </div>
    );
  }
}
