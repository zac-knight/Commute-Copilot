import type { TransitMode } from "../api/types";
import { trainLine } from "./trainlines";

/** One place for how each kind of service is named and coloured, so a tram never looks like a bus. */
export const MODE_LABEL: Record<TransitMode, string> = { bus: "Bus", train: "Train", tram: "Tram", ferry: "Ferry" };
export const MODE_COLOUR: Record<TransitMode, string> = { bus: "var(--route-bus)", train: "var(--route-train)", tram: "#16a34a", ferry: "#0e7490" };
export const ALL_MODES: TransitMode[] = ["bus", "train", "tram", "ferry"];

/** Colour for a service. South East Queensland trains get per-line colours (the codes like BNBR name real lines). */
export function serviceColour(mode: TransitMode | null | undefined, route?: string | null): string {
  if (!mode) return "var(--route-walk)";
  if (mode === "train" && route) return trainLine(route).primary.colour;
  return MODE_COLOUR[mode];
}
