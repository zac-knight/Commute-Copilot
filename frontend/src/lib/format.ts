const TZ = "Australia/Brisbane";

export function fmtMs(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "–";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  return s < 100 ? `${s.toFixed(1)} s` : `${Math.round(s)} s`;
}

export function fmtTokens(n: number): string {
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
}

const dayParts = new Intl.DateTimeFormat("en-AU", { timeZone: TZ, weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const timeOnly = new Intl.DateTimeFormat("en-AU", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
const dayKey = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });

/** "Thu 19 Nov, 14:00" in Brisbane time. Built from parts so the result does not depend on locale punctuation. */
export function fmtWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const get = (t: string) => dayParts.formatToParts(d).find((x) => x.type === t)?.value ?? "";
  return `${get("weekday")} ${get("day")} ${get("month")}, ${get("hour")}:${get("minute")}`;
}

/** "Thu 19 Nov, 15:00–16:00" (second day shown when the event crosses midnight). */
export function fmtRange(startIso: string, endIso: string): string {
  const a = new Date(startIso), b = new Date(endIso);
  if (Number.isNaN(a.getTime()) || Number.isNaN(b.getTime())) return `${startIso} – ${endIso}`;
  const same = dayKey.format(a) === dayKey.format(b);
  return same ? `${fmtWhen(startIso)}–${timeOnly.format(b)}` : `${fmtWhen(startIso)} – ${fmtWhen(endIso)}`;
}

/** Value of an <input type="datetime-local"> ("2026-11-19T15:00") as an ISO string in Brisbane (+10:00, no DST). */
export function toBrisbaneIso(localValue: string): string {
  const v = localValue.length === 16 ? `${localValue}:00` : localValue;
  return `${v}+10:00`;
}

/** ISO string -> datetime-local input value, in Brisbane time. */
export function toLocalInput(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false, hourCycle: "h23" }).formatToParts(d);
  const get = (t: string) => p.find((x) => x.type === t)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}`;
}

/** Minutes until an ISO time (negative if past). */
export function minutesUntil(iso: string, now: number = Date.now()): number {
  return Math.round((new Date(iso).getTime() - now) / 60000);
}

/** Seconds late as a human string: "+2.5 min", "-1.0 min" (early) or "on time" within 30 s. */
export function fmtDelayS(s: number): string {
  if (!Number.isFinite(s)) return "–";
  if (Math.abs(s) < 30) return "on time";
  return `${s > 0 ? "+" : "-"}${(Math.abs(s) / 60).toFixed(1)} min`;
}

export const fmtPct = (rate: number, digits = 0) => `${(100 * rate).toFixed(digits)}%`;

/** A proportion with its interval, e.g. "92% (79-97%)". */
export function fmtRate(r: { rate: number; lo: number; hi: number }): string {
  return `${fmtPct(r.rate)} (${fmtPct(r.lo)}–${fmtPct(r.hi)})`;
}

export const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
