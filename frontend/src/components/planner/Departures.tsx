import { useEffect, useState } from "react";
import type { Api } from "../../api/client";
import { ApiError, type DepartureRow, type DeparturesResponse } from "../../api/types";
import { fmtClock } from "../../lib/journey";
import { MODE_COLOUR } from "../../lib/modes";

/** Live departures board for one stop or station (timetable + realtime), refreshed every 30 s. */
export function Departures({ api, stop, name, now, onPick, activeTrip }: { api: Api; stop: string | null; name: string; now: Date; onPick?: (d: DepartureRow) => void; activeTrip?: string | null }) {
  const [data, setData] = useState<DeparturesResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!stop) return;
    let live = true;
    const load = () => {
      setLoading(true);
      api.departures(stop, 10)
        .then((d) => { if (live) { setData(d); setErr(null); } })
        .catch((e: unknown) => { if (live) setErr(e instanceof ApiError ? e.message : "Could not load departures."); })
        .finally(() => { if (live) setLoading(false); });
    };
    load();
    const t = setInterval(load, 30_000);
    return () => { live = false; clearInterval(t); };
  }, [api, stop]);

  if (!stop) return <p className="rounded-2xl border border-dashed border-line p-5 text-center text-[14px] text-muted">Choose a starting point (A) to see what is leaving from it.</p>;
  return (
    <section aria-label={`Departures from ${name}`}>
      <div className="mb-2 flex items-center gap-2">
        <h2 className="text-[15px] font-extrabold">Leaving {name}</h2>
        {loading && <span aria-hidden className="size-3.5 animate-spin rounded-full border-2 border-muted border-t-transparent" />}
        {data?.realtime && <span className={"ml-auto rounded-full px-2 py-0.5 text-[11.5px] font-bold " + (data.realtime.source === "realtime" ? "bg-ok-bg text-ok" : "bg-warn-bg text-warn")}>{data.realtime.source === "realtime" ? "Live" : "Timetable only"}</span>}
      </div>
      {onPick && data && data.departures.length > 0 && <p className="mb-2 text-[12.5px] text-muted">Tap a service to see where it is right now and the route it takes.</p>}
      {err && <p role="alert" className="rounded-xl bg-bad-bg px-3 py-2 text-[13px] text-bad">{err}</p>}
      {data && data.departures.length === 0 && !err && <p className="text-[14px] text-muted">{data.note ?? "No departures soon."}</p>}
      <ul className="divide-y divide-line overflow-hidden rounded-2xl border border-line bg-surface">
        {data?.departures.map((d, i) => {
          const when = new Date(d.predicted_iso ?? d.scheduled_iso);
          const mins = Math.round((when.getTime() - now.getTime()) / 60_000);
          const bad = d.status === "cancelled", late = d.status === "delayed";
          return (
            <li key={i}>
              <button type="button" onClick={() => onPick?.(d)} disabled={!onPick} title={onPick ? "Show where this service is on the map" : undefined}
                className={"flex w-full items-center gap-3 px-3.5 py-2.5 text-left transition " + (onPick ? "hover:bg-surface2 " : "") + (activeTrip && activeTrip === d.trip_id ? "bg-accent/10 " : "")}>
              <span className="min-w-12 rounded-full px-2 py-0.5 text-center text-[13px] font-extrabold text-white" style={{ background: MODE_COLOUR[d.mode] ?? "var(--route-bus)" }}>{d.route}</span>
              <span className="min-w-0 flex-1"><span className="block truncate text-[14px] font-semibold">{d.headsign}</span>
                <span className="block truncate text-[12px] text-muted">{d.stop_name}{d.platform ? ` · platform ${d.platform}` : ""}</span></span>
              <span className="text-right"><span className={"block text-[14px] font-extrabold tabular-nums " + (bad ? "text-bad line-through" : "")}>{fmtClock(when)}</span>
                <span className={"block text-[12px] font-semibold " + (bad ? "text-bad" : late ? "text-warn" : "text-muted")}>{bad ? "Cancelled" : late ? `${Math.round(d.delay_min ?? 0)} min late` : mins <= 0 ? "Now" : `in ${mins} min`}</span></span>
              {onPick && <svg aria-hidden width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-accent"><path d="M12 21s7-6.2 7-12a7 7 0 0 0-14 0c0 5.8 7 12 7 12z" /><circle cx="12" cy="9" r="2.5" /></svg>}
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
