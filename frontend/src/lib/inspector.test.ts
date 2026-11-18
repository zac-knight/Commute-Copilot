import { describe, expect, it } from "vitest";
import type { LiveEvent } from "../api/types";
import { initialInspector, inspectorReducer, isLive, prettyMaybeJson, tokensPerSecond, type InspectorState, type LlmEntry, type ToolEntry } from "./inspector";

const run = (events: LiveEvent[]): InspectorState => events.reduce((s, event) => inspectorReducer(s, { type: "event", event }), initialInspector);
const started = (step: string, n = 1): LiveEvent => ({ type: "llm_started", step, agent: "transit", n, model: "qwen3:8b", think: false, prompt_version: "transit-v1", messages: 1, tools: ["plan_trip"] });

describe("inspectorReducer", () => {
  it("builds one LLM entry and streams reasoning and answer into it", () => {
    const s = run([started("s1"), { type: "thought", step: "s1", agent: "transit", delta: "Need " }, { type: "thought", step: "s1", agent: "transit", delta: "a trip." }, { type: "token", step: "s1", agent: "transit", phase: "agent", delta: "Bus " }, { type: "token", step: "s1", agent: "transit", phase: "agent", delta: "at 8." }]);
    const e = s.entries[0] as LlmEntry;
    expect(s.entries).toHaveLength(1);
    expect(e.thinking).toBe("Need a trip.");
    expect(e.text).toBe("Bus at 8.");
    expect(e.done).toBe(false);
  });
  it("routes deltas to the right step when agents run in parallel", () => {
    const s = run([started("s1"), started("s2"), { type: "token", step: "s1", agent: "transit", phase: "agent", delta: "A" }, { type: "token", step: "s2", agent: "weather", phase: "agent", delta: "B" }]);
    const by = (id: string) => (s.entries.find((e) => e.kind === "llm" && e.step === id) as LlmEntry).text;
    expect([by("s1"), by("s2")]).toEqual(["A", "B"]);
  });
  it("attaches usage and marks the call done", () => {
    const s = run([started("s1"), { type: "llm_usage", step: "s1", agent: "transit", n: 1, model: "m", tokens_in: 100, tokens_out: 50, duration_ms: 2000, thinking_chars: 0 }]);
    const e = s.entries[0] as LlmEntry;
    expect(e.done).toBe(true);
    expect(tokensPerSecond(e.usage!)).toBe(25);
  });
  it("pairs a tool result with its call, once", () => {
    const s = run([
      { type: "tool_call", step: "s1", agent: "transit", call_id: "c1", name: "plan_trip", input: { origin: "home" } },
      { type: "tool_call", step: "s1", agent: "transit", call_id: "c2", name: "find_stops", input: {} },
      { type: "tool_result", step: "s1", agent: "transit", call_id: "c2", name: "find_stops", output: "[]", is_error: false, duration_ms: 3 },
    ]);
    const [a, b] = s.entries as ToolEntry[];
    expect(a!.output).toBeUndefined();
    expect(b).toMatchObject({ output: "[]", ms: 3, isError: false });
  });
  it("shows a failed tool as an error", () => {
    const s = run([{ type: "tool_call", step: "s1", agent: "weather", call_id: "c", name: "get_current", input: {} }, { type: "tool_result", step: "s1", agent: "weather", call_id: "c", name: "get_current", output: "{\"error\":1}", is_error: true, duration_ms: 9000 }]);
    expect((s.entries[0] as ToolEntry).isError).toBe(true);
  });
  it("ignores a delta that arrives before any LLM call, and resets", () => {
    expect(run([{ type: "token", step: "s9", agent: "x", phase: "agent", delta: "?" }]).entries).toEqual([]);
    expect(inspectorReducer(run([started("s1")]), { type: "reset" })).toEqual(initialInspector);
  });
  it("classifies live events", () => {
    expect(isLive({ type: "token", step: "", agent: "", phase: "", delta: "x" })).toBe(true);
    expect(isLive({ type: "error", request_id: null, message: "x" })).toBe(false);
  });
  it("pretty-prints JSON and leaves prose alone", () => {
    expect(prettyMaybeJson('{"a":1}')).toBe('{\n  "a": 1\n}');
    expect(prettyMaybeJson("plain text")).toBe("plain text");
  });
});
