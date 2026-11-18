import { useSyncExternalStore } from "react";

/** Track a CSS media query (so a component can render ONE variant instead of hiding a duplicate). */
export function useMedia(query: string): boolean {
  return useSyncExternalStore(
    (cb) => { const m = window.matchMedia(query); m.addEventListener("change", cb); return () => m.removeEventListener("change", cb); },
    () => window.matchMedia(query).matches,
    () => false,
  );
}
