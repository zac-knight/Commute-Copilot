import { Link } from "react-router-dom";
import { planLabel, type AssistantMessage, type ChatMessage } from "../lib/chat";
import { fmtMs, fmtTokens } from "../lib/format";
import { parseMarkdown } from "../lib/markdown";
import { PlanView } from "./Plan";
import { useMedia } from "../state/useMedia";
import { TripCard } from "./TripCard";
import { Icon, Markdown, Spinner } from "./ui";

const SOURCE: Record<string, string> = {
  transit: "TransLink GTFS + GTFS-Realtime",
  weather: "Open-Meteo forecast",
  calendar: "your calendar (local)",
};

const BADGE: Partial<Record<AssistantMessage["status"], { text: string; cls: string }>> = {
  clarify: { text: "Question for you", cls: "bg-warn-bg text-warn" },
  out_of_scope: { text: "Out of scope", cls: "bg-surface2 text-muted" },
  partial: { text: "Partly done", cls: "bg-warn-bg text-warn" },
  failed: { text: "Could not complete", cls: "bg-bad-bg text-bad" },
  error: { text: "Error", cls: "bg-bad-bg text-bad" },
  stopped: { text: "Stopped", cls: "bg-surface2 text-muted" },
};

function Assistant({ m, onJumpToApprovals }: { m: AssistantMessage; onJumpToApprovals: () => void }) {
  const desktop = useMedia("(min-width: 1024px)"); // desktop shows trips in the Result pane instead
  const streaming = m.status === "streaming";
  const badge = BADGE[m.status];
  return (
    <article className="flex gap-3" aria-label="Assistant reply" data-testid="assistant-message" data-status={m.status}>
      <div aria-hidden className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-full grad-violet"><Icon.Spark /></div>
      <div className="min-w-0 flex-1 space-y-3">
        <PlanView label={planLabel(m.plan)} steps={m.steps} streaming={streaming} />
        {streaming && m.steps.length === 0 && <p className="flex items-center gap-2 text-sm text-muted"><Spinner /> Working out who can help…</p>}
        {m.text && (
          <div className="rise rounded-2xl rounded-tl-md border border-line bg-surface px-4 py-3 shadow-sm" data-testid="answer">
            {badge && <span className={"mb-2 inline-block rounded-full px-2.5 py-0.5 text-xs font-semibold " + badge.cls}>{badge.text}</span>}
            <Markdown blocks={parseMarkdown(m.text)} />
          </div>
        )}
        {streaming && m.liveText && !m.text && (
          <div className="rounded-2xl rounded-tl-md border border-line bg-surface px-4 py-3" data-testid="live-answer"><p className="whitespace-pre-wrap text-[13.5px] leading-relaxed">{m.liveText}</p></div>
        )}
        {!desktop && <div className="space-y-3">{m.trips.map((t, i) => <TripCard key={i} art={t} />)}</div>}
        {m.error && <p role="alert" className="rounded-lg bg-bad-bg px-3 py-2 text-sm text-bad">{m.error}</p>}
        {m.pendingApprovals.length > 0 && (
          <button type="button" onClick={onJumpToApprovals} className="flex items-center gap-2 rounded-lg bg-warn-bg px-3 py-2 text-sm font-semibold text-warn">
            <Icon.Shield /> {m.pendingApprovals.length} change{m.pendingApprovals.length > 1 ? "s" : ""} waiting for your approval
          </button>
        )}
        {!streaming && m.steps.length > 0 && (
          <p className="flex flex-wrap items-center gap-1.5 text-[12px]" aria-label="Data sources">
            <span className="text-muted">Sources:</span>
            {[...new Set(m.steps.filter((s) => s.status === "ok").map((s) => s.agent))].map((a) => (
              <span key={a} className="rounded-full border border-line px-2 py-0.5 text-muted">{SOURCE[a]}</span>
            ))}
          </p>
        )}
        {!streaming && (m.requestId || m.durationMs > 0) && (
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
            {m.durationMs > 0 && <span>{fmtMs(m.durationMs)}</span>}
            {m.tokensIn + m.tokensOut > 0 && <span>{fmtTokens(m.tokensIn)} in / {fmtTokens(m.tokensOut)} out tokens</span>}
            <span>local model · no API cost</span>
            {m.requestId && <Link className="font-semibold text-accent underline underline-offset-2" to={`/traces/${m.requestId}`}>View trace</Link>}
          </p>
        )}
      </div>
    </article>
  );
}

export function MessageView({ m, onJumpToApprovals }: { m: ChatMessage; onJumpToApprovals: () => void }) {
  if (m.kind === "user") {
    return (
      <div className="flex justify-end" data-testid="user-message">
        <p className="max-w-[85%] grad-violet rounded-2xl rounded-br-md px-4 py-2.5 text-[14px] shadow-md [overflow-wrap:anywhere]">{m.text}</p>
      </div>
    );
  }
  if (m.kind === "note") {
    const tone = m.tone === "ok" ? "bg-ok-bg text-ok" : m.tone === "bad" ? "bg-bad-bg text-bad" : "bg-surface2 text-muted";
    return <p role="status" data-testid="note" className={"mx-auto flex max-w-[90%] items-center gap-2 rounded-full px-4 py-1.5 text-sm font-medium " + tone}>{m.tone === "ok" && <Icon.Check />}{m.text}</p>;
  }
  return <Assistant m={m} onJumpToApprovals={onJumpToApprovals} />;
}
