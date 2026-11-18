/**
 * A tiny, SAFE markdown subset for model answers: paragraphs, "- " bullets, "1. " lists, **bold**.
 * It produces a tree that React renders as text nodes. There is no HTML path at all, so model or
 * calendar text can never inject markup (the answer may quote untrusted event titles).
 */
export type Inline = { t: "text"; v: string } | { t: "b"; v: string };
export type Block =
  | { type: "p"; lines: Inline[][] }
  | { type: "ul"; items: Inline[][] }
  | { type: "ol"; items: Inline[][] };

export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  const re = /\*\*(?=\S)(.+?)(?<=\S)\*\*/g; // content must not start/end with a space (as in markdown)
  let last = 0;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > last) out.push({ t: "text", v: text.slice(last, m.index) });
    out.push({ t: "b", v: m[1] ?? "" });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ t: "text", v: text.slice(last) });
  return out;
}

export function parseMarkdown(src: string): Block[] {
  const blocks: Block[] = [];
  let current: Block | null = null;
  const flush = () => { if (current) blocks.push(current); current = null; };
  for (const raw of src.replace(/\r\n/g, "\n").split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) { flush(); continue; }
    const ul = /^\s*[-*•]\s+(.*)$/.exec(line);
    const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    if (ul) {
      if (!current || current.type !== "ul") { flush(); current = { type: "ul", items: [] }; }
      current.items.push(parseInline(ul[1] ?? ""));
    } else if (ol) {
      if (!current || current.type !== "ol") { flush(); current = { type: "ol", items: [] }; }
      current.items.push(parseInline(ol[1] ?? ""));
    } else {
      if (!current || current.type !== "p") { flush(); current = { type: "p", lines: [] }; }
      current.lines.push(parseInline(line.trim()));
    }
  }
  flush();
  return blocks;
}
