import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api/types";

export interface Fetched<T> { data: T | null; error: string | null; loading: boolean; reload: () => void }

/** Load something once (and on demand). Errors become a message; the previous data stays visible. */
export function useFetch<T>(load: () => Promise<T>, deps: unknown[]): Fetched<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    load().then((d) => { if (live) { setData(d); setError(null); } })
      .catch((e: unknown) => {
        if (!live) return;
        setError(e instanceof ApiError && e.status === 404
          ? "The running backend does not have this endpoint, so it is older than this UI. Stop it (Ctrl-C) and run ./start.sh again."
          : e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Something went wrong");
      })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { data, error, loading, reload: useCallback(() => { setLoading(true); setTick((t) => t + 1); }, []) };
}
