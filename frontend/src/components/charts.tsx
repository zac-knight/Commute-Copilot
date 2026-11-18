import type { ReactNode } from "react";
import { domainOf, scale, ticks } from "../lib/chartmath";

export interface Bar { label: string; value: number; lo?: number; hi?: number; note?: string; tone?: "accent" | "ok" | "warn" | "bad" | "muted" }
const TONE = { accent: "var(--accent)", ok: "var(--ok)", warn: "var(--warn)", bad: "var(--bad)", muted: "var(--muted)" };

export function StatTile({ label, value, sub, tone = "ink" }: { label: string; value: ReactNode; sub?: ReactNode; tone?: "ink" | "ok" | "warn" | "bad" | "accent" }) {
  const c = { ink: "text-ink", ok: "text-ok", warn: "text-warn", bad: "text-bad", accent: "text-accent" }[tone];
  return (
    <div className="glass rounded-2xl p-4">
      <p className="label">{label}</p>
      <p className={"mono mt-1 text-3xl font-extrabold tabular-nums tracking-tight " + c}>{value}</p>
      {sub && <p className="mt-1 text-xs text-muted">{sub}</p>}
    </div>
  );
}

/** Table fallback for every chart: the numbers, readable by screen readers and copyable. */
export function DataTable({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
  return (
    <details className="mt-2 text-xs text-muted">
      <summary className="cursor-pointer font-semibold">View as table</summary>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full text-left"><thead><tr>{head.map((h) => <th key={h} className="py-1 pr-4 font-bold">{h}</th>)}</tr></thead>
          <tbody>{rows.map((r, i) => <tr key={i} className="border-t border-line">{r.map((c, j) => <td key={j} className="py-1 pr-4 tabular-nums">{c}</td>)}</tr>)}</tbody></table>
      </div>
    </details>
  );
}

/** Horizontal bars with optional interval whiskers (lo..hi). */
export function HBars({ bars, format, label, max }: { bars: Bar[]; format: (v: number) => string; label: string; max?: number }) {
  const dom = domainOf([...bars.flatMap((b) => [b.value, b.lo ?? b.value, b.hi ?? b.value]), ...(max ? [max] : [])]);
  const W = 560, L = 130, R = 118, rowH = 30, H = bars.length * rowH + 28;
  const x = (v: number) => L + scale(dom, v, W - L - R);
  return (
    <div>
      <svg role="img" aria-label={label} viewBox={`0 0 ${W} ${H}`} className="w-full">
        {ticks(dom).map((t) => (
          <g key={t}><line x1={x(t)} x2={x(t)} y1={0} y2={H - 20} stroke="var(--border)" strokeDasharray={t === 0 ? undefined : "2 4"} /><text x={x(t)} y={H - 6} fontSize={10} textAnchor="middle" fill="var(--muted)">{format(t)}</text></g>
        ))}
        {bars.map((b, i) => {
          const y = i * rowH + 6, c = TONE[b.tone ?? "accent"];
          const x0 = x(Math.min(0, b.value)), x1 = x(Math.max(0, b.value));
          return (
            <g key={b.label}>
              <text x={L - 8} y={y + 13} fontSize={11} textAnchor="end" fill="var(--text)">{b.label}</text>
              <rect x={x0} y={y} width={Math.max(2, x1 - x0)} height={16} rx={4} fill={c} opacity={0.85} />
              {b.lo != null && b.hi != null && <g stroke="var(--text)" strokeWidth={1.5}><line x1={x(b.lo)} x2={x(b.hi)} y1={y + 8} y2={y + 8} /><line x1={x(b.lo)} x2={x(b.lo)} y1={y + 3} y2={y + 13} /><line x1={x(b.hi)} x2={x(b.hi)} y1={y + 3} y2={y + 13} /></g>}
              <text x={Math.min(W - 4, Math.max(x1, b.hi != null ? x(b.hi) : x1) + 6)} y={y + 12} fontSize={10.5} fill="var(--muted)" textAnchor="start">{format(b.value)}{b.note ? ` · ${b.note}` : ""}</text>
            </g>
          );
        })}
      </svg>
      <DataTable head={["", "value", "low", "high", "note"]} rows={bars.map((b) => [b.label, format(b.value), b.lo != null ? format(b.lo) : "", b.hi != null ? format(b.hi) : "", b.note ?? ""])} />
    </div>
  );
}

/** Vertical columns (e.g. by hour) with whiskers. */
export function Columns({ bars, format, label }: { bars: Bar[]; format: (v: number) => string; label: string }) {
  const dom = domainOf(bars.flatMap((b) => [b.value, b.lo ?? b.value, b.hi ?? b.value]));
  const W = 560, H = 190, T = 10, B = 26, L = 44, R = 6;
  const y = (v: number) => T + (H - T - B) - scale(dom, v, H - T - B);
  const bw = (W - L - R) / Math.max(1, bars.length);
  return (
    <div>
      <svg role="img" aria-label={label} viewBox={`0 0 ${W} ${H}`} className="w-full">
        {ticks(dom, 4).map((t) => <g key={t}><line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="var(--border)" strokeDasharray={t === 0 ? undefined : "2 4"} /><text x={L - 6} y={y(t) + 3} fontSize={10} textAnchor="end" fill="var(--muted)">{format(t)}</text></g>)}
        {bars.map((b, i) => {
          const cx = L + bw * i + bw / 2, c = TONE[b.tone ?? "accent"];
          return (
            <g key={b.label}>
              <rect x={cx - bw * 0.32} y={Math.min(y(b.value), y(0))} width={bw * 0.64} height={Math.max(2, Math.abs(y(b.value) - y(0)))} rx={3} fill={c} opacity={0.85} />
              {b.lo != null && b.hi != null && <g stroke="var(--text)" strokeWidth={1.4}><line x1={cx} x2={cx} y1={y(b.lo)} y2={y(b.hi)} /><line x1={cx - 3} x2={cx + 3} y1={y(b.hi)} y2={y(b.hi)} /><line x1={cx - 3} x2={cx + 3} y1={y(b.lo)} y2={y(b.lo)} /></g>}
              <text x={cx} y={H - 8} fontSize={10} textAnchor="middle" fill="var(--muted)">{b.label}</text>
            </g>
          );
        })}
      </svg>
      <DataTable head={["", "value", "low", "high", "note"]} rows={bars.map((b) => [b.label, format(b.value), b.lo != null ? format(b.lo) : "", b.hi != null ? format(b.hi) : "", b.note ?? ""])} />
    </div>
  );
}

export function Empty({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-line bg-surface2/40 p-5 text-center">
      <p className="text-sm font-bold text-ink">{title}</p>
      <p className="mx-auto mt-1 max-w-prose text-xs text-muted">{children}</p>
    </div>
  );
}

export function Section({ title, sub, children, id }: { title: string; sub?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section id={id} className="glass rise rounded-2xl p-5">
      <h2 className="mb-0.5 text-[15px] font-extrabold">{title}</h2>
      {sub && <p className="mb-3 mt-0.5 text-xs text-muted">{sub}</p>}
      {children}
    </section>
  );
}
