import { describe, expect, it } from "vitest";
import { darken } from "./darkstyle";

const base = {
  version: 8, sources: {},
  layers: [
    { id: "background", type: "background", paint: { "background-color": "rgb(242,243,240)" } },
    { id: "water", type: "fill", paint: { "fill-color": "rgb(194,200,202)" } },
    { id: "highway_motorway_inner", type: "line", paint: { "line-color": ["interpolate", ["linear"], ["zoom"], 5, "#fff"] } },
    { id: "label_city", type: "symbol", paint: { "text-color": "#000", "text-halo-color": "#fff", "text-halo-width": 1 } },
    { id: "unknown_layer", type: "fill", paint: { "fill-color": "#abcdef" } },
  ],
};

const lum = (hex: string) => { const n = parseInt(hex.slice(1, 7), 16); return (0.2126 * (n >> 16) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255)) / 255; };

describe("darken", () => {
  const out = darken(base);
  const by = (id: string) => out.layers.find((l) => l.id === id)!;
  it("makes the background and water dark", () => {
    expect(lum(by("background").paint!["background-color"] as string)).toBeLessThan(0.15);
    expect(lum(by("water").paint!["fill-color"] as string)).toBeLessThan(0.25);
  });
  it("replaces expression-valued road colours and keeps motorways bright", () => {
    const c = by("highway_motorway_inner").paint!["line-color"];
    expect(typeof c).toBe("string");
    expect(lum(c as string)).toBeGreaterThan(0.4);
  });
  it("gives labels light text and a dark, wide-enough halo", () => {
    const p = by("label_city").paint!;
    expect(lum(p["text-color"] as string)).toBeGreaterThan(0.8);
    expect(lum(p["text-halo-color"] as string)).toBeLessThan(0.15);
    expect(p["text-halo-width"] as number).toBeGreaterThanOrEqual(1.5);
  });
  it("leaves layers it does not know alone and does not mutate the input", () => {
    expect(by("unknown_layer").paint!["fill-color"]).toBe("#abcdef");
    expect(base.layers[0]!.paint!["background-color"]).toBe("rgb(242,243,240)");
  });
});
