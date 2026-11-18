import type { AgentCard, AgentName } from "../api/types";
import { Icon } from "../components/ui";
import { Empty } from "../components/charts";
import { fmtTokens } from "../lib/format";
import { useApp } from "../state/AppContext";
import { useFetch } from "../state/useFetch";

const STYLE: Record<AgentName, { icon: React.ReactNode; text: string; glow: string; bg: string }> = {
  transit: { icon: <Icon.Bus />, text: "text-transit", glow: "glow-transit", bg: "bg-transit-bg" },
  weather: { icon: <Icon.Cloud />, text: "text-weather", glow: "glow-weather", bg: "bg-weather-bg" },
  calendar: { icon: <Icon.Calendar />, text: "text-calendar", glow: "glow-calendar", bg: "bg-calendar-bg" },
};

function Card({ a }: { a: AgentCard }) {
  const s = STYLE[a.name];
  return (
    <article className={"glass rise p-4 " + (a.available ? s.glow : "opacity-60")} data-testid={`agent-card-${a.name}`}>
      <header className="flex items-center gap-3">
        <span className={"grid size-9 place-items-center border border-line " + s.bg + " " + s.text}>{s.icon}</span>
        <div>
          <h2 className="text-base font-extrabold uppercase tracking-wider">{a.title}</h2>
          <p className="font-mono text-[12px] text-muted">{a.prompt_version}</p>
        </div>
        <span className={"ml-auto rounded-full px-2.5 py-1 text-xs font-bold " + (a.available ? "bg-ok-bg text-ok" : "bg-bad-bg text-bad")}>{a.available ? "available" : "unavailable"}</span>
      </header>
      <p className="mt-2 text-[13px]">{a.summary}</p>
      <p className="mt-2 text-xs text-muted"><span className="font-semibold text-ink">Data source:</span> {a.source}</p>
      <h3 className="label mb-1.5 mt-3">Tools · permissions enforced in code</h3>
      <ul className="divide-y divide-line border border-line">
        {a.tools.map((t) => (
          <li key={t.name} className="flex items-center gap-2 px-2.5 py-1.5" title={t.description}>
            <code className="mono text-[12px] font-bold">{t.name}</code>
            <span className="mono ml-1 truncate text-[12px] text-muted">{t.calls_24h ? `${t.calls_24h} calls/24h` : ""}</span>
            <span className={"mono ml-auto shrink-0 text-[12px] font-bold uppercase " + (t.access === "read" ? "text-ok" : "text-warn")}>{t.access === "read" ? "read-only" : "proposes · you approve"}</span>
          </li>
        ))}
      </ul>
      <dl className="mt-4 grid grid-cols-3 gap-2 text-center">
        {[["runs / 24 h", String(a.calls_24h)], ["mean time", a.mean_s_24h ? `${a.mean_s_24h.toFixed(1)} s` : "–"], ["tokens", fmtTokens(a.tokens_in_24h + a.tokens_out_24h)]].map(([k, v]) => (
          <div key={k} className="rounded-xl bg-surface2/50 p-2"><dd className="text-lg font-extrabold tabular-nums">{v}</dd><dt className="text-[12px] uppercase tracking-wider text-muted">{k}</dt></div>
        ))}
      </dl>
    </article>
  );
}

/** Static architecture diagram: the only path to a write is through the approval gate. */
function Architecture() {
  const box = (x: number, y: number, w: number, label: string, sub: string, color: string, fill: string) => (
    <g transform={`translate(${x} ${y})`}><rect width={w} height={52} rx={3} fill={fill} stroke={color} /><text x={w / 2} y={22} textAnchor="middle" fontSize={13} fontWeight={700} fill={color}>{label}</text><text x={w / 2} y={39} textAnchor="middle" fontSize={10} fill="var(--muted)">{sub}</text></g>
  );
  const arrow = (d: string, dashed = false) => <path d={d} fill="none" stroke="var(--border)" strokeWidth={1.6} strokeDasharray={dashed ? "5 4" : undefined} markerEnd="url(#ah)" />;
  return (
    <svg role="img" aria-label="Architecture: React UI to FastAPI to Orchestrator, which routes to the Transit, Weather and Calendar agents. Calendar writes go through an approval gate to the executor." viewBox="0 0 860 330" className="w-full">
      <defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 10 5 0 10z" fill="var(--muted)" /></marker></defs>
      {box(10, 20, 130, "React UI", "chat · traces · eval", "var(--accent)", "var(--surface-2)")}
      {box(190, 20, 140, "FastAPI", "SSE · REST · typed", "var(--accent)", "var(--surface-2)")}
      {box(380, 20, 170, "Orchestrator", "router → plan → merge", "var(--ok)", "var(--ok-bg)")}
      {box(600, 20, 150, "Synthesiser", "facts from results only", "var(--ok)", "var(--ok-bg)")}
      {box(150, 150, 180, "Transit agent", "GTFS + GTFS-RT", "var(--transit)", "var(--transit-bg)")}
      {box(370, 150, 180, "Weather agent", "Open-Meteo", "var(--weather)", "var(--weather-bg)")}
      {box(590, 150, 180, "Calendar agent", "read · propose", "var(--calendar)", "var(--calendar-bg)")}
      {box(590, 260, 180, "Approval gate", "hash · expiry · human", "var(--warn)", "var(--warn-bg)")}
      {box(380, 260, 170, "Executor", "only writer", "var(--bad)", "var(--bad-bg)")}
      {box(10, 150, 110, "Trace store", "spans · tokens", "var(--muted)", "var(--surface-2)")}
      {arrow("M140 46H190")}{arrow("M330 46H380")}{arrow("M550 46H600")}
      {arrow("M440 72V110H240V150")}{arrow("M465 72V150")}{arrow("M490 72V110H680V150")}
      {arrow("M680 202V260")}{arrow("M590 286H550")}
      {arrow("M440 72Q300 100 65 150", true)}
      <text x={692} y={236} fontSize={10} fill="var(--warn)">proposal</text>
      <text x={570} y={252} fontSize={10} fill="var(--bad)" textAnchor="middle">approved only</text>
    </svg>
  );
}

export function AgentsPage() {
  const { api } = useApp();
  const { data, error, loading } = useFetch(() => api.agents(), [api]);
  return (
    <div className="mx-auto w-full max-w-[1760px] space-y-4 px-5 py-4">
      <header>
        <h1 className="page-title">Agents</h1>
        <p className="text-sm text-muted">Three specialists, composed only by the orchestrator. Agents never call each other and cannot reach the write path.</p>
      </header>
      {error && <p role="alert" className="rounded-xl bg-bad-bg px-4 py-2 text-sm text-bad">{error}</p>}
      {loading && !data && <p className="text-sm text-muted">Loading…</p>}
      {data && <div className="grid gap-5 lg:grid-cols-3">{data.map((a) => <Card key={a.name} a={a} />)}</div>}
      {data?.every((a) => a.calls_24h === 0) && <Empty title="No usage in the last 24 hours">Usage and latency fill in from the trace store as you ask questions.</Empty>}
      <section className="glass rounded-2xl p-5">
        <h2 className="text-base font-extrabold">How a request flows</h2>
        <p className="mb-2 text-xs text-muted">The model decides which tools to call; deterministic code owns everything that must be true (journey planning, advice thresholds, free-slot maths, approvals).</p>
        <Architecture />
      </section>
    </div>
  );
}
