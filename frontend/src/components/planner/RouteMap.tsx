import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "maplibre-gl/dist/maplibre-gl.css";
import "@maplibre/maplibre-gl-leaflet";
import { setWorkerUrl } from "maplibre-gl";
import { darkStyle } from "../../lib/darkstyle";
import { serviceColour } from "../../lib/modes";
import { LINE_FAMILIES, lineNames, trainLine } from "../../lib/trainlines";
import maplibreWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { TrainLineRoute, TripArtifact, TripPath, VehicleRow, VehiclesResponse } from "../../api/types";
import { fmtDist } from "../../lib/tripmap";
import { legTitle, type Point, type VJourney, type VLeg } from "../../lib/journey";

export type Focus =
  | { kind: "fit"; nonce: number }
  | { kind: "box"; box: [number, number, number, number]; nonce: number }
  | { kind: "leg"; index: number; nonce: number }
  | { kind: "point"; lat: number; lon: number; zoom?: number; nonce: number };

setWorkerUrl(maplibreWorkerUrl); // Vite serves the worker file itself; without this MapLibre cannot find it

type Base = "vector" | "clean" | "dark" | "classic" | "satellite" | "plain";
/** Vector basemaps (OpenFreeMap, no key): redrawn at every zoom and screen density, so always sharp.
 *  Dark is the minimal style recoloured in CSS. Raster tiles stay as the "Classic" option and the fallback. */
const VECTOR: Partial<Record<Base, string>> = {
  vector: "https://tiles.openfreemap.org/styles/liberty",
  clean: "https://tiles.openfreemap.org/styles/positron",
};
const VECTOR_ATTRIBUTION = '<a href="https://openfreemap.org" target="_blank" rel="noreferrer">OpenFreeMap</a> © <a href="https://www.openmaptiles.org/" target="_blank" rel="noreferrer">OpenMapTiles</a> Data from <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a>';
const TILES: Record<"classic" | "satellite", { url: string; attribution: string; maxNative: number }> = {
  // map = light (Esri World Street Map: soft, labelled); dark = OpenStreetMap recoloured in CSS
  classic: { url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}", attribution: "Tiles © Esri, HERE, Garmin, OpenStreetMap contributors", maxNative: 19 },
  satellite: {
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attribution: "Imagery © Esri, Maxar, Earthstar Geographics", maxNative: 18, // deeper levels are missing in many areas: stretch level 18 instead of asking for tiles that do not exist
  },
};

/** A map inside a hidden or collapsed container has size 0, and Leaflet then computes NaN coordinates and throws,
 *  which used to take the whole page down. Camera moves are skipped until the map has a real size. */
const sized = (m: L.Map): boolean => { const z = m.getSize(); return z.x > 0 && z.y > 0; };
const flyBounds = (m: L.Map, b: L.LatLngBounds, o?: L.FitBoundsOptions): void => { if (sized(m) && b.isValid()) m.flyToBounds(b, o); };
const flyPoint = (m: L.Map, ll: L.LatLngExpression, z: number, o?: L.ZoomPanOptions): void => { if (sized(m)) m.flyTo(ll, z, o); };
const fitTo = (m: L.Map, b: L.LatLngBounds, o?: L.FitBoundsOptions): void => { if (sized(m) && b.isValid()) m.fitBounds(b, o); };

const bearingDeg = (a: L.LatLng, b: L.LatLng): number => {
  const r = Math.PI / 180, y = Math.sin((b.lng - a.lng) * r) * Math.cos(b.lat * r);
  const x = Math.cos(a.lat * r) * Math.sin(b.lat * r) - Math.sin(a.lat * r) * Math.cos(b.lat * r) * Math.cos((b.lng - a.lng) * r);
  return ((Math.atan2(y, x) / r) + 360) % 360;
};
const reduced = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

/** Draw a line in along its length (stroke-dashoffset), then hand the dash style back to Leaflet. */
function drawIn(line: L.Polyline, delayMs: number): void {
  const path = line.getElement() as SVGPathElement | undefined;
  if (!path || reduced() || typeof path.getTotalLength !== "function") return;
  const len = path.getTotalLength();
  path.style.strokeDasharray = `${len}`;
  path.style.strokeDashoffset = `${len}`;
  void path.getBoundingClientRect();
  path.style.transition = `stroke-dashoffset ${Math.min(1400, 500 + len / 2)}ms cubic-bezier(.4,0,.2,1) ${delayMs}ms`;
  path.style.strokeDashoffset = "0";
  const done = () => { path.style.transition = ""; path.style.strokeDasharray = line.options.dashArray ? String(line.options.dashArray) : ""; path.style.strokeDashoffset = ""; };
  path.addEventListener("transitionend", done, { once: true });
}

const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#1d6fd6";
const legColour = (l: VLeg) => (l.kind === "walk" ? css("--route-walk") : l.mode === "train" || l.mode === "tram" || l.mode === "ferry" ? serviceColour(l.mode, l.route) : css("--route-bus"));
const readBase = (): Base => { try { const v = localStorage.getItem("map-base"); return v === "satellite" || v === "plain" || v === "dark" || v === "clean" || v === "classic" ? v : "vector"; } catch { return "vector"; } }; // light by default, whatever the app theme

const pinIcon = (label: string, colour: string) => L.divIcon({
  className: "", iconSize: [30, 30], iconAnchor: [15, 30], tooltipAnchor: [14, -22],
  html: `<div class="pin" style="background:${colour}"><b>${label}</b></div>`,
});

function Btn({ label, onClick, active, children }: { label: string; onClick: () => void; active?: boolean; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick} data-tip={label} aria-label={label} aria-pressed={active}
      className={"tip grid size-10 place-items-center rounded-xl border border-line shadow-md transition " + (active ? "bg-accent text-accent-ink" : "bg-surface text-ink hover:bg-surface2")}>
      {children}
    </button>
  );
}
const ico = { width: 20, height: 20, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true } as const;

interface Props {
  art: TripArtifact | null; journey: VJourney | null; origin?: Point | null; dest?: Point | null;
  activeLeg: number | null; onHoverLeg: (i: number | null) => void; onPickLeg: (i: number) => void;
  focus: Focus; expanded: boolean; onToggleExpand?: () => void; dark?: boolean;
  /** When given, a live-vehicles layer (buses and trains moving on the map) is offered. */
  fetchVehicles?: (bbox: [number, number, number, number], tripIds: string[], signal: AbortSignal, modes?: string[]) => Promise<VehiclesResponse>;
  /** Every train line's route, for the network view. */
  fetchTrainLines?: () => Promise<TrainLineRoute[]>;
  /** Full stop sequence of a service, so clicking a vehicle can draw its whole route. */
  fetchTripPath?: (tripId: string) => Promise<TripPath>;
  /** Ask the map to find a service (e.g. from the departures board), show where it is and draw its route. */
  focusTrip?: { tripId: string; label: string; nonce: number } | null;
}

interface VehEntry { marker: L.Marker; from: L.LatLng; to: L.LatLng; t0: number; dur: number; row: VehicleRow; bearing: number | null; rot: HTMLElement | null }
interface TrainView { live: boolean; network: boolean; sel: string[]; onlyActive: boolean }
const NO_TRAINS: TrainView = { live: false, network: false, sel: [], onlyActive: false };
const SEQ_BOX: [number, number, number, number] = [-28.9, 151.9, -25.9, 153.9]; // every train in South East Queensland
interface VehicleFilter { bus: boolean; train: boolean; other: boolean; routes: string[]; mineOnly: boolean }
const NO_FILTER: VehicleFilter = { bus: true, train: true, other: true, routes: [], mineOnly: false };
const isFiltering = (f: VehicleFilter) => !f.bus || !f.train || !f.other || f.routes.length > 0 || f.mineOnly;
const POLL_MS = 10_000;
const MODE_WORD: Record<string, string> = { bus: "Bus", train: "Train", ferry: "Ferry", tram: "Tram" };
const esc = (t: string) => t.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] ?? c);
const TRAIN_SVG = '<svg viewBox="0 0 24 24" width="62%" height="62%" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="3" width="14" height="14" rx="3"/><path d="M5 11h14M9 14h.01M15 14h.01M8 21l2-4M16 21l-2-4"/></svg>';
const vehSize = (mine: boolean) => (mine ? 30 : 24);

/** Real, zoomable map (OpenStreetMap tiles, or satellite, or none): the route is drawn on top and the
 *  user can zoom from the whole trip down to individual stops (z21). */
export function RouteMap({ art, journey, origin, dest, activeLeg, onHoverLeg, onPickLeg, focus, expanded, onToggleExpand, fetchVehicles, fetchTripPath, fetchTrainLines, focusTrip }: Props) {
  const box = SEQ_BOX;
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const tiles = useRef<L.Layer | null>(null);
  const route = useRef<L.LayerGroup | null>(null);
  const ctx = useRef<L.LayerGroup | null>(null);
  const you = useRef<L.Marker | null>(null);
  const legLines = useRef<L.Polyline[]>([]);
  const hadRoute = useRef(false);
  const vehLayer = useRef<L.LayerGroup | null>(null);
  const vehicles = useRef(new Map<string, VehEntry>());
  const following = useRef<string | null>(null);
  // Off by default: hundreds of vehicles drown the map. Your own journey's services and any service you pick are still shown.
  const [showVeh, setShowVeh] = useState(() => { try { return localStorage.getItem("map-vehicles") === "on"; } catch { return false; } });
  const [trackTrip, setTrackTrip] = useState(false);
  const [tv, setTv] = useState<TrainView>(NO_TRAINS);
  const tvRef = useRef(tv);
  const [tlOpen, setTlOpen] = useState(false);
  const [lines, setLines] = useState<TrainLineRoute[] | null>(null);
  const [activeCounts, setActiveCounts] = useState<Record<string, number>>({});
  const netLayer = useRef<L.LayerGroup | null>(null);
  const [vehStat, setVehStat] = useState<{ bus: number; train: number; other: number; age: number | null; err: string | null } | null>(null);
  const [followLabel, setFollowLabel] = useState<string | null>(null);
  const [sel, setSel] = useState<{ id: string; label: string; line: string; headsign: string | null; err: boolean; live: boolean } | null>(null);
  const pendingTrip = useRef<{ tripId: string; label: string } | null>(null);
  const selLayer = useRef<L.LayerGroup | null>(null);
  const actions = useRef<{ follow: () => void; clear: () => void; refilter: () => void; selectTrip: (tripId: string, label: string) => Promise<void> }>({ follow: () => undefined, clear: () => undefined, refilter: () => undefined, selectTrip: async () => undefined });
  const [vf, setVf] = useState<VehicleFilter>(NO_FILTER);
  const vfRef = useRef(vf);
  const [filterOpen, setFilterOpen] = useState(false);
  const [baseOpen, setBaseOpen] = useState(false);
  const [routeQuery, setRouteQuery] = useState("");
  const [avail, setAvail] = useState<{ modes: Record<string, number>; routes: { route: string; mode: string; n: number }[] }>({ modes: {}, routes: [] });
  const [shown, setShown] = useState<number | null>(null);
  useEffect(() => { vfRef.current = vf; actions.current.refilter(); }, [vf]);
  useEffect(() => { tvRef.current = tv; actions.current.refilter(); }, [tv]);
  const tripIds = journey ? journey.legs.map((l) => l.tripId).filter((t): t is string => !!t) : [];
  const tripKey = tripIds.join(",");
  const layerOn = showVeh || tripKey.length > 0 || trackTrip || tv.live; // the live layer runs for "all", or just for the services you care about
  const [base, setBase] = useState<Base>(readBase);
  const [showStops, setShowStops] = useState(true);
  const [zoom, setZoom] = useState(12);
  const [geoErr, setGeoErr] = useState<string | null>(null);
  const [tileErr, setTileErr] = useState(false);
  const cb = useRef({ onHoverLeg, onPickLeg });
  useEffect(() => { cb.current = { onHoverLeg, onPickLeg }; });

  // create the map once
  useEffect(() => {
    if (!el.current) return;
    const m = L.map(el.current, {
      zoomControl: false, zoomSnap: 0, zoomDelta: 1, scrollWheelZoom: false, minZoom: 6, maxZoom: 21, zoomAnimationThreshold: 6, inertiaDeceleration: 2600,
      center: [-27.47, 153.02], zoom: 12,
    });
    L.control.zoom({ position: "bottomright" }).addTo(m);
    L.control.scale({ position: "bottomleft", imperial: false }).addTo(m);
    m.attributionControl.setPrefix(false);
    route.current = L.layerGroup().addTo(m);
    ctx.current = L.layerGroup().addTo(m);
    netLayer.current = L.layerGroup().addTo(m);
    selLayer.current = L.layerGroup().addTo(m);
    vehLayer.current = L.layerGroup().addTo(m);
    m.on("dragstart", () => { if (following.current) { following.current = null; setFollowLabel(null); vehicles.current.forEach((v) => v.marker.getElement()?.firstElementChild?.classList.remove("follow")); } });
    const zclass = () => { const z = m.getZoom(); const c = m.getContainer().classList; c.toggle("z-lo", z < 12); c.toggle("z-mid", z >= 12 && z < 14); c.toggle("z-hi", z >= 14); };
    zclass();
    m.on("zoomend", () => { setZoom(m.getZoom()); zclass(); });
    map.current = m;
    const ro = new ResizeObserver(() => m.invalidateSize());
    ro.observe(el.current);
    // Own wheel/pinch zoom: proportional to the scroll and faster the harder you scroll. Each step uses Leaflet's
    // GPU-accelerated zoom animation; further scrolling is queued and chained so it never janks or jumps.
    const node = el.current;
    let pending = 0, anchor: L.Point | null = null, busy = false;
    const flush = () => {
      if (!anchor || Math.abs(pending) < 0.02) { pending = 0; return; }
      const step = Math.max(-4, Math.min(4, pending));
      pending -= step;
      const target = Math.max(m.getMinZoom(), Math.min(m.getMaxZoom(), m.getZoom() + step));
      if (Math.abs(target - m.getZoom()) < 0.02) { pending = 0; return; }
      busy = true;
      m.setZoomAround(anchor, target, { animate: true });
    };
    m.on("zoomend", () => { busy = false; flush(); });
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const px = e.deltaMode === 1 ? e.deltaY * 32 : e.deltaMode === 2 ? e.deltaY * 400 : e.deltaY;
      const speed = 1 + Math.min(Math.abs(px) / 50, 3);
      pending += -px * 0.004 * speed * (e.ctrlKey ? 2.5 : 1);
      anchor = m.mouseEventToContainerPoint(e);
      if (!busy) flush();
    };
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => { node.removeEventListener("wheel", onWheel); ro.disconnect(); m.remove(); map.current = null; };
  }, []);

  // base layer
  useEffect(() => {
    const m = map.current;
    if (!m) return;
    tiles.current?.remove();
    tiles.current = null;
    setTileErr(false);
    try { localStorage.setItem("map-base", base); } catch { /* ignore */ }
    if (base === "plain") return;
    let cancelled = false, timer = 0;
    const raster = (kind: "classic" | "satellite") => {
      const t = TILES[kind];
      const layer = L.tileLayer(t.url, { attribution: t.attribution, maxNativeZoom: t.maxNative, maxZoom: 21, crossOrigin: true, keepBuffer: 4, updateWhenZooming: false }); // no retina doubling: half-size tiles meant twice as many seams
      layer.on("tileerror", () => setTileErr(true));
      layer.on("tileload", () => setTileErr(false));
      layer.addTo(m);
      layer.bringToBack();
      tiles.current = layer;
    };
    const addVector = (style: string | object) => {
      try {
        const gl = L.maplibreGL({ style, attribution: VECTOR_ATTRIBUTION, maxZoom: 22, fadeDuration: 150, pixelRatio: Math.min(3, Math.max(2, window.devicePixelRatio || 1)) } as unknown as L.LeafletMaplibreGLOptions);
        gl.addTo(m);
        const ml = gl.getMaplibreMap();
        // if the style cannot load (offline, WebGL unavailable), fall back to raster tiles instead of a blank map
        const fail = (why: unknown) => { if (cancelled) return; cancelled = true; console.warn("vector basemap unavailable, using raster tiles:", why); gl.remove(); raster("classic"); };
        ml.once("error", (e) => { if (!ml.isStyleLoaded()) fail(e.error ?? e); });
        timer = window.setTimeout(() => { if (!cancelled && !ml.isStyleLoaded()) fail("style load timeout"); }, 12_000);
        tiles.current = gl;
      } catch (e) {
        console.warn("vector basemap unavailable, using raster tiles:", e);
        raster("classic");
      }
    };
    if (base === "dark") {
      darkStyle().then((st) => { if (!cancelled) addVector(st); }).catch(() => { if (!cancelled) raster("classic"); });
    } else if (VECTOR[base]) {
      addVector(VECTOR[base]!);
    } else {
      raster(base === "satellite" ? "satellite" : "classic");
    }
    return () => { cancelled = true; clearTimeout(timer); };
  }, [base]);

  // draw the selected journey
  useEffect(() => {
    const m = map.current, g = route.current;
    if (!m || !g) return;
    g.clearLayers();
    legLines.current = [];
    if (!journey) return;
    const bg = css("--surface");
    journey.legs.forEach((l, i) => {
      if (l.path.length < 2) return;
      const c = legColour(l);
      const casing = L.polyline(l.path, { color: bg, weight: l.kind === "walk" ? 8 : 10, opacity: 0.95, lineCap: "round", lineJoin: "round", interactive: false });
      const line = L.polyline(l.path, { color: c, weight: l.kind === "walk" ? 4 : 6, opacity: 1, dashArray: l.kind === "walk" ? "1 9" : undefined, lineCap: "round", lineJoin: "round" });
      line.bindTooltip(`${legTitle(l)}: ${l.from} → ${l.to}`, { sticky: true, className: "stop-tip" });
      line.on("mouseover", () => cb.current.onHoverLeg(i));
      line.on("mouseout", () => cb.current.onHoverLeg(null));
      line.on("click", () => cb.current.onPickLeg(i));
      casing.addTo(g); line.addTo(g);
      legLines.current[i] = line;
      if (l.kind === "transit") { (casing.getElement() as SVGPathElement | undefined)?.classList.add("fade-in"); drawIn(line, i * 250); }
      else (line.getElement() as SVGPathElement | undefined)?.classList.add("fade-in");
      // every stop the service passes
      l.stops.forEach((s, k) => {
        const end = k === 0 || k === l.stops.length - 1;
        const dot = L.circleMarker([s.lat, s.lon], { radius: end ? 6 : 3.5, color: c, weight: end ? 3 : 2, fillColor: bg, fillOpacity: 1, className: "fade-in" });
        dot.bindTooltip(s.name, { className: "stop-tip", direction: "top", offset: [0, -6] });
        dot.addTo(g);
        (dot as L.CircleMarker & { _mid?: boolean })._mid = !end;
      });
    });
    const o = journey.legs[0]?.path[0], dd = journey.legs[journey.legs.length - 1]?.path.slice(-1)[0];
    const ok = css("--ok"), bad = css("--bad");
    if (o) L.marker(o, { icon: pinIcon("A", ok), zIndexOffset: 1000 }).bindTooltip(origin?.name ?? journey.legs[0]?.from ?? "Start", { className: "stop-tip", direction: "right" }).addTo(g);
    if (dd) L.marker(dd, { icon: pinIcon("B", bad), zIndexOffset: 1000 }).bindTooltip(dest?.name ?? journey.legs[journey.legs.length - 1]?.to ?? "End", { className: "stop-tip", direction: "right" }).addTo(g);
    // transfers: ring + permanent label
    journey.legs.filter((l) => l.kind === "transit" && !l.stopover).slice(1).forEach((l) => {
      const s = l.stops[0];
      if (s) L.circleMarker([s.lat, s.lon], { radius: 9, color: css("--warn"), weight: 4, fillColor: bg, fillOpacity: 1 }).bindTooltip(`Change: ${s.name}`, { direction: "right", offset: [10, 0], className: "stop-tip" }).addTo(g);
    });
    journey.legs.forEach((l) => {
      const at = l.path[0];
      if (l.stopover && at) L.marker(at, { icon: pinIcon(String(l.stopoverIndex ?? ""), "#d97706"), zIndexOffset: 900 }).bindTooltip(`Stop ${l.stopoverIndex}: ${l.stopover}${l.stayMin ? ` · ${l.stayMin} min` : ""}`, { className: "stop-tip", direction: "right" }).addTo(g);
    });
    const all = journey.legs.flatMap((l) => l.path);
    if (all.length) {
      const b = L.latLngBounds(all);
      if (hadRoute.current && !reduced()) flyBounds(m, b, { padding: [60, 60], maxZoom: 17, duration: 0.9, easeLinearity: 0.2 });
      else fitTo(m, b, { padding: [60, 60], maxZoom: 17, animate: false });
      hadRoute.current = true;
    }
  }, [journey, origin, dest]);

  // intermediate stop dots only when zoomed in enough to be useful
  useEffect(() => {
    const g = route.current;
    if (!g) return;
    g.eachLayer((layer) => {
      const cm = layer as L.CircleMarker & { _mid?: boolean };
      if (cm._mid === undefined) return;
      const on = zoom >= 14.5;
      cm.setStyle({ opacity: on ? 1 : 0, fillOpacity: on ? 1 : 0 });
      cm.options.interactive = on;
    });
  }, [zoom, journey]);

  // context stops (every nearby bus/train stop), only at street scale
  useEffect(() => {
    const g = ctx.current;
    if (!g) return;
    g.clearLayers();
    if (!art || !showStops || zoom < 14) return;
    const c = css("--muted");
    art.context_stops.forEach((s) => L.circleMarker([s.lat, s.lon], { radius: s.train ? 4 : 2.5, color: c, weight: 1, fillColor: s.train ? css("--route-train") : c, fillOpacity: 0.55, interactive: false }).addTo(g));
  }, [art, showStops, zoom]);

  // live vehicles: poll the feed, then glide each marker from where it was to where it now is
  useEffect(() => {
    const m = map.current, layer = vehLayer.current;
    if (!m || !layer || !fetchVehicles || !layerOn) return;
    const live = vehicles.current;
    const allMode = showVeh;
    let alive = true, ctl: AbortController | null = null, raf = 0, last = 0, debounce = 0;
    const colourFor = (mode: string) => (mode === "train" ? "--route-train" : "--route-bus");
    void colourFor;

    const makeIcon = (r: VehicleRow) => {
      const sz = vehSize(r.on_trip);
      return L.divIcon({
        className: "veh-wrap", iconSize: [sz, sz], iconAnchor: [sz / 2, sz / 2], tooltipAnchor: [sz / 2, 0],
        html: `<div class="veh ${r.mode}${r.on_trip ? " mine" : ""}"${r.mode !== "bus" ? ` style="--c:${serviceColour(r.mode, r.route)}"` : ""}><i class="rot"></i>${r.mode === "bus" ? `<b>${esc(r.route).slice(0, 4)}</b>` : TRAIN_SVG}</div>`,
      });
    };
    const tip = (r: VehicleRow) => {
      const ago = Math.max(0, Math.round(Date.now() / 1000 - r.timestamp));
      const what = r.status === "STOPPED_AT" ? "At a stop" : r.speed_kmh != null && r.speed_kmh > 0 ? `${r.speed_kmh} km/h` : "Moving";
      // trains are identified by their line, never by the timetable code (that is in the details card)
      const title = r.mode === "bus" ? [r.route, r.route_name].filter(Boolean).join(" · ") : r.route_name || MODE_WORD[r.mode] || "Service";
      const sub = [MODE_WORD[r.mode] ?? "Bus", r.headsign ? `towards ${r.headsign}` : ""].filter(Boolean).join(" · ");
      const meta = [what, `updated ${ago}s ago`, r.on_trip ? "Your service" : ""].filter(Boolean).join(" · ");
      const dot = r.mode !== "bus" ? `<i class="vt-dot" style="background:${serviceColour(r.mode, r.route)}"></i>` : "";
      return `<div class="vt"><div class="vt-title">${dot}${esc(title)}</div><div class="vt-sub">${esc(sub)}</div><div class="vt-meta">${esc(meta)}</div></div>`;
    };
    const setBearing = (e: VehEntry, deg: number | null) => {
      e.bearing = deg;
      const el = e.marker.getElement();
      e.rot ??= el?.querySelector<HTMLElement>(".rot") ?? null;
      el?.firstElementChild?.classList.toggle("nohead", deg === null);
      if (e.rot && deg !== null) e.rot.style.transform = `rotate(${deg}deg)`;
    };
    const follow = (id: string) => {
      const was = following.current;
      following.current = was === id ? null : id;
      live.forEach((v, k) => v.marker.getElement()?.firstElementChild?.classList.toggle("follow", k === following.current));
      const e = live.get(id);
      setFollowLabel(following.current && e ? `${MODE_WORD[e.row.mode] ?? "Bus"} ${e.row.route}` : null);
      if (following.current && e) flyPoint(m, e.marker.getLatLng(), Math.max(m.getZoom(), 16), { duration: 0.8 });
    };

    let selId: string | null = null, selTrip: string | null = null;
    const visible = (r: VehicleRow, id: string) => {
      const f = vfRef.current;
      if (r.on_trip || id === selId) return true; // your own services and the selected one are never filtered out
      const t = tvRef.current;
      if (r.mode === "train" && t.sel.length > 0 && !trainLine(r.route).families.some((x) => t.sel.includes(x.id))) return false;
      if (!allMode) return t.live && r.mode === "train"; // "live trains" on, everything else off
      if (f.mineOnly) return false;
      if (!f[r.mode === "bus" ? "bus" : r.mode === "train" ? "train" : "other"]) return false;
      return f.routes.length === 0 || f.routes.includes(r.route);
    };
    const refilter = () => {
      let n = 0;
      live.forEach((e, id) => { const v = visible(e.row, id); e.marker.getElement()?.classList.toggle("veh-hide", !v); if (v) n++; });
      setShown(n);
    };
    const mark = () => {
      const chosen = selId ? live.get(selId) : null;
      m.getContainer().classList.toggle("has-sel", !!chosen);
      live.forEach((v, k) => {
        const c = v.marker.getElement()?.firstElementChild;
        c?.classList.toggle("sel", k === selId);
        c?.classList.toggle("same", !!chosen && v.row.route === chosen.row.route && v.row.mode === chosen.row.mode);
      });
    };
    const clearSel = () => {
      selId = null; selTrip = null; selLayer.current?.clearLayers(); mark(); setSel(null);
      if (!allMode) setTrackTrip(false); // nothing left to follow: stop polling
    };
    const showPath = async (id: string, tripId: string, label: string, line: string, vehicle: L.LatLng | null) => {
      if (!fetchTripPath || !tripId) return;
      try {
        const p = await fetchTripPath(tripId);
        if (selId !== id || !selLayer.current) return;
        const pts = p.path.length > 1 ? p.path : p.stops.map((x) => [x.lat, x.lon] as [number, number]); // real road shape when the feed has one
        const c = p.mode === "bus" ? css("--route-bus") : serviceColour(p.mode, p.route);
        const g = selLayer.current;
        L.polyline(pts, { color: css("--surface"), weight: 12, opacity: 0.95, lineCap: "round", lineJoin: "round", interactive: false }).addTo(g);
        L.polyline(pts, { color: c, weight: 7, lineCap: "round", lineJoin: "round", interactive: false }).addTo(g);
        p.stops.forEach((x, k) => {
          const end = k === 0 || k === p.stops.length - 1;
          L.circleMarker([x.lat, x.lon], { radius: end ? 7 : 3.5, color: c, weight: end ? 4 : 2, fillColor: css("--surface"), fillOpacity: 1, className: "fade-in" })
            .bindTooltip(end ? `${k === 0 ? "From" : "To"}: ${x.name}` : x.name, { className: "stop-tip", direction: "top", offset: [0, -6] }).addTo(g);
        });
        if (vehicle) flyPoint(m, vehicle, Math.max(m.getZoom(), 15.5), { duration: 0.9 }); // "where is it": go to the vehicle itself
        else if (pts.length) flyBounds(m, L.latLngBounds(pts), { padding: [90, 90], maxZoom: 16, duration: 0.9 });
        setSel({ id, label, line: p.mode === "bus" ? p.route_name || line : line || `Line code ${p.route}`, headsign: p.headsign, err: false, live: !!vehicle });
      } catch {
        if (selId === id) setSel({ id, label, line, headsign: null, err: true, live: !!vehicle });
      }
    };
    const select = async (id: string, toVehicle = false) => {
      if (selId === id && !toVehicle) { clearSel(); return; }
      const e = live.get(id);
      if (!e) return;
      selLayer.current?.clearLayers();
      selId = id; selTrip = e.row.trip_id || null; mark();
      const label = e.row.mode === "bus" ? `Bus ${e.row.route}` : e.row.route_name || MODE_WORD[e.row.mode] || "Service";
      const line = e.row.mode === "bus" ? e.row.route_name : `Line code ${e.row.route}`;
      setSel({ id, label, line, headsign: null, err: false, live: true });
      await showPath(id, e.row.trip_id, label, line, toVehicle ? e.marker.getLatLng() : null);
    };
    const selectTrip = async (tripId: string, label: string) => {
      selTrip = tripId;
      await poll(); // the selected trip is always requested, even when it is off-screen
      const hit = [...live.entries()].find(([, v]) => v.row.trip_id === tripId);
      if (hit) { await select(hit[0], true); return; }
      selLayer.current?.clearLayers(); // not reporting a position (not started yet): still show its route
      const id = `trip:${tripId}`;
      selId = id; mark();
      setSel({ id, label, line: "", headsign: null, err: false, live: false });
      await showPath(id, tripId, label, "", null);
    };
    actions.current = {
      follow: () => { if (selId) follow(selId); },
      clear: clearSel,
      refilter,
      selectTrip,
    };

    const apply = (res: VehiclesResponse) => {
      const now = performance.now();
      const seen = new Set<string>();
      let bus = 0, train = 0, other = 0;
      for (const r of res.vehicles) {
        seen.add(r.id);
        if (r.mode === "bus") bus++; else if (r.mode === "train") train++; else other++;
        const to = L.latLng(r.lat, r.lon);
        const cur = live.get(r.id);
        if (cur) {
          const from = cur.marker.getLatLng();
          const moved = from.distanceTo(to);
          cur.from = from; cur.to = to; cur.t0 = now; cur.dur = POLL_MS * 0.95; cur.row = r;
          if (r.bearing !== null) setBearing(cur, r.bearing);
          else if (moved > 8) setBearing(cur, bearingDeg(from, to));
          if (cur.row.on_trip !== r.on_trip) cur.marker.setIcon(makeIcon(r));
          cur.marker.setTooltipContent(tip(r));
        } else {
          const marker = L.marker(to, { icon: makeIcon(r), keyboard: false, zIndexOffset: r.on_trip ? 900 : 0, riseOnHover: true });
          marker.bindTooltip(tip(r), { className: "stop-tip veh-tip", direction: "top", offset: [0, -8] });
          marker.on("click", () => void select(r.id));
          marker.addTo(layer);
          const e: VehEntry = { marker, from: to, to, t0: now, dur: 1, row: r, bearing: null, rot: null };
          live.set(r.id, e);
          requestAnimationFrame(() => { setBearing(e, r.bearing); refilter(); });
        }
      }
      live.forEach((e, id) => { if (!seen.has(id)) { e.marker.remove(); live.delete(id); if (following.current === id) { following.current = null; setFollowLabel(null); } } });
      mark(); // a selection (and its drawn route) stays until the user clears it
      const modes: Record<string, number> = {}, routeN = new Map<string, { route: string; mode: string; n: number }>();
      for (const r of res.vehicles) {
        modes[r.mode] = (modes[r.mode] ?? 0) + 1;
        const k = `${r.mode}:${r.route}`, cur = routeN.get(k);
        if (cur) cur.n++; else routeN.set(k, { route: r.route, mode: r.mode, n: 1 });
      }
      setAvail({ modes, routes: [...routeN.values()].sort((a, b) => a.route.localeCompare(b.route, undefined, { numeric: true })) });
      refilter();
      setVehStat({ bus, train, other, age: res.age_s, err: res.available ? null : res.error ?? "unavailable" });
    };

    const poll = async () => {
      if (!alive || document.hidden) return;
      ctl?.abort();
      ctl = new AbortController();
      const c = m.getCenter(), e = 0.0005; // not showing everything: a pinpoint box, so only the wanted trips come back
      const trainsOnly = !allMode && tvRef.current.live;
      const b = allMode ? m.getBounds().pad(0.25) : trainsOnly ? L.latLngBounds([box[0], box[1]], [box[2], box[3]]) : L.latLngBounds([c.lat - e, c.lng - e], [c.lat + e, c.lng + e]);
      try {
        const trips = [...(tripKey ? tripKey.split(",") : []), ...(selTrip ? [selTrip] : [])]; // the selected service is always requested, even off-screen
        apply(await fetchVehicles([b.getSouth(), b.getWest(), b.getNorth(), b.getEast()], trips, ctl.signal, trainsOnly ? ["train"] : undefined));
      } catch (e) {
        if (alive && !(e instanceof DOMException && e.name === "AbortError")) setVehStat((s) => ({ bus: s?.bus ?? 0, train: s?.train ?? 0, other: 0, age: null, err: "Live positions unavailable" }));
      }
    };

    const frame = (t: number) => {
      raf = requestAnimationFrame(frame);
      if (t - last < 32) return; // ~30 fps is plenty for slow-moving vehicles
      last = t;
      const now = performance.now();
      const glide = m.getZoom() >= 12; // zoomed far out they are specks: just jump
      live.forEach((e) => {
        if (e.marker.getElement()?.classList.contains("veh-hide")) { e.marker.setLatLng(e.to); return; }
        const p = Math.min(1, (now - e.t0) / e.dur);
        if (p >= 1 || !glide) { if (!e.marker.getLatLng().equals(e.to, 1e-7)) e.marker.setLatLng(e.to); return; }
        e.marker.setLatLng([e.from.lat + (e.to.lat - e.from.lat) * p, e.from.lng + (e.to.lng - e.from.lng) * p]);
      });
      const f = following.current && live.get(following.current);
      if (f) m.panTo(f.marker.getLatLng(), { animate: false, noMoveStart: true } as L.PanOptions);
    };

    const onMapClick = () => { if (selId) clearSel(); }; // clicking empty map deselects the service
    m.on("click", onMapClick);
    const queued = pendingTrip.current;
    pendingTrip.current = null;
    if (queued) void selectTrip(queued.tripId, queued.label); else void poll();
    const timer = setInterval(() => void poll(), POLL_MS);
    const onMove = () => { clearTimeout(debounce); debounce = window.setTimeout(() => void poll(), 450); };
    const onVis = () => { if (!document.hidden) void poll(); };
    m.on("moveend", onMove);
    document.addEventListener("visibilitychange", onVis);
    raf = requestAnimationFrame(frame);
    return () => {
      alive = false; ctl?.abort(); clearInterval(timer); clearTimeout(debounce); cancelAnimationFrame(raf);
      m.off("moveend", onMove); m.off("click", onMapClick); document.removeEventListener("visibilitychange", onVis);
      setShown(null); live.forEach((e) => e.marker.remove()); live.clear(); following.current = null; selLayer.current?.clearLayers(); m.getContainer().classList.remove("has-sel");
    };
  }, [fetchVehicles, fetchTripPath, showVeh, layerOn, tripKey, tv.live, box]);

  // which train lines have a train running right now (per line family)
  useEffect(() => {
    if (!fetchVehicles || !(tlOpen || tv.onlyActive)) return;
    let alive = true;
    const ctl = new AbortController();
    const load = () => fetchVehicles(box, [], ctl.signal, ["train"]).then((r) => {
      if (!alive) return;
      const n: Record<string, number> = {};
      for (const v of r.vehicles) for (const f of trainLine(v.route).families) n[f.id] = (n[f.id] ?? 0) + 1;
      setActiveCounts(n);
    }).catch(() => undefined);
    void load();
    const t = setInterval(() => void load(), 20_000);
    return () => { alive = false; ctl.abort(); clearInterval(t); };
  }, [fetchVehicles, tlOpen, tv.onlyActive, box]);

  // the network view needs every line's route: fetch once, on first use
  useEffect(() => {
    if (!fetchTrainLines || lines || !(tv.network || tlOpen)) return;
    let alive = true;
    fetchTrainLines().then((l) => { if (alive) setLines(l); }).catch(() => undefined);
    return () => { alive = false; };
  }, [fetchTrainLines, lines, tv.network, tlOpen]);

  // draw every line's route in its own colour
  useEffect(() => {
    const g = netLayer.current;
    if (!g) return;
    g.clearLayers();
    if (!tv.network || !lines) return;
    const bg = css("--surface");
    const wanted = lines.filter((ln) => {
      const fams = trainLine(ln.route).families;
      if (tv.sel.length > 0 && !fams.some((f) => tv.sel.includes(f.id))) return false;
      return !tv.onlyActive || fams.some((f) => (activeCounts[f.id] ?? 0) > 0);
    });
    for (const ln of wanted) {
      const info = trainLine(ln.route);
      if (ln.path.length < 2) continue;
      L.polyline(ln.path, { color: bg, weight: 9, opacity: 0.9, lineCap: "round", lineJoin: "round", interactive: false }).addTo(g);
      L.polyline(ln.path, { color: info.primary.colour, weight: 5, lineCap: "round", lineJoin: "round" })
        .bindTooltip(`<div class="vt"><div class="vt-title"><i class="vt-dot" style="background:${info.primary.colour}"></i>${esc(lineNames(ln.route))} line</div><div class="vt-sub">${esc(ln.route_name)}</div></div>`, { sticky: true, className: "stop-tip veh-tip" }).addTo(g);
    }
  }, [tv.network, tv.sel, tv.onlyActive, lines, activeCounts]);

  // a service picked elsewhere (departures board): switch the live layer on if needed, then show it
  useEffect(() => {
    if (!focusTrip || !fetchVehicles) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reacting to an external request from the departures board
    if (!layerOn) { pendingTrip.current = { tripId: focusTrip.tripId, label: focusTrip.label }; setTrackTrip(true); return; }
    void actions.current.selectTrip(focusTrip.tripId, focusTrip.label);
  }, [focusTrip]); // eslint-disable-line react-hooks/exhaustive-deps

  // highlight the active leg
  useEffect(() => {
    legLines.current.forEach((line, i) => {
      if (!line) return;
      const l = journey?.legs[i];
      const on = activeLeg === i;
      line.setStyle({ weight: l?.kind === "walk" ? (on ? 6 : 4) : on ? 9 : 6, opacity: activeLeg === null || on ? 1 : 0.35 });
      if (on) line.bringToFront();
    });
  }, [activeLeg, journey]);

  // programmatic focus (timeline clicks, "fit route")
  useEffect(() => {
    const m = map.current;
    if (!m) return;
    if (focus.kind === "fit") {
      const all = journey?.legs.flatMap((l) => l.path) ?? [];
      if (all.length) flyBounds(m, L.latLngBounds(all), { padding: [60, 60], maxZoom: 17, duration: 0.6 });
    } else if (focus.kind === "box") {
      flyBounds(m, L.latLngBounds([focus.box[0], focus.box[1]], [focus.box[2], focus.box[3]]), { padding: [30, 30], maxZoom: 12, duration: 1.4 });
    } else if (focus.kind === "leg") {
      const l = journey?.legs[focus.index];
      if (l && l.path.length) flyBounds(m, L.latLngBounds(l.path), { padding: [80, 80], maxZoom: 18, duration: 0.6 });
    } else {
      flyPoint(m, [focus.lat, focus.lon], focus.zoom ?? 18, { duration: 0.6 });
    }
  }, [focus, journey]);

  const locate = () => {
    setGeoErr(null);
    if (!navigator.geolocation) { setGeoErr("This browser cannot share your location."); return; }
    navigator.geolocation.getCurrentPosition(
      (p) => {
        const m = map.current;
        if (!m) return;
        you.current?.remove();
        you.current = L.marker([p.coords.latitude, p.coords.longitude], { icon: L.divIcon({ className: "", html: '<div class="you-dot"></div>', iconSize: [16, 16] }), keyboard: false }).addTo(m);
        flyPoint(m, [p.coords.latitude, p.coords.longitude], 17, { duration: 0.6 });
      },
      () => setGeoErr("Could not get your location (permission denied or unavailable)."),
      { enableHighAccuracy: true, timeout: 8000 },
    );
  };


  return (
    <div className="relative size-full" data-testid="route-map">
      <div ref={el} className="size-full" role="application" aria-label="Interactive map. Use plus and minus to zoom, arrow keys to pan." />
      <div className="no-print absolute right-3 top-3 z-[800] flex flex-col gap-2">
        {onToggleExpand && (
          <Btn label={expanded ? "Show journey panel" : "Expand map"} onClick={onToggleExpand} active={expanded}>
            {expanded ? <svg {...ico}><path d="M9 4H4v5M15 4h5v5M9 20H4v-5M15 20h5v-5" /></svg> : <svg {...ico}><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" /></svg>}
          </Btn>
        )}
        <Btn label="Zoom out to all of South East Queensland" onClick={() => flyBounds(map.current!, L.latLngBounds([-28.4, 152.3], [-25.9, 153.9]), { padding: [10, 10], duration: 1.4 })}>
          <svg {...ico}><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18" /></svg>
        </Btn>
        <Btn label="Fit whole route" onClick={() => { const all = journey?.legs.flatMap((l) => l.path) ?? []; if (all.length) if (map.current) flyBounds(map.current, L.latLngBounds(all), { padding: [60, 60], maxZoom: 17, duration: 0.6 }); }}>
          <svg {...ico}><path d="M3 12h4l3-8 4 16 3-8h4" /></svg>
        </Btn>
        <div className="relative">
          <Btn label="Map style" onClick={() => { setFilterOpen(false); setTlOpen(false); setBaseOpen((o) => !o); }} active={baseOpen}>
            <svg {...ico}><path d="m12 3 9 5-9 5-9-5z" /><path d="m3 13 9 5 9-5" /></svg>
          </Btn>
          {base === "plain" && <span aria-hidden className="absolute -right-1 -top-1 size-3 rounded-full border-2 border-surface bg-warn" />}
        </div>
        {fetchVehicles && (
          <Btn label={showVeh ? "Hide all live buses and trains" : "Show all live buses and trains"} onClick={() => { setShowVeh((v) => { const n = !v; try { localStorage.setItem("map-vehicles", n ? "on" : "off"); } catch { /* ignore */ } return n; }); setVehStat(null); setFollowLabel(null); }} active={showVeh}>
            <svg {...ico}><rect x="4" y="3" width="16" height="14" rx="2.5" /><path d="M4 11h16M8 21v-4M16 21v-4M8 14h.01M16 14h.01" /></svg>
          </Btn>
        )}
        {fetchVehicles && (
          <div className="relative">
            <Btn label="Train lines" onClick={() => { setFilterOpen(false); setBaseOpen(false); setTlOpen((o) => !o); }} active={tlOpen || tv.live || tv.network}>
              <svg {...ico}><rect x="5" y="3" width="14" height="14" rx="3" /><path d="M5 11h14M9 14h.01M15 14h.01M8 21l2-4M16 21l-2-4" /></svg>
            </Btn>
            {(tv.live || tv.network) && <span aria-hidden className="absolute -right-1 -top-1 size-3 rounded-full border-2 border-surface bg-ok" />}
          </div>
        )}
        {fetchVehicles && showVeh && (
          <div className="relative">
            <Btn label="Filter buses and trains" onClick={() => { setBaseOpen(false); setTlOpen(false); setFilterOpen((o) => !o); }} active={filterOpen || isFiltering(vf)}>
              <svg {...ico}><path d="M3 5h18l-7 8v6l-4-2v-4z" /></svg>
            </Btn>
            {isFiltering(vf) && <span aria-hidden className="absolute -right-1 -top-1 size-3 rounded-full border-2 border-surface bg-warn" />}
          </div>
        )}
        <Btn label={showStops ? "Hide nearby stops" : "Show nearby stops"} onClick={() => setShowStops((s) => !s)} active={showStops}>
          <svg {...ico}><circle cx="12" cy="12" r="3" /><circle cx="12" cy="12" r="9" /></svg>
        </Btn>
        <Btn label="Show my location" onClick={locate}>
          <svg {...ico}><circle cx="12" cy="12" r="3" /><path d="M12 2v4M12 18v4M2 12h4M18 12h4" /></svg>
        </Btn>
      </div>
      {baseOpen && (
        <div role="dialog" aria-label="Map style" className="no-print rise absolute right-16 top-3 z-[600] w-56 rounded-2xl border border-line bg-surface p-2 shadow-2xl">
          <p className="label px-2 pb-1 pt-1">Map style</p>
          {([["vector", "Detailed", "Sharp vector map, full colour"], ["clean", "Clean", "Minimal, easy to read"], ["dark", "Dark", "Easy on the eyes at night"], ["classic", "Classic", "Raster street tiles"], ["satellite", "Satellite", "Aerial imagery"], ["plain", "Route only", "No background map"]] as const).map(([k, l, d]) => (
            <button key={k} type="button" role="radio" aria-checked={base === k} onClick={() => { setBase(k); setBaseOpen(false); }}
              className={"flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-left transition " + (base === k ? "bg-accent/10" : "hover:bg-surface2")}>
              <span className={"grid size-5 place-items-center rounded-full border-2 " + (base === k ? "border-accent" : "border-line")}>{base === k && <span className="size-2.5 rounded-full bg-accent" />}</span>
              <span><span className="block text-[13.5px] font-bold">{l}</span><span className="block text-[12px] text-muted">{d}</span></span>
            </button>
          ))}
        </div>
      )}
      {base === "plain" && (
        <button type="button" onClick={() => setBase("vector")} className="no-print pointer-events-auto absolute bottom-24 left-1/2 z-[500] -translate-x-1/2 rounded-full border border-line bg-surface px-4 py-2 text-[13px] font-bold shadow-lg hover:bg-surface2">
          Background map is hidden · <span className="text-accent">Show map</span>
        </button>
      )}
      {tlOpen && fetchVehicles && (
        <div role="dialog" aria-label="Train lines" className="no-print rise absolute right-16 top-3 z-[600] w-[20rem] max-w-[calc(100%-5rem)] rounded-2xl border border-line bg-surface p-3.5 shadow-2xl" data-testid="train-lines">
          <div className="flex items-center">
            <h2 className="text-[15px] font-extrabold">Train lines</h2>
            <button type="button" onClick={() => setTlOpen(false)} aria-label="Close train lines" className="ml-auto grid size-7 place-items-center rounded-full text-muted hover:bg-surface2"><svg {...ico} width={16} height={16}><path d="M18 6 6 18M6 6l12 12" /></svg></button>
          </div>
          <div className="mt-2 grid grid-cols-2 gap-1.5" role="group" aria-label="What to show">
            <button type="button" aria-pressed={tv.live} onClick={() => setTv((t) => ({ ...t, live: !t.live }))}
              className={"rounded-xl border px-3 py-2 text-left transition " + (tv.live ? "border-transparent pill-on" : "border-line hover:bg-surface2")}><span className="block text-[13.5px] font-extrabold">Live trains</span><span className="block text-[11.5px] opacity-80">Running right now</span></button>
            <button type="button" aria-pressed={tv.network} onClick={() => setTv((t) => ({ ...t, network: !t.network }))}
              className={"rounded-xl border px-3 py-2 text-left transition " + (tv.network ? "border-transparent pill-on" : "border-line hover:bg-surface2")}><span className="block text-[13.5px] font-extrabold">Line routes</span><span className="block text-[11.5px] opacity-80">Where each line goes</span></button>
          </div>
          <label className="mt-2.5 flex cursor-pointer items-center gap-2 text-[13px] font-semibold">
            <input type="checkbox" checked={tv.onlyActive} onChange={(e) => setTv((t) => ({ ...t, onlyActive: e.target.checked }))} className="size-4 accent-[var(--accent)]" />Only lines with a train running now
          </label>
          <div className="mt-3 flex items-center justify-between"><p className="label">Lines{tv.sel.length ? ` · ${tv.sel.length} selected` : ""}</p>{tv.sel.length > 0 && <button type="button" onClick={() => setTv((t) => ({ ...t, sel: [] }))} className="text-[12px] font-bold text-accent hover:underline">All lines</button>}</div>
          <ul className="mt-1.5 max-h-[27rem] space-y-1 overflow-y-auto" aria-label="Lines">
            {LINE_FAMILIES.filter((f) => !tv.onlyActive || (activeCounts[f.id] ?? 0) > 0).map((f) => {
              const on = tv.sel.includes(f.id), n = activeCounts[f.id];
              return (
                <li key={f.id}>
                  <button type="button" aria-pressed={on} onClick={() => setTv((t) => ({ ...t, sel: on ? t.sel.filter((x) => x !== f.id) : [...t.sel, f.id] }))}
                    className={"flex w-full items-center gap-2.5 rounded-xl px-2.5 py-1.5 text-left transition " + (on ? "bg-accent/10 ring-1 ring-accent" : "hover:bg-surface2")}>
                    <span aria-hidden className="h-3.5 w-7 shrink-0 rounded-full" style={{ background: f.colour }} />
                    <span className="min-w-0 flex-1 truncate text-[13.5px] font-semibold">{f.name}</span>
                    <span className={"text-[12px] font-bold " + (n ? "text-ok" : "text-muted")}>{n === undefined ? "" : n > 0 ? `${n} running` : "none now"}</span>
                  </button>
                </li>
              );
            })}
          </ul>
          <p className="mt-2 text-[11.5px] leading-snug text-muted">Pick one or more lines to show only those. Services that run across two lines (like Springfield – Redcliffe Peninsula) appear under both.</p>
        </div>
      )}
      {filterOpen && fetchVehicles && showVeh && (
        <div role="dialog" aria-label="Filter buses and trains" className="no-print rise absolute right-16 top-3 z-[600] w-[19rem] max-w-[calc(100%-5rem)] rounded-2xl border border-line bg-surface p-3.5 shadow-2xl" data-testid="vehicle-filter">
          <div className="flex items-center">
            <h2 className="text-[15px] font-extrabold">Show on the map</h2>
            <button type="button" onClick={() => setFilterOpen(false)} aria-label="Close filter" className="ml-auto grid size-7 place-items-center rounded-full text-muted hover:bg-surface2"><svg {...ico} width={16} height={16}><path d="M18 6 6 18M6 6l12 12" /></svg></button>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5" role="group" aria-label="Vehicle types">
            {([["bus", "Buses"], ["train", "Trains"], ["other", "Ferries & trams"]] as const).map(([k, l]) => {
              const n = k === "other" ? (avail.modes.ferry ?? 0) + (avail.modes.tram ?? 0) : avail.modes[k] ?? 0;
              return (
                <button key={k} type="button" aria-pressed={vf[k]} onClick={() => setVf((f) => ({ ...f, [k]: !f[k] }))}
                  className={"rounded-full border px-3 py-1.5 text-[13px] font-bold transition " + (vf[k] ? "border-transparent pill-on" : "border-line text-muted line-through hover:text-ink")}>{l} <span className="font-semibold opacity-75">{n}</span></button>
              );
            })}
          </div>
          {tripKey && (
            <label className="mt-3 flex cursor-pointer items-center gap-2 text-[13px] font-semibold">
              <input type="checkbox" checked={vf.mineOnly} onChange={(e) => setVf((f) => ({ ...f, mineOnly: e.target.checked }))} className="size-4 accent-[var(--accent)]" />Only the services on my journey
            </label>
          )}
          <div className="mt-3">
            <div className="flex items-center justify-between"><p className="label">Routes in view</p>{vf.routes.length > 0 && <button type="button" onClick={() => setVf((f) => ({ ...f, routes: [] }))} className="text-[12px] font-bold text-accent hover:underline">Any route</button>}</div>
            <input value={routeQuery} onChange={(e) => setRouteQuery(e.target.value)} placeholder="Find a route, e.g. 428 or BRCA" aria-label="Find a route" className="mt-1.5 h-9 w-full rounded-lg border border-line bg-surface px-2.5 text-[13px] outline-none focus:border-accent" />
            <div className="mt-2 flex max-h-40 flex-wrap gap-1.5 overflow-y-auto" role="group" aria-label="Routes">
              {avail.routes.filter((r) => !routeQuery.trim() || r.route.toLowerCase().includes(routeQuery.trim().toLowerCase())).slice(0, 80).map((r) => {
                const on = vf.routes.includes(r.route);
                return (
                  <button key={`${r.mode}${r.route}`} type="button" aria-pressed={on} onClick={() => setVf((f) => ({ ...f, routes: on ? f.routes.filter((x) => x !== r.route) : [...f.routes, r.route] }))}
                    className={"rounded-full px-2.5 py-1 text-[12.5px] font-extrabold transition " + (on ? "text-white shadow" : "border border-line text-ink hover:border-muted")}
                    style={on ? { background: r.mode === "train" ? "var(--route-train)" : "var(--route-bus)" } : undefined}>{r.route}</button>
                );
              })}
              {avail.routes.length === 0 && <p className="text-[12.5px] text-muted">Waiting for live positions…</p>}
            </div>
          </div>
          {isFiltering(vf) && <button type="button" onClick={() => { setVf(NO_FILTER); setRouteQuery(""); }} className="mt-3 w-full rounded-lg border border-line py-1.5 text-[13px] font-bold hover:bg-surface2">Reset filters</button>}
        </div>
      )}
      <div className="no-print pointer-events-none absolute left-3 top-3 z-[800] flex max-w-[60%] flex-col gap-2">
        {journey && (
          <div className="pointer-events-auto flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-line bg-surface/95 px-3 py-2 text-[12.5px] shadow-md">
            <span className="flex items-center gap-1.5"><i className="inline-block h-1.5 w-5 rounded bg-bus" />Bus</span>
            <span className="flex items-center gap-1.5"><i className="inline-block h-1.5 w-5 rounded bg-train" />Train</span>
            <span className="flex items-center gap-1.5"><i className="inline-block w-5 border-t-[3px] border-dotted border-walk" />Walk</span>
            <span className="flex items-center gap-1.5"><i className="inline-block size-3 rounded-full border-[3px] border-warn bg-surface" />Change</span>
            <span className="text-muted">{fmtDist(journey.totalM)}</span>
          </div>
        )}
        {fetchVehicles && showVeh && vehStat && (
          <div className="pointer-events-auto flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-line bg-surface/95 px-3 py-2 text-[12.5px] shadow-md" role="status" aria-label="Live vehicles">
            {vehStat.err ? <span className="text-warn">{vehStat.err}</span> : <>
              <span className="flex items-center gap-1.5 font-bold"><i className="inline-block size-2 animate-pulse rounded-full bg-ok" />Live</span>
              {isFiltering(vf) && shown !== null
                ? <span>Showing {shown} of {vehStat.bus + vehStat.train + vehStat.other}</span>
                : <><span>{vehStat.bus} bus{vehStat.bus === 1 ? "" : "es"}</span>{vehStat.train > 0 && <span>{vehStat.train} train{vehStat.train === 1 ? "" : "s"}</span>}</>}
              {vehStat.age !== null && <span className="text-muted">updated {Math.round(vehStat.age)}s ago</span>}
            </>}
          </div>
        )}
        {sel && (
          <div className="pointer-events-auto rise rounded-xl border border-line bg-surface px-3 py-2.5 shadow-lg" role="status" aria-label="Selected service" data-testid="selected-vehicle">
            <p className="text-[13.5px] font-extrabold">{sel.label}{sel.line && sel.label.startsWith("Bus") ? <span className="font-semibold"> · {sel.line}</span> : null}</p>
            {sel.line && !sel.label.startsWith("Bus") && <p className="text-[12px] font-semibold text-muted">{sel.line}</p>}
            {sel.headsign && <p className="text-[12.5px] font-semibold text-muted">Towards {sel.headsign}</p>}
            <p className="mt-0.5 text-[12px] text-muted">{sel.err ? "Could not load this service's route." : sel.headsign === null && fetchTripPath ? "Loading route…" : sel.live ? "Its full route is highlighted; other services are dimmed." : "No live position yet (it may not have started). Showing its route."}</p>
            <div className="mt-2 flex gap-2">
              {sel.live && <button type="button" onClick={() => actions.current.follow()} className={"rounded-lg px-2.5 py-1 text-[12.5px] font-bold " + (followLabel ? "pill-on" : "border border-line hover:bg-surface2")}>{followLabel ? "Stop following" : "Follow"}</button>}
              <button type="button" onClick={() => actions.current.clear()} className="rounded-lg border border-line px-2.5 py-1 text-[12.5px] font-bold hover:bg-surface2">Clear</button>
            </div>
          </div>
        )}
        {geoErr && <p role="alert" className="pointer-events-auto rounded-xl border border-line bg-warn-bg px-3 py-2 text-[12.5px] text-warn shadow-md">{geoErr}</p>}
        {tileErr && base !== "plain" && <p role="status" className="pointer-events-auto rounded-xl border border-line bg-surface px-3 py-2 text-[12.5px] text-muted shadow-md">Map tiles unavailable (offline?). The route is still drawn; switch to “Route only” to hide this.</p>}
      </div>
    </div>
  );
}
