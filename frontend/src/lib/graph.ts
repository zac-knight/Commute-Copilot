/** Layout and live status for the agent graph: user -> router -> specialists (by dependency layer)
 *  -> synthesiser. Pure functions of the streamed steps, so the picture is unit-tested. */
import type { AgentName, StepStatus } from "../api/types";
import type { StepView } from "./chat";

export type NodeKind = "user" | "router" | "agent" | "synth";
export type NodeStatus = StepStatus | "idle";
export interface GNode { id: string; kind: NodeKind; label: string; sub?: string; agent?: AgentName; status: NodeStatus; x: number; y: number }
export interface GEdge { from: string; to: string; active: boolean; done: boolean }
export interface Graph { nodes: GNode[]; edges: GEdge[]; width: number; height: number; vertical: boolean }

export const NODE_W = 132;
export const NODE_H = 46;
const COL = 168;
const ROW = 62;
const VGAP = 16; // horizontal gap between nodes of one layer (vertical layout)
const VROW = 62; // vertical distance between layers
const PAD = 12;

export interface GraphInput {
  steps: StepView[];
  /** The user picked an agent: the router is skipped. */
  direct: boolean;
  /** The request is still running (no final event yet). */
  streaming: boolean;
  /** A plan has arrived. */
  planned: boolean;
  /** The final answer arrived. */
  finished: boolean;
  /** Stack the layers top to bottom (for a narrow side panel) instead of left to right. */
  vertical?: boolean;
}

/** Dependency depth of each step (0 = no dependencies). Cycles cannot occur (the planner rejects them),
 *  but are guarded anyway. */
export function depths(steps: StepView[]): Map<string, number> {
  const by = new Map(steps.map((s) => [s.id, s]));
  const memo = new Map<string, number>();
  const go = (id: string, seen: Set<string>): number => {
    if (memo.has(id)) return memo.get(id)!;
    const s = by.get(id);
    if (!s || seen.has(id)) return 0;
    seen.add(id);
    const d = s.dependsOn.length ? 1 + Math.max(...s.dependsOn.map((x) => go(x, seen))) : 0;
    memo.set(id, d);
    return d;
  };
  for (const s of steps) go(s.id, new Set());
  return memo;
}

export function layoutGraph(g: GraphInput): Graph {
  const dep = depths(g.steps);
  const layers: StepView[][] = [];
  for (const s of g.steps) (layers[dep.get(s.id) ?? 0] ??= []).push(s);
  const real = layers.filter(Boolean);
  const maxRows = Math.max(1, ...real.map((l) => l.length));
  const rowY = (i: number, n: number) => PAD + ((maxRows - n) * ROW) / 2 + i * ROW;
  const V = g.vertical === true;
  // place(col, i, n): col = layer index, i = position within the layer, n = layer size
  const place = (col: number, i: number, n: number): { x: number; y: number } =>
    V ? { x: PAD + ((maxRows - n) * (NODE_W + VGAP)) / 2 + i * (NODE_W + VGAP), y: PAD + col * VROW }
      : { x: PAD + COL * col, y: rowY(i, n) };

  const allDone = g.steps.length > 0 && g.steps.every((s) => s.status === "ok" || s.status === "failed" || s.status === "skipped");
  const nodes: GNode[] = [];
  nodes.push({ id: "user", kind: "user", label: "You", status: "ok", ...place(0, 0, 1) });
  nodes.push({
    id: "router", kind: "router", label: g.direct ? "Router" : "Router", sub: g.direct ? "skipped (you chose)" : "plans the steps",
    status: g.direct ? "skipped" : g.planned ? "ok" : g.streaming ? "running" : "idle", ...place(1, 0, 1),
  });
  real.forEach((layer, li) => layer.forEach((s, i) => nodes.push({
    id: s.id, kind: "agent", agent: s.agent, label: s.agent[0]!.toUpperCase() + s.agent.slice(1), sub: s.toolCalls.length ? [...new Set(s.toolCalls)].slice(0, 2).join(", ") : s.id,
    status: s.status, ...place(2 + li, i, layer.length),
  })));
  const synthCol = 2 + real.length;
  nodes.push({
    id: "synth", kind: "synth", label: "Synthesiser", sub: "writes the answer",
    status: g.finished ? "ok" : g.streaming && allDone ? "running" : "idle", ...place(synthCol, 0, 1),
  });

  const status = new Map(nodes.map((n) => [n.id, n.status]));
  const edges: GEdge[] = [];
  const link = (from: string, to: string) => edges.push({
    from, to, active: status.get(to) === "running", done: status.get(from) === "ok" && (status.get(to) === "ok" || status.get(to) === "skipped"),
  });
  link("user", "router");
  const hasDependents = new Set(g.steps.flatMap((s) => s.dependsOn));
  for (const s of g.steps) {
    if (s.dependsOn.length === 0) link("router", s.id);
    for (const d of s.dependsOn) link(d, s.id);
    if (!hasDependents.has(s.id)) link(s.id, "synth");
  }
  if (g.steps.length === 0) link("router", "synth");
  if (V) {
    return { nodes, edges, width: PAD * 2 + maxRows * NODE_W + (maxRows - 1) * VGAP, height: PAD * 2 + VROW * synthCol + NODE_H, vertical: true };
  }
  const width = PAD * 2 + COL * synthCol + NODE_W;
  return { nodes, edges, width, height: PAD * 2 + ROW * (maxRows - 1) + NODE_H, vertical: false };
}
