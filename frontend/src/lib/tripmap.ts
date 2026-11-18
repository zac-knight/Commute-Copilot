/** Map maths for the trip card: projection, scale bar and formatting. Pure and unit-tested.
 *  No map tiles: the picture is drawn from the trip's own geometry plus the GTFS stops around it. */

export type BBox = [south: number, west: number, north: number, east: number];
export interface Projection { x: (lon: number) => number; y: (lat: number) => number; metresPerPx: number; width: number; height: number }

const M_PER_DEG_LAT = 111_320;

/** Equirectangular projection with a cos(latitude) correction, fitted to the box with padding. */
export function makeProjection(bbox: BBox, width: number, height: number, pad = 28): Projection {
  const [s, w, n, e] = bbox;
  const cosLat = Math.cos(((s + n) / 2) * (Math.PI / 180));
  const spanX = Math.max(1e-6, (e - w) * cosLat);
  const spanY = Math.max(1e-6, n - s);
  const k = Math.min((width - 2 * pad) / spanX, (height - 2 * pad) / spanY); // px per degree of latitude
  const offX = (width - spanX * k) / 2;
  const offY = (height - spanY * k) / 2;
  return {
    x: (lon) => offX + (lon - w) * cosLat * k,
    y: (lat) => height - offY - (lat - s) * k,
    metresPerPx: M_PER_DEG_LAT / k, width, height,
  };
}

/** A round distance (1/2/5 x 10^k metres) whose bar is at most `maxPx` wide. */
export function niceScale(metresPerPx: number, maxPx = 150): { metres: number; px: number; label: string } {
  const maxM = metresPerPx * maxPx;
  const mag = 10 ** Math.floor(Math.log10(maxM));
  const metres = [5, 2, 1].map((m) => m * mag).find((m) => m <= maxM) ?? mag;
  return { metres, px: metres / metresPerPx, label: fmtDist(metres) };
}

export function fmtDist(m: number): string {
  if (!Number.isFinite(m)) return "–";
  if (m < 1000) return `${Math.round(m)} m`;
  return `${(m / 1000).toFixed(m < 10_000 ? 2 : 1)} km`;
}

export const toPoints = (path: [number, number][], p: Projection) => path.map(([lat, lon]) => `${p.x(lon).toFixed(1)},${p.y(lat).toFixed(1)}`).join(" ");

/** Share of the journey's distance per leg, for the stacked bar (sums to 1; empty trip -> []). */
export function legShares(distances: number[]): number[] {
  const total = distances.reduce((a, b) => a + b, 0);
  return total > 0 ? distances.map((d) => d / total) : distances.map(() => 0);
}

/** Initial bearing in degrees (0 = north) from a to b, for the direction chevrons. */
export function bearing(aLat: number, aLon: number, bLat: number, bLon: number): number {
  const r = Math.PI / 180;
  const y = Math.sin((bLon - aLon) * r) * Math.cos(bLat * r);
  const x = Math.cos(aLat * r) * Math.sin(bLat * r) - Math.sin(aLat * r) * Math.cos(bLat * r) * Math.cos((bLon - aLon) * r);
  return ((Math.atan2(y, x) / r) + 360) % 360;
}

/** Canvas height that suits the route's shape: tall for a north-south trip, wide for east-west,
 *  clamped so the map never gets absurdly tall or flat. */
export function mapHeight(bbox: BBox, width: number, min = 380, max = 640): number {
  const [s, w, n, e] = bbox;
  const cosLat = Math.cos(((s + n) / 2) * (Math.PI / 180));
  const aspect = (n - s) / Math.max(1e-6, (e - w) * cosLat);
  return Math.round(Math.min(max, Math.max(min, width * aspect * 0.9)));
}
