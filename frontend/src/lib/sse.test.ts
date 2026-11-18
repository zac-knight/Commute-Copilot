import { describe, expect, it } from "vitest";
import { parseSse, readSse } from "./sse";

describe("parseSse", () => {
  it("parses complete frames and keeps the unfinished remainder", () => {
    const { frames, rest } = parseSse('event: plan\ndata: {"a":1}\n\nevent: final\ndata: {"b":2}\n\nevent: par');
    expect(frames).toEqual([{ event: "plan", data: '{"a":1}' }, { event: "final", data: '{"b":2}' }]);
    expect(rest).toBe("event: par");
  });
  it("handles CRLF line endings", () => {
    expect(parseSse("event: x\r\ndata: y\r\n\r\n").frames).toEqual([{ event: "x", data: "y" }]);
  });
  it("joins multi-line data and ignores comments", () => {
    const { frames } = parseSse(": keep-alive\n\nevent: e\ndata: line1\ndata: line2\n\n");
    expect(frames).toEqual([{ event: "e", data: "line1\nline2" }]);
  });
  it("defaults the event name and tolerates a missing space after the colon", () => {
    expect(parseSse("data:abc\n\n").frames).toEqual([{ event: "message", data: "abc" }]);
  });
  it("drops frames with no data", () => {
    expect(parseSse("event: ping\n\n").frames).toEqual([]);
  });
  it("is stable when a frame is split at every possible position", () => {
    const text = 'event: plan\ndata: {"x":"héllo"}\n\nevent: final\ndata: {"y":1}\n\n';
    for (let cut = 1; cut < text.length; cut++) {
      const a = parseSse(text.slice(0, cut));
      const b = parseSse(a.rest + text.slice(cut));
      expect([...a.frames, ...b.frames].map((f) => f.event)).toEqual(["plan", "final"]);
    }
  });
});

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({ start(c) { chunks.forEach((x) => c.enqueue(enc.encode(x))); c.close(); } });
}

describe("readSse", () => {
  it("reassembles frames across chunks, including a split multi-byte character", async () => {
    const bytes = new TextEncoder().encode('event: a\ndata: {"t":"é"}\n\n');
    const mid = bytes.length - 6; // inside the UTF-8 sequence region
    const body = new ReadableStream<Uint8Array>({ start(c) { c.enqueue(bytes.slice(0, mid)); c.enqueue(bytes.slice(mid)); c.close(); } });
    const out: string[] = [];
    for await (const f of readSse(body)) out.push(f.data);
    expect(out).toEqual(['{"t":"é"}']);
  });
  it("delivers a final frame even if the server omits the trailing blank line", async () => {
    const out: string[] = [];
    for await (const f of readSse(streamOf(["event: final\ndata: {}"]))) out.push(f.event);
    expect(out).toEqual(["final"]);
  });
});
