import { describe, expect, it } from "vitest";
import type { PendingAction, SpanNode } from "../api/types";
import { actionKind, approvalRows, confirmation } from "./diff";
import { fmtMs, fmtRange, fmtTokens, fmtWhen, toBrisbaneIso, toLocalInput } from "./format";
import { parseInline, parseMarkdown } from "./markdown";
import { bounds, clip, failedCount, findPlan, flatten } from "./waterfall";

describe("markdown (safe subset)", () => {
  it("parses bold, paragraphs and soft line breaks", () => {
    expect(parseInline("a **b** c")).toEqual([{ t: "text", v: "a " }, { t: "b", v: "b" }, { t: "text", v: " c" }]);
    const blocks = parseMarkdown("Line one  \nline two\n\nSecond paragraph");
    expect(blocks).toHaveLength(2);
    expect(blocks[0]).toMatchObject({ type: "p" });
    expect((blocks[0] as { lines: unknown[] }).lines).toHaveLength(2);
  });
  it("parses numbered and bulleted lists", () => {
    const blocks = parseMarkdown("1. **680** at 08:00\n2. **680** at 08:19\n\n- note\n- another");
    expect(blocks.map((b) => b.type)).toEqual(["ol", "ul"]);
    expect((blocks[0] as { items: unknown[] }).items).toHaveLength(2);
  });
  it("never produces HTML: markup in the text stays literal text", () => {
    const evil = '<img src=x onerror=alert(1)> **<script>alert(2)</script>**';
    const blocks = parseMarkdown(evil);
    const flat = JSON.stringify(blocks);
    expect(flat).toContain("<img src=x onerror=alert(1)>"); // kept as data, rendered by React as text
    expect(blocks.every((b) => ["p", "ul", "ol"].includes(b.type))).toBe(true);
  });
  it("handles empty input and unmatched markers", () => {
    expect(parseMarkdown("")).toEqual([]);
    expect(parseInline("5 ** 3 and **open")).toEqual([{ t: "text", v: "5 ** 3 and **open" }]);
  });
});

describe("format", () => {
  it("durations and tokens", () => {
    expect(fmtMs(0)).toBe("0 ms"); expect(fmtMs(820)).toBe("820 ms"); expect(fmtMs(12_340)).toBe("12.3 s");
    expect(fmtMs(112_000)).toBe("112 s"); expect(fmtMs(-1)).toBe("–"); expect(fmtMs(NaN)).toBe("–");
    expect(fmtTokens(950)).toBe("950"); expect(fmtTokens(4804)).toBe("4.8k"); expect(fmtTokens(11_904)).toBe("12k");
  });
  it("shows times in Brisbane regardless of the viewer's zone", () => {
    expect(fmtWhen("2026-11-19T15:00:00+10:00")).toBe("Thu 19 Nov, 15:00");
    expect(fmtWhen("2026-11-19T05:00:00Z")).toBe("Thu 19 Nov, 15:00"); // UTC input, Brisbane output
    expect(fmtRange("2026-11-19T15:00:00+10:00", "2026-11-19T16:00:00+10:00")).toBe("Thu 19 Nov, 15:00–16:00");
    expect(fmtRange("2026-11-19T23:00:00+10:00", "2026-11-20T01:00:00+10:00")).toContain(" – ");
    expect(fmtWhen("nonsense")).toBe("nonsense");
  });
  it("converts datetime-local values to Brisbane ISO and back", () => {
    expect(toBrisbaneIso("2026-11-19T15:00")).toBe("2026-11-19T15:00:00+10:00");
    expect(toLocalInput("2026-11-19T15:00:00+10:00")).toBe("2026-11-19T15:00");
    expect(toLocalInput("2026-11-19T05:00:00Z")).toBe("2026-11-19T15:00");
    expect(toLocalInput("2026-11-19T00:00:00+10:00")).toBe("2026-11-19T00:00"); // midnight is 00, not 24
    expect(toLocalInput("bad")).toBe("");
  });
});

const act = (type: string, payload: Record<string, unknown>, result: Record<string, unknown> | null = null) =>
  ({ type, payload, result }) as Pick<PendingAction, "type" | "payload" | "result">;

describe("approval rows", () => {
  it("create: shows the exact new event", () => {
    const rows = approvalRows(act("calendar.create_event", { title: "Dentist", start: "2026-11-19T14:00:00+10:00", end: "2026-11-19T15:00:00+10:00", location: "Chermside", description: "" }));
    expect(rows).toEqual([
      { label: "Title", after: "Dentist" },
      { label: "When", after: "Thu 19 Nov, 14:00–15:00" },
      { label: "Where", after: "Chermside" },
    ]);
  });
  it("update: before AND after, only for fields that change", () => {
    const rows = approvalRows(act("calendar.update_event", {
      before: { title: "Team sync", start: "2026-11-19T10:00:00+10:00", end: "2026-11-19T11:00:00+10:00", location: "Room 4", description: "" },
      changes: { start: "2026-11-19T11:00:00+10:00", end: "2026-11-19T12:00:00+10:00", title: "Team sync (moved)" },
    }));
    expect(rows.map((r) => r.label)).toEqual(["Title", "When"]);
    expect(rows[0]).toEqual({ label: "Title", before: "Team sync", after: "Team sync (moved)" });
    expect(rows[1]).toMatchObject({ before: "Thu 19 Nov, 10:00–11:00", after: "Thu 19 Nov, 11:00–12:00" });
  });
  it("update with only a new start keeps the old end in the after text", () => {
    const rows = approvalRows(act("calendar.update_event", {
      before: { start: "2026-11-19T10:00:00+10:00", end: "2026-11-19T11:00:00+10:00" },
      changes: { start: "2026-11-19T10:30:00+10:00" },
    }));
    expect(rows[0]?.after).toBe("Thu 19 Nov, 10:30–11:00");
  });
  it("delete: shows what will be removed", () => {
    const rows = approvalRows(act("calendar.delete_event", { before: { title: "Lunch", start: "2026-11-19T12:00:00+10:00", end: "2026-11-19T13:00:00+10:00", location: "" } }));
    expect(rows).toEqual([{ label: "Title", before: "Lunch" }, { label: "When", before: "Thu 19 Nov, 12:00–13:00" }]);
  });
  it("is robust to malformed payloads and unknown types", () => {
    expect(approvalRows(act("calendar.update_event", {}))).toEqual([]);
    expect(approvalRows(act("something.else", { a: 1 }))).toEqual([]);
    expect(() => approvalRows(act("calendar.delete_event", { before: null }))).not.toThrow();
  });
  it("kinds and confirmations", () => {
    expect(actionKind("calendar.create_event")).toBe("create");
    expect(actionKind("calendar.delete_event")).toBe("delete");
    expect(actionKind("x")).toBe("other");
    expect(confirmation(act("calendar.create_event", { title: "Dentist", start: "2026-11-19T14:00:00+10:00" }, { title: "Dentist" }))).toBe("Added “Dentist” to your calendar (Thu 19 Nov, 14:00).");
    expect(confirmation(act("calendar.delete_event", { before: { title: "Lunch" } }, { deleted: false }))).toBe("That event was already gone.");
  });
});

const node = (id: string, start: number, end: number, children: SpanNode[] = [], extra: Partial<SpanNode> = {}): SpanNode => ({
  span_id: id, request_id: "r", parent_id: null, kind: "tool", name: id, start, end, status: "ok", input_preview: "", output_preview: "",
  tokens_in: 0, tokens_out: 0, model: null, cost_estimate: null, error: null, attributes: {}, children, ...extra,
});

describe("waterfall", () => {
  const tree = [node("root", 0, 10, [node("a", 0, 4, [node("a1", 1, 2)]), node("b", 4, 10)])];
  it("flattens in order with depth and proportional positions", () => {
    const rows = flatten(tree);
    expect(rows.map((r) => [r.node.span_id, r.depth])).toEqual([["root", 0], ["a", 1], ["a1", 2], ["b", 1]]);
    expect(rows[1]).toMatchObject({ leftPct: 0, widthPct: 40, hasChildren: true });
    expect(rows[3]).toMatchObject({ leftPct: 40, widthPct: 60, durationMs: 6000 });
  });
  it("collapsing a span hides its descendants only", () => {
    expect(flatten(tree, new Set(["a"])).map((r) => r.node.span_id)).toEqual(["root", "a", "b"]);
  });
  it("tiny spans stay visible and bars never overflow", () => {
    const rows = flatten([node("r", 0, 100, [node("tiny", 99.99, 100)])]);
    expect(rows[1]!.widthPct).toBeGreaterThanOrEqual(0.8);
    expect(rows.every((r) => r.leftPct + r.widthPct <= 100.0001)).toBe(true);
  });
  it("handles an empty tree and open (unfinished) spans", () => {
    expect(bounds([])).toEqual({ start: 0, end: 0 });
    expect(flatten([])).toEqual([]);
    expect(flatten([node("open", 5, null as unknown as number)])[0]!.durationMs).toBe(0);
  });
  it("finds the plan, counts failures and clips previews", () => {
    const t = [node("o", 0, 1, [node("router", 0, 1, [], { attributes: { plan: { intent: "transit" } } }), node("x", 0, 1, [], { status: "error" })])];
    expect(findPlan(t)).toEqual({ intent: "transit" });
    expect(findPlan([node("n", 0, 1)])).toBeUndefined();
    expect(failedCount(t)).toBe(1);
    expect(clip("abc", 5)).toEqual({ text: "abc", cut: false });
    expect(clip("abcdef", 3)).toEqual({ text: "abc", cut: true });
  });
});
