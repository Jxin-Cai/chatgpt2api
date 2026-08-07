import type { RealtimeEvent } from "@/lib/realtime-webrtc";

export type TranscriptUpdate = {
  role: "user" | "assistant";
  text: string;
  mode: "append" | "replace";
  final: boolean;
  sourceId?: string;
};

export type ChatTranscriptCursor = {
  role: "user" | "assistant";
  sourceId: string;
};

const ASSISTANT_DELTA_EVENTS = new Set([
  "response.audio_transcript.delta",
  "response.text.delta",
  "response.output_audio_transcript.delta",
  "response.output_text.delta",
]);

const ASSISTANT_DONE_EVENTS = new Set([
  "response.audio_transcript.done",
  "response.text.done",
  "response.output_audio_transcript.done",
  "response.output_text.done",
]);

const USER_DELTA_EVENTS = new Set([
  "conversation.item.input_audio_transcription.delta",
  "conversation.item.input_audio_transcription.partial",
]);

const USER_DONE_EVENTS = new Set([
  "conversation.item.input_audio_transcription.completed",
  "conversation.item.input_audio_transcription.done",
]);

function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value) return value;
  }
  return "";
}

function contentText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => {
      if (typeof part === "string") return part;
      if (!part || typeof part !== "object") return "";
      const value = part as Record<string, unknown>;
      return firstString(value.transcript, value.text, value.content);
    })
    .join("");
}

function sourceId(data: RealtimeEvent, nested?: Record<string, unknown>): string | undefined {
  const value = firstString(
    data.item_id,
    data.response_id,
    data.message_id,
    nested?.id,
    nested?.item_id,
  );
  return value || undefined;
}

/** Normalize both OpenAI Realtime events and ChatGPT web voice message events. */
export function transcriptUpdateFromEvent(data: RealtimeEvent): TranscriptUpdate | null {
  const type = data.type;
  if (ASSISTANT_DELTA_EVENTS.has(type)) {
    return {
      role: "assistant",
      text: firstString(data.delta, data.text, data.transcript),
      mode: "append",
      final: false,
      sourceId: sourceId(data),
    };
  }
  if (ASSISTANT_DONE_EVENTS.has(type)) {
    return {
      role: "assistant",
      text: firstString(data.transcript, data.text),
      mode: "replace",
      final: true,
      sourceId: sourceId(data),
    };
  }
  if (USER_DELTA_EVENTS.has(type)) {
    return {
      role: "user",
      text: firstString(data.delta, data.text, data.transcript),
      mode: "append",
      final: false,
      sourceId: sourceId(data),
    };
  }
  if (USER_DONE_EVENTS.has(type)) {
    return {
      role: "user",
      text: firstString(data.transcript, data.text),
      mode: "replace",
      final: true,
      sourceId: sourceId(data),
    };
  }

  const message = (data.message && typeof data.message === "object"
    ? data.message
    : data.item && typeof data.item === "object"
      ? data.item
      : null) as Record<string, unknown> | null;
  if (!message) return null;

  const author = message.author && typeof message.author === "object"
    ? message.author as Record<string, unknown>
    : null;
  const rawRole = firstString(message.role, author?.role);
  const role = rawRole === "user" ? "user" : rawRole === "assistant" ? "assistant" : null;
  if (!role) return null;

  const nestedContent = message.content && typeof message.content === "object" && !Array.isArray(message.content)
    ? message.content as Record<string, unknown>
    : null;
  const text = firstString(
    message.transcript,
    message.text,
    contentText(message.content),
    nestedContent?.text,
    nestedContent?.transcript,
    contentText(nestedContent?.parts),
  );
  if (!text) return null;

  const status = firstString(message.status, data.status);
  return {
    role,
    text,
    mode: "replace",
    final: status === "finished_successfully" || status === "completed" || type.endsWith(".done"),
    sourceId: sourceId(data, message),
  };
}

type ChatDeltaResult = {
  cursor: ChatTranscriptCursor | null;
  update: TranscriptUpdate | null;
};

/** Assemble ChatGPT Web Voice's JSON-Patch based `chat_message_delta` stream. */
export function chatTranscriptUpdateFromEvent(
  data: RealtimeEvent,
  cursor: ChatTranscriptCursor | null,
): ChatDeltaResult | null {
  if (data.type !== "chat_message_delta" || !data.delta || typeof data.delta !== "object") {
    return null;
  }

  const delta = data.delta as Record<string, unknown>;
  if (delta.o === "add" && delta.v && typeof delta.v === "object") {
    const value = delta.v as Record<string, unknown>;
    const message = value.message && typeof value.message === "object"
      ? value.message as Record<string, unknown>
      : null;
    if (!message) return { cursor, update: null };
    const author = message.author && typeof message.author === "object"
      ? message.author as Record<string, unknown>
      : null;
    const rawRole = firstString(message.role, author?.role);
    if (rawRole !== "user" && rawRole !== "assistant") return { cursor, update: null };
    const nextCursor: ChatTranscriptCursor = {
      role: rawRole,
      sourceId: firstString(message.id) || `chat-${String(delta.c ?? "message")}`,
    };
    const parsed = transcriptUpdateFromEvent({ type: "chat.message", message });
    return {
      cursor: nextCursor,
      update: parsed ? { ...parsed, sourceId: nextCursor.sourceId } : null,
    };
  }

  if (!cursor) return { cursor: null, update: null };
  // After the first patch ChatGPT omits the repeated outer `o: "patch"` field,
  // but keeps sending the operation array in `v`.
  const operations = Array.isArray(delta.v) ? delta.v : [delta];
  let text = "";
  let mode: TranscriptUpdate["mode"] = "append";
  let final = false;

  for (const rawOperation of operations) {
    if (!rawOperation || typeof rawOperation !== "object") continue;
    const operation = rawOperation as Record<string, unknown>;
    const path = firstString(operation.p);
    const op = firstString(operation.o);
    if (path.includes("/message/content/parts/") && path.endsWith("/text")) {
      const value = firstString(operation.v);
      if (value) text += value;
      if (op === "replace") mode = "replace";
    }
    if (path === "/message/status" && operation.v === "finished_successfully") {
      final = true;
    }
  }

  return {
    cursor,
    update: text || final
      ? { role: cursor.role, sourceId: cursor.sourceId, text, mode, final }
      : null,
  };
}
