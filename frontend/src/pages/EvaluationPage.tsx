import { useState } from "react";
import type { EvalSummary, Rate } from "../api/types";
import { Empty, HBars, Section, StatTile, type Bar } from "../components/charts";
import { Button, Icon, Spinner } from "../components/ui";
import { normalizeEval } from "../lib/evalshape";
import { fmtPct, fmtRate } from "../lib/format";
import { useApp } from "../state/AppContext";
import { useFetch } from "../state/useFetch";

const rateBar = (label: string, r: Rate): Bar => ({ label, value: 100 * r.rate, lo: 100 * r.lo, hi: 100 * r.hi, note: `${r.k}/${r.n}`, tone: r.rate >= 0.9 ? "ok" : r.rate >= 0.7 ? "warn" : "bad" });
const pctFmt = (v: number) => `${v.toFixed(0)}%`;
const NOT_RUN = (cmd: string) => <>Not run yet. <code className="font-mono">{cmd}</code></>;

function Suites({ s }: { s: NonNullable<EvalSummary["deterministic"]> }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {Object.entries(s.suites).map(([name, v]) => (
        <div key={name} className="rounded-xl border border-line bg-surface2/40 p-3" data-testid={`suite-${name}`}>
          <div className="flex items-baseline justify-between"><h3 className="text-sm font-bold capitalize">{name}</h3><span className={"text-sm font-extrabold tabular-nums " + (v.passed === v.n ? "text-ok" : "text-bad")}>{v.passed}/{v.n}</span></div>
          <div className="mt-2 flex flex-wrap gap-1" role="img" aria-label={`${name}: ${v.passed} of ${v.n} cases passed`}>
            {(v.cases ?? []).map((c) => <span key={c.id} title={`${c.id}: ${c.desc}`} className={"size-3 rounded-[3px] " + (c.passed ? "bg-ok" : "bg-bad")} />)}
          </div>
        </div>
      ))}
    </div>
  );
}

export function EvaluationPage() {
  const { api } = useApp();
  const { data, error, loading, reload } = useFetch(() => api.evalLatest(), [api]);
  const [running, setRunning] = useState(false);
  const [local, setLocal] = useState<EvalSummary | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const d = local ?? (data ? normalizeEval(data) : null);
  const run = async () => {
    setRunning(true); setRunError(null);
    try { setLocal(normalizeEval(await api.runDeterministicEval())); } catch (e) { setRunError(e instanceof Error ? e.message : "Run failed"); } finally { setRunning(false); }
  };
  const routerModels = d ? Object.entries(d.router) : [];
  const e2eModels = d ? Object.entries(d.e2e) : [];
  return (
    <div className="mx-auto w-full max-w-[1760px] space-y-4 px-5 py-4">
      <header className="flex flex-wrap items-end gap-3">
        <div>
          <h1 className="page-title">Evaluation</h1>
          <p className="text-sm text-muted">What was measured, how, and what the measurements cannot tell you. 143 labelled cases across six groups.</p>
        </div>
        <Button variant="primary" className="ml-auto" onClick={() => void run()} disabled={running} data-testid="run-det">{running ? <Spinner /> : <Icon.Play />}Run offline suites now</Button>
        <Button onClick={() => { setLocal(null); reload(); }}>Refresh</Button>
      </header>
      {(error || runError) && <p role="alert" className="rounded-xl bg-bad-bg px-4 py-2 text-sm text-bad">{runError ?? error}</p>}
      {loading && !d && <p className="text-sm text-muted">Loading…</p>}
      {d && (
        <>
          <Section title="Deterministic suites" sub="Transit correctness, commute advice, calendar maths and safety on frozen fixtures. No model, no network: they run in CI on every push.">
            {d.deterministic ? <><Suites s={d.deterministic} /><p className="mt-3 text-xs text-muted">Last run {d.deterministic.run_at}. Each square is one case; hover for its description. Expectations were derived by hand and written by the same author as the code, so these guard against regressions more than they discover bugs.</p></> : <Empty title="No results yet">{NOT_RUN("make eval")}</Empty>}
          </Section>

          <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">
            <Section title="Routing accuracy" sub="Does the plan have the right intent, agents and dependencies? Bars show 95% Wilson intervals.">
              {routerModels.length === 0 ? <Empty title="Not run">{NOT_RUN("uv run python -m evals.run_eval router --model qwen3:8b")}</Empty> : routerModels.map(([m, r]) => (
                <div key={m} className="mb-4 last:mb-0">
                  <h3 className="mb-1 font-mono text-xs font-bold">{m} · {r.n} cases</h3>
                  <HBars label={`Routing accuracy by category, ${m}`} format={pctFmt} max={100} bars={[rateBar("All (exact plan)", r.exact), rateBar("Intent only", r.intent), ...Object.entries(r.by_category).map(([c, x]) => rateBar(c.replace(/_/g, " "), x))]} />
                  <p className="mt-2 text-xs text-muted">Over-acted (ran agents when it should have asked or refused): <b className="text-ink">{r.over_acted}</b> · Over-clarified: <b className="text-ink">{r.over_clarified}</b> · latency p50 {(r.latency_ms.p50 / 1000).toFixed(1)} s, p95 {(r.latency_ms.p95 / 1000).toFixed(1)} s · {r.tokens_in_mean.toFixed(0)} tokens in / request · cost $0 (local)</p>
                </div>
              ))}
            </Section>

            <Section title="End to end (live model, frozen world)" sub="Whole-orchestrator flows. A case passes only if tools, approvals, answer checks and the hallucination check all pass.">
              {e2eModels.length === 0 ? <Empty title="Not run">{NOT_RUN("uv run python -m evals.run_eval e2e --model qwen3:8b")}</Empty> : e2eModels.map(([m, r]) => (
                <div key={m} className="mb-4 last:mb-0">
                  <h3 className="mb-2 font-mono text-xs font-bold">{m} · {r.n} flows</h3>
                  <div className="grid grid-cols-2 gap-3">
                    <StatTile label="Task success" value={fmtPct(r.task_success.rate)} sub={fmtRate(r.task_success)} tone={r.task_success.rate >= 0.8 ? "ok" : "warn"} />
                    <StatTile label="Grounded answers" value={fmtPct(r.grounded.rate)} sub={`${r.unsupported_claims.unsupported} unsupported of ${r.unsupported_claims.checked} checkable claims`} tone={r.grounded.rate >= 0.9 ? "ok" : "warn"} />
                    <StatTile label="Unapproved writes" value={r.no_unapproved_write.k === r.no_unapproved_write.n ? "0" : String(r.no_unapproved_write.n - r.no_unapproved_write.k)} sub="calendar changed without approval" tone={r.no_unapproved_write.k === r.no_unapproved_write.n ? "ok" : "bad"} />
                    <StatTile label="Latency p50 / p95" value={`${(r.latency_ms.p50 / 1000).toFixed(0)}s`} sub={`p95 ${(r.latency_ms.p95 / 1000).toFixed(0)} s on a laptop`} />
                  </div>
                  <h4 className="mb-1 mt-4 text-[12px] font-bold uppercase tracking-wider text-muted">Tool selection by agent (when the router invoked it)</h4>
                  <HBars label="Tool selection accuracy" format={pctFmt} max={100} bars={Object.entries(r.tool_selection).map(([a, v]) => rateBar(a, v.when_invoked))} />
                </div>
              ))}
            </Section>
          </div>

          <Section title="Model comparison" sub="The brief asks for a two-model comparison with intervals.">
            {routerModels.length < 2 ? <Empty title="One model evaluated, so no recommendation">Only {routerModels.length ? <code className="font-mono">{routerModels[0]![0]}</code> : "no model"} has results. Pull a second open-weight model and run <code className="font-mono">MODELS='qwen3:8b qwen3:14b' make eval-live</code>; the report then shows accuracy, latency and a paired-bootstrap interval for the difference.</Empty> : <p className="text-sm">Two models evaluated. See <code className="font-mono">docs/evaluation.md</code> for the paired comparison.</p>}
          </Section>

          <Section title="Read before quoting any number">
            <ul className="list-disc space-y-1.5 pl-5 text-sm text-muted">
              <li>Cases were written by the system's author: small, synthetic, and shaped by the author's own phrasing.</li>
              <li>Intervals reflect sampling over these cases only; they cannot capture author bias or unseen phrasings.</li>
              <li>One local 8B model with thinking off, one run per model: no run-to-run variance is measured.</li>
              <li>The hallucination check is mechanical: it catches invented times, routes and stops, not wrong reasoning.</li>
              <li>Safety is measured against a model that always obeys injected text, to test the approval gate, not model manners.</li>
              <li>The LLM judge is unvalidated until a human labels its sheet (see <code className="font-mono">docs/evaluation.md</code>).</li>
            </ul>
          </Section>
        </>
      )}
    </div>
  );
}
