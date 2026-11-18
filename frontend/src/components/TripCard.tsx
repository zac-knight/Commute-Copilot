import { useCallback, useMemo, useState } from "react";
import { useApp } from "../state/AppContext";
import type { TripArtifact, TripLeg } from "../api/types";
import { fmtDist, legShares } from "../lib/tripmap";
import { toJourneys } from "../lib/journey";
import { useTheme } from "../state/theme";
import { RouteMap, type Focus } from "./planner/RouteMap";

const colour = (l: TripLeg) => (l.kind === "walk" ? "var(--route-walk)" : l.mode === "train" ? "var(--route-train)" : "var(--route-bus)");
const label = (l: TripLeg) => (l.kind === "walk" ? "Walk" : `${l.mode === "train" ? "Train" : "Bus"} ${l.route ?? ""}`.trim());

function Kpi({ k, v, sub }: { k: string; v: string; sub?: string }) {
  return <div className="border border-line bg-surface/60 px-3 py-2"><p className="label">{k}</p><p className="mono text-lg font-extrabold tabular-nums leading-tight">{v}</p>{sub && <p className="text-[12px] text-muted">{sub}</p>}</div>;
}

/** A planned trip: map, start and end, distances, and every leg with its times and delay status. */
export function TripCard({ art }: { art: TripArtifact }) {
  const [ji, setJi] = useState(0);
  const [active, setActive] = useState<number | null>(null);
  const [focus, setFocus] = useState<Focus>({ kind: "fit", nonce: 0 });
  const [theme] = useTheme();
  const { api } = useApp();
  const fetchTripPath = useCallback((id: string) => api.tripPath(id), [api]);
  const fetchVehicles = useCallback((b: [number, number, number, number], t: string[], signal: AbortSignal, modes?: string[]) => api.vehicles(b, t, signal, modes), [api]);
  const fetchTrainLines = useCallback(() => api.trainLines(), [api]);
  const views = useMemo(() => toJourneys(art), [art]);
  const j = art.journeys[ji] ?? art.journeys[0]!;
  const s = j.summary;
  const stages = s.stages ?? j.legs.filter((l) => l.kind === "transit").length; // older payloads lack it
  const shares = legShares(j.legs.map((l) => l.distance_m));
  return (
    <section className="rise glass overflow-hidden" data-testid="trip-card" aria-label="Planned trip">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-line px-4 py-3">
        <p className="label">Planned trip</p>
        <h3 className="mono text-[13px] font-extrabold"><span className="text-ok">A</span> {art.origin_label} <span className="text-muted">→</span> <span className="text-bad">B</span> {art.destination_label}</h3>
        <p className="mono ml-auto text-[12px] tabular-nums"><b>{j.depart}</b> <span className="text-muted">→</span> <b>{j.arrive}</b> <span className="text-muted">· {j.duration_min} min · {stages} stage{stages === 1 ? "" : "s"}{j.transfers ? ` · ${j.transfers} transfer${j.transfers === 1 ? "" : "s"}` : " · direct"}</span></p>
      </header>

      {art.journeys.length > 1 && (
        <div role="tablist" aria-label="Journey options" className="flex gap-px overflow-x-auto border-b border-line bg-line">
          {art.journeys.map((x, i) => (
            <button key={i} role="tab" aria-selected={i === ji} type="button" onClick={() => { setJi(i); setActive(null); }}
              className={"mono shrink-0 px-3 py-1.5 text-[12px] font-bold " + (i === ji ? "bg-surface2 text-accent shadow-[inset_0_-2px_0_var(--accent)]" : "bg-surface text-muted hover:text-ink")}>
              option {i + 1} · {x.depart.split(" ").pop()}→{x.arrive.split(" ").pop()} · {x.duration_min}m · {x.summary.stages ?? x.legs.filter((l) => l.kind === "transit").length} stages · {fmtDist(x.summary.total_m)}
            </button>
          ))}
        </div>
      )}

      <div className="grid gap-3 p-3 sm:grid-cols-3 lg:grid-cols-6">
        <Kpi k="distance" v={fmtDist(s.total_m)} sub="along the route" />
        <Kpi k="straight line" v={fmtDist(s.straight_line_m)} sub={s.circuity ? `route is ${s.circuity}× longer` : undefined} />
        <Kpi k="walking" v={fmtDist(s.walk_m)} sub={`${Math.round((100 * s.walk_m) / Math.max(1, s.total_m))}% of the trip`} />
        <Kpi k="on transit" v={fmtDist(s.transit_m)} sub={Object.entries(s.by_mode_m).map(([m, d]) => `${m} ${fmtDist(d)}`).join(" · ")} />
        <Kpi k="stages" v={String(stages)} sub={`${s.all_bus ? "all bus" : Object.keys(s.by_mode_m).join(" + ")} · ${s.stops_passed} stops${s.wait_min ? ` · ${s.wait_min} min waiting` : ""}`} />
        <Kpi k="avg speed" v={s.avg_speed_kmh ? `${s.avg_speed_kmh} km/h` : "–"} sub="door to door" />
      </div>

      <div className="space-y-3 px-3 pb-3">
        <div data-testid="trip-map" className="h-[460px] overflow-hidden rounded-xl border border-line">
          <RouteMap art={art} journey={views[ji] ?? views[0] ?? null} activeLeg={active} onHoverLeg={setActive}
            onPickLeg={(i) => setFocus({ kind: "leg", index: i, nonce: Date.now() })} focus={focus} expanded={false} dark={theme === "dark"} fetchVehicles={fetchVehicles} fetchTripPath={fetchTripPath} fetchTrainLines={fetchTrainLines} />
        </div>
        <div className="min-w-0">
          <p className="label mb-1.5">Distance by leg</p>
          <div className="mb-3 flex h-3 w-full overflow-hidden border border-line" role="img" aria-label="Share of distance by leg">
            {j.legs.map((l, i) => <span key={i} onMouseEnter={() => setActive(i)} onMouseLeave={() => setActive(null)} style={{ width: `${Math.max(0.8, shares[i]! * 100)}%`, background: colour(l), opacity: active === null || active === i ? 1 : 0.35 }} title={`${label(l)}: ${fmtDist(l.distance_m)}`} />)}
          </div>
          <ol className="grid divide-y divide-line border border-line xl:grid-cols-2 xl:divide-y-0 [&>li]:border-b [&>li]:border-line" data-testid="trip-legs">
            {j.legs.map((l, i) => (
              <li key={i} onMouseEnter={() => setActive(i)} onMouseLeave={() => setActive(null)} className={"px-3 py-2 transition " + (active === i ? "bg-surface2" : "")}>
                {(l.wait_before_min ?? 0) > 0 && l.kind === "transit" && (
                  <p className="mono mb-1.5 border-l-2 border-warn pl-2 text-[12px] text-warn">wait {l.wait_before_min} min at {l.from}</p>
                )}
                <div className="flex items-center gap-2">
                  <span className="mono grid size-5 place-items-center text-[12px] font-extrabold text-[#04110a]" style={{ background: colour(l) }}>{l.kind === "walk" ? "→" : l.stage}</span>
                  <span className="mono text-[12px] font-extrabold" style={{ color: colour(l) }}>{label(l)}</span>
                  {l.headsign && <span className="truncate text-[12px] text-muted">to {l.headsign}</span>}
                  <span className="mono ml-auto text-[12px] font-bold tabular-nums">{fmtDist(l.distance_m)}</span>
                </div>
                <p className="mt-1 text-[12px]">{l.from} <span className="text-muted">→</span> {l.to}</p>
                <p className="mono mt-0.5 flex flex-wrap gap-x-3 text-[12px] text-muted">
                  <span>{l.depart.split(" ").pop()} → {l.arrive.split(" ").pop()}</span>
                  {l.num_stops > 0 && <span>{l.num_stops} stops</span>}
                  {l.realtime && <span className={l.realtime.status === "cancelled" ? "font-bold text-bad" : l.realtime.status === "delayed" ? "font-bold text-warn" : "text-ok"}>{l.realtime.status.replace("_", " ")}{l.realtime.delay_min ? ` ${l.realtime.delay_min > 0 ? "+" : ""}${l.realtime.delay_min} min` : ""}</span>}
                </p>
              </li>
            ))}
          </ol>
        </div>
      </div>
      <p className="mono border-t border-line px-4 py-2 text-[12px] text-muted">
        {art.realtime?.source === "realtime" ? `realtime ${art.realtime.feed_age_s ?? "?"}s old` : "timetable only (no realtime)"} · {art.measurement_notes}
      </p>
    </section>
  );
}
