/** Scale helpers for the SVG charts (pure, tested). */
export interface Domain { min: number; max: number }

/** A domain that always includes zero and every value, padded a little so whiskers are not clipped. */
export function domainOf(values: number[], pad = 0.08): Domain {
  const finite = values.filter(Number.isFinite);
  const lo = Math.min(0, ...finite), hi = Math.max(0, ...finite);
  const span = hi - lo || 1;
  return { min: lo < 0 ? lo - span * pad : 0, max: hi + span * pad };
}

export const scale = (d: Domain, v: number, size: number) => ((v - d.min) / (d.max - d.min || 1)) * size;

/** "Nice" tick values (1/2/5 x 10^k) covering the domain, at most about `target` of them. */
export function ticks(d: Domain, target = 5): number[] {
  const span = d.max - d.min;
  if (!(span > 0)) return [0];
  const raw = span / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? mag * 10;
  const out: number[] = [];
  for (let t = Math.ceil(d.min / step) * step; t <= d.max + 1e-9; t += step) out.push(Math.round(t / step) * step);
  return out;
}
