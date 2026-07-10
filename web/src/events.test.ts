/** The parser's only interesting property: chunks do not respect message boundaries. */
import { strict as assert } from "node:assert";
import { test } from "node:test";

import { drainFrames, describe, parseEvent, type AgentEvent } from "./events.js";

const answer = (text: string): string =>
  `data: ${JSON.stringify({ type: "answer", text, request_id: "r" })}\n\n`;

test("a frame split across three reads is reassembled", () => {
  const whole = answer("ROAS was 9.70.");
  const [a, b, c] = [whole.slice(0, 5), whole.slice(5, 20), whole.slice(20)];

  let buffer = "";
  const seen: AgentEvent[] = [];
  for (const chunk of [a, b, c]) {
    buffer += chunk;
    const { events, rest } = drainFrames(buffer);
    buffer = rest;
    seen.push(...events);
  }
  assert.equal(seen.length, 1);
  assert.equal(seen[0]?.type, "answer");
});

test("two frames in one chunk both emerge", () => {
  const { events, rest } = drainFrames(answer("a") + answer("b"));
  assert.equal(events.length, 2);
  assert.equal(rest, "");
});

test("a partial frame is retained, not emitted", () => {
  const { events, rest } = drainFrames('data: {"type":"answer"');
  assert.equal(events.length, 0);
  assert.notEqual(rest, "");
});

test("malformed json is dropped rather than rendered", () => {
  assert.equal(parseEvent("{not json"), null);
});

test("an unknown event type is dropped", () => {
  assert.equal(parseEvent('{"type":"speculate"}'), null);
});

test("every event type has a description", () => {
  const events: AgentEvent[] = [
    { type: "start", question: "q", request_id: "r" },
    { type: "step", index: 0, action: "tool", reasoning: "why", request_id: "r" },
    { type: "tool_call", tool: "run_sql", arguments: { sql: "x" }, request_id: "r" },
    { type: "tool_result", tool: "run_sql", ok: false, error_code: "NOT_A_SELECT", preview: "", request_id: "r" },
    { type: "grounding", ok: false, checked: 2, ungrounded: ["$412,000"], request_id: "r" },
    { type: "clarify", question: "blended?", request_id: "r" },
    { type: "answer", text: "a", request_id: "r" },
    { type: "error", reason: "internal", text: "boom", request_id: "r" },
    { type: "done", ok: true, answer: "a", request_id: "r" },
  ];
  for (const event of events) assert.ok(describe(event).length > 0);
});

test("a blocked answer names the offending number", () => {
  const message = describe({
    type: "grounding", ok: false, checked: 2, ungrounded: ["$412,000"], request_id: "r",
  });
  assert.match(message, /\$412,000/);
  assert.match(message, /came from no query/);
});
