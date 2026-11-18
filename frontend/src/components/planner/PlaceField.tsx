import { useEffect, useId, useRef, useState } from "react";
import type { Api } from "../../api/client";
import type { PlaceHit } from "../../api/types";

export interface PlaceSel { id: string; name: string; kind: PlaceHit["kind"]; lat?: number; lon?: number }

const KIND_LABEL: Record<PlaceHit["kind"], string> = { saved: "Saved", station: "Station", stop: "Stop", address: "Address / place", gps: "Current location" };
const KEY = "recent-places";
export function readRecents(): PlaceSel[] {
  try { const v = JSON.parse(localStorage.getItem(KEY) ?? "[]") as PlaceSel[]; return Array.isArray(v) ? v.filter((p) => p && typeof p.id === "string" && typeof p.name === "string").slice(0, 6) : []; } catch { return []; }
}
export function rememberPlace(p: PlaceSel): void {
  if (p.kind === "gps") return;
  try { localStorage.setItem(KEY, JSON.stringify([p, ...readRecents().filter((x) => x.id !== p.id)].slice(0, 6))); } catch { /* ignore */ }
}

const ico = { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true } as const;
const KindIcon = ({ kind }: { kind: PlaceHit["kind"] }) =>
  kind === "saved" ? <svg {...ico}><path d="m3 11 9-8 9 8v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z" /></svg>
  : kind === "station" ? <svg {...ico}><rect x="5" y="3" width="14" height="14" rx="3" /><path d="M5 11h14M8 21l2-4M16 21l-2-4" /></svg>
  : kind === "stop" ? <svg {...ico}><rect x="4" y="3" width="16" height="14" rx="2" /><path d="M4 11h16M8 21v-4M16 21v-4" /></svg>
  : <svg {...ico}><path d="M12 21s7-6.2 7-12a7 7 0 0 0-14 0c0 5.8 7 12 7 12z" /><circle cx="12" cy="9" r="2.5" /></svg>;

interface Props {
  label: string; marker: string; markerColor?: string; text: string; onText: (t: string) => void; value: PlaceSel | null; onPick: (p: PlaceSel | null) => void;
  api: Api; inputRef?: React.Ref<HTMLInputElement>; onEnter?: () => void; allowGps?: boolean;
}

/** Place search box: saved places and recents on focus, stops/stations as you type, and (only when the
 *  timetable has few matches) addresses/landmarks from OpenStreetMap. */
export function PlaceField({ label, marker, markerColor, text, onText, value, onPick, api, inputRef, onEnter, allowGps }: Props) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [hits, setHits] = useState<PlaceHit[]>([]);
  const [busy, setBusy] = useState(false);
  const [active, setActive] = useState(-1);
  const [msg, setMsg] = useState<string | null>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const seq = useRef(0);

  useEffect(() => {
    if (!open) return;
    const n = ++seq.current;
    const ctl = new AbortController();
    const t = setTimeout(async () => {
      setBusy(true);
      try {
        const q = text.trim();
        let r = await api.places(q, false, ctl.signal);
        if (n !== seq.current) return;
        setHits(r);
        const stops = r.filter((h) => h.kind === "stop" || h.kind === "station").length;
        if (q.length >= 4 && stops < 3) {
          const more = await api.places(q, true, ctl.signal);
          if (n !== seq.current) return;
          r = more;
          setHits(more);
        }
        setMsg(r.length === 0 && q ? "No matching stops or places." : null);
      } catch (e) {
        if (!(e instanceof DOMException && e.name === "AbortError") && n === seq.current) { setHits([]); setMsg("Could not search places."); }
      } finally {
        if (n === seq.current) setBusy(false);
      }
    }, text.trim() ? 180 : 0);
    return () => { clearTimeout(t); ctl.abort(); };
  }, [text, open, api]);

  useEffect(() => {
    const away = (e: MouseEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, []);

  const recents = !text.trim() ? readRecents().filter((r) => !hits.some((h) => h.id === r.id)) : [];
  const items: (PlaceSel & { modes?: string[] })[] = [...hits.map((h) => ({ id: h.id, name: h.name, kind: h.kind, lat: h.lat, lon: h.lon, modes: h.modes })), ...recents];

  const pick = (p: PlaceSel) => { onPick(p); onText(p.name); setOpen(false); setActive(-1); rememberPlace(p); };
  const gps = () => {
    setMsg(null);
    if (!navigator.geolocation) { setMsg("This browser cannot share your location."); setOpen(true); return; }
    navigator.geolocation.getCurrentPosition(
      (p) => pick({ id: `${p.coords.latitude.toFixed(6)},${p.coords.longitude.toFixed(6)}`, name: "My location", kind: "gps", lat: p.coords.latitude, lon: p.coords.longitude }),
      () => { setMsg("Could not get your location (permission denied?)."); setOpen(true); },
      { enableHighAccuracy: true, timeout: 8000 },
    );
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setActive((a) => Math.min(items.length - 1, a + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(-1, a - 1)); }
    else if (e.key === "Escape") setOpen(false);
    else if (e.key === "Enter") {
      const it = items[active];
      if (open && it) { e.preventDefault(); pick(it); }
      else { setOpen(false); onEnter?.(); }
    }
  };

  return (
    <div ref={wrap} className="relative">
      <div className="flex items-center gap-2 rounded-xl border border-line bg-surface px-2.5 focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/25">
        <span aria-hidden className={"grid size-5 shrink-0 place-items-center rounded-full text-[11px] font-extrabold text-white " + (marker === "A" ? "bg-ok" : "bg-bad")} style={{ background: markerColor ?? (marker === "A" ? "#1a8a4f" : "#d93025") }}>{marker}</span>
        <input ref={inputRef} role="combobox" aria-expanded={open} aria-controls={`${id}-list`} aria-autocomplete="list" aria-label={label} aria-activedescendant={active >= 0 ? `${id}-${active}` : undefined}
          value={text} placeholder={label} autoComplete="off" spellCheck={false}
          onChange={(e) => { onText(e.target.value); if (value) onPick(null); setOpen(true); setActive(-1); }}
          onFocus={(e) => { setOpen(true); e.currentTarget.select(); }} onKeyDown={onKey}
          className="h-11 min-w-0 flex-1 bg-transparent text-[15px] outline-none placeholder:text-muted" />
        {busy && <span aria-hidden className="size-3.5 animate-spin rounded-full border-2 border-muted border-t-transparent" />}
        {text && <button type="button" aria-label={`Clear ${label}`} onClick={() => { onText(""); onPick(null); setOpen(true); }} className="grid size-7 place-items-center rounded-full text-muted hover:bg-surface2 hover:text-ink"><svg {...ico} width={16} height={16}><path d="M18 6 6 18M6 6l12 12" /></svg></button>}
        {allowGps && <button type="button" aria-label="Use my current location" data-tip="Use my location" onClick={gps} className="tip grid size-8 place-items-center rounded-full text-accent hover:bg-accent/10"><svg {...ico}><circle cx="12" cy="12" r="3" /><circle cx="12" cy="12" r="8" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3" /></svg></button>}
      </div>
      {open && (items.length > 0 || msg) && (
        <ul id={`${id}-list`} role="listbox" aria-label={`${label} suggestions`} className="absolute inset-x-0 top-full z-[900] mt-1 max-h-72 overflow-auto rounded-xl border border-line bg-surface py-1 shadow-xl">
          {items.map((it, i) => (
            <li key={`${it.kind}-${it.id}-${i}`} id={`${id}-${i}`} role="option" aria-selected={i === active}
              onMouseDown={(e) => { e.preventDefault(); pick(it); }} onMouseEnter={() => setActive(i)}
              className={"flex cursor-pointer items-center gap-3 px-3 py-2 " + (i === active ? "bg-surface2" : "")}>
              <span className="text-muted"><KindIcon kind={it.kind} /></span>
              <span className="min-w-0 flex-1"><span className="block truncate text-[14px] font-semibold">{it.name}</span>
                <span className="block text-[12px] text-muted">{KIND_LABEL[it.kind]}{it.modes && it.modes.length > 0 ? ` · ${it.modes.join(" + ")}` : ""}{i >= hits.length ? " · recent" : ""}</span></span>
            </li>
          ))}
          {msg && <li role="presentation" className="px-3 py-2 text-[13px] text-muted">{msg}</li>}
        </ul>
      )}
    </div>
  );
}
