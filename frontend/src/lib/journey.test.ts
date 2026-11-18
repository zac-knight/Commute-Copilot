import { describe, expect, it } from "vitest";
import type { TripArtifact, TripLeg } from "../api/types";
import { setDisplayZone, applyFilters, badges, brisbaneParts, dayOptions, decodeSearch, DEFAULT_FILTERS, encodeSearch, fmtClock, fmtDuration, isCoord, sortJourneys, startsIn, toJourneys } from "./journey";

const leg = (o: Partial<TripLeg> & { depart_iso: string; arrive_iso: string }): TripLeg => ({
  kind: "transit", from: "A", to: "B", depart: "", arrive: "", mode: "bus", route: "428", headsign: null, realtime: null,
  path: [[-27.5, 153.01], [-27.49, 153.02]], distance_m: 1500, num_stops: 3, ...o,
});
const art = (js: { legs: TripLeg[]; transfers: number }[]): TripArtifact => ({
  origin_label: "A", destination_label: "B", bbox: [0, 0, 1, 1], context_stops: [], realtime: null, note: null, measurement_notes: "",
  journeys: js.map((j) => ({
    depart: "", arrive: "", duration_min: 0, transfers: j.transfers, legs: j.legs,
    summary: { stages: 1, wait_min: 0, all_bus: true, total_m: 0, walk_m: 0, transit_m: 0, by_mode_m: {}, straight_line_m: 0, circuity: null, stops_passed: 0, avg_speed_kmh: null },
    origin: { name: "A", lat: 0, lon: 0 }, destination: { name: "B", lat: 0, lon: 0 },
  })),
});
const T = (hhmm: string) => `2026-11-18T${hhmm}:00+10:00`;

describe("display zone", () => {
  it("shows the same instant in the zone being planned in (Sydney is on daylight saving in January)", () => {
    const instant = new Date("2026-01-15T03:50:00Z");
    try {
      setDisplayZone("Australia/Brisbane");
      expect(fmtClock(instant)).toBe("1:50 pm");
      setDisplayZone("Australia/Sydney");
      expect(fmtClock(instant)).toBe("2:50 pm");
      setDisplayZone("Australia/Perth");
      expect(fmtClock(instant)).toBe("11:50 am");
    } finally {
      setDisplayZone("Australia/Brisbane");
    }
  });
  it("falls back to Brisbane when no zone is given", () => {
    setDisplayZone("");
    expect(fmtClock(new Date("2026-01-15T03:50:00Z"))).toBe("1:50 pm");
  });
});

describe("time formatting", () => {
  it("formats Brisbane clock time like TransLink", () => {
    expect(fmtClock(new Date(T("13:50")))).toBe("1:50 pm");
    expect(fmtClock(new Date(T("00:05")))).toBe("12:05 am");
  });
  it("formats durations", () => {
    expect(fmtDuration(1)).toBe("1 min");
    expect(fmtDuration(27)).toBe("27 mins");
    expect(fmtDuration(68)).toBe("1 hr 8 mins");
    expect(fmtDuration(120)).toBe("2 hr");
  });
  it("says when a trip starts", () => {
    const now = new Date(T("13:48"));
    expect(startsIn(new Date(T("13:50")), now)).toBe("in 2 min");
    expect(startsIn(new Date(T("13:47")), now)).toBe("leaving now");
    expect(startsIn(new Date(T("15:48")), now)).toBe("in 2 hr");
  });
  it("reads Brisbane parts whatever the browser zone", () => {
    const p = brisbaneParts(new Date("2026-11-17T20:30:00Z")); // 06:30 on the 18th in Brisbane
    expect([p.day, p.h, p.mi, p.dow]).toEqual([18, 6, 30, 3]);
  });
  it("offers Today and Tomorrow first", () => {
    const o = dayOptions(new Date(T("13:50")), 3);
    expect(o[0]).toEqual({ value: "2026-11-18", label: "Today (Wednesday)" });
    expect(o[1]?.label).toBe("Tomorrow (Thursday)");
    expect(o).toHaveLength(3);
  });
});

describe("toJourneys", () => {
  const base = art([{ transfers: 0, legs: [leg({ depart_iso: T("14:00"), arrive_iso: T("14:20") })] }]);
  it("computes times and duration from the legs", () => {
    const [j] = toJourneys(base);
    expect(j?.minutes).toBe(20);
    expect(j?.routes).toEqual(["428"]);
    expect(j?.legs[0]?.status).toBe("scheduled");
  });
  it("adds estimated walks when the place is not a stop, and skips them when it is", () => {
    const far = { name: "Mini golf", lat: -27.5, lon: 153.0 }; // ~1 km from the stop
    const dest = { name: "Cafe", lat: -27.49, lon: 153.0125 }; // ~0.7 km from the last stop
    const [j] = toJourneys(base, far, dest);
    expect(j?.legs.map((l) => l.kind)).toEqual(["walk", "transit", "walk"]);
    expect(j?.legs[0]?.estimated).toBe(true);
    expect(j?.legs[0]?.end.getTime()).toBe(new Date(T("14:00")).getTime());
    expect(j!.start.getTime()).toBeLessThan(new Date(T("14:00")).getTime());
    expect(j!.end.getTime()).toBeGreaterThan(new Date(T("14:20")).getTime());
    const near = { name: "A", lat: -27.5, lon: 153.01 };
    expect(toJourneys(base, near, null)[0]?.legs).toHaveLength(1);
  });
  it("drops absurdly long access walks", () => {
    const [j] = toJourneys(base, { name: "far", lat: -26.0, lon: 153.0 }, null);
    expect(j?.legs).toHaveLength(1);
  });
  it("carries realtime status", () => {
    const a = art([{ transfers: 0, legs: [leg({ depart_iso: T("14:00"), arrive_iso: T("14:20"), realtime: { status: "delayed", delay_min: 4 } })] }]);
    expect(toJourneys(a)[0]?.maxDelayMin).toBe(4);
  });
});

describe("sort, filter, badges", () => {
  const a = art([
    { transfers: 1, legs: [leg({ depart_iso: T("14:00"), arrive_iso: T("14:50") })] },
    { transfers: 0, legs: [leg({ depart_iso: T("14:10"), arrive_iso: T("15:10") }), leg({ kind: "walk", mode: null, route: null, depart_iso: T("15:10"), arrive_iso: T("15:15"), distance_m: 400 })] },
    { transfers: 0, legs: [leg({ mode: "train", route: "BN", depart_iso: T("14:20"), arrive_iso: T("14:45"), realtime: { status: "cancelled", delay_min: null } })] },
  ]);
  const js = toJourneys(a);
  it("sorts by each key", () => {
    expect(sortJourneys(js, "depart").map((j) => j.minutes)).toEqual([50, 65, 25]);
    expect(sortJourneys(js, "fastest")[0]?.minutes).toBe(25);
    expect(sortJourneys(js, "changes")[0]?.transfers).toBe(0);
    expect(sortJourneys(js, "walk")[0]?.walkM).toBe(0);
  });
  it("hides cancelled trips and honours mode and walking filters", () => {
    expect(applyFilters(js, DEFAULT_FILTERS)).toHaveLength(2);
    expect(applyFilters(js, { ...DEFAULT_FILTERS, hideCancelled: false })).toHaveLength(3);
    expect(applyFilters(js, { ...DEFAULT_FILTERS, hideCancelled: false, train: false })).toHaveLength(2);
    expect(applyFilters(js, { ...DEFAULT_FILTERS, maxWalkM: 200 }).every((j) => j.walkM <= 200)).toBe(true);
  });
  it("badges the winner of each category", () => {
    const ok = applyFilters(js, DEFAULT_FILTERS);
    expect(badges(ok, ok[0]!)).toContain("Fastest");
    expect(badges([ok[0]!], ok[0]!)).toEqual([]);
  });
});

describe("search state in the URL", () => {
  it("round-trips", () => {
    const s = { from: "home", fromName: "North Lakes", to: "-27.1,153.0", toName: "Mini Golf", date: "2026-11-18", time: "13:50", by: "arrive" as const, changes: 1, vias: [{ id: "B", name: "Bravo Road" }, { id: "-27.2,153.1", name: "Cafe" }], stay: 15 };
    expect(decodeSearch(encodeSearch(s))).toEqual(s);
  });
  it("defaults to no stops and a 5 minute stay, and caps stops at three", () => {
    expect(decodeSearch("from=a&to=b")).toMatchObject({ vias: [], stay: 5 });
    expect(decodeSearch("from=a&to=b&v=1&v=2&v=3&v=4&s=999")).toMatchObject({ vias: [{ id: "1" }, { id: "2" }, { id: "3" }], stay: 240 });
  });
  it("rejects incomplete or malformed input", () => {
    expect(decodeSearch("from=a")).toBeNull();
    expect(decodeSearch("from=a&to=b&d=junk&t=99&c=77")).toMatchObject({ changes: 4 });
    expect(decodeSearch("from=a&to=b&d=junk")).not.toHaveProperty("date");
  });
  it("recognises coordinates", () => {
    expect(isCoord("-27.49,153.01")).toBe(true);
    expect(isCoord("North Lakes")).toBe(false);
  });
});
