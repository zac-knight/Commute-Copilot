import type { StepView } from "../lib/chat";
import { layoutGraph, NODE_H, NODE_W, type GNode } from "../lib/graph";

const COLOR: Record<string, string> = {
  transit: "var(--transit)", weather: "var(--weather)", calendar: "var(--calendar)",
  router: "var(--accent)", synth: "var(--ok)", user: "var(--muted)",
};
const FILL: Record<string, string> = {
  transit: "var(--transit-bg)", weather: "var(--weather-bg)", calendar: "var(--calendar-bg)",
  router: "var(--surface-2)", synth: "var(--ok-bg)", user: "var(--surface-2)",
};
const STATUS_TEXT: Record<string, string> = { idle: "waiting", pending: "waiting", running: "working", ok: "done", failed: "failed", skipped: "skipped" };

function Node({ n }: { n: GNode }) {
  const key = n.agent ?? n.kind;
  const c = COLOR[key]!;
  const dim = n.status === "idle" || n.status === "pending" || n.status === "skipped";
  return (
    <g transform={`translate(${n.x} ${n.y})`} opacity={dim ? 0.55 : 1} data-node={n.id} data-status={n.status}>
      {n.status === "running" && <rect className="ping-soft" width={NODE_W} height={NODE_H} rx={3} fill="none" stroke={c} strokeWidth={2} />}
      <rect width={NODE_W} height={NODE_H} rx={3} fill={FILL[key]} stroke={n.status === "failed" ? "var(--bad)" : c} strokeWidth={n.status === "running" ? 2 : 1.25} strokeDasharray={n.status === "skipped" ? "4 3" : undefined} />
      <text x={12} y={19} fontSize={12.5} fontWeight={700} fill={n.status === "failed" ? "var(--bad)" : c}>{n.label}</text>
      <text x={12} y={35} fontSize={10.5} fill="var(--muted)" className="font-mono">{(n.sub ?? "").slice(0, 21)}</text>
      <circle cx={NODE_W - 14} cy={16} r={4.5} fill={n.status === "ok" ? "var(--ok)" : n.status === "failed" ? "var(--bad)" : n.status === "running" ? "var(--warn)" : "var(--border)"} />
      <title>{`${n.label}: ${STATUS_TEXT[n.status]}`}</title>
    </g>
  );
}

/** The request as a live graph: nodes light up as the router plans and each specialist works. */
export function AgentGraph({ steps, direct, streaming, planned, finished, vertical = true }: { steps: StepView[]; direct: boolean; streaming: boolean; planned: boolean; finished: boolean; vertical?: boolean }) {
  const g = layoutGraph({ steps, direct, streaming, planned, finished, vertical });
  const at = new Map(g.nodes.map((n) => [n.id, n]));
  return (
    <div className="overflow-x-auto border border-line bg-bg/40 p-2" data-testid="agent-graph">
      <svg role="img" aria-label={"Request flow: " + g.nodes.map((n) => `${n.label} ${STATUS_TEXT[n.status]}`).join(", ")} viewBox={`0 0 ${g.width} ${g.height}`} width={g.width} height={g.height} className="mx-auto block max-w-full">
        {g.edges.map((e) => {
          const a = at.get(e.from)!, b = at.get(e.to)!;
          const d = g.vertical
            ? (() => { const x1 = a.x + NODE_W / 2, y1 = a.y + NODE_H, x2 = b.x + NODE_W / 2, y2 = b.y, my = (y1 + y2) / 2; return `M${x1} ${y1} C${x1} ${my} ${x2} ${my} ${x2} ${y2}`; })()
            : (() => { const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2, mx = (x1 + x2) / 2; return `M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}`; })();
          return <path key={e.from + e.to} d={d} fill="none" strokeWidth={e.active ? 2.25 : 1.5}
            stroke={e.active ? "var(--accent)" : e.done ? "var(--ok)" : "var(--border)"} className={e.active ? "flow-edge" : undefined} opacity={e.active || e.done ? 1 : 0.7} />;
        })}
        {g.nodes.map((n) => <Node key={n.id} n={n} />)}
      </svg>
    </div>
  );
}
