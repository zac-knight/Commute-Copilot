import { describe, expect, it } from "vitest";
import type { ChatEvent, Plan, PendingAction } from "../api/types";
import { chatReducer, initialChat, isBusy, planLabel, type AssistantMessage, type ChatAction, type ChatState } from "./chat";

const plan: Plan = {
  intent: "multi_agent", clarification_question: null, repairs: [],
  steps: [
    { id: "s1", agent: "calendar", task: "Find a slot", depends_on: [] },
    { id: "s2", agent: "transit", task: "Trip arriving by {s1.start-15m}", depends_on: ["s1"] },
  ],
};
const action = { id: "a1", summary: "Create 'Dentist'", status: "pending" } as PendingAction;

function run(...events: ChatEvent[]): AssistantMessage {
  let s: ChatState = chatReducer(initialChat, { type: "assistant_start", id: "m1" });
  for (const event of events) s = chatReducer(s, { type: "event", id: "m1", event });
  return s.messages[0] as AssistantMessage;
}

describe("chatReducer", () => {
  it("adds user and assistant messages in order", () => {
    let s = chatReducer(initialChat, { type: "user", id: "u1", text: "hi" });
    s = chatReducer(s, { type: "assistant_start", id: "m1" });
    expect(s.messages.map((m) => m.kind)).toEqual(["user", "assistant"]);
    expect(isBusy(s)).toBe(true);
  });

  it("builds step views from the plan and walks them through running -> ok", () => {
    const m = run(
      { type: "plan", request_id: "r1", plan },
      { type: "step_started", id: "s1", agent: "calendar", task: "Find a slot" },
    );
    expect(m.steps.map((s) => [s.id, s.status])).toEqual([["s1", "running"], ["s2", "pending"]]);
    expect(m.steps[1]?.dependsOn).toEqual(["s1"]);
    const done = run(
      { type: "plan", request_id: "r1", plan },
      { type: "step_started", id: "s1", agent: "calendar", task: "Find a slot" },
      { type: "step_result", id: "s1", agent: "calendar", status: "ok", text: "found", error: null, tool_calls: ["find_free_slots"], duration_ms: 1200 },
    );
    expect(done.steps[0]).toMatchObject({ status: "ok", toolCalls: ["find_free_slots"], durationMs: 1200, text: "found" });
  });

  it("shows the RESOLVED task when a step starts (placeholders filled in by the server)", () => {
    const m = run(
      { type: "plan", request_id: "r1", plan },
      { type: "step_started", id: "s2", agent: "transit", task: "Trip arriving by 2026-11-19T13:55+10:00" },
    );
    expect(m.steps[1]?.task).toBe("Trip arriving by 2026-11-19T13:55+10:00");
  });

  it("records approval cards once, even if the event repeats", () => {
    const m = run({ type: "approval_required", action }, { type: "approval_required", action });
    expect(m.pendingApprovals).toEqual(["a1"]);
  });

  it("final fills in the answer, totals and request id, and closes unfinished steps", () => {
    const m = run(
      { type: "plan", request_id: "r1", plan },
      { type: "final", request_id: "r1", status: "partial", text: "Partly done", pending_approvals: ["a9"], tokens_in: 5000, tokens_out: 300, duration_ms: 42000 },
    );
    expect(m).toMatchObject({ status: "partial", text: "Partly done", requestId: "r1", tokensIn: 5000, durationMs: 42000, pendingApprovals: ["a9"] });
    expect(m.steps.map((s) => s.status)).toEqual(["skipped", "skipped"]);
  });

  it("a clarification has no steps", () => {
    const m = run({ type: "final", request_id: "r2", status: "clarify", text: "Where to?", pending_approvals: [], tokens_in: 1, tokens_out: 1, duration_ms: 1 });
    expect(m.status).toBe("clarify");
    expect(m.steps).toEqual([]);
  });

  it("stopped only affects a streaming message; failed records the error", () => {
    let s: ChatState = chatReducer(initialChat, { type: "assistant_start", id: "m1" });
    s = chatReducer(s, { type: "stopped", id: "m1" });
    expect((s.messages[0] as AssistantMessage).status).toBe("stopped");
    expect(isBusy(s)).toBe(false);
    const done = run({ type: "final", request_id: "r", status: "ok", text: "x", pending_approvals: [], tokens_in: 0, tokens_out: 0, duration_ms: 0 });
    const kept = chatReducer({ messages: [done] }, { type: "stopped", id: "m1" } as ChatAction);
    expect((kept.messages[0] as AssistantMessage).status).toBe("ok");
    const failed = chatReducer(s, { type: "failed", id: "m1", message: "network down" });
    expect((failed.messages[0] as AssistantMessage).error).toBe("network down");
    expect((failed.messages[0] as AssistantMessage).status).toBe("error");
  });

  it("events for another message id are ignored, and the reducer does not mutate its input", () => {
    const start = chatReducer(initialChat, { type: "assistant_start", id: "m1" });
    const frozen = JSON.stringify(start);
    const next = chatReducer(start, { type: "event", id: "other", event: { type: "error", request_id: null, message: "x" } });
    expect(JSON.stringify(next)).toBe(frozen);
    chatReducer(start, { type: "event", id: "m1", event: { type: "plan", request_id: "r", plan } });
    expect(JSON.stringify(start)).toBe(frozen);
  });

  it("notes and clear", () => {
    let s = chatReducer(initialChat, { type: "note", id: "n1", text: "Approved", tone: "ok" });
    expect(s.messages[0]).toMatchObject({ kind: "note", tone: "ok" });
    s = chatReducer(s, { type: "clear" });
    expect(s.messages).toEqual([]);
  });
});

describe("planLabel", () => {
  it("orders by dependency depth", () => {
    expect(planLabel(plan)).toBe("Calendar → Transit");
  });
  it("joins parallel steps with +", () => {
    const p: Plan = { ...plan, steps: [
      { id: "s1", agent: "calendar", task: "a task", depends_on: [] },
      { id: "s2", agent: "transit", task: "a task", depends_on: [] },
    ] };
    expect(planLabel(p)).toBe("Calendar + Transit");
  });
  it("is empty for no plan or no steps", () => {
    expect(planLabel(null)).toBe("");
    expect(planLabel({ ...plan, steps: [] })).toBe("");
  });
  it("survives a malformed dependency cycle without looping", () => {
    const p: Plan = { ...plan, steps: [
      { id: "s1", agent: "calendar", task: "a task", depends_on: ["s2"] },
      { id: "s2", agent: "transit", task: "a task", depends_on: ["s1"] },
    ] };
    expect(() => planLabel(p)).not.toThrow();
  });
});
