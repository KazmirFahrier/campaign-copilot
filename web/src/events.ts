/**
 * The agent's event stream, typed.
 *
 * The server streams decisions rather than tokens: the plan, the tool call, the tool's
 * verdict, the grounding result, and finally the answer. A discriminated union means the
 * renderer cannot forget to handle one -- `noFallthroughCasesInSwitch` and an exhaustive
 * `never` check turn a new server event into a compile error rather than a blank panel.
 */

export type AgentEvent =
  | { type: "start"; question: string; request_id: string }
  | { type: "step"; index: number; action: "tool" | "clarify" | "answer"; reasoning: string; request_id: string }
  | { type: "tool_call"; tool: string; arguments: Record<string, unknown>; request_id: string }
  | { type: "tool_result"; tool: string; ok: boolean; error_code: string | null; preview: string; request_id: string }
  | { type: "grounding"; ok: boolean; checked: number; ungrounded: string[]; request_id: string }
  | { type: "clarify"; question: string; request_id: string }
  | { type: "compressed"; turns: number; request_id: string }
  | { type: "answer"; text: string; request_id: string }
  | { type: "error"; reason: string; text: string; request_id: string }
  | { type: "done"; ok: boolean; answer: string; request_id: string };

export type AgentEventType = AgentEvent["type"];

/** Narrow an unknown payload from the wire. Anything unrecognised is dropped, not rendered. */
export function parseEvent(raw: string): AgentEvent | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof value !== "object" || value === null) return null;
  const candidate = value as { type?: unknown };
  if (typeof candidate.type !== "string") return null;

  const known: readonly AgentEventType[] = [
    "start", "step", "tool_call", "tool_result", "grounding", "clarify", "compressed",
    "answer", "error", "done",
  ];
  return known.includes(candidate.type as AgentEventType) ? (value as AgentEvent) : null;
}

/**
 * Split a growing SSE buffer into complete frames.
 *
 * Chunks arrive on arbitrary boundaries: a single `data:` line can be delivered in three
 * reads, and two frames can arrive in one. Returning the unconsumed remainder rather than
 * assuming a chunk is a message is the whole difference between a stream that works and one
 * that works until the payload gets long.
 */
export function drainFrames(buffer: string): { events: AgentEvent[]; rest: string } {
  const events: AgentEvent[] = [];
  let rest = buffer;

  for (;;) {
    const boundary = rest.indexOf("\n\n");
    if (boundary === -1) break;
    const frame = rest.slice(0, boundary);
    rest = rest.slice(boundary + 2);

    for (const line of frame.split("\n")) {
      if (!line.startsWith("data: ")) continue;
      const event = parseEvent(line.slice(6));
      if (event) events.push(event);
    }
  }
  return { events, rest };
}

/** Human-readable one-liner for the activity log. Exhaustive by construction. */
export function describe(event: AgentEvent): string {
  switch (event.type) {
    case "start":
      return `Question: ${event.question}`;
    case "step":
      return `Planned: ${event.action} — ${event.reasoning}`;
    case "tool_call":
      return `Calling ${event.tool}(${Object.keys(event.arguments).join(", ")})`;
    case "tool_result":
      return event.ok ? `${event.tool} returned` : `${event.tool} failed: ${event.error_code}`;
    case "grounding":
      return event.ok
        ? `Grounded: all ${event.checked} numbers trace to a query`
        : `Blocked: ${event.ungrounded.join(", ")} came from no query`;
    case "clarify":
      return `Needs clarification: ${event.question}`;
    case "compressed":
      return `Compressed ${event.turns} older turns into the running summary`;
    case "answer":
      return "Answer accepted";
    case "error":
      return `Error (${event.reason}): ${event.text}`;
    case "done":
      return event.ok ? "Done" : "Stopped without an answer";
    default: {
      const exhaustive: never = event;
      return exhaustive;
    }
  }
}
