/** View-model and pure helpers for the journey planner. No React, no DOM: unit tested.
 *  All clock times are Australia/Brisbane (no daylight saving), whatever the browser's zone. */
import type { TransitMode, TripArtifact, TripJourney, TripLeg, TripStop } from "../api/types";
import { MODE_LABEL } from "./modes";
import { fmtDist } from "./tripmap";

export const DEFAULT_ZONE = "Australia/Brisbane";
let zone = DEFAULT_ZONE;
/** All clock times are shown in the region being planned in (set from the plan response); SEQ is Brisbane. */
export const setDisplayZone = (z: string): void => { zone = z || DEFAULT_ZONE; };
export const displayZone = (): string => zone;
const WALK_M_PER_MIN = 80; // ~4.8 km/h
const DETOUR = 1.25; // streets are longer than the straight line
export const MAX_ACCESS_WALK_M = 1500;

export interface Point { name: string; lat: number; lon: number }
export interface VLeg {
  kind: "walk" | "transit"; mode: TransitMode | null; route: string | null; routeName: string | null; headsign: string | null;
  from: string; to: string; start: Date; end: Date; minutes: number; distanceM: number; numStops: number;
  path: [number, number][]; stops: TripStop[]; waitBeforeMin: number; tripId: string | null;
  /** Set on the first leg after a stop the user added: where they are stopping, how long, and which stop (1-based). */
  stopover: string | null; stayMin: number | null; stopoverIndex: number | null;
  status: "on_time" | "delayed" | "early" | "cancelled" | "scheduled" | null; delayMin: number | null;
  /** A walk to/from a place that is not itself a stop: straight-line estimate, drawn by the UI. */
  estimated: boolean;
}
export interface VJourney {
  id: string; start: Date; end: Date; minutes: number; transfers: number; legs: VLeg[];
  walkM: number; walkMin: number; waitMin: number; stopsPassed: number; totalM: number;
  routes: string[]; cancelled: boolean; maxDelayMin: number; raw: TripJourney;
}

export const haversine = (a: [number, number], b: [number, number]): number => {
  const r = Math.PI / 180, R = 6371008.8;
  const dLat = (b[0] - a[0]) * r, dLon = (b[1] - a[1]) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
};

const status = (l: TripLeg): VLeg["status"] => {
  const s = l.realtime?.status;
  return s === "on_time" || s === "delayed" || s === "early" || s === "cancelled" ? s : l.kind === "transit" ? "scheduled" : null;
};

function accessWalk(from: Point, to: [number, number], toName: string, end: Date | null, start: Date | null, label: "from" | "to"): VLeg | null {
  const straight = haversine([from.lat, from.lon], to);
  if (straight < 60) return null; // already at the stop
  const metres = Math.round(straight * DETOUR);
  const minutes = Math.max(1, Math.round(metres / WALK_M_PER_MIN));
  const path: [number, number][] = [[from.lat, from.lon], to];
  const mk = (s: Date, e: Date): VLeg => ({
    kind: "walk", mode: null, route: null, routeName: null, headsign: null,
    from: label === "from" ? from.name : toName, to: label === "from" ? toName : from.name,
    start: s, end: e, minutes, distanceM: metres, numStops: 0, path: label === "from" ? path : [path[1]!, path[0]!],
    stops: [], waitBeforeMin: 0, tripId: null, stopover: null, stayMin: null, stopoverIndex: null, status: null, delayMin: null, estimated: true,
  });
  if (label === "from" && end) return mk(new Date(end.getTime() - minutes * 60_000), end);
  if (label === "to" && start) return mk(start, new Date(start.getTime() + minutes * 60_000));
  return null;
}

/** Build the display model. ``origin``/``dest`` are the places the user typed: when one is not itself a
 *  stop (an address, "my location"), a straight-line walking leg to/from the first/last stop is added
 *  and flagged ``estimated``. */
export function toJourneys(art: TripArtifact, origin?: Point | null, dest?: Point | null): VJourney[] {
  return art.journeys.map((j, idx) => {
    const legs: VLeg[] = j.legs.map((l) => {
      const start = new Date(l.depart_iso ?? ""), end = new Date(l.arrive_iso ?? "");
      return {
        kind: l.kind, mode: l.mode, route: l.route, routeName: l.route_name ?? null, headsign: l.headsign, from: l.from, to: l.to, start, end,
        minutes: Math.max(0, Math.round((end.getTime() - start.getTime()) / 60_000)), distanceM: l.distance_m,
        numStops: l.num_stops, path: l.path, stops: l.stops ?? [], waitBeforeMin: l.wait_before_min ?? 0, tripId: l.trip_id ?? null, stopover: l.stopover ?? null, stayMin: l.stay_min ?? null, stopoverIndex: l.stopover_index ?? null,
        status: status(l), delayMin: l.realtime?.delay_min ?? null, estimated: false,
      };
    });
    const firstStop = legs[0]?.path[0], lastLeg = legs[legs.length - 1];
    const lastStop = lastLeg?.path[lastLeg.path.length - 1];
    if (origin && firstStop && legs[0]) {
      const w = accessWalk(origin, firstStop, legs[0].from, legs[0].start, null, "from");
      if (w && w.distanceM <= MAX_ACCESS_WALK_M) legs.unshift(w);
    }
    if (dest && lastStop && lastLeg) {
      const w = accessWalk(dest, lastStop, lastLeg.to, null, lastLeg.end, "to");
      if (w && w.distanceM <= MAX_ACCESS_WALK_M) legs.push(w);
    }
    const start = legs[0]?.start ?? new Date(NaN), end = legs[legs.length - 1]?.end ?? new Date(NaN);
    const walk = legs.filter((l) => l.kind === "walk");
    const transit = legs.filter((l) => l.kind === "transit");
    return {
      id: `${idx}-${start.getTime()}`, start, end, minutes: Math.round((end.getTime() - start.getTime()) / 60_000),
      transfers: j.transfers, legs, walkM: walk.reduce((a, l) => a + l.distanceM, 0),
      walkMin: walk.reduce((a, l) => a + l.minutes, 0), waitMin: legs.reduce((a, l) => a + l.waitBeforeMin, 0),
      stopsPassed: transit.reduce((a, l) => a + l.numStops, 0), totalM: legs.reduce((a, l) => a + l.distanceM, 0),
      routes: [...new Set(transit.map((l) => l.route ?? ""))].filter(Boolean),
      cancelled: transit.some((l) => l.status === "cancelled"),
      maxDelayMin: Math.max(0, ...transit.map((l) => l.delayMin ?? 0)), raw: j,
    };
  });
}

/* ---------- sorting and filtering ---------- */

export type SortKey = "depart" | "fastest" | "changes" | "walk";
export const SORTS: { key: SortKey; label: string }[] = [
  { key: "depart", label: "Earliest" }, { key: "fastest", label: "Fastest" },
  { key: "changes", label: "Fewest changes" }, { key: "walk", label: "Least walking" },
];
export interface Filters { bus: boolean; train: boolean; tram: boolean; ferry: boolean; maxWalkM: number | null; hideCancelled: boolean }
export const DEFAULT_FILTERS: Filters = { bus: true, train: true, tram: true, ferry: true, maxWalkM: null, hideCancelled: true };

export function applyFilters(js: VJourney[], f: Filters): VJourney[] {
  return js.filter((j) => {
    if (f.hideCancelled && j.cancelled) return false;
    if (f.maxWalkM !== null && j.walkM > f.maxWalkM) return false;
    const modes = new Set(j.legs.filter((l) => l.kind === "transit").map((l) => l.mode));
    for (const m of ["bus", "train", "tram", "ferry"] as const) if (modes.has(m) && !f[m]) return false;
    return true;
  });
}

export function sortJourneys(js: VJourney[], key: SortKey): VJourney[] {
  const t = (j: VJourney) => j.start.getTime();
  const cmp: Record<SortKey, (a: VJourney, b: VJourney) => number> = {
    depart: (a, b) => t(a) - t(b) || a.minutes - b.minutes,
    fastest: (a, b) => a.minutes - b.minutes || t(a) - t(b),
    changes: (a, b) => a.transfers - b.transfers || a.minutes - b.minutes,
    walk: (a, b) => a.walkM - b.walkM || a.minutes - b.minutes,
  };
  return [...js].sort(cmp[key]);
}

/** Labels like "Fastest" / "Fewest changes" / "Least walking" for a card, when it wins that category. */
export function badges(js: VJourney[], j: VJourney): string[] {
  if (js.length < 2) return [];
  const out: string[] = [];
  const min = (f: (x: VJourney) => number) => Math.min(...js.map(f));
  if (j.minutes === min((x) => x.minutes)) out.push("Fastest");
  if (j.transfers === min((x) => x.transfers) && js.some((x) => x.transfers !== j.transfers)) out.push("Fewest changes");
  if (j.walkM === min((x) => x.walkM) && js.some((x) => x.walkM !== j.walkM)) out.push("Least walking");
  return out;
}

/* ---------- time formatting (Brisbane) ---------- */

const f12s = new Map<string, Intl.DateTimeFormat>();
const f12 = (): Intl.DateTimeFormat => {
  let f = f12s.get(zone);
  if (!f) { f = new Intl.DateTimeFormat("en-AU", { timeZone: zone, hour: "numeric", minute: "2-digit", hour12: true }); f12s.set(zone, f); }
  return f;
};
export const fmtClock = (d: Date): string => (Number.isNaN(d.getTime()) ? "–" : f12().format(d).replace(/\s?([ap])m/i, (_m, x: string) => ` ${x.toLowerCase()}m`));

export function fmtDuration(min: number): string {
  if (!Number.isFinite(min)) return "–";
  if (min < 60) return `${min} min${min === 1 ? "" : "s"}`;
  const h = Math.floor(min / 60), m = min % 60;
  return m ? `${h} hr ${m} min${m === 1 ? "" : "s"}` : `${h} hr`;
}

export function startsIn(start: Date, now: Date): string {
  const m = Math.round((start.getTime() - now.getTime()) / 60_000);
  if (m <= 0) return "leaving now";
  if (m < 60) return `in ${m} min`;
  return `in ${Math.floor(m / 60)} hr${m % 60 ? ` ${m % 60} min` : ""}`;
}

/** Brisbane calendar parts of an instant. */
export function brisbaneParts(d: Date): { y: number; mo: number; day: number; h: number; mi: number; dow: number } {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-AU", { timeZone: zone, year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", hour12: false, weekday: "short" })
    .formatToParts(d).map((x) => [x.type, x.value]));
  const dows = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  return { y: Number(p.year), mo: Number(p.month), day: Number(p.day), h: Number(p.hour) % 24, mi: Number(p.minute), dow: dows.indexOf(String(p.weekday)) };
}
const pad = (n: number) => String(n).padStart(2, "0");
export const isoDate = (d: Date): string => { const p = brisbaneParts(d); return `${p.y}-${pad(p.mo)}-${pad(p.day)}`; };
export const isoTime = (d: Date): string => { const p = brisbaneParts(d); return `${pad(p.h)}:${pad(p.mi)}`; };
export const toWhen = (date: string, time: string): string => `${date}T${time}`;

/** The next ``n`` days as select options: "Today (Wednesday)", "Tomorrow (Thursday)", "Fri 20 Nov"... */
export function dayOptions(now: Date, n = 14): { value: string; label: string }[] {
  const wk = new Intl.DateTimeFormat("en-AU", { timeZone: zone, weekday: "long" });
  const short = new Intl.DateTimeFormat("en-AU", { timeZone: zone, weekday: "short", day: "numeric", month: "short" });
  return Array.from({ length: n }, (_, i) => {
    const d = new Date(now.getTime() + i * 86_400_000);
    return { value: isoDate(d), label: i === 0 ? `Today (${wk.format(d)})` : i === 1 ? `Tomorrow (${wk.format(d)})` : short.format(d) };
  });
}

/* ---------- misc ---------- */

export const legTitle = (l: VLeg): string => (l.kind === "walk" ? `Walk ${fmtDist(l.distanceM)}` : `${MODE_LABEL[l.mode ?? "bus"]} ${l.route ?? ""}`.trim());

/** "lat,lon" place ids (from GPS / address results) are passed to the backend as coordinates. */
export const isCoord = (s: string): boolean => /^-?\d+(\.\d+)?,\s*-?\d+(\.\d+)?$/.test(s);

/** Round-trip the search through the URL hash so a journey can be shared and survives reload. */
export interface ViaState { id: string; name: string }
export interface SearchState { from: string; fromName: string; to: string; toName: string; date: string; time: string; by: "leave" | "arrive"; changes: number; vias: ViaState[]; stay: number }
export function encodeSearch(s: SearchState): string {
  const p = new URLSearchParams({ from: s.from, fn: s.fromName, to: s.to, tn: s.toName, d: s.date, t: s.time, by: s.by, c: String(s.changes) });
  for (const v of s.vias) { p.append("v", v.id); p.append("vn", v.name); }
  if (s.vias.length) p.set("s", String(s.stay));
  return p.toString();
}
export function decodeSearch(q: string): Partial<SearchState> | null {
  const p = new URLSearchParams(q);
  const from = p.get("from"), to = p.get("to");
  if (!from || !to) return null;
  const by = p.get("by") === "arrive" ? "arrive" : "leave";
  const date = p.get("d") ?? "", time = p.get("t") ?? "";
  const changes = Number(p.get("c") ?? 2);
  const ids = p.getAll("v").slice(0, 3), names = p.getAll("vn");
  const stay = Number(p.get("s") ?? 5);
  return {
    from, to, fromName: p.get("fn") ?? from, toName: p.get("tn") ?? to, by,
    ...(/^\d{4}-\d{2}-\d{2}$/.test(date) ? { date } : {}), ...(/^\d{2}:\d{2}$/.test(time) ? { time } : {}),
    changes: Number.isFinite(changes) ? Math.min(4, Math.max(0, changes)) : 2,
    vias: ids.map((id, i) => ({ id, name: names[i] ?? id })),
    stay: Number.isFinite(stay) ? Math.min(240, Math.max(0, Math.round(stay))) : 5,
  };
}
