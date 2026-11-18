import { useEffect, useRef, useState, type ReactNode } from "react";
import { serviceColour } from "../../lib/modes";
import { badges, fmtClock, fmtDuration, legTitle, startsIn, type VJourney, type VLeg } from "../../lib/journey";
import { fmtDist } from "../../lib/tripmap";

const ico = { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true } as const;
export const WalkIcon = ({ size = 16 }: { size?: number }) => <svg {...ico} width={size} height={size}><circle cx="13" cy="4.5" r="1.8" /><path d="m9 21 2.5-6.5L9.5 12l1.5-4 3.5 1.5L17 12M12 9.5 8 11l-1 3.5" /></svg>;
export const BusIcon = ({ size = 16 }: { size?: number }) => <svg {...ico} width={size} height={size}><rect x="4" y="3" width="16" height="14" rx="2.5" /><path d="M4 11h16M8 21v-4M16 21v-4M8 14h.01M16 14h.01" /></svg>;
export const TrainIcon = ({ size = 16 }: { size?: number }) => <svg {...ico} width={size} height={size}><rect x="5" y="3" width="14" height="14" rx="3" /><path d="M5 11h14M9 14h.01M15 14h.01M8 21l2-4M16 21l-2-4" /></svg>;
export const TramIcon = ({ size = 16 }: { size?: number }) => <svg {...ico} width={size} height={size}><rect x="5" y="5" width="14" height="12" rx="3" /><path d="M5 12h14M9 3l3 2 3-2M8 21l2-4M16 21l-2-4M9 15h.01M15 15h.01" /></svg>;
export const FerryIcon = ({ size = 16 }: { size?: number }) => <svg {...ico} width={size} height={size}><path d="M4 15l2 5h12l2-5-8-3zM12 12V4M8 8h8" /><path d="M2 21c2 0 2-1 4-1s2 1 4 1 2-1 4-1 2 1 4 1 2-1 4-1" /></svg>;
const Icon = ({ l, size }: { l: VLeg; size?: number }) => (l.kind === "walk" ? <WalkIcon size={size} /> : l.mode === "train" ? <TrainIcon size={size} /> : l.mode === "tram" ? <TramIcon size={size} /> : l.mode === "ferry" ? <FerryIcon size={size} /> : <BusIcon size={size} />);
const colourVar = (l: VLeg) => (l.kind === "walk" ? serviceColour(null) : serviceColour(l.mode, l.route));

/** The route pill: "428" in the leg's colour, like a TransLink route badge. */
export function RouteBadge({ l }: { l: VLeg }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[12.5px] font-extrabold text-white" style={{ background: colourVar(l) }}>
      <Icon l={l} size={13} />{l.route}
    </span>
  );
}

function StatusChip({ l }: { l: VLeg }) {
  if (l.status === "cancelled") return <span className="rounded-full bg-bad-bg px-2 py-0.5 text-[12px] font-bold text-bad">Cancelled</span>;
  if (l.status === "delayed") return <span className="rounded-full bg-warn-bg px-2 py-0.5 text-[12px] font-bold text-warn">{Math.round(l.delayMin ?? 0)} min late</span>;
  if (l.status === "early") return <span className="rounded-full bg-warn-bg px-2 py-0.5 text-[12px] font-bold text-warn">Running early</span>;
  if (l.status === "on_time") return <span className="rounded-full bg-ok-bg px-2 py-0.5 text-[12px] font-bold text-ok">On time (live)</span>;
  return null;
}

/** Thin stacked bar: how the trip's minutes split between walking, waiting and riding. */
function TimeBar({ j }: { j: VJourney }) {
  const total = Math.max(1, j.end.getTime() - j.start.getTime());
  let prevEnd = j.start.getTime();
  const segs: ReactNode[] = [];
  j.legs.forEach((l, i) => {
    const gap = l.start.getTime() - prevEnd;
    if (gap > 60_000) segs.push(<span key={`w${i}`} style={{ width: `${(gap / total) * 100}%` }} className="h-full bg-line" />);
    const w = Math.max(1.5, ((l.end.getTime() - l.start.getTime()) / total) * 100);
    segs.push(<span key={i} style={{ width: `${w}%`, background: l.kind === "walk" ? "repeating-linear-gradient(90deg,var(--route-walk) 0 3px,transparent 3px 6px)" : colourVar(l) }} className="h-full" />);
    prevEnd = l.end.getTime();
  });
  return <div className="flex h-1.5 w-full gap-px overflow-hidden rounded-full bg-surface2" aria-hidden>{segs}</div>;
}

interface CardProps {
  j: VJourney; all: VJourney[]; index: number; now: Date; selected: boolean; onSelect: () => void;
  activeLeg: number | null; onHoverLeg: (i: number | null) => void; onPickLeg: (i: number) => void;
  onFocusPoint: (lat: number, lon: number) => void; extra?: ReactNode; delay?: number;
}

export function JourneyCard({ j, all, index, now, selected, onSelect, activeLeg, onHoverLeg, onPickLeg, onFocusPoint, extra, delay = 0 }: CardProps) {
  const tags = badges(all, j);
  const late = j.maxDelayMin > 0;
  const li = useRef<HTMLLIElement>(null);
  useEffect(() => { if (selected) li.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }); }, [selected]);
  return (
    <li ref={li} style={{ animationDelay: `${delay}ms`, borderLeft: `5px solid ${j.legs.some((l) => l.mode === "train") ? "var(--route-train)" : "var(--route-bus)"}` }} className={"rise overflow-hidden rounded-2xl border bg-surface transition-all duration-200 " + (selected ? "border-accent shadow-lg ring-2 ring-accent/20" : "border-line hover:border-muted/60")} data-testid="journey-card">
      <button type="button" onClick={onSelect} aria-expanded={selected} className="block w-full px-3.5 pb-3 pt-3 text-left">
        <div className="flex items-baseline gap-2">
          <span className="grid size-6 shrink-0 place-items-center rounded-full bg-nav text-[12px] font-extrabold text-nav-ink">{index + 1}</span>
          <span className="text-[17px] font-extrabold tabular-nums">{fmtClock(j.start)}<span className="mx-1.5 font-normal text-muted">→</span>{fmtClock(j.end)}</span>
          <span className="ml-auto text-[15px] font-bold tabular-nums">{fmtDuration(j.minutes)}</span>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-x-1.5 gap-y-1.5" aria-label="Journey steps">
          {j.legs.map((l, i) => (
            <span key={i} className="flex items-center gap-1.5">
              {l.kind === "walk"
                ? <span className="flex items-center gap-1 text-[12.5px] text-muted"><WalkIcon size={15} />{l.minutes} min</span>
                : <RouteBadge l={l} />}
              {i < j.legs.length - 1 && <svg {...ico} width={12} height={12} className="text-muted"><path d="m9 6 6 6-6 6" /></svg>}
            </span>
          ))}
        </div>
        <div className="mt-2.5"><TimeBar j={j} /></div>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px] text-muted">
          <span className="font-semibold text-ink">{startsIn(j.start, now)}</span>
          <span>{j.transfers === 0 ? "Direct" : `${j.transfers} change${j.transfers === 1 ? "" : "s"}`}</span>
          {j.legs.some((l) => l.stopover) && <span className="font-semibold text-weather">via {j.legs.filter((l) => l.stopover).map((l) => l.stopover).join(", ")}</span>}
          <span>{fmtDist(j.walkM)} walk</span>
          {j.waitMin > 0 && <span>{j.waitMin} min waiting</span>}
          {late && <span className="rounded-full bg-warn-bg px-2 py-0.5 font-bold text-warn">Up to {Math.round(j.maxDelayMin)} min late</span>}
          {j.cancelled && <span className="rounded-full bg-bad-bg px-2 py-0.5 font-bold text-bad">Includes a cancelled service</span>}
          {tags.map((t) => <span key={t} className={"rounded-full px-2 py-0.5 font-bold " + (t === "Fastest" ? "bg-ok-bg text-ok" : t === "Fewest changes" ? "bg-calendar-bg text-calendar" : "bg-accent/15 text-accent")}>{t}</span>)}
        </div>
      </button>
      {selected && <Timeline j={j} activeLeg={activeLeg} onHoverLeg={onHoverLeg} onPickLeg={onPickLeg} onFocusPoint={onFocusPoint} extra={extra} />}
    </li>
  );
}

function Timeline({ j, activeLeg, onHoverLeg, onPickLeg, onFocusPoint, extra }: Pick<CardProps, "j" | "activeLeg" | "onHoverLeg" | "onPickLeg" | "onFocusPoint" | "extra">) {
  const [openStops, setOpenStops] = useState<Set<number>>(new Set());
  const toggle = (i: number) => setOpenStops((s) => { const n = new Set(s); if (n.has(i)) n.delete(i); else n.add(i); return n; });
  const last = j.legs[j.legs.length - 1];
  const stopRow = (time: Date, name: string, lat?: number, lon?: number) => (
    <div className="flex items-center gap-3 rounded-lg bg-surface2 px-2.5 py-1.5">
      <span className="w-[4.2rem] shrink-0 text-[13px] font-bold tabular-nums">{fmtClock(time)}</span>
      <span className="size-3 shrink-0 rounded-full border-[3px] border-nav bg-surface" aria-hidden />
      {lat !== undefined && lon !== undefined
        ? <button type="button" onClick={() => onFocusPoint(lat, lon)} className="min-w-0 flex-1 truncate text-left text-[13.5px] font-semibold underline decoration-dotted underline-offset-2 hover:text-accent" title="Zoom the map to this stop">{name}</button>
        : <span className="min-w-0 flex-1 truncate text-[13.5px] font-semibold">{name}</span>}
    </div>
  );
  return (
    <div className="rise border-t border-line px-3.5 pb-3.5 pt-3" data-testid="journey-detail">
      <ol className="space-y-1.5">
        {j.legs.map((l, i) => {
          const first = l.path[0], mid = l.stops.slice(1, -1);
          const c = colourVar(l);
          return (
            <li key={i} style={{ animationDelay: `${60 + i * 45}ms` }} onMouseEnter={() => onHoverLeg(i)} onMouseLeave={() => onHoverLeg(null)} className={"rise rounded-xl transition-colors duration-150 " + (activeLeg === i ? "bg-surface2/70" : "")}>
              {l.stopover && (
                <div className="mb-1.5 flex items-center gap-2 rounded-xl bg-weather-bg px-3 py-2 text-weather" data-testid="stopover">
                  <span className="grid size-6 shrink-0 place-items-center rounded-full text-[12px] font-extrabold text-white" style={{ background: "#d97706" }}>{l.stopoverIndex}</span>
                  <span className="min-w-0 flex-1 truncate text-[13px] font-bold">Stop {l.stopoverIndex}: {l.stopover}</span>
                  <span className="shrink-0 text-[12.5px] font-semibold">{l.stayMin ? `${l.stayMin} min stopover` : "no wait"}</span>
                </div>
              )}
              {l.waitBeforeMin > 0 && l.kind === "transit" && !l.stopover && <p className="mb-1 ml-[4.9rem] border-l-2 border-warn pl-2 text-[12.5px] font-semibold text-warn">Wait about {l.waitBeforeMin} min at {l.from}</p>}
              {stopRow(l.start, l.from, first?.[0], first?.[1])}
              <div className="relative ml-[4.9rem] border-l-[5px] py-2 pl-4" style={{ borderColor: c, borderLeftStyle: l.kind === "walk" ? "dotted" : "solid" }}>
                <button type="button" onClick={() => onPickLeg(i)} className="flex flex-wrap items-center gap-2 text-left" title="Zoom the map to this step">
                  <span className="grid size-7 place-items-center rounded-full text-white" style={{ background: c }}><Icon l={l} size={15} /></span>
                  {l.kind === "transit" && <RouteBadge l={l} />}
                  <span className="text-[13.5px] font-bold">{l.kind === "walk" ? legTitle(l) : l.headsign ? `towards ${l.headsign}` : legTitle(l)}</span>
                </button>
                {l.kind === "transit" && l.mode === "train" && l.routeName && <p className="mt-0.5 text-[13px] font-semibold">{l.routeName} line</p>}
                <p className="mt-1 text-[13px] text-muted">
                  Travel time is about {fmtDuration(l.minutes)}{l.estimated ? " (straight-line estimate)" : ""}
                  {l.kind === "transit" && l.numStops > 0 ? ` · ${l.numStops} stop${l.numStops === 1 ? "" : "s"} · ${fmtDist(l.distanceM)}` : ""}
                </p>
                {l.kind === "transit" && <div className="mt-1 flex flex-wrap items-center gap-2"><StatusChip l={l} />
                  {mid.length > 0 && <button type="button" onClick={() => toggle(i)} aria-expanded={openStops.has(i)} className="text-[12.5px] font-semibold text-accent hover:underline">{openStops.has(i) ? "Hide" : "Show"} {mid.length} stop{mid.length === 1 ? "" : "s"} on the way</button>}
                </div>}
                {openStops.has(i) && (
                  <ul className="mt-1.5 space-y-0.5 text-[13px]">
                    {mid.map((s, k) => <li key={k}><button type="button" onClick={() => onFocusPoint(s.lat, s.lon)} className="flex items-center gap-2 text-left hover:text-accent"><span className="size-2 rounded-full border-2 bg-surface" style={{ borderColor: c }} aria-hidden />{s.name}</button></li>)}
                  </ul>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      {last && <div className="mt-1.5">{stopRow(last.end, last.to, last.path[last.path.length - 1]?.[0], last.path[last.path.length - 1]?.[1])}</div>}
      <dl className="mt-3 grid grid-cols-4 gap-2 text-center" aria-label="Journey totals">
        {[["Total", fmtDuration(j.minutes)], ["Walking", `${fmtDist(j.walkM)} · ${j.walkMin}m`], ["Stops", String(j.stopsPassed)], ["Distance", fmtDist(j.totalM)]].map(([k, v]) => (
          <div key={k} className="rounded-xl bg-surface2 px-1 py-2"><dt className="text-[11px] font-bold uppercase tracking-wide text-muted">{k}</dt><dd className="text-[13.5px] font-extrabold tabular-nums">{v}</dd></div>
        ))}
      </dl>
      {extra}
    </div>
  );
}
