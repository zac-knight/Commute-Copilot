import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, type SpanNode, type TraceDetail, type TraceSummary } from "../api/types";
import { Button, Card, Icon, Spinner } from "../components/ui";
import { fmtMs, fmtTokens } from "../lib/format";
import { clip, failedCount, findPlan, flatten } from "../lib/waterfall";
import { useApp } from "../state/AppContext";

const KIND_CLS: Record<SpanNode["kind"], string> = {
  orchestrator: "bg-surface2 text-ink", agent: "bg-calendar-bg text-calendar", llm: "bg-transit-bg text-transit", tool: "bg-ok-bg text-ok",
};

function Preview({ label, text }: { label: string; text: string }) {
  const [full, setFull] = useState(false);
  if (!text) return null;
  const c = clip(text, 400);
  return (
    <div>
      <h4 className="text-xs font-bold uppercase tracking-wide text-muted">{label}</h4>
      <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-surface2 p-2 font-mono text-xs [overflow-wrap:anywhere]">{full ? text : c.text}{c.cut && !full ? "…" : ""}</pre>
      {c.cut && <button type="button" className="mt-1 text-xs font-semibold text-accent underline" onClick={() => setFull((f) => !f)}>{full ? "Show less" : `Show all ${text.length} characters`}</button>}
    </div>
  );
}

function Details({ n }: { n: SpanNode }) {
  const attrs = Object.entries(n.attributes).filter(([k]) => k !== "plan");
  return (
    <div className="space-y-3 border-t border-line bg-surface px-3 py-3 text-sm" data-testid="span-details">
      {n.error && <p role="alert" className="rounded-lg bg-bad-bg px-2.5 py-1.5 text-bad">{n.error}</p>}
      <Preview label={n.kind === "tool" ? "Tool input" : "Input"} text={n.input_preview} />
      <Preview label={n.kind === "tool" ? "Tool output" : "Output"} text={n.output_preview} />
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        {n.model && <><dt className="font-semibold text-muted">Model</dt><dd>{n.model}</dd></>}
        {(n.tokens_in > 0 || n.tokens_out > 0) && <><dt className="font-semibold text-muted">Tokens</dt><dd>{n.tokens_in} in / {n.tokens_out} out</dd></>}
        {attrs.map(([k, v]) => <Fragment key={k}><dt className="font-semibold text-muted">{k}</dt><dd className="font-mono [overflow-wrap:anywhere]">{typeof v === "string" ? v : JSON.stringify(v)}</dd></Fragment>)}
      </dl>
    </div>
  );
}

function SpanTree({ roots }: { roots: SpanNode[] }) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState<Set<string>>(new Set());
  const rows = useMemo(() => flatten(roots, collapsed), [roots, collapsed]);
  const flip = (set: Set<string>, id: string) => { const s = new Set(set); if (s.has(id)) s.delete(id); else s.add(id); return s; };
  return (
    <ul role="tree" aria-label="Spans" className="divide-y divide-line overflow-hidden rounded-xl border border-line" data-testid="span-tree">
      {rows.map(({ node: n, depth, leftPct, widthPct, durationMs, hasChildren }) => {
        const failed = n.status === "error";
        const expanded = open.has(n.span_id);
        return (
          <li key={n.span_id} role="treeitem" aria-expanded={hasChildren ? !collapsed.has(n.span_id) : undefined} aria-selected={expanded} data-failed={failed} className={failed ? "bg-bad-bg" : "bg-surface"}>
            <div className="flex items-center gap-2 px-2 py-1.5" style={{ paddingLeft: 8 + depth * 18 }}>
              {hasChildren ? (
                <button type="button" aria-label={collapsed.has(n.span_id) ? `Expand ${n.name}` : `Collapse ${n.name}`} onClick={() => setCollapsed((c) => flip(c, n.span_id))} className="grid size-6 shrink-0 place-items-center rounded text-muted hover:bg-surface2">
                  <span className={"transition " + (collapsed.has(n.span_id) ? "" : "rotate-90")}><Icon.Chevron /></span>
                </button>
              ) : <span className="size-6 shrink-0" />}
              <button type="button" onClick={() => setOpen((o) => flip(o, n.span_id))} aria-label={`Details for ${n.name}`} className="flex min-w-0 flex-1 items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-surface2">
                <span className={"shrink-0 rounded px-1.5 py-0.5 text-[12px] font-bold uppercase " + KIND_CLS[n.kind]}>{n.kind}</span>
                <span className="min-w-0 truncate text-sm font-semibold">{n.name}</span>
                {failed && <span className="shrink-0 rounded bg-bad px-1.5 py-0.5 text-[12px] font-bold text-accent-ink dark:text-bg">ERROR</span>}
              </button>
              <span className="hidden w-14 shrink-0 text-right text-xs text-muted sm:block">{n.tokens_in + n.tokens_out > 0 ? fmtTokens(n.tokens_in + n.tokens_out) + " tok" : ""}</span>
              <span className="w-16 shrink-0 text-right text-xs tabular-nums text-muted">{fmtMs(durationMs)}</span>
              <div aria-hidden className="relative hidden h-3 w-[34%] shrink-0 rounded bg-surface2 md:block">
                <div className={"absolute top-0 h-3 rounded " + (failed ? "bg-bad" : n.kind === "llm" ? "bg-accent" : n.kind === "tool" ? "bg-ok" : "bg-muted")} style={{ left: `${leftPct}%`, width: `${widthPct}%` }} />
              </div>
            </div>
            {expanded && <Details n={n} />}
          </li>
        );
      })}
    </ul>
  );
}

function TraceView({ detail }: { detail: TraceDetail }) {
  const plan = findPlan(detail.tree);
  const s = detail.summary;
  const wall = s.end != null ? (s.end - s.start) * 1000 : 0;
  const failed = failedCount(detail.tree);
  return (
    <div className="space-y-4" data-testid="trace-detail">
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[["Duration", fmtMs(wall)], ["Tokens in", fmtTokens(s.tokens_in)], ["Tokens out", fmtTokens(s.tokens_out)], ["Spans", `${s.span_count}${failed ? ` · ${failed} failed` : ""}`]].map(([k, v]) => (
          <div key={k} className="rounded-xl border border-line bg-surface px-3 py-2"><dt className="text-xs font-semibold text-muted">{k}</dt><dd className={"text-lg font-bold " + (k === "Spans" && failed ? "text-bad" : "")}>{v}</dd></div>
        ))}
      </dl>
      <p className="text-xs text-muted">Cost: $0. The model runs locally, so tokens and latency are the cost metrics.</p>
      {plan !== undefined && (
        <details className="rounded-xl border border-line bg-surface" data-testid="plan-json">
          <summary className="cursor-pointer px-3 py-2 text-sm font-semibold">Plan the router produced (JSON)</summary>
          <pre className="max-h-72 overflow-auto border-t border-line p-3 font-mono text-xs">{JSON.stringify(plan, null, 2)}</pre>
        </details>
      )}
      <div>
        <h3 className="mb-2 text-sm font-bold">Spans <span className="font-normal text-muted">(click a row for inputs and outputs)</span></h3>
        <SpanTree roots={detail.tree} />
      </div>
    </div>
  );
}

export function TracesPage() {
  const { api } = useApp();
  const { id } = useParams();
  const [list, setList] = useState<TraceSummary[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  // The result is keyed by the id it was fetched for, so "loading" and "the current detail" are DERIVED
  // (no synchronous setState in the effect, and a slow response for an old id is simply ignored).
  const [loaded, setLoaded] = useState<{ id: string; detail?: TraceDetail; error?: string } | null>(null);
  const loading = !!id && loaded?.id !== id;
  const detail = id && loaded?.id === id ? (loaded.detail ?? null) : null;
  const error = listError ?? (id && loaded?.id === id ? (loaded.error ?? null) : null);

  const load = useCallback(() => {
    api.traces(50).then((l) => { setList(l); setListError(null); }).catch((e: unknown) => setListError(e instanceof ApiError ? e.message : "Could not load traces"));
  }, [api]);
  useEffect(load, [load]);
  useEffect(() => {
    if (!id) return;
    let live = true;
    api.trace(id)
      .then((d) => { if (live) setLoaded({ id, detail: d }); })
      .catch((e: unknown) => { if (live) setLoaded({ id, error: e instanceof ApiError ? e.message : "Could not load that trace" }); });
    return () => { live = false; };
  }, [api, id]);

  return (
    <div className="mx-auto grid w-full max-w-7xl gap-6 px-4 py-4 lg:grid-cols-[18rem_minmax(0,1fr)]">
      <section aria-labelledby="req-h" className="space-y-2">
        <h1 className="sr-only">Traces</h1>
        <div className="flex items-center"><h2 id="req-h" className="text-base font-bold">Requests</h2><Button variant="ghost" className="ml-auto" onClick={load}>Refresh</Button></div>
        {list === null && !error && <p className="text-sm text-muted"><Spinner /> Loading…</p>}
        {list?.length === 0 && <Card className="p-4 text-sm text-muted">No requests yet. Ask something in Chat, then come back.</Card>}
        <ul className="space-y-1.5">
          {list?.map((t) => (
            <li key={t.request_id}>
              <Link to={`/traces/${t.request_id}`} aria-current={t.request_id === id ? "page" : undefined}
                className={"block rounded-xl border px-3 py-2 text-sm " + (t.request_id === id ? "border-accent bg-surface" : "border-line bg-surface hover:bg-surface2")}>
                <span className="flex items-center gap-2"><span className="font-mono text-xs">{t.request_id.slice(0, 8)}</span>{t.error_count > 0 && <span className="rounded bg-bad-bg px-1.5 text-xs font-bold text-bad">{t.error_count} failed</span>}</span>
                <span className="text-xs text-muted">{new Date(t.start * 1000).toLocaleTimeString("en-AU", { timeZone: "Australia/Brisbane", hour12: false })} · {t.span_count} spans · {fmtTokens(t.tokens_in + t.tokens_out)} tok</span>
              </Link>
            </li>
          ))}
        </ul>
      </section>
      <section aria-labelledby="trace-h" className="min-w-0">
        <h2 id="trace-h" className="mb-3 text-base font-bold">{id ? <>Trace <span className="font-mono text-sm font-normal">{id.slice(0, 12)}</span></> : "Trace"}</h2>
        {error && <p role="alert" className="rounded-lg bg-bad-bg px-3 py-2 text-sm text-bad">{error}</p>}
        {loading && <p className="text-sm text-muted"><Spinner /> Loading trace…</p>}
        {!id && !error && <Card className="p-6 text-sm text-muted">Pick a request on the left to see its plan, every model call and tool call, with timings.</Card>}
        {detail && !loading && <TraceView detail={detail} />}
      </section>
    </div>
  );
}
