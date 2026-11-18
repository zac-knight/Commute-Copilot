/** Server-Sent Events parsing: pure, chunk-boundary safe. */

export interface SseFrame { event: string; data: string }

/** Split ``buffer`` into complete frames plus the unfinished remainder. */
export function parseSse(buffer: string): { frames: SseFrame[]; rest: string } {
  const normalised = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const parts = normalised.split("\n\n");
  const rest = parts.pop() ?? "";
  const frames: SseFrame[] = [];
  for (const part of parts) {
    let event = "message";
    const data: string[] = [];
    for (const line of part.split("\n")) {
      if (line.startsWith(":") || line === "") continue; // comment / keep-alive
      const i = line.indexOf(":");
      const field = i === -1 ? line : line.slice(0, i);
      let value = i === -1 ? "" : line.slice(i + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
    if (data.length) frames.push({ event, data: data.join("\n") });
  }
  return { frames, rest };
}

/** Async-iterate the frames of a streaming fetch Response. */
export async function* readSse(body: ReadableStream<Uint8Array>): AsyncGenerator<SseFrame> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { frames, rest } = parseSse(buffer);
      buffer = rest;
      yield* frames;
    }
    buffer += decoder.decode();
    // a server that closes without the final blank line still delivered a complete frame
    const { frames } = parseSse(buffer + "\n\n");
    yield* frames;
  } finally {
    reader.releaseLock();
  }
}
