/** What the model is processing, folded from the live stream (pure reducer, unit-tested). */
import type { ChatEvent, LiveEvent } from "../api/types";

export interface LlmEntry {
  kind: "llm"; id: string; step: string; agent: string; n: number; model: string; think: boolean | null;
  promptVersion: string; messages: number; tools: string[]; text: string; thinking: string;
  usage?: { tokensIn: number; tokensOut: number; ms: number; thinkingChars: number };
  done: boolean;
}
export interface ToolEntry {
  kind: "tool"; id: string; step: string; agent: string; name: string; input: Record<string, unknown>;
  output?: string; isError?: boolean; ms?: number;
}
export type InspectorEntry = LlmEntry | ToolEntry;
export interface InspectorState { entries: InspectorEntry[] }
export const initialInspector: InspectorState = { entries: [] };

export type InspectorAction = { type: "reset" } | { type: "event"; event: ChatEvent };

const LIVE = new Set(["llm_started", "thought", "token", "tool_call", "tool_result", "llm_usage"]);
export const isLive = (e: ChatEvent): e is LiveEvent => LIVE.has(e.type);

const llmId = (step: string, n: number) => `${step}#${n}`;

function patchLlm(s: InspectorState, step: string, fn: (e: LlmEntry) => LlmEntry): InspectorState {
  // deltas belong to the most recent LLM call of that step
  for (let i = s.entries.length - 1; i >= 0; i--) {
    const e = s.entries[i]!;
    if (e.kind === "llm" && e.step === step) {
      const entries = s.entries.slice();
      entries[i] = fn(e);
      return { entries };
    }
  }
  return s;
}

export function inspectorReducer(s: InspectorState, a: InspectorAction): InspectorState {
  if (a.type === "reset") return initialInspector;
  const e = a.event;
  switch (e.type) {
    case "llm_started":
      return { entries: [...s.entries, { kind: "llm", id: llmId(e.step, e.n), step: e.step, agent: e.agent, n: e.n, model: e.model, think: e.think, promptVersion: e.prompt_version, messages: e.messages, tools: e.tools, text: "", thinking: "", done: false }] };
    case "thought":
      return patchLlm(s, e.step, (x) => ({ ...x, thinking: x.thinking + e.delta }));
    case "token":
      return patchLlm(s, e.step, (x) => ({ ...x, text: x.text + e.delta }));
    case "llm_usage":
      return patchLlm(s, e.step, (x) => ({ ...x, done: true, usage: { tokensIn: e.tokens_in, tokensOut: e.tokens_out, ms: e.duration_ms, thinkingChars: e.thinking_chars } }));
    case "tool_call":
      return { entries: [...s.entries, { kind: "tool", id: e.call_id || `${e.step}:${e.name}:${s.entries.length}`, step: e.step, agent: e.agent, name: e.name, input: e.input }] };
    case "tool_result":
      return { entries: s.entries.map((x) => (x.kind === "tool" && x.id === e.call_id && x.output === undefined ? { ...x, output: e.output, isError: e.is_error, ms: e.duration_ms } : x)) };
    default:
      return s;
  }
}

/** Output tokens per second for a finished call (0 when unknown). */
export const tokensPerSecond = (u: NonNullable<LlmEntry["usage"]>) => (u.ms > 0 ? (u.tokensOut / u.ms) * 1000 : 0);

/** Pretty-print JSON when the text is JSON (the router's plan streams as JSON), else return it. */
export function prettyMaybeJson(text: string): string {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
}
