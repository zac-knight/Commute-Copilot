import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ALL_MODES, MODE_LABEL } from "../lib/modes";
import { ApiError, type AlertRow, type DepartureRow, type PlanResponse } from "../api/types";
import { Departures } from "../components/planner/Departures";
import { JourneyCard } from "../components/planner/JourneyCard";
import { PlaceField, rememberPlace, type PlaceSel } from "../components/planner/PlaceField";
import { RouteMap, type Focus } from "../components/planner/RouteMap";
import {
  DEFAULT_FILTERS, SORTS, applyFilters, brisbaneParts, dayOptions, decodeSearch, encodeSearch, fmtClock, isCoord, isoDate, isoTime,
  sortJourneys, toJourneys, toWhen, type Filters, type Point, type SortKey, type ViaState,
} from "../lib/journey";
import { useApp } from "../state/AppContext";
import { useTheme } from "../state/theme";

const ico = { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true } as const;
type Tab = "plan" | "departures";
const point = (p: PlaceSel | null): Point | null => (p && p.lat !== undefined && p.lon !== undefined && (p.kind === "address" || p.kind === "gps") ? { name: p.name, lat: p.lat, lon: p.lon } : null);
const errMsg = (e: unknown) => (e instanceof ApiError ? (e.status === 404 && e.kind === "http_error" ? "The running backend is older than this UI. Restart it (./start.sh)." : e.message) : e instanceof Error ? e.message : "Something went wrong");

const QUICK_HUES: [string, string][] = [["--calendar", "--calendar-bg"], ["--weather", "--weather-bg"], ["--coral", "--coral-bg"], ["--ok", "--ok-bg"], ["--transit", "--transit-bg"]];
const heroClass = (d: Date): string => { const h = brisbaneParts(d).h; return h < 5 ? "hero-night" : h < 11 ? "hero-morning" : h < 17 ? "hero-afternoon" : h < 21 ? "hero-evening" : "hero-night"; };
const greeting = (d: Date): string => { const h = brisbaneParts(d).h; return h < 5 ? "Up late?" : h < 12 ? "Good morning" : h < 17 ? "Good arvo" : h < 21 ? "Good evening" : "Late one?"; };
const mkPlace = (id: string, name: string): PlaceSel => {
  const [la, lo] = isCoord(id) ? id.split(",").map(Number) : [undefined, undefined];
  return { id, name, kind: isCoord(id) ? "gps" : "stop", lat: la, lon: lo };
};

export function PlannerPage() {
  const { api, health } = useApp();
  const [theme] = useTheme();
  const [params, setParams] = useSearchParams();

  const [now0] = useState(() => new Date());
  const [boot] = useState(() => {
    const s = decodeSearch(params.toString());
    if (!s) return null;
    return { s, a: mkPlace(s.from!, s.fromName!), z: mkPlace(s.to!, s.toName!), v: (s.vias ?? []).map((x) => mkPlace(x.id, x.name)) };
  });
  const [fromText, setFromText] = useState(boot?.a.name ?? ""), [toText, setToText] = useState(boot?.z.name ?? "");
  const [from, setFrom] = useState<PlaceSel | null>(boot?.a ?? null), [to, setTo] = useState<PlaceSel | null>(boot?.z ?? null);
  const [date, setDate] = useState(() => boot?.s.date ?? isoDate(now0)), [time, setTime] = useState(() => boot?.s.time ?? isoTime(now0));
  const [by, setBy] = useState<"leave" | "arrive">(boot?.s.by ?? "leave");
  const [changes, setChanges] = useState(boot?.s.changes ?? 2);
  const [vias, setVias] = useState<{ key: number; text: string; sel: PlaceSel | null }[]>(() => (boot?.v ?? []).map((v, i) => ({ key: i, text: v.name, sel: v })));
  const [stay, setStay] = useState(boot?.s.stay ?? 5);
  const viaKey = useRef(10);
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [sort, setSort] = useState<SortKey>("depart");
  const [tab, setTab] = useState<Tab>("plan");
  const [showAdv, setShowAdv] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const [res, setRes] = useState<PlanResponse | null>(null);
  const [searched, setSearched] = useState<{ from: PlaceSel | null; to: PlaceSel | null; by: "leave" | "arrive" } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [hoverLeg, setHoverLeg] = useState<number | null>(null);
  const [focus, setFocus] = useState<Focus>({ kind: "fit", nonce: 0 });
  const [alertStore, setAlertStore] = useState<{ key: string; rows: AlertRow[] }>({ key: "", rows: [] });
  const [copied, setCopied] = useState(false);
  const [focusTrip, setFocusTrip] = useState<{ tripId: string; label: string; nonce: number } | null>(null);
  const mapBox = useRef<HTMLDivElement>(null);
  const [saved, setSaved] = useState<PlaceSel[]>([]);
  const [now, setClock] = useState(() => new Date());

  const nonce = useRef(1);
  const fromRef = useRef<HTMLInputElement>(null);
  const abort = useRef<AbortController | null>(null);
  const lastKey = useRef("");

  useEffect(() => { const t = setInterval(() => setClock(new Date()), 20_000); return () => clearInterval(t); }, []);

  useEffect(() => {
    api.places("", false).then((h) => {
      const sv = h.filter((x) => x.kind === "saved").map((x) => ({ id: x.id, name: x.name, kind: x.kind, lat: x.lat, lon: x.lon }));
      setSaved(sv);
      const home = sv.find((x) => x.id === "home");
      if (home && !boot) setFocus({ kind: "point", lat: home.lat, lon: home.lon, zoom: 14, nonce: nonce.current++ }); // idle map starts on home
    }).catch(() => undefined);
  }, [api, boot]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "/" && !(e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLSelectElement)) { e.preventDefault(); fromRef.current?.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  /** Turn what the user typed or picked into something the backend resolves (id, alias or "lat,lon"). */
  const resolve = useCallback(async (sel: PlaceSel | null, text: string): Promise<PlaceSel | null> => {
    if (sel) return sel;
    const t = text.trim();
    if (!t) return null;
    if (isCoord(t)) { const [la, lo] = t.split(",").map(Number); return { id: t.replace(/\s/g, ""), name: t, kind: "gps", lat: la, lon: lo }; }
    try {
      const hit = (await api.places(t, false))[0];
      if (hit) return { id: hit.id, name: hit.name, kind: hit.kind, lat: hit.lat, lon: hit.lon };
    } catch { /* fall through to the raw text */ }
    return { id: t, name: t, kind: "stop" };
  }, [api]);

  const run = useCallback(async (o: { from?: PlaceSel | null; to?: PlaceSel | null; fromText?: string; toText?: string; date?: string; time?: string; by?: "leave" | "arrive"; changes?: number; vias?: PlaceSel[]; stay?: number } = {}) => {
    const st = o.stay ?? stay;
    const d = o.date ?? date, t = o.time ?? time, b = o.by ?? by, c = o.changes ?? changes;
    setError(null);
    const [a, z] = await Promise.all([resolve(o.from !== undefined ? o.from : from, o.fromText ?? fromText), resolve(o.to !== undefined ? o.to : to, o.toText ?? toText)]);
    const viaPlaces = o.vias ?? (await Promise.all(vias.map((v) => resolve(v.sel, v.text)))).filter((x): x is PlaceSel => !!x);
    setStay(st);
    setDate(d); setTime(t); setBy(b); setChanges(c);
    if (!a || !z) { setError(!a && !z ? "Enter where you are starting and where you are going." : !a ? "Enter a starting point." : "Enter a destination."); return; }
    if (a.id === z.id) { setError("Start and destination are the same place."); return; }
    setFrom(a); setTo(z); setFromText(a.name); setToText(z.name);
    setVias(viaPlaces.map((v, i) => ({ key: viaKey.current + i, text: v.name, sel: v }))); viaKey.current += viaPlaces.length + 1;
    abort.current?.abort();
    const ctl = new AbortController();
    abort.current = ctl;
    setLoading(true); setTab("plan");
    try {
      const r = await api.plan({ origin: a.id, destination: z.id, when: toWhen(d, t), by: b, maxTransfers: c, via: viaPlaces.map((v) => v.id), stay: st }, ctl.signal);
      setClock(new Date());
      setRes(r); setSearched({ from: a, to: z, by: b }); setSelected(null);
      rememberPlace(a); rememberPlace(z); viaPlaces.forEach(rememberPlace);
      const enc = encodeSearch({ from: a.id, fromName: a.name, to: z.id, toName: z.name, date: d, time: t, by: b, changes: c, vias: viaPlaces.map((v): ViaState => ({ id: v.id, name: v.name })), stay: st });
      lastKey.current = enc;
      setParams(enc, { replace: true });
      setFocus({ kind: "fit", nonce: nonce.current++ });
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setError(errMsg(e)); setRes(null);
    } finally {
      if (abort.current === ctl) setLoading(false);
    }
  }, [api, resolve, from, to, fromText, toText, date, time, by, changes, vias, stay, setParams]);

  // run the search described by the URL: on first load, and when a shared link is opened in this tab
  useEffect(() => {
    const key = params.toString();
    if (key === lastKey.current) return;
    lastKey.current = key;
    const s = decodeSearch(key);
    if (!s) return;
    const a = mkPlace(s.from!, s.fromName!), z = mkPlace(s.to!, s.toName!);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- syncing state from the URL (an external source)
    void run({ from: a, to: z, date: s.date, time: s.time, by: s.by, changes: s.changes, vias: (s.vias ?? []).map((x) => mkPlace(x.id, x.name)), stay: s.stay });
  }, [params]); // eslint-disable-line react-hooks/exhaustive-deps

  const all = useMemo(() => (res?.artifact ? toJourneys(res.artifact, point(searched?.from ?? null), point(searched?.to ?? null)) : []), [res, searched]);
  const shown = useMemo(() => sortJourneys(applyFilters(all, filters), sort), [all, filters, sort]);
  const sel = shown.find((j) => j.id === selected) ?? shown[0] ?? null;
  const hidden = all.length - shown.length;

  // alerts for the routes of the selected journey (external text: rendered as text only)
  const routesKey = sel?.routes.join(",") ?? "";
  useEffect(() => {
    if (!routesKey) return;
    let live = true;
    api.alerts(routesKey.split(",")).then((r) => { if (live) setAlertStore({ key: routesKey, rows: r.alerts }); }).catch(() => { if (live) setAlertStore({ key: routesKey, rows: [] }); });
    return () => { live = false; };
  }, [api, routesKey]);

  const alerts = alertStore.key === routesKey ? alertStore.rows : [];
  const swap = () => { const f = from, t = to, ft = fromText, tt = toText; setFrom(t); setTo(f); setFromText(tt); setToText(ft); setVias((v) => [...v].reverse()); };
  const addVia = () => setVias((v) => (v.length >= 3 ? v : [...v, { key: viaKey.current++, text: "", sel: null }]));
  const moveVia = (i: number) => setVias((v) => { const n = [...v]; const a = n[i - 1], b = n[i]; if (a && b) { n[i - 1] = b; n[i] = a; } return n; });
  const setNow = () => { const n = new Date(); setDate(isoDate(n)); setTime(isoTime(n)); setBy("leave"); };
  const shift = (j: "earlier" | "later") => {
    if (!shown.length) return;
    const starts = all.map((x) => x.start.getTime());
    const base = j === "later" ? Math.max(...starts) + 60_000 : Math.min(...starts) - 45 * 60_000;
    const d = new Date(base);
    setDate(isoDate(d)); setTime(isoTime(d)); setBy("leave");
    void run({ date: isoDate(d), time: isoTime(d), by: "leave" });
  };
  const share = async () => {
    try { await navigator.clipboard.writeText(window.location.href); setCopied(true); setTimeout(() => setCopied(false), 1800); } catch { setError("Could not copy the link; copy it from the address bar."); }
  };
  const fetchVehicles = useCallback((b: [number, number, number, number], trips: string[], signal: AbortSignal, modes?: string[]) => api.vehicles(b, trips, signal, modes), [api]);
  const fetchTrainLines = useCallback(() => api.trainLines(), [api]);
  const fetchTripPath = useCallback((id: string) => api.tripPath(id), [api]);
  const showDeparture = useCallback((d: DepartureRow) => {
    setFocusTrip({ tripId: d.trip_id, label: d.mode === "train" ? d.route_name || "Train" : `Bus ${d.route}`, nonce: nonce.current++ });
    mapBox.current?.scrollIntoView({ block: "start", behavior: "smooth" }); // on a phone the map is above the list
  }, []);
  const focusPoint = (lat: number, lon: number) => setFocus({ kind: "point", lat, lon, nonce: nonce.current++ });
  const pickLeg = (i: number) => setFocus({ kind: "leg", index: i, nonce: nonce.current++ });
  const days = useMemo(() => { const o = dayOptions(now0); return o.some((x) => x.value === date) ? o : [{ value: date, label: date }, ...o]; }, [now0, date]);
  const quick = saved.filter((s) => s.id !== "home");
  const homeSel = saved.find((s) => s.id === "home") ?? null;
  const modesInResults = ALL_MODES.filter((m) => all.some((j) => j.legs.some((l) => l.mode === m)));
  const modeChips = modesInResults.length > 1 ? modesInResults : [];
  const modeCount = ALL_MODES.filter((m) => filters[m]).length;

  const extra = (
    <div className="no-print mt-3 space-y-2">
      {alerts.length > 0 && (
        <div role="region" aria-label="Service alerts" className="rounded-xl border border-line bg-warn-bg p-3 text-warn">
          <p className="text-[13px] font-extrabold">Service alerts on this trip ({alerts.length})</p>
          <ul className="mt-1 space-y-1.5">{alerts.slice(0, 3).map((a) => (
            <li key={a.alert_id} className="text-[13px]"><span className="font-bold">{a.header}</span>{a.description && <span className="mt-0.5 block text-[12.5px] opacity-90">{a.description.slice(0, 260)}{a.description.length > 260 ? "…" : ""}</span>}
              {/^https:\/\//.test(a.url) && <a href={a.url} target="_blank" rel="noreferrer noopener" className="text-[12.5px] font-bold underline">Details</a>}</li>
          ))}</ul>
        </div>
      )}
      <div className="flex gap-2">
        <button type="button" onClick={share} className="flex-1 rounded-xl border border-line bg-surface px-3 py-2 text-[13px] font-bold hover:bg-surface2">{copied ? "Link copied ✓" : "Copy link"}</button>
        <button type="button" onClick={() => window.print()} className="flex-1 rounded-xl border border-line bg-surface px-3 py-2 text-[13px] font-bold hover:bg-surface2">Print</button>
      </div>
    </div>
  );

  const fieldBtn = "h-11 w-full rounded-xl border border-line bg-surface px-3 text-[14px] outline-none focus:border-accent focus:ring-2 focus:ring-accent/25";

  return (
    <div className="flex h-full min-h-0 flex-col overflow-y-auto md:flex-row md:overflow-hidden" data-testid="planner">
      {/* ---------------- side panel ---------------- */}
      <h1 className="sr-only">Plan a journey</h1>
      <section className={"relative shrink-0 overflow-hidden border-line bg-bg transition-[width,opacity] duration-300 ease-out md:border-r " + (expanded ? "md:w-0 md:border-r-0 md:opacity-0" : "md:w-[clamp(380px,29vw,450px)]")} aria-label="Journey planner" aria-hidden={expanded}>
       {loading && <div className="absolute inset-x-0 top-0 z-10 h-0.5 overflow-hidden bg-accent/15" role="progressbar" aria-label="Searching"><div className="bar h-full w-1/3 bg-accent" /></div>}
       <div className="flex h-full min-h-0 flex-col md:w-[clamp(380px,29vw,450px)]">
        <div className="shrink-0 space-y-2.5 border-b border-line bg-surface px-3.5 pb-3 pt-3">
          <div className="flex items-center gap-1" role="tablist" aria-label="Planner views">
            {([["plan", "Plan a journey"], ["departures", "Departures"]] as const).map(([k, l]) => (
              <button key={k} role="tab" type="button" aria-selected={tab === k} onClick={() => setTab(k)}
                className={"rounded-full px-3.5 py-1.5 text-[13.5px] font-bold transition " + (tab === k ? "pill-on" : "text-muted hover:bg-surface2 hover:text-ink")}>{l}</button>
            ))}
            <span className="ml-auto flex items-center gap-1.5 text-[12px] font-semibold text-muted">
              <span className="size-2 rounded-full bg-ok" aria-hidden />{health?.demo ? "Demo data" : "Live · SEQ"}
            </span>
          </div>

          <form onSubmit={(e) => { e.preventDefault(); void run(); }} className="space-y-2" aria-label="Plan your journey">
            <div className="flex items-stretch gap-2">
              <div className="min-w-0 flex-1 space-y-2">
                <PlaceField label="From: stop, station, address" marker="A" text={fromText} onText={setFromText} value={from} onPick={setFrom} api={api} inputRef={fromRef} allowGps onEnter={() => void run()} />
                {vias.map((v, i) => (
                  <div key={v.key} className="flex items-center gap-1.5">
                    <div className="min-w-0 flex-1"><PlaceField label={`Stop ${i + 1}: stop, station, address`} marker={String(i + 1)} markerColor="#d97706" text={v.text} onText={(t) => setVias((x) => x.map((y) => (y.key === v.key ? { ...y, text: t } : y)))} value={v.sel} onPick={(p) => setVias((x) => x.map((y) => (y.key === v.key ? { ...y, sel: p } : y)))} api={api} onEnter={() => void run()} /></div>
                    <div className="flex flex-col">
                      {i > 0 && <button type="button" onClick={() => moveVia(i)} aria-label={`Move stop ${i + 1} up`} title="Move up" className="grid h-5 w-7 place-items-center rounded text-muted hover:bg-surface2 hover:text-ink">▲</button>}
                      <button type="button" onClick={() => setVias((x) => x.filter((y) => y.key !== v.key))} aria-label={`Remove stop ${i + 1}`} title="Remove stop" className="grid h-5 w-7 place-items-center rounded text-muted hover:bg-bad-bg hover:text-bad">✕</button>
                    </div>
                  </div>
                ))}
                <PlaceField label="To: stop, station, address" marker="B" text={toText} onText={setToText} value={to} onPick={setTo} api={api} onEnter={() => void run()} />
              </div>
              <button type="button" onClick={swap} aria-label="Swap start and destination" data-tip="Swap start and destination" className="tip tip-right grid w-11 place-items-center rounded-xl border border-line bg-surface text-accent hover:bg-surface2">
                <svg {...ico} width={20} height={20}><path d="M7 4v16M7 20l-3-3M7 20l3-3M17 20V4M17 4l-3 3M17 4l3 3" /></svg>
              </button>
            </div>
            {tab === "plan" && (<>
              <div className="flex items-center gap-2">
                <button type="button" onClick={addVia} disabled={vias.length >= 3} className="flex items-center gap-1.5 rounded-full border border-dashed border-line px-3 py-1 text-[13px] font-bold text-accent transition hover:border-accent hover:bg-accent/10 disabled:cursor-not-allowed disabled:opacity-50" data-testid="add-stop">
                  <span aria-hidden className="text-[16px] leading-none">+</span>Add stop{vias.length > 0 ? ` (${vias.length}/3)` : ""}
                </button>
                {vias.length > 0 && (
                  <label className="ml-auto flex items-center gap-1.5 text-[12.5px] text-muted">Time at each stop
                    <select value={stay} onChange={(e) => setStay(Number(e.target.value))} aria-label="Time at each stop" className="h-8 rounded-lg border border-line bg-surface px-1.5 text-[13px] font-semibold text-ink">
                      {[0, 5, 10, 15, 30, 45, 60, 90, 120].map((m) => <option key={m} value={m}>{m === 0 ? "None" : m < 60 ? `${m} min` : `${m / 60} hr`}</option>)}
                    </select>
                  </label>
                )}
              </div>
              <div className="grid grid-cols-[1fr_7.5rem] gap-2">
                <select aria-label="Day" value={date} onChange={(e) => setDate(e.target.value)} className={fieldBtn}>{days.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}</select>
                <input aria-label="Time" type="time" value={time} onChange={(e) => setTime(e.target.value)} className={fieldBtn + " tabular-nums"} />
              </div>
              <div className="flex gap-2">
                <div role="radiogroup" aria-label="Time applies to" className="grid flex-1 grid-cols-2 rounded-xl border border-line bg-surface2 p-0.5">
                  {([["leave", "Leave after"], ["arrive", "Arrive before"]] as const).map(([k, l]) => (
                    <button key={k} type="button" role="radio" aria-checked={by === k} onClick={() => setBy(k)}
                      className={"h-10 rounded-[10px] text-[13.5px] font-bold transition " + (by === k ? "pill-on shadow" : "text-muted hover:text-ink")}>{by === k && "✓ "}{l}</button>
                  ))}
                </div>
                <button type="button" onClick={setNow} className="rounded-xl border border-line bg-surface px-3.5 text-[13.5px] font-bold hover:bg-surface2">Now</button>
              </div>
              <div className="flex items-center gap-2">
                <button type="button" onClick={() => setShowAdv((s) => !s)} aria-expanded={showAdv} className="text-[13.5px] font-semibold text-accent underline decoration-dotted underline-offset-4">Advanced options{(changes !== 2 || modeCount < 2 || filters.maxWalkM !== null) ? " •" : ""}</button>
                <button type="submit" disabled={loading} className="btn-brand ml-auto flex h-11 min-w-36 items-center justify-center gap-2 rounded-xl px-5 text-[15px] font-extrabold transition hover:brightness-110 disabled:opacity-70">
                  {loading ? <><span aria-hidden className="size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />Searching…</> : "Find a journey"}
                </button>
              </div>
              {showAdv && (
                <div className="grid grid-cols-2 gap-x-3 gap-y-2 rounded-xl border border-line bg-surface2 p-3 text-[13px]" data-testid="advanced">
                  <label className="space-y-1"><span className="font-bold">Max changes</span>
                    <select value={changes} onChange={(e) => setChanges(Number(e.target.value))} className={fieldBtn + " !h-9"}>{[0, 1, 2, 3, 4].map((n) => <option key={n} value={n}>{n === 0 ? "Direct only" : `Up to ${n}`}</option>)}</select></label>
                  <label className="space-y-1"><span className="font-bold">Max walking</span>
                    <select value={filters.maxWalkM ?? ""} onChange={(e) => setFilters({ ...filters, maxWalkM: e.target.value ? Number(e.target.value) : null })} className={fieldBtn + " !h-9"}>
                      <option value="">Any</option><option value="400">400 m</option><option value="800">800 m</option><option value="1200">1.2 km</option></select></label>
                  <fieldset className="col-span-2 flex flex-wrap items-center gap-x-4 gap-y-1"><legend className="sr-only">Modes</legend>
                    {ALL_MODES.map((m) => <label key={m} className="flex items-center gap-1.5"><input type="checkbox" checked={filters[m]} onChange={(e) => setFilters({ ...filters, [m]: e.target.checked })} />{MODE_LABEL[m]}</label>)}
                    <label className="flex items-center gap-1.5"><input type="checkbox" checked={filters.hideCancelled} onChange={(e) => setFilters({ ...filters, hideCancelled: e.target.checked })} />Hide cancelled</label>
                  </fieldset>
                </div>
              )}
            </>)}
          </form>
        </div>

        {/* results */}
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3.5 py-3" aria-live="polite">
          {error && <p role="alert" className="rounded-xl border border-bad/40 bg-bad-bg px-3 py-2.5 text-[13.5px] font-semibold text-bad">{error}</p>}

          {tab === "departures" && <div className="rise"><Departures api={api} stop={from && !isCoord(from.id) ? from.id : null} name={from?.name ?? ""} now={now} onPick={showDeparture} activeTrip={focusTrip?.tripId} /></div>}

          {tab === "plan" && !res && !loading && !error && (
            <div className="space-y-5 pt-1">
              <div className={"hero rounded-3xl px-5 py-5 shadow-lg " + heroClass(now)}>
                <h2 className="relative z-10 text-[24px] font-extrabold leading-tight">{greeting(now)}</h2>
                <p className="relative z-10 mt-0.5 text-[12px] font-bold uppercase tracking-wider text-white/85">South East Queensland</p>
                <p className="relative z-10 mt-1.5 max-w-[17rem] text-[13.5px] text-white/95">Where to? Buses, trains, trams and ferries, live on the map. Press <kbd className="rounded border border-white/50 bg-white/15 px-1.5 text-[12px]">/</kbd> to search.</p>
              </div>
              {homeSel && quick.length > 0 && (
                <div><p className="label mb-2">Quick trips from {homeSel.name}</p>
                  <div className="grid gap-2">{quick.map((q, qi) => (
                    <button key={q.id} type="button" onClick={() => { setFrom(homeSel); setFromText(homeSel.name); setTo(q); setToText(q.name); void run({ from: homeSel, to: q }); }}
                      className="group flex items-center gap-3 rounded-2xl border border-line bg-surface px-3.5 py-3 text-left shadow-sm transition hover:-translate-y-px hover:border-accent hover:shadow-md">
                      <span className="grid size-10 place-items-center rounded-2xl" style={{ background: `var(${QUICK_HUES[qi % QUICK_HUES.length]![1]})`, color: `var(${QUICK_HUES[qi % QUICK_HUES.length]![0]})` }}><svg {...ico}><path d="M5 12h14M13 6l6 6-6 6" /></svg></span>
                      <span className="min-w-0 flex-1"><span className="block truncate text-[14.5px] font-bold">{q.name}</span><span className="block text-[12px] text-muted">Leave now · {q.id}</span></span>
                      <span className="text-muted transition group-hover:translate-x-0.5 group-hover:text-accent">→</span>
                    </button>))}</div></div>
              )}
              {homeSel && <Departures api={api} stop="home" name={homeSel.name} now={now} onPick={showDeparture} activeTrip={focusTrip?.tripId} />}
            </div>
          )}

          {tab === "plan" && loading && !res && (
            <ul className="space-y-3" aria-label="Searching">{[0, 1, 2].map((i) => <li key={i} className="skeleton h-28 rounded-2xl border border-line" style={{ animationDelay: `${i * 120}ms` }} />)}</ul>
          )}

          {tab === "plan" && res && (
            <>
              <div className="flex items-center gap-2">
                <h2 className="text-[16px] font-extrabold">{shown.length === 0 ? "No options" : `${shown.length} travel option${shown.length === 1 ? "" : "s"}`}</h2>
                {loading && <span aria-hidden className="size-3.5 animate-spin rounded-full border-2 border-muted border-t-transparent" />}
                <label className="ml-auto flex items-center gap-1.5 text-[12.5px] text-muted">Sort
                  <select value={sort} onChange={(e) => setSort(e.target.value as SortKey)} className="h-8 rounded-lg border border-line bg-surface px-2 text-[13px] font-semibold text-ink">{SORTS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}</select></label>
              </div>
              <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter options">
                {modeChips.map((k) => ([k, MODE_LABEL[k]] as const)).map(([k, l]) => (
                  <button key={k} type="button" aria-pressed={filters[k]} onClick={() => setFilters({ ...filters, [k]: !filters[k] })}
                    className={"rounded-full border px-3 py-1 text-[12.5px] font-bold transition " + (filters[k] ? "border-transparent pill-on" : "border-line text-muted line-through hover:text-ink")}>{l}</button>
                ))}
                <button type="button" aria-pressed={filters.maxWalkM === 800} onClick={() => setFilters({ ...filters, maxWalkM: filters.maxWalkM === 800 ? null : 800 })}
                  className={"rounded-full border px-3 py-1 text-[12.5px] font-bold transition " + (filters.maxWalkM === 800 ? "border-transparent pill-on" : "border-line text-muted hover:text-ink")}>Walk ≤ 800 m</button>
                <button type="button" aria-pressed={changes === 0} onClick={() => { const c = changes === 0 ? 2 : 0; setChanges(c); void run({ changes: c }); }}
                  className={"rounded-full border px-3 py-1 text-[12.5px] font-bold transition " + (changes === 0 ? "border-transparent pill-on" : "border-line text-muted hover:text-ink")}>Direct only</button>
              </div>
              {res.note && <p className="rounded-xl bg-warn-bg px-3 py-2 text-[13px] text-warn">{res.note}</p>}
              {hidden > 0 && <p className="text-[12.5px] text-muted">{hidden} option{hidden === 1 ? "" : "s"} hidden by your filters. <button type="button" className="font-bold text-accent underline" onClick={() => setFilters(DEFAULT_FILTERS)}>Show all</button></p>}
              {all.length > 0 && <button type="button" onClick={() => shift("earlier")} disabled={loading} className="w-full rounded-xl border border-dashed border-line py-2 text-[13px] font-bold text-muted hover:border-accent hover:text-accent">↑ Earlier services</button>}
              <ol className={"space-y-3 transition-opacity duration-200 " + (loading ? "opacity-50" : "")} aria-label="Travel options">
                {shown.map((j, i) => (
                  <JourneyCard key={j.id} delay={Math.min(i, 8) * 55} j={j} all={shown} index={i} now={now} selected={sel?.id === j.id}
                    onSelect={() => { setSelected(j.id); setFocus({ kind: "fit", nonce: nonce.current++ }); }}
                    activeLeg={sel?.id === j.id ? hoverLeg : null} onHoverLeg={setHoverLeg} onPickLeg={pickLeg} onFocusPoint={focusPoint} extra={sel?.id === j.id ? extra : undefined} />
                ))}
              </ol>
              {all.length > 0 && <button type="button" onClick={() => shift("later")} disabled={loading} className="w-full rounded-xl border border-dashed border-line py-2 text-[13px] font-bold text-muted hover:border-accent hover:text-accent">↓ Later services</button>}
              <p className="pb-2 text-[11.5px] leading-snug text-muted">
                {res.realtime?.source === "realtime" ? `Realtime ${res.realtime.feed_age_s ?? "?"}s old.` : "Timetable only (no realtime)."} Times are Brisbane time as of {fmtClock(now)}. Contains data from TransLink (Queensland Government), CC BY 4.0. {res.artifact?.measurement_notes} Walks to or from an address are straight-line estimates. No fare estimate: the timetable feed has no fare data.
              </p>
            </>
          )}
        </div>
       </div>
      </section>

      {/* ---------------- map ---------------- */}
      <div ref={mapBox} className="sticky top-0 z-10 order-first h-[40dvh] shrink-0 md:static md:order-none md:h-auto md:min-w-0 md:flex-1">
        <RouteMap art={res?.artifact ?? null} journey={sel} origin={point(searched?.from ?? null)} dest={point(searched?.to ?? null)}
          activeLeg={hoverLeg} onHoverLeg={setHoverLeg} onPickLeg={pickLeg} focus={focus} expanded={expanded} onToggleExpand={() => setExpanded((e) => !e)} dark={theme === "dark"} fetchVehicles={fetchVehicles} fetchTripPath={fetchTripPath} fetchTrainLines={fetchTrainLines} focusTrip={focusTrip} />
        {!sel && !loading && (
          <div className="pointer-events-none absolute inset-x-0 bottom-6 z-[400] flex justify-center px-4">
            <p className="rounded-full border border-line bg-surface/95 px-4 py-2 text-[13.5px] font-semibold shadow-lg">{res ? "Nothing to show for this search" : "Your route will appear here. Scroll to zoom, drag to pan."}</p>
          </div>
        )}
      </div>
      <span className="sr-only" aria-live="polite">{sel ? `Selected option: leave ${fmtClock(sel.start)}, arrive ${fmtClock(sel.end)}` : ""}</span>
    </div>
  );
}
