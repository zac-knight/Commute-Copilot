import type { EvalSummary } from "../api/types";

/** The backend's /eval/latest has changed shape over time (an older build returns only
 *  {available:false, note}). Fill in anything missing so the page can never crash on it. */
export function normalizeEval(raw: unknown): EvalSummary {
  const r = (raw && typeof raw === "object" ? raw : {}) as Partial<EvalSummary>;
  return {
    available: Boolean(r.available),
    deterministic: r.deterministic ?? null,
    router: r.router && typeof r.router === "object" ? r.router : {},
    e2e: r.e2e && typeof r.e2e === "object" ? r.e2e : {},
  };
}
