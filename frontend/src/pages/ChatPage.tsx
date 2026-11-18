import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { ApiError, type AgentCard, type AgentName, type Mode } from "../api/types";
import { AgentGraph } from "../components/AgentGraph";
import { Inspector } from "../components/Inspector";
import { TripCard } from "../components/TripCard";
import { initialInspector, inspectorReducer, isLive } from "../lib/inspector";
import { AgentPicker, modeLabel } from "../components/AgentPicker";
import { ApprovalsPanel } from "../components/Approvals";
import { Composer } from "../components/Composer";
import { MessageView } from "../components/Message";
import { chatReducer, initialChat, isBusy, type AssistantMessage } from "../lib/chat";
import { Icon, Markdown } from "../components/ui";
import { parseMarkdown } from "../lib/markdown";
import { useApp } from "../state/AppContext";

const EXAMPLES: Record<Mode, string[]> = {
  auto: [
    "Next 3 buses from home to Chermside",
    "Find me a free 30 minute slot tomorrow afternoon and tell me how to get to Chermside for it.",
    "Should I leave now for Chermside or wait? It looks like rain.",
    "Add 'Dentist' Thursday 2pm at Chermside and warn me if I can't get there on time.",
  ],
  transit: ["Next 3 buses from home to Chermside", "Is the 680 cancelled?", "How do I get from home to Southbank?"],
  weather: ["Is it going to rain this morning?", "What's the weather like right now?"],
  calendar: ["What's on my calendar tomorrow?", "Find me a free 30 minute slot tomorrow afternoon.", "Add 'Dentist' Thursday at 2pm"],
};

let counter = 0;
const uid = (p: string) => `${p}${Date.now().toString(36)}${(counter++).toString(36)}`;
function sessionId(): string {
  try {
    const saved = sessionStorage.getItem("session");
    if (saved) return saved;
    const fresh = uid("s");
    sessionStorage.setItem("session", fresh);
    return fresh;
  } catch {
    return "session";
  }
}

export function ChatPage() {
  const { api, health, approvals, setOutcomeHandler } = useApp();
  const [state, dispatch] = useReducer(chatReducer, initialChat);
  const abort = useRef<AbortController | null>(null);
  const [mode, setMode] = useState<Mode>("auto");
  const [agents, setAgents] = useState<AgentCard[] | null>(null);
  const [inspector, dispatchInsp] = useReducer(inspectorReducer, initialInspector);
  const [reasoning, setReasoning] = useState(false);
  const [tab, setTab] = useState<"result" | "inspector" | "flow">("result");
  const [directIds, setDirectIds] = useState<ReadonlySet<string>>(new Set());
  const endRef = useRef<HTMLDivElement>(null);
  const busy = isBusy(state);

  useEffect(() => { endRef.current?.scrollIntoView?.({ block: "end", behavior: "smooth" }); }, [state]);
  useEffect(() => {
    setOutcomeHandler((text, tone) => dispatch({ type: "note", id: uid("n"), text, tone }));
    return () => { setOutcomeHandler(null); abort.current?.abort(); };
  }, [setOutcomeHandler]);

  useEffect(() => { let live = true; api.agents().then((a) => { if (live) setAgents(a); }).catch(() => undefined); return () => { live = false; }; }, [api]);

  const send = useCallback(async (text: string) => {
    const id = uid("m");
    if (mode !== "auto") setDirectIds((d) => new Set(d).add(id));
    dispatchInsp({ type: "reset" });
    dispatch({ type: "user", id: uid("u"), text });
    dispatch({ type: "assistant_start", id });
    const ctl = new AbortController();
    abort.current = ctl;
    let sawFinal = false;
    try {
      for await (const event of api.chat(sessionId(), text, ctl.signal, mode === "auto" ? null : (mode as AgentName), reasoning)) {
        dispatch({ type: "event", id, event });
        if (isLive(event)) dispatchInsp({ type: "event", event });
        if (event.type === "trip") setTab("result");
        if (event.type === "approval_required") void approvals.refresh();
        if (event.type === "final") sawFinal = true;
      }
      if (!sawFinal) dispatch({ type: "failed", id, message: "The connection ended before an answer arrived." });
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") dispatch({ type: "stopped", id });
      else dispatch({ type: "failed", id, message: e instanceof ApiError ? e.message : "Something went wrong while waiting for the answer." });
    } finally {
      abort.current = null;
      void approvals.refresh();
    }
  }, [api, approvals, mode, reasoning]);

  const demoEx = mode === "auto" ? health?.demo_examples : health?.demo_examples_by_agent?.[mode];
  const examples = state.messages.length === 0 ? (health?.demo ? (demoEx ?? []) : EXAMPLES[mode]) : [];
  const card = mode === "auto" ? null : agents?.find((a) => a.name === mode) ?? null;
  const jump = () => document.getElementById("approvals")?.scrollIntoView({ behavior: "smooth", block: "start" });
  const lastAssistant = [...state.messages].reverse().find((m): m is AssistantMessage => m.kind === "assistant") ?? null;
  const flowDirect = lastAssistant ? directIds.has(lastAssistant.id) : mode !== "auto";
  const trips = lastAssistant?.trips ?? [];
  const TABS = [["result", "Result"], ["inspector", `Inspector${inspector.entries.length ? ` (${inspector.entries.length})` : ""}`], ["flow", "Flow"]] as const;

  return (
    <div className="flex min-h-full flex-col lg:grid lg:h-full lg:min-h-0 lg:grid-cols-[minmax(22rem,34%)_minmax(0,1fr)]">
      <section aria-label="Conversation" className="flex min-h-[75dvh] min-w-0 flex-1 flex-col lg:min-h-0 lg:border-r lg:border-line">
        <div className="space-y-3 border-b border-line bg-surface px-4 py-3">
          <div className="flex items-center gap-3">
            <div><h1 className="page-title">Assistant</h1><p className="text-[12.5px] text-muted">Ask about buses, trains, weather and your calendar</p></div>
            <label className="ml-auto flex shrink-0 cursor-pointer items-center gap-2 whitespace-nowrap rounded-full border border-line bg-surface px-3 py-1.5 text-[12.5px] font-semibold text-muted hover:text-ink" title="Let the model reason before answering (slower). Its reasoning streams into the Inspector.">
              <input type="checkbox" checked={reasoning} onChange={(e) => setReasoning(e.target.checked)} className="size-4 accent-[var(--accent)]" />Reasoning
            </label>
          </div>
          <AgentPicker mode={mode} onChange={setMode} agents={agents} disabled={busy} />
        </div>
        <div role="log" aria-live="polite" aria-relevant="additions text" className="flex-1 space-y-4 px-4 py-4 lg:min-h-0 lg:overflow-y-auto">
          {state.messages.length === 0 ? (
            <div className="flex h-full flex-col justify-center gap-4">
              <div>
                <div className="grad-violet mb-3 grid size-12 place-items-center rounded-2xl shadow-lg"><Icon.Spark /></div>
                <h2 className="text-xl font-extrabold">{mode === "auto" ? "Where are you headed?" : `Ask the ${modeLabel(mode)} agent`}</h2>
                <p className="mt-1 text-[13.5px] text-muted">
                  {mode === "auto"
                    ? "Plans trips by bus and train with a map, checks the weather for the journey, and finds time in your calendar. Calendar changes are only proposed: you approve each one."
                    : "You are talking to this specialist directly (the router is skipped). It can only use the tools listed under Agents."}
                </p>
                {health?.demo && <p className="mt-2 rounded-xl bg-warn-bg px-3 py-2 text-[12.5px] text-warn">Demo mode: answers are recorded from the real system (frozen data, fake calendar). Pick a prompt.</p>}
                {!health && <p className="mt-2 text-sm text-muted">Connecting…</p>}
              </div>
              <div role="group" aria-label="Example questions" className="grid gap-2">
                {examples.map((q) => (
                  <button key={q} type="button" disabled={busy} onClick={() => void send(q)} className="flex items-start gap-3 rounded-2xl border border-line bg-surface px-3.5 py-3 text-left text-[13.5px] shadow-sm transition hover:-translate-y-px hover:border-accent hover:shadow-md disabled:opacity-50">
                    <span className={"mt-0.5 grid size-8 shrink-0 place-items-center rounded-xl " + (/rain|weather/i.test(q) ? "bg-weather-bg text-weather" : /calendar|slot|add /i.test(q) ? "bg-calendar-bg text-calendar" : "bg-transit-bg text-transit")}>{/rain|weather/i.test(q) ? <Icon.Cloud /> : /calendar|slot|add /i.test(q) ? <Icon.Calendar /> : <Icon.Bus />}</span><span>{q}</span>
                  </button>
                ))}
              </div>
            </div>
          ) : state.messages.map((m) => <MessageView key={m.id} m={m} onJumpToApprovals={jump} />)}
          <div ref={endRef} />
        </div>
        <div className="sticky bottom-0 border-t border-line bg-bg/95 px-4 pb-3 pt-3 lg:static">
          <Composer busy={busy} onSend={(t) => void send(t)} onStop={() => abort.current?.abort()} examples={[]} placeholder={mode === "auto" ? "Ask about your commute…" : `Ask the ${modeLabel(mode)} agent…`} />
        </div>
      </section>

      <aside aria-label="Results" className="order-first min-h-0 border-b border-line lg:order-none lg:overflow-y-auto lg:border-b-0">
        <div className="space-y-3 p-3">
          <ApprovalsPanel />
          <div className="hidden lg:block">
            <div role="tablist" aria-label="Results view" className="inline-flex gap-1 rounded-full bg-surface2 p-1">
              {TABS.map(([k, l]) => (
                <button key={k} role="tab" type="button" aria-selected={tab === k} onClick={() => setTab(k)} data-testid={`tab-${k}`}
                  className={"rounded-full px-4 py-1.5 text-[13px] font-bold transition " + (tab === k ? "bg-surface text-ink shadow" : "text-muted hover:text-ink")}>{l}</button>
              ))}
            </div>
            <div className="pt-3">
              {tab === "result" && (trips.length > 0 ? <div className="space-y-4">{trips.map((t, i) => <TripCard key={i} art={t} />)}</div> : lastAssistant?.text ? (
                <div className="space-y-3" data-testid="result-answer">
                  <p className="label">Answer</p>
                  <div className="rounded-2xl border border-line bg-surface p-4"><Markdown blocks={parseMarkdown(lastAssistant.text)} /></div>
                  <p className="text-[13px] text-muted">A map appears when the question involves getting somewhere (a destination), for example "How do I get from home to Chermside?".</p>
                </div>
              ) : (
                <div className="rounded-2xl border border-dashed border-line p-8 text-center">
                  <p className="text-[15px] font-bold">Your trip appears here</p>
                  <p className="mx-auto mt-1 max-w-md text-[13.5px] text-muted">Ask for a journey (for example "Next 3 buses from home to Chermside") and this panel shows the route on a map with distances, every leg, and live delay status.</p>
                  {card && <p className="mt-3 text-[12.5px] text-muted">Data source: {card.source}</p>}
                </div>
              ))}
              {tab === "inspector" && <Inspector entries={inspector.entries} streaming={busy} />}
              {tab === "flow" && <div className="mx-auto max-w-sm"><AgentGraph steps={lastAssistant?.steps ?? []} direct={flowDirect} streaming={lastAssistant?.status === "streaming"} planned={lastAssistant?.plan != null} finished={lastAssistant != null && lastAssistant.status !== "streaming" && lastAssistant.text !== ""} /></div>}
            </div>
          </div>
        </div>
      </aside>
    </div>
  );
}
