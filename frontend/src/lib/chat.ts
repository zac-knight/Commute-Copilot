/** Chat state: a pure reducer that folds streamed backend events into messages. */
import type { AgentName, ChatEvent, FinalStatus, Plan, StepStatus, TripArtifact } from "../api/types";

export interface StepView {
  id: string;
  agent: AgentName;
  task: string;
  dependsOn: string[];
  status: StepStatus;
  text: string;
  error: string | null;
  toolCalls: string[];
  durationMs: number;
}

export interface UserMessage { kind: "user"; id: string; text: string }
export interface NoteMessage { kind: "note"; id: string; text: string; tone: "ok" | "info" | "bad" }
export interface AssistantMessage {
  kind: "assistant";
  id: string;
  plan: Plan | null;
  steps: StepView[];
  text: string;
  status: "streaming" | FinalStatus | "error" | "stopped";
  requestId: string | null;
  tokensIn: number;
  tokensOut: number;
  durationMs: number;
  pendingApprovals: string[];
  error: string | null;
  /** Answer text as the synthesiser writes it, before the final (checked) text replaces it. */
  liveText: string;
  /** Map-ready trips built from plan_trip results. */
  trips: TripArtifact[];
}
export type ChatMessage = UserMessage | NoteMessage | AssistantMessage;
export interface ChatState { messages: ChatMessage[] }

export type ChatAction =
  | { type: "user"; id: string; text: string }
  | { type: "assistant_start"; id: string }
  | { type: "event"; id: string; event: ChatEvent }
  | { type: "stopped"; id: string }
  | { type: "failed"; id: string; message: string }
  | { type: "note"; id: string; text: string; tone: NoteMessage["tone"] }
  | { type: "clear" };

export const initialChat: ChatState = { messages: [] };

const blankAssistant = (id: string): AssistantMessage => ({
  kind: "assistant", id, plan: null, steps: [], text: "", status: "streaming", requestId: null,
  tokensIn: 0, tokensOut: 0, durationMs: 0, pendingApprovals: [], error: null, liveText: "", trips: [],
});

function updateAssistant(state: ChatState, id: string, fn: (m: AssistantMessage) => AssistantMessage): ChatState {
  return { messages: state.messages.map((m) => (m.kind === "assistant" && m.id === id ? fn(m) : m)) };
}

function patchStep(steps: StepView[], id: string, patch: Partial<StepView>): StepView[] {
  return steps.map((s) => (s.id === id ? { ...s, ...patch } : s));
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case "user":
      return { messages: [...state.messages, { kind: "user", id: action.id, text: action.text }] };
    case "assistant_start":
      return { messages: [...state.messages, blankAssistant(action.id)] };
    case "note":
      return { messages: [...state.messages, { kind: "note", id: action.id, text: action.text, tone: action.tone }] };
    case "clear":
      return initialChat;
    case "stopped":
      return updateAssistant(state, action.id, (m) => (m.status === "streaming" ? { ...m, status: "stopped" } : m));
    case "failed":
      return updateAssistant(state, action.id, (m) => ({ ...m, status: "error", error: action.message }));
    case "event":
      return updateAssistant(state, action.id, (m) => applyEvent(m, action.event));
  }
}

function applyEvent(m: AssistantMessage, e: ChatEvent): AssistantMessage {
  switch (e.type) {
    case "plan":
      return {
        ...m, plan: e.plan, requestId: e.request_id,
        steps: e.plan.steps.map((s) => ({
          id: s.id, agent: s.agent, task: s.task, dependsOn: s.depends_on, status: "pending", text: "", error: null, toolCalls: [], durationMs: 0,
        })),
      };
    case "step_started":
      // the task shown is the RESOLVED one (placeholders like {s1.start-15m} are filled in by the server)
      return { ...m, steps: patchStep(m.steps, e.id, { status: "running", task: e.task }) };
    case "step_result":
      return { ...m, steps: patchStep(m.steps, e.id, { status: e.status, text: e.text, error: e.error, toolCalls: e.tool_calls, durationMs: e.duration_ms }) };
    case "approval_required":
      return m.pendingApprovals.includes(e.action.id) ? m : { ...m, pendingApprovals: [...m.pendingApprovals, e.action.id] };
    case "final":
      return {
        ...m, text: e.text, liveText: "", status: e.status, requestId: e.request_id || m.requestId,
        tokensIn: e.tokens_in, tokensOut: e.tokens_out, durationMs: e.duration_ms,
        pendingApprovals: e.pending_approvals.length ? e.pending_approvals : m.pendingApprovals,
        // any step still shown as running/pending when the request ends did not finish
        steps: m.steps.map((s) => (s.status === "running" || s.status === "pending" ? { ...s, status: "skipped" } : s)),
      };
    case "error":
      return { ...m, error: e.message };
    case "trip":
      return { ...m, trips: [...m.trips, e.artifact] };
    case "token":
      return e.phase === "synth" ? { ...m, liveText: m.liveText + e.delta } : m;
    default:
      return m; // llm_started / thought / tool_call / tool_result / llm_usage feed the Inspector instead
  }
}

/** Short route summary for the plan header, e.g. "Calendar → Transit". */
export function planLabel(plan: Plan | null): string {
  if (!plan || plan.steps.length === 0) return "";
  const names: Record<AgentName, string> = { transit: "Transit", weather: "Weather", calendar: "Calendar" };
  const by = new Map(plan.steps.map((s) => [s.id, s]));
  // order by dependency depth so the label reads left to right
  const depth = (id: string, seen = new Set<string>()): number => {
    const step = by.get(id);
    if (!step || seen.has(id)) return 0;
    seen.add(id);
    return step.depends_on.length ? 1 + Math.max(...step.depends_on.map((d) => depth(d, seen))) : 0;
  };
  const layers: string[][] = [];
  for (const s of plan.steps) (layers[depth(s.id)] ??= []).push(names[s.agent]);
  return layers.filter(Boolean).map((l) => [...new Set(l)].join(" + ")).join(" → ");
}

export function isBusy(state: ChatState): boolean {
  return state.messages.some((m) => m.kind === "assistant" && m.status === "streaming");
}
