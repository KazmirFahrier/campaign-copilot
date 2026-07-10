/**
 * Streaming client for POST /v1/chat.
 *
 * `EventSource` cannot issue a POST, so the stream is read off `fetch` directly. The reader is
 * always released and the request is always abortable: a chat UI whose only way to stop a
 * fifteen-second query is to close the tab is a chat UI nobody trusts.
 */

import { drainFrames, type AgentEvent } from "./events.js";

export interface ChatOptions {
  readonly baseUrl?: string;
  readonly sessionId?: string;
  readonly signal?: AbortSignal;
  readonly requestId?: string;
}

export async function* streamChat(
  question: string,
  options: ChatOptions = {},
): AsyncGenerator<AgentEvent, void, undefined> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (options.requestId) headers["X-Request-ID"] = options.requestId;

  const init: RequestInit = {
    method: "POST",
    headers,
    body: JSON.stringify({ question, session_id: options.sessionId ?? "default" }),
  };
  if (options.signal) init.signal = options.signal;

  const response = await fetch(`${options.baseUrl ?? ""}/v1/chat`, init);
  if (!response.ok) throw new Error(`chat failed: ${response.status}`);
  if (!response.body) throw new Error("chat returned no body");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { events, rest } = drainFrames(buffer);
      buffer = rest;
      for (const event of events) yield event;
    }
  } finally {
    reader.releaseLock();
  }
}
