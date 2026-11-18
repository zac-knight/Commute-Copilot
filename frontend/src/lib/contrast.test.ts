import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// WCAG 2.x relative luminance / contrast ratio.
function lum(hex: string): number {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0]! + 0.7152 * c[1]! + 0.0722 * c[2]!;
}
function contrast(a: string, b: string): number {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi! + 0.05) / (lo! + 0.05);
}

function tokens(block: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of block.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})/g)) out[m[1]!] = m[2]!;
  return out;
}
const css = readFileSync(new URL("../index.css", import.meta.url), "utf8");
const light = tokens(css.split(".dark {")[0]!.split(":root {")[1]!);
const dark = tokens(css.split(".dark {")[1]!.split("}")[0]!);

// text colour on its background; AA for normal text is 4.5:1
const PAIRS: [string, string][] = [
  ["text", "bg"], ["text", "surface"], ["text", "surface-2"], ["muted", "bg"], ["muted", "surface"],
  ["muted", "surface-2"], ["accent", "surface"], ["accent", "bg"], ["accent-text", "accent"],
  ["ok", "ok-bg"], ["warn", "warn-bg"], ["bad", "bad-bg"], ["ok", "surface"], ["bad", "surface"],
  ["transit", "transit-bg"], ["weather", "weather-bg"], ["calendar", "calendar-bg"],
];

describe.each([["light", light], ["dark", dark]] as const)("%s theme", (_name, theme) => {
  it.each(PAIRS)("%s on %s meets WCAG AA (4.5:1)", (fg, bg) => {
    expect(theme[fg], `missing token ${fg}`).toBeDefined();
    expect(theme[bg], `missing token ${bg}`).toBeDefined();
    expect(contrast(theme[fg]!, theme[bg]!)).toBeGreaterThanOrEqual(4.5);
  });
});

describe("contrast helper", () => {
  it("matches the known extremes", () => {
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 0);
    expect(contrast("#777777", "#777777")).toBeCloseTo(1, 5);
  });
});
