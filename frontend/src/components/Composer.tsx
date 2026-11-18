import { useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Button, Icon } from "./ui";

export function Composer({ busy, onSend, onStop, examples, placeholder = "Ask about your commute…" }: { busy: boolean; onSend: (text: string) => void; onStop: () => void; examples: string[]; placeholder?: string }) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    const t = text.trim();
    if (!t || busy) return;
    onSend(t);
    setText("");
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) submit(e);
  };
  return (
    <form onSubmit={submit} className="space-y-2" aria-label="Ask a question">
      {examples.length > 0 && (
        <div className="flex flex-wrap gap-2" role="group" aria-label="Example questions">
          {examples.map((q) => (
            <button key={q} type="button" disabled={busy} onClick={() => onSend(q)}
              className="max-w-full rounded-full border border-line bg-surface px-3 py-1.5 text-left text-xs text-ink transition hover:border-accent disabled:opacity-50">{q}</button>
          ))}
        </div>
      )}
      <div className="flex items-end gap-2 rounded-2xl border border-line bg-surface p-1.5 shadow-md transition focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/25">
        <label htmlFor="ask" className="sr-only">Ask about buses, trains, weather or your calendar</label>
        <textarea
          id="ask" ref={ref} value={text} rows={1} maxLength={2000} onChange={(e) => setText(e.target.value)} onKeyDown={onKey}
          placeholder={placeholder}
          className="max-h-40 min-h-10 flex-1 resize-none bg-transparent px-2 py-2 text-base outline-none placeholder:text-muted"
        />
        {busy ? (
          <Button variant="secondary" onClick={onStop} aria-label="Stop"><Icon.Stop />Stop</Button>
        ) : (
          <Button variant="primary" type="submit" disabled={!text.trim()} aria-label="Send"><Icon.Send />Send</Button>
        )}
      </div>
      <p className="px-1 text-[12px] text-muted">Enter to send · Shift+Enter for a new line · runs on your own machine, so answers can take a minute</p>
    </form>
  );
}
