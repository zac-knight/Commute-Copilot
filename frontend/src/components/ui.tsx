import type { ButtonHTMLAttributes, ReactNode } from "react";
import type { AgentName, StepStatus } from "../api/types";
import type { Block, Inline } from "../lib/markdown";

const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");

/* ---- icons (inline SVG, decorative: aria-hidden) ---- */
const base = { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true } as const;
export const Icon = {
  Bus: () => <svg {...base}><rect x="4" y="3" width="16" height="14" rx="2" /><path d="M4 11h16M8 21v-4M16 21v-4M8 14h.01M16 14h.01" /></svg>,
  Cloud: () => <svg {...base}><path d="M17.5 19a4.5 4.5 0 1 0-1.4-8.8A6 6 0 1 0 6 18.5" /><path d="M6 19h11.5" /></svg>,
  Calendar: () => <svg {...base}><rect x="3" y="5" width="18" height="16" rx="2" /><path d="M16 3v4M8 3v4M3 11h18" /></svg>,
  Check: () => <svg {...base}><path d="M20 6 9 17l-5-5" /></svg>,
  X: () => <svg {...base}><path d="M18 6 6 18M6 6l12 12" /></svg>,
  Minus: () => <svg {...base}><path d="M5 12h14" /></svg>,
  Clock: () => <svg {...base}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>,
  Sun: () => <svg {...base}><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>,
  Moon: () => <svg {...base}><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></svg>,
  Send: () => <svg {...base}><path d="m22 2-7 20-4-9-9-4z" /><path d="M22 2 11 13" /></svg>,
  Stop: () => <svg {...base}><rect x="6" y="6" width="12" height="12" rx="1" /></svg>,
  Chevron: () => <svg {...base}><path d="m9 6 6 6-6 6" /></svg>,
  Spark: () => <svg {...base}><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" /><path d="M19 17l.8 2.2L22 20l-2.2.8L19 23l-.8-2.2L16 20l2.2-.8z" /></svg>,
  Chat: () => <svg {...base}><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z" /></svg>,
  Grid: () => <svg {...base}><rect x="3" y="3" width="7" height="7" rx="1.5" /><rect x="14" y="3" width="7" height="7" rx="1.5" /><rect x="3" y="14" width="7" height="7" rx="1.5" /><rect x="14" y="14" width="7" height="7" rx="1.5" /></svg>,
  Chart: () => <svg {...base}><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></svg>,
  Flask: () => <svg {...base}><path d="M9 3h6M10 3v6L4.5 19a1.5 1.5 0 0 0 1.3 2.2h12.4a1.5 1.5 0 0 0 1.3-2.2L14 9V3" /><path d="M7.5 15h9" /></svg>,
  Trace: () => <svg {...base}><path d="M3 6h10M7 12h14M5 18h9" /></svg>,
  Gear: () => <svg {...base}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" /></svg>,
  Play: () => <svg {...base}><path d="M6 4l14 8-14 8z" /></svg>,
  Map: () => <svg {...base}><path d="m9 4-6 2v14l6-2 6 2 6-2V4l-6 2z" /><path d="M9 4v14M15 6v14" /></svg>,
  Shield: () => <svg {...base}><path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z" /></svg>,
};

export function Spinner({ className = "" }: { className?: string }) {
  return <span aria-hidden className={cx("inline-block size-3.5 animate-spin rounded-full border-2 border-current border-t-transparent", className)} />;
}

/* ---- agent + status ---- */
const AGENTS: Record<AgentName, { label: string; cls: string; icon: ReactNode }> = {
  transit: { label: "Transit", cls: "bg-transit-bg text-transit", icon: <Icon.Bus /> },
  weather: { label: "Weather", cls: "bg-weather-bg text-weather", icon: <Icon.Cloud /> },
  calendar: { label: "Calendar", cls: "bg-calendar-bg text-calendar", icon: <Icon.Calendar /> },
};
export const agentLabel = (a: AgentName) => AGENTS[a].label;

export function AgentBadge({ agent }: { agent: AgentName }) {
  const a = AGENTS[agent];
  return <span className={cx("inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold", a.cls)}>{a.icon}{a.label}</span>;
}

const STATUS: Record<StepStatus, { label: string; cls: string; icon: ReactNode }> = {
  pending: { label: "Waiting", cls: "bg-surface2 text-muted border border-dashed border-line", icon: <Icon.Clock /> },
  running: { label: "Working", cls: "bg-warn-bg text-warn", icon: <Spinner /> },
  ok: { label: "Done", cls: "bg-ok-bg text-ok", icon: <Icon.Check /> },
  failed: { label: "Failed", cls: "bg-bad-bg text-bad", icon: <Icon.X /> },
  skipped: { label: "Skipped", cls: "bg-surface2 text-muted", icon: <Icon.Minus /> },
};
export function StatusChip({ status }: { status: StepStatus }) {
  const s = STATUS[status];
  return <span data-status={status} className={cx("inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold", s.cls)}>{s.icon}<span>{s.label}</span></span>;
}

/* ---- buttons ---- */
type Variant = "primary" | "secondary" | "danger" | "ghost";
const VARIANT: Record<Variant, string> = {
  primary: "btn-brand hover:brightness-110",
  secondary: "border border-line bg-surface text-ink hover:bg-surface2",
  danger: "border border-bad text-bad hover:bg-bad-bg",
  ghost: "text-muted hover:bg-surface2 hover:text-ink",
};
export function Button({ variant = "secondary", className, ...p }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return <button type="button" {...p} className={cx("inline-flex min-h-9 items-center justify-center gap-2 rounded-lg px-3.5 py-1.5 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-50", VARIANT[variant], className)} />;
}

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cx("glass rounded-2xl shadow-lg shadow-black/10", className)}>{children}</div>;
}

/* ---- safe markdown rendering: text nodes only, never HTML ---- */
function Inlines({ parts }: { parts: Inline[] }) {
  return <>{parts.map((p, i) => (p.t === "b" ? <strong key={i}>{p.v}</strong> : <span key={i}>{p.v}</span>))}</>;
}
export function Markdown({ blocks }: { blocks: Block[] }) {
  return (
    <div className="space-y-2 leading-relaxed [overflow-wrap:anywhere]">
      {blocks.map((b, i) => {
        if (b.type === "ul") return <ul key={i} className="list-disc space-y-1 pl-5">{b.items.map((it, j) => <li key={j}><Inlines parts={it} /></li>)}</ul>;
        if (b.type === "ol") return <ol key={i} className="list-decimal space-y-1 pl-5">{b.items.map((it, j) => <li key={j}><Inlines parts={it} /></li>)}</ol>;
        return <p key={i}>{b.lines.map((l, j) => <span key={j}>{j > 0 && <br />}<Inlines parts={l} /></span>)}</p>;
      })}
    </div>
  );
}
