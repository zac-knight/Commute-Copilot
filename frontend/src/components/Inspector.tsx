import { useEffect, useRef } from "react";
import { prettyMaybeJson, tokensPerSecond, type InspectorEntry } from "../lib/inspector";

const name = (agent: string) => (agent === "router" ? "Router" : agent === "synthesiser" ? "Synthesiser" : agent ? agent[0]!.toUpperCase() + agent.slice(1) : "");

function Llm({ e }: { e: Extract<InspectorEntry, { kind: "llm" }> }) {
  return (
    <div className="border border-line bg-surface/60">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-line px-3 py-1.5">
        <span className="mono text-[12px] font-bold text-accent">{name(e.agent)} · model call {e.n}</span>
        <span className="mono text-[12px] text-muted">{e.model} · prompt {e.promptVersion || "n/a"} · {e.messages} msg · reasoning {e.think ? "on" : "off"}</span>
        {!e.done && <span className="mono ml-auto text-[12px] text-warn">generating…</span>}
        {e.usage && <span className="mono ml-auto text-[12px] text-muted">{e.usage.tokensIn} in · {e.usage.tokensOut} out · {(e.usage.ms / 1000).toFixed(1)} s · {tokensPerSecond(e.usage).toFixed(1)} tok/s</span>}
      </div>
      {e.tools.length > 0 && <p className="mono border-b border-line px-3 py-1 text-[12px] text-muted">tools offered: {e.tools.join(", ")}</p>}
      {e.thinking && (
        <details open={!e.done} className="border-b border-line px-3 py-2">
          <summary className="label cursor-pointer">reasoning ({e.thinking.length} chars)</summary>
          <p className="mt-1 whitespace-pre-wrap text-[12.5px] italic leading-relaxed text-muted">{e.thinking}</p>
        </details>
      )}
      {e.text && (
        <div className="px-3 py-2">
          <p className="label mb-1">{e.agent === "router" ? "plan (json)" : "output"}</p>
          <pre className={"mono whitespace-pre-wrap break-words text-[12px] leading-relaxed " + (e.done ? "" : "cursor")}>{e.agent === "router" && e.done ? prettyMaybeJson(e.text) : e.text}</pre>
        </div>
      )}
      {!e.thinking && !e.text && e.done && <p className="px-3 py-2 text-[12px] text-muted">No text: the model answered with tool calls only.</p>}
    </div>
  );
}

function Tool({ e }: { e: Extract<InspectorEntry, { kind: "tool" }> }) {
  const pending = e.output === undefined;
  return (
    <div className={"border bg-surface/60 " + (e.isError ? "border-bad" : "border-line")}>
      <div className="flex flex-wrap items-center gap-2 px-3 py-1.5">
        <span className="mono text-[12px] font-bold text-ok">⚙ {e.name}</span>
        <span className="mono text-[12px] text-muted">{name(e.agent)}</span>
        <span className={"mono ml-auto text-[12px] " + (e.isError ? "text-bad" : pending ? "text-warn" : "text-muted")}>{pending ? "running…" : `${e.isError ? "error · " : ""}${e.ms} ms`}</span>
      </div>
      <pre className="mono max-h-40 overflow-auto whitespace-pre-wrap break-words border-t border-line px-3 py-1.5 text-[12px] text-muted">{JSON.stringify(e.input, null, 2)}</pre>
      {e.output !== undefined && (
        <details className="border-t border-line px-3 py-1.5">
          <summary className="label cursor-pointer">result ({e.output.length.toLocaleString()} chars)</summary>
          <pre className="mono mt-1 max-h-72 overflow-auto whitespace-pre-wrap break-words text-[12px]">{prettyMaybeJson(e.output)}</pre>
        </details>
      )}
    </div>
  );
}

/** The live processing timeline: prompts, reasoning, tool calls with arguments, raw results, usage. */
export function Inspector({ entries, streaming }: { entries: InspectorEntry[]; streaming: boolean }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { if (streaming) end.current?.scrollIntoView?.({ block: "end" }); }, [entries, streaming]);
  if (entries.length === 0) {
    return <p className="border border-dashed border-line p-4 text-sm text-muted">Nothing yet. Ask something and each model call appears here as it runs: the prompt context, any reasoning, every tool call with its arguments, and the raw result. Turn on <b>Reasoning</b> in the header to see the model think first.</p>;
  }
  return (
    <div className="space-y-2" data-testid="inspector">
      {entries.map((e) => (e.kind === "llm" ? <Llm key={e.id + e.step} e={e} /> : <Tool key={e.id} e={e} />))}
      <div ref={end} />
    </div>
  );
}
