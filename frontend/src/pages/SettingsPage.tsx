import { useEffect, useId, useState, type FormEvent } from "react";
import { ApiError, type Settings } from "../api/types";
import { Button, Card, Spinner } from "../components/ui";
import { useApp } from "../state/AppContext";

interface Row { id: number; name: string; place: string }
let rowId = 0;
const toRows = (d: Record<string, string>): Row[] => Object.entries(d).map(([name, place]) => ({ id: rowId++, name, place }));
const input = "mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm";

export function SettingsPage() {
  const { api, health } = useApp();
  const uid = useId();
  const [saved, setSaved] = useState<Settings | null>(null);
  const [home, setHome] = useState("");
  const [rows, setRows] = useState<Row[]>([]);
  const [start, setStart] = useState("09:00");
  const [end, setEnd] = useState("17:00");
  const [buffer, setBuffer] = useState(10);
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const apply = (s: Settings) => {
    setSaved(s); setHome(s.home); setRows(toRows(s.destinations)); setStart(s.working_hours_start);
    setEnd(s.working_hours_end); setBuffer(s.buffer_minutes); setModel(s.router_model);
  };
  useEffect(() => {
    // `live` makes a late/duplicate response (StrictMode runs effects twice; slow networks) harmless:
    // it must never overwrite what the user has started typing.
    let live = true;
    api.settings()
      .then((s) => { if (live) apply(s); })
      .catch((e: unknown) => { if (live) setMsg({ tone: "bad", text: e instanceof ApiError ? e.message : "Could not load settings" }); });
    return () => { live = false; };
  }, [api]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setMsg(null); setFieldErrors({});
    const destinations: Record<string, string> = {};
    for (const r of rows) if (r.name.trim() && r.place.trim()) destinations[r.name.trim()] = r.place.trim();
    try {
      apply(await api.saveSettings({ home, destinations, working_hours_start: start, working_hours_end: end, buffer_minutes: buffer, router_model: model }));
      setMsg({ tone: "ok", text: "Saved. The change applies to your next question." });
    } catch (err) {
      if (err instanceof ApiError) {
        setFieldErrors(Object.fromEntries(err.details.map((d) => [d.field, d.message])));
        setMsg({ tone: "bad", text: err.details.length ? "Please fix the highlighted fields." : err.message });
      } else setMsg({ tone: "bad", text: "Could not save settings" });
    } finally { setBusy(false); }
  };
  const err = (k: string) => fieldErrors[k] && <p role="alert" className="mt-1 text-xs text-bad">{fieldErrors[k]}</p>;

  if (!saved && !msg) return <p className="mx-auto max-w-3xl px-4 py-6 text-sm text-muted"><Spinner /> Loading settings…</p>;
  const models = saved ? [...new Set([saved.router_model, ...saved.available_models])] : [];
  return (
    <form onSubmit={(e) => void submit(e)} className="mx-auto w-full max-w-3xl space-y-5 px-4 py-4" aria-label="Settings">
      <h1 className="page-title">Settings</h1>
      {health?.demo && <p className="rounded-lg bg-warn-bg px-3 py-2 text-sm text-warn">Demo mode: changes are kept in a temporary profile, not your real one.</p>}

      <Card className="space-y-4 p-4">
        <h3 className="font-bold">Where you travel from</h3>
        <label htmlFor={`${uid}-home`} className="block text-sm font-semibold">Home stop or station
          <input id={`${uid}-home`} className={input} value={home} maxLength={100} required onChange={(e) => setHome(e.target.value)} aria-describedby={`${uid}-homehint`} aria-invalid={!!fieldErrors.home} />
        </label>
        <p id={`${uid}-homehint`} className="text-xs text-muted">{saved?.home_resolves_to ? <>Matched to <strong>{saved.home_resolves_to}</strong> in the timetable.</> : "A stop or station name from the TransLink timetable."}</p>
        {err("home")}
        <fieldset>
          <legend className="text-sm font-semibold">Usual destinations <span className="font-normal text-muted">(nickname → place)</span></legend>
          <div className="mt-2 space-y-2">
            {rows.map((r, i) => (
              <div key={r.id} className="flex flex-wrap items-center gap-2">
                <label className="sr-only" htmlFor={`${uid}-n${r.id}`}>Nickname {i + 1}</label>
                <input id={`${uid}-n${r.id}`} className={input + " mt-0 w-36"} placeholder="nickname" maxLength={40} value={r.name} onChange={(e) => setRows((x) => x.map((y) => (y.id === r.id ? { ...y, name: e.target.value } : y)))} />
                <span aria-hidden>→</span>
                <label className="sr-only" htmlFor={`${uid}-p${r.id}`}>Place {i + 1}</label>
                <input id={`${uid}-p${r.id}`} className={input + " mt-0 min-w-40 flex-1"} placeholder="stop or station" maxLength={100} value={r.place} onChange={(e) => setRows((x) => x.map((y) => (y.id === r.id ? { ...y, place: e.target.value } : y)))} />
                <Button variant="ghost" aria-label={`Remove destination ${i + 1}`} onClick={() => setRows((x) => x.filter((y) => y.id !== r.id))}>Remove</Button>
              </div>
            ))}
            {rows.length < 20 && <Button onClick={() => setRows((x) => [...x, { id: rowId++, name: "", place: "" }])}>Add destination</Button>}
          </div>
          {err("destinations")}
        </fieldset>
      </Card>

      <Card className="space-y-4 p-4">
        <h3 className="font-bold">Scheduling</h3>
        <div className="grid gap-4 sm:grid-cols-3">
          <label htmlFor={`${uid}-ws`} className="block text-sm font-semibold">Working day starts<input id={`${uid}-ws`} type="time" className={input} value={start} onChange={(e) => setStart(e.target.value)} required />{err("working_hours_start")}</label>
          <label htmlFor={`${uid}-we`} className="block text-sm font-semibold">Working day ends<input id={`${uid}-we`} type="time" className={input} value={end} onChange={(e) => setEnd(e.target.value)} required />{err("working_hours_end")}</label>
          <label htmlFor={`${uid}-b`} className="block text-sm font-semibold">Buffer around meetings (min)<input id={`${uid}-b`} type="number" min={0} max={120} className={input} value={buffer} onChange={(e) => setBuffer(Number(e.target.value))} required />{err("buffer_minutes")}</label>
        </div>
        <p className="text-xs text-muted">Free-slot search only offers times inside your working day, keeping this much clear time around other events.</p>
      </Card>

      <Card className="space-y-3 p-4">
        <h3 className="font-bold">Model</h3>
        <label htmlFor={`${uid}-m`} className="block text-sm font-semibold">Routing model
          <select id={`${uid}-m`} className={input} value={model} onChange={(e) => setModel(e.target.value)}>
            {models.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </label>
        <p className="text-xs text-muted">Decides which agents handle your question. Only models installed in Ollama are listed. {saved && saved.available_models.length === 0 && "(The model list is unavailable right now.)"}</p>
      </Card>

      <div className="flex items-center gap-3">
        <Button variant="primary" type="submit" disabled={busy}>{busy && <Spinner />}Save settings</Button>
        {msg && <p role={msg.tone === "bad" ? "alert" : "status"} className={"text-sm font-semibold " + (msg.tone === "ok" ? "text-ok" : "text-bad")}>{msg.text}</p>}
      </div>
    </form>
  );
}
