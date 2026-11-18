import { describe, expect, it } from "vitest";
import { ALL_MODES, MODE_COLOUR, MODE_LABEL, serviceColour } from "./modes";
import { applyFilters, DEFAULT_FILTERS, toJourneys } from "./journey";
import type { TripArtifact, TripLeg } from "../api/types";

describe("service colours", () => {
  it("gives every mode a label and a distinct colour, so a tram never looks like a bus", () => {
    expect(ALL_MODES).toEqual(["bus", "train", "tram", "ferry"]);
    expect(new Set(ALL_MODES.map((m) => MODE_COLOUR[m])).size).toBe(4);
    expect(MODE_LABEL.tram).toBe("Tram");
  });
  it("uses per-line colours for trains, and one colour per other mode", () => {
    expect(serviceColour("train", "BNBR")).toBe("#2563eb"); // Beenleigh
    expect(serviceColour("tram", "L1")).toBe(MODE_COLOUR.tram);
    expect(serviceColour("ferry", "F1")).toBe(MODE_COLOUR.ferry);
  });
  it("draws walking in the neutral colour", () => {
    expect(serviceColour(null)).toBe("var(--route-walk)");
  });
});

const leg = (mode: TripLeg["mode"], depart_iso: string, arrive_iso: string): TripLeg => ({
  kind: "transit", from: "A", to: "B", depart: "", arrive: "", mode, route: "96", headsign: null, realtime: null,
  path: [[-37.8, 144.9], [-37.81, 144.95]], distance_m: 1000, num_stops: 3, depart_iso, arrive_iso,
});
const art = (modes: TripLeg["mode"][]): TripArtifact => ({
  origin_label: "A", destination_label: "B", bbox: [0, 0, 1, 1], context_stops: [], realtime: null, note: null, measurement_notes: "",
  journeys: modes.map((m) => ({
    depart: "", arrive: "", duration_min: 10, transfers: 0, legs: [leg(m, "2026-11-18T15:00:00+11:00", "2026-11-18T15:10:00+11:00")],
    summary: { stages: 1, wait_min: 0, all_bus: false, total_m: 0, walk_m: 0, transit_m: 0, by_mode_m: {}, straight_line_m: 0, circuity: null, stops_passed: 0, avg_speed_kmh: null },
    origin: { name: "A", lat: 0, lon: 0 }, destination: { name: "B", lat: 0, lon: 0 },
  })),
});

describe("filtering by every mode", () => {
  const js = toJourneys(art(["bus", "train", "tram", "ferry"]));
  it("keeps everything by default", () => expect(applyFilters(js, DEFAULT_FILTERS)).toHaveLength(4));
  it.each(["bus", "train", "tram", "ferry"] as const)("hides %s journeys when that mode is off", (m) => {
    const out = applyFilters(js, { ...DEFAULT_FILTERS, [m]: false });
    expect(out).toHaveLength(3);
    expect(out.every((j) => !j.legs.some((l) => l.mode === m))).toBe(true);
  });
});
