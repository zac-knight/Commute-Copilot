import type { Analytics, DelayRow } from "../api/types";
import { Columns, DataTable, Empty, HBars, Section, StatTile, type Bar } from "../components/charts";
import { fmtDelayS, fmtPct, fmtRate, WEEKDAYS } from "../lib/format";
import { useApp } from "../state/AppContext";
import { useFetch } from "../state/useFetch";

const mins = (s: number) => `${(s / 60).toFixed(1)} min`;
const tone = (s: number) => (s > 180 ? "bad" : s > 60 ? "warn" : "ok") as Bar["tone"];
const bars = (rows: DelayRow[], label: (k: string | number) => string): Bar[] =>
  rows.map((r) => ({ label: label(r.key), value: r.median_s, lo: r.q1_s, hi: r.q3_s, note: `n=${r.n}`, tone: tone(r.median_s) }));

function Coverage({ a }: { a: Analytics }) {
  const c = a.coverage;
  return (
    <>
      <div className={"rounded-2xl border p-4 " + (c.collector_running ? "border-ok bg-ok-bg/40" : "border-line bg-surface2/40")} role="status">
        <p className="text-sm font-bold">{c.collector_running ? "Collector is running" : c.polls ? "Collector is not running" : "No data has been collected yet"}</p>
        <p className="mt-1 text-xs text-muted">
          {c.polls
            ? `${c.polls.toLocaleString()} polls over ${c.hours_collected.toFixed(1)} h (${c.days_collected} day${c.days_collected === 1 ? "" : "s"}); ${c.rows.toLocaleString()} observations. Everything below is computed from real TransLink realtime data only; nothing here is simulated.`
            : "Start recording with `make collect` (polls the public TransLink GTFS-Realtime feed every 60 s). Every chart below fills in as data accrues, and says how much more it needs."}
        </p>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatTile label="Observed departures" value={c.final_rows.toLocaleString()} sub={`${c.rows_with_delay.toLocaleString()} with a realtime delay`} />
        <StatTile label="Days collected" value={c.days_collected} sub={`${c.hours_collected.toFixed(1)} hours of polling`} />
        <StatTile label="Poll success" value={c.polls ? fmtPct(c.success_rate.rate) : "–"} sub={c.polls ? `${c.ok_polls}/${c.polls} polls had live realtime` : "no polls yet"} tone={c.polls && c.success_rate.rate < 0.9 ? "warn" : "ink"} />
        <StatTile label="Median delay" value={a.distributions ? fmtDelayS(a.distributions.overall.median_s) : "–"} sub={a.distributions ? `IQR ${fmtDelayS(a.distributions.overall.q1_s)} to ${fmtDelayS(a.distributions.overall.q3_s)}` : "not enough data"} />
      </div>
    </>
  );
}

function ModelSection({ m }: { m: Analytics["model"] }) {
  if (!m.available) return <Empty title="Not enough data to train and fairly test a model yet">{m.reason}. The model is judged on a later, held-out period against two baselines, and it only counts as useful if it beats both with a confidence interval that excludes zero.</Empty>;
  const rows = (["always_on_time", "last_observed_delay", "model"] as const).map((k) => ({ k, mae: m.mae_s[k] }));
  const label = { always_on_time: "Always on time", last_observed_delay: "Last observed delay", model: "Our model" };
  return (
    <>
      <HBars label="Mean absolute error on the held-out period, in seconds (lower is better)" format={(v) => `${v.toFixed(0)} s`}
        bars={rows.map((r) => ({ label: label[r.k], value: r.mae, tone: r.k === "model" ? (m.beats_baselines ? "ok" : "warn") : "muted" }))} />
      <p className={"mt-3 rounded-xl px-3 py-2 text-sm " + (m.beats_baselines ? "bg-ok-bg text-ok" : "bg-warn-bg text-warn")}>
        {m.beats_baselines
          ? `The model beats the best baseline (${label[m.best_baseline as keyof typeof label]}) by ${m.gain_over_best_baseline_s.toFixed(0)} s (95% CI ${m.gain_ci_s[0].toFixed(0)} to ${m.gain_ci_s[1].toFixed(0)} s).`
          : `The model does NOT reliably beat the best baseline: improvement ${m.gain_over_best_baseline_s.toFixed(0)} s, 95% CI ${m.gain_ci_s[0].toFixed(0)} to ${m.gain_ci_s[1].toFixed(0)} s includes zero. It is therefore not offered to the agents.`}
      </p>
      <p className="mt-2 text-xs text-muted">Trained on {m.train_rows.toLocaleString()} departures up to {m.train_until}; tested on {m.test_rows.toLocaleString()} later departures over {m.test_days} day(s). Features known 10 minutes before departure: the delay reported then, the previous trip's delay, and the route-by-hour median.</p>
      <DataTable head={["coefficient", "value"]} rows={Object.entries(m.coefficients).map(([k, v]) => [k, v.toFixed(3)])} />
    </>
  );
}

export function AnalyticsPage() {
  const { api } = useApp();
  const { data: a, error, loading, reload } = useFetch(() => api.analytics(), [api]);
  return (
    <div className="mx-auto w-full max-w-[1760px] space-y-4 px-5 py-4">
      <header className="flex flex-wrap items-end gap-3">
        <div>
          <h1 className="page-title">Delay analytics</h1>
          <p className="text-sm text-muted">Do SEQ services run late, when, and can we predict it? Honest intervals, held-out evaluation, explicit about what is missing.</p>
        </div>
        <button type="button" onClick={reload} className="ml-auto rounded-xl border border-line px-3 py-1.5 text-sm font-semibold hover:bg-surface2">Refresh</button>
      </header>
      {error && <p role="alert" className="rounded-xl bg-bad-bg px-4 py-2 text-sm text-bad">{error}</p>}
      {loading && !a && <p className="text-sm text-muted">Loading…</p>}
      {a && (
        <>
          <Coverage a={a} />
          <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">
            <Section title="Delay by hour of day" sub="Median delay with the interquartile range (25th to 75th percentile) as the whisker.">
              {a.distributions ? <Columns label="Median delay by hour" format={(v) => `${(v / 60).toFixed(0)}m`} bars={bars(a.distributions.hour, (k) => String(k))} /> : <Empty title="Not enough data yet">{a.distributions_reason}</Empty>}
            </Section>
            <Section title="Delay by route" sub="Median and interquartile range; groups under 15 departures are hidden.">
              {a.distributions ? <HBars label="Median delay by route" format={mins} bars={bars(a.distributions.route, String)} /> : <Empty title="Not enough data yet">{a.distributions_reason}</Empty>}
            </Section>
            <Section title="Delay by weekday">
              {a.distributions ? <Columns label="Median delay by weekday" format={(v) => `${(v / 60).toFixed(0)}m`} bars={bars(a.distributions.weekday, (k) => WEEKDAYS[Number(k)] ?? String(k))} /> : <Empty title="Not enough data yet">{a.distributions_reason}</Empty>}
            </Section>
            <Section title="Cancellations" sub="Share of observed scheduled departures that were cancelled, with a 95% Wilson interval.">
              {a.cancellations ? (
                <div><p className="text-4xl font-extrabold tabular-nums">{fmtPct(a.cancellations.rate, 1)}</p><p className="mt-1 text-sm text-muted">{a.cancellations.k} of {a.cancellations.n} departures · {fmtRate(a.cancellations)}</p></div>
              ) : <Empty title="Not enough data yet">{a.cancellations_reason}</Empty>}
            </Section>
            <Section title="Does rain make services later?" sub="Median delay in rainy hours (at least 0.2 mm, Open-Meteo history) versus dry hours.">
              {a.rain ? (
                <div>
                  <HBars label="Median delay, wet vs dry hours" format={mins} bars={[{ label: `Rainy (n=${a.rain.wet_n})`, value: a.rain.wet_median_s, tone: "accent" }, { label: `Dry (n=${a.rain.dry_n})`, value: a.rain.dry_median_s, tone: "muted" }]} />
                  <p className="mt-2 text-sm">Difference {fmtDelayS(a.rain.diff_s)} (95% CI {fmtDelayS(a.rain.ci_lo_s)} to {fmtDelayS(a.rain.ci_hi_s)}). <span className="text-muted">{a.rain.note}.</span></p>
                </div>
              ) : <Empty title="Not enough data yet">{a.rain_reason}</Empty>}
            </Section>
            <Section title="Can we predict the delay?" sub="A small model against two baselines, judged on a later held-out period.">
              <ModelSection m={a.model} />
            </Section>
          </div>
        </>
      )}
    </div>
  );
}
