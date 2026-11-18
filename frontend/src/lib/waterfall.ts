/** Layout for the trace viewer: flatten the span tree and place each span on a shared time axis. */
import type { SpanNode } from "../api/types";

export interface Row { node: SpanNode; depth: number; leftPct: number; widthPct: number; durationMs: number; hasChildren: boolean }

export function durationOf(n: SpanNode): number {
  return Math.max(0, ((n.end ?? n.start) - n.start) * 1000);
}

export function bounds(roots: SpanNode[]): { start: number; end: number } {
  let start = Infinity, end = -Infinity;
  const walk = (n: SpanNode) => {
    start = Math.min(start, n.start);
    end = Math.max(end, n.end ?? n.start);
    n.children.forEach(walk);
  };
  roots.forEach(walk);
  return Number.isFinite(start) ? { start, end } : { start: 0, end: 0 };
}

/** Rows in display order; children of ``collapsed`` span ids are hidden. */
export function flatten(roots: SpanNode[], collapsed: ReadonlySet<string> = new Set()): Row[] {
  const { start, end } = bounds(roots);
  const total = Math.max(end - start, 1e-9);
  const rows: Row[] = [];
  const walk = (n: SpanNode, depth: number) => {
    const rawWidth = (((n.end ?? n.start) - n.start) / total) * 100;
    const widthPct = Math.min(Math.max(rawWidth, 0.8), 100); // a span is always visible
    const leftPct = Math.min(Math.max(((n.start - start) / total) * 100, 0), 100 - widthPct); // and never overflows
    rows.push({ node: n, depth, hasChildren: n.children.length > 0, durationMs: durationOf(n), leftPct, widthPct });
    if (!collapsed.has(n.span_id)) n.children.forEach((c) => walk(c, depth + 1));
  };
  roots.forEach((r) => walk(r, 0));
  return rows;
}

export function findPlan(roots: SpanNode[]): unknown {
  let found: unknown;
  const walk = (n: SpanNode) => {
    if (found === undefined && n.name === "router" && n.attributes.plan !== undefined) found = n.attributes.plan;
    n.children.forEach(walk);
  };
  roots.forEach(walk);
  return found;
}

/** Truncate a preview for display; returns the shortened text and whether anything was cut. */
export function clip(text: string, max = 400): { text: string; cut: boolean } {
  return text.length > max ? { text: text.slice(0, max), cut: true } : { text, cut: false };
}

export function failedCount(roots: SpanNode[]): number {
  let n = 0;
  const walk = (s: SpanNode) => { if (s.status === "error") n++; s.children.forEach(walk); };
  roots.forEach(walk);
  return n;
}
