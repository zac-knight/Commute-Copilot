import { describe, expect, it } from "vitest";
import type { StepView } from "./chat";
import { depths, layoutGraph } from "./graph";

const step = (id: string, agent: StepView["agent"], status: StepView["status"], dependsOn: string[] = []): StepView =>
  ({ id, agent, task: "t", dependsOn, status, text: "", error: null, toolCalls: [], durationMs: 0 });
const base = { direct: false, streaming: true, planned: true, finished: false };

describe("depths", () => {
  it("is 0 without dependencies and grows along a chain", () => {
    const d = depths([step("s1", "calendar", "ok"), step("s2", "transit", "ok", ["s1"]), step("s3", "weather", "ok", ["s2"])]);
    expect([d.get("s1"), d.get("s2"), d.get("s3")]).toEqual([0, 1, 2]);
  });
  it("survives a (rejected-by-planner) cycle instead of looping", () => {
    expect(() => depths([step("a", "transit", "ok", ["b"]), step("b", "weather", "ok", ["a"])])).not.toThrow();
  });
});

describe("layoutGraph", () => {
  it("lays out user, router, one column per dependency layer, then the synthesiser", () => {
    const g = layoutGraph({ ...base, steps: [step("s1", "calendar", "ok"), step("s2", "transit", "running", ["s1"])] });
    const x = (id: string) => g.nodes.find((n) => n.id === id)!.x;
    expect(x("user")).toBeLessThan(x("router"));
    expect(x("router")).toBeLessThan(x("s1"));
    expect(x("s1")).toBeLessThan(x("s2"));
    expect(x("s2")).toBeLessThan(x("synth"));
  });
  it("puts independent steps in the same column on different rows", () => {
    const g = layoutGraph({ ...base, steps: [step("s1", "transit", "running"), step("s2", "weather", "running")] });
    const [a, b] = [g.nodes.find((n) => n.id === "s1")!, g.nodes.find((n) => n.id === "s2")!];
    expect(a.x).toBe(b.x);
    expect(a.y).not.toBe(b.y);
  });
  it("connects router to roots, dependencies to dependents, and leaves to the synthesiser", () => {
    const g = layoutGraph({ ...base, steps: [step("s1", "calendar", "ok"), step("s2", "transit", "ok", ["s1"])] });
    const pairs = g.edges.map((e) => `${e.from}>${e.to}`).sort();
    expect(pairs).toEqual(["router>s1", "s1>s2", "s2>synth", "user>router"]);
  });
  it("marks the edge into a running node active and a finished path done", () => {
    const g = layoutGraph({ ...base, steps: [step("s1", "calendar", "ok"), step("s2", "transit", "running", ["s1"])] });
    expect(g.edges.find((e) => e.to === "s2")!.active).toBe(true);
    expect(g.edges.find((e) => e.to === "s1")!.done).toBe(true);
  });
  it("shows the router as skipped in direct mode", () => {
    const g = layoutGraph({ ...base, direct: true, steps: [step("s1", "weather", "running")] });
    expect(g.nodes.find((n) => n.id === "router")).toMatchObject({ status: "skipped", sub: "skipped (you chose)" });
  });
  it("starts the synthesiser only when every step has finished, and completes it on the final answer", () => {
    const pending = layoutGraph({ ...base, steps: [step("s1", "transit", "running")] });
    expect(pending.nodes.find((n) => n.id === "synth")!.status).toBe("idle");
    const allDone = layoutGraph({ ...base, steps: [step("s1", "transit", "ok")] });
    expect(allDone.nodes.find((n) => n.id === "synth")!.status).toBe("running");
    const fin = layoutGraph({ ...base, streaming: false, finished: true, steps: [step("s1", "transit", "ok")] });
    expect(fin.nodes.find((n) => n.id === "synth")!.status).toBe("ok");
  });
  it("is just user -> router -> synth for a clarification (no steps)", () => {
    const g = layoutGraph({ ...base, streaming: false, finished: true, steps: [] });
    expect(g.edges.map((e) => `${e.from}>${e.to}`).sort()).toEqual(["router>synth", "user>router"]);
  });
  it("has a positive size that grows with the number of layers", () => {
    const one = layoutGraph({ ...base, steps: [step("s1", "transit", "ok")] });
    const two = layoutGraph({ ...base, steps: [step("s1", "transit", "ok"), step("s2", "weather", "ok", ["s1"])] });
    expect(two.width).toBeGreaterThan(one.width);
    expect(one.height).toBeGreaterThan(0);
  });
});

describe("vertical layout (side panel)", () => {
  it("stacks layers top to bottom and keeps parallel steps side by side", () => {
    const g = layoutGraph({ ...base, vertical: true, steps: [step("s1", "transit", "ok"), step("s2", "weather", "ok"), step("s3", "calendar", "ok", ["s1"])] });
    const n = (id: string) => g.nodes.find((x) => x.id === id)!;
    expect(n("user").y).toBeLessThan(n("router").y);
    expect(n("router").y).toBeLessThan(n("s1").y);
    expect(n("s1").y).toBe(n("s2").y);
    expect(n("s1").x).not.toBe(n("s2").x);
    expect(n("s3").y).toBeGreaterThan(n("s1").y);
    expect(n("s3").y).toBeLessThan(n("synth").y);
    expect(g.vertical).toBe(true);
  });
  it("is narrow enough for a 21rem panel with two parallel agents", () => {
    const g = layoutGraph({ ...base, vertical: true, steps: [step("s1", "transit", "ok"), step("s2", "weather", "ok")] });
    expect(g.width).toBeLessThanOrEqual(336);
  });
});
