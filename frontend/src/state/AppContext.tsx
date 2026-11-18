import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Api } from "../api/client";
import { ApiError, type Health, type PendingAction } from "../api/types";
import { confirmation } from "../lib/diff";

export interface Approvals {
  pending: PendingAction[];
  recent: PendingAction[];
  busy: ReadonlySet<string>;
  errors: Record<string, string>;
  refresh: () => Promise<void>;
  approve: (a: PendingAction) => Promise<void>;
  reject: (a: PendingAction, reason?: string) => Promise<void>;
  edit: (a: PendingAction, payload: Record<string, unknown>) => Promise<void>;
}

interface Ctx {
  api: Api;
  health: Health | null;
  healthError: string | null;
  approvals: Approvals;
  /** Chat registers a callback so approval outcomes can be echoed into the conversation. */
  setOutcomeHandler: (fn: ((text: string, tone: "ok" | "info" | "bad") => void) | null) => void;
}

const AppCtx = createContext<Ctx | null>(null);
export function useApp(): Ctx {
  const c = useContext(AppCtx);
  if (!c) throw new Error("useApp must be used inside <AppProvider>");
  return c;
}

const POLL_MS = 4000;
const message = (e: unknown) => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Something went wrong");

export function AppProvider({ api, children }: { api: Api; children: ReactNode }) {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingAction[]>([]);
  const [recent, setRecent] = useState<PendingAction[]>([]);
  const [busy, setBusy] = useState<ReadonlySet<string>>(new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});
  const onOutcome = useRef<Parameters<Ctx["setOutcomeHandler"]>[0]>(null);
  const setOutcomeHandler = useCallback<Ctx["setOutcomeHandler"]>((fn) => { onOutcome.current = fn; }, []);

  useEffect(() => {
    let live = true;
    api.health().then((h) => { if (live) { setHealth(h); setHealthError(null); } })
      .catch((e: unknown) => { if (live) setHealthError(message(e)); });
    return () => { live = false; };
  }, [api]);

  const refresh = useCallback(async () => {
    try {
      const [p, all] = await Promise.all([api.approvals("pending"), api.approvals()]);
      setPending(p);
      setRecent(all.filter((a) => a.status !== "pending").slice(0, 5));
    } catch {
      /* the header shows connectivity; polling just tries again */
    }
  }, [api]);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => { if (!document.hidden) void refresh(); }, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  const run = useCallback(async (id: string, work: () => Promise<PendingAction>, done: (a: PendingAction) => void) => {
    setBusy((b) => new Set(b).add(id));
    setErrors((e) => Object.fromEntries(Object.entries(e).filter(([k]) => k !== id)));
    try {
      done(await work());
    } catch (e) {
      setErrors((x) => ({ ...x, [id]: message(e) }));
    } finally {
      setBusy((b) => { const n = new Set(b); n.delete(id); return n; });
      await refresh();
    }
  }, [refresh]);

  const approvals = useMemo<Approvals>(() => ({
    pending, recent, busy, errors, refresh,
    approve: (a) => run(a.id, () => api.approve(a.id, a.payload_hash), (r) => {
      if (r.status === "executed") onOutcome.current?.(`Approved. ${confirmation(r)}`, "ok");
      else if (r.status === "failed") onOutcome.current?.(`Approved, but it could not be applied: ${r.error ?? "unknown error"}`, "bad");
    }),
    reject: (a, reason) => run(a.id, () => api.reject(a.id, reason), () => onOutcome.current?.(`Rejected: ${a.summary}. Nothing was changed.`, "info")),
    edit: (a, payload) => run(a.id, () => api.edit(a.id, payload), () => { /* the card re-renders with the new diff */ }),
  }), [pending, recent, busy, errors, refresh, run, api]);

  const value = useMemo<Ctx>(() => ({ api, health, healthError, approvals, setOutcomeHandler }), [api, health, healthError, approvals, setOutcomeHandler]);
  return <AppCtx.Provider value={value}>{children}</AppCtx.Provider>;
}
