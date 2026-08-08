import type { ChatContentPart, ChatMessage } from "./types";

export const CHAT_SESSION_STORAGE_KEY = "arc-chat-session-v1";
export const CHAT_SESSION_SCHEMA_VERSION = 1 as const;
export const CHAT_SESSION_IMAGE_PLACEHOLDER = "[图片未保存：仅当前页面有效]";

const MAX_MESSAGES = 80;
const MAX_MESSAGE_PARTS = 48;
const MAX_MESSAGE_CHARS = 16_000;
const MAX_TOTAL_CHARS = 120_000;
const MAX_INPUT_CHARS = 12_000;
const MAX_MODEL_CHARS = 160;
const MAX_REASONING_CHARS = 32;
const MAX_IMAGE_URL_CHARS = 8_000;
const DATA_URL_REPLACE_PATTERN = /data:[^\s,]+,[^\s)]+/gi;
const DATA_URL_TEST_PATTERN = /data:[^\s,]+,[^\s)]+/i;
const DATA_IMAGE_MARKDOWN_PATTERN = /!\[[^\]]*\]\(\s*data:image\/[^)]*\)/gi;
const DATA_IMAGE_TEST_PATTERN = /data:image\//i;

export type ChatSessionSnapshot = {
  version: typeof CHAT_SESSION_SCHEMA_VERSION;
  messages: ChatMessage[];
  model: string;
  reasoningEffort: string;
  input: string;
};

export type ChatSessionReadResult =
  | { status: "empty" }
  | { status: "restored"; snapshot: ChatSessionSnapshot }
  | { status: "invalid" }
  | { status: "unavailable" };

type SessionDraft = Omit<ChatSessionSnapshot, "version">;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isRole(value: unknown): value is ChatMessage["role"] {
  return value === "system" || value === "user" || value === "assistant";
}

function isDataUrl(value: string): boolean {
  return /^data:/i.test(value.trim());
}

function containsDataUrl(value: string): boolean {
  return DATA_URL_TEST_PATTERN.test(value);
}

function containsEmbeddedImageData(value: string): boolean {
  return DATA_IMAGE_TEST_PATTERN.test(value);
}

function containsForbiddenData(value: string): boolean {
  return containsDataUrl(value) || containsEmbeddedImageData(value);
}

function projectText(value: string, limit: number): string {
  const withoutImages = value.replace(DATA_IMAGE_MARKDOWN_PATTERN, CHAT_SESSION_IMAGE_PLACEHOLDER);
  const withoutData = withoutImages.replace(DATA_URL_REPLACE_PATTERN, CHAT_SESSION_IMAGE_PLACEHOLDER);
  return clipText(containsEmbeddedImageData(withoutData) ? CHAT_SESSION_IMAGE_PLACEHOLDER : withoutData, limit);
}

function messageCharCount(message: ChatMessage): number {
  if (typeof message.content === "string") return message.content.length;
  return message.content.reduce(
    (total, part) => total + (part.type === "text" ? part.text.length : CHAT_SESSION_IMAGE_PLACEHOLDER.length),
    0,
  );
}

function clipText(value: string, limit: number): string {
  return value.length > limit ? value.slice(0, limit) : value;
}

function projectMessage(message: ChatMessage, limit = MAX_MESSAGE_CHARS): ChatMessage {
  if (typeof message.content === "string") {
    return { role: message.role, content: projectText(message.content, limit) };
  }

  const parts: ChatContentPart[] = [];
  let remaining = limit;
  for (const part of message.content.slice(0, MAX_MESSAGE_PARTS)) {
    if (part.type === "text") {
      if (remaining <= 0) continue;
      const text = projectText(part.text, remaining);
      if (text) parts.push({ type: "text", text });
      remaining -= text.length;
      continue;
    }
    if (remaining < CHAT_SESSION_IMAGE_PLACEHOLDER.length) continue;
    parts.push({ type: "text", text: CHAT_SESSION_IMAGE_PLACEHOLDER });
    remaining -= CHAT_SESSION_IMAGE_PLACEHOLDER.length;
  }
  return { role: message.role, content: parts.length ? parts : "" };
}

function projectMessages(messages: ChatMessage[]): ChatMessage[] {
  const projected = messages.slice(-MAX_MESSAGES).map((message) => projectMessage(message));
  let total = projected.reduce((sum, message) => sum + messageCharCount(message), 0);
  while (total > MAX_TOTAL_CHARS && projected.length > 1) {
    const removed = projected.shift();
    total -= removed ? messageCharCount(removed) : 0;
  }
  if (projected.length && total > MAX_TOTAL_CHARS) {
    projected[0] = projectMessage(projected[0], MAX_TOTAL_CHARS);
  }
  return projected;
}

export function createChatSessionSnapshot(draft: SessionDraft): ChatSessionSnapshot {
  const snapshot: ChatSessionSnapshot = {
    version: CHAT_SESSION_SCHEMA_VERSION,
    messages: projectMessages(draft.messages),
    model: projectText(draft.model, MAX_MODEL_CHARS),
    reasoningEffort: projectText(draft.reasoningEffort, MAX_REASONING_CHARS),
    input: projectText(draft.input, MAX_INPUT_CHARS),
  };
  return ensureNoEmbeddedImageData(snapshot);
}

function ensureNoEmbeddedImageData(snapshot: ChatSessionSnapshot): ChatSessionSnapshot {
  const serialized = JSON.stringify(snapshot);
  if (!containsEmbeddedImageData(serialized)) return snapshot;
  return {
    ...snapshot,
    messages: snapshot.messages.map((message) => {
      if (typeof message.content === "string") {
        return {
          role: message.role,
          content: containsEmbeddedImageData(message.content) ? CHAT_SESSION_IMAGE_PLACEHOLDER : message.content,
        };
      }
      return {
        role: message.role,
        content: message.content.map((part) => part.type === "text"
          ? { type: "text" as const, text: containsEmbeddedImageData(part.text) ? CHAT_SESSION_IMAGE_PLACEHOLDER : part.text }
          : { type: "text" as const, text: CHAT_SESSION_IMAGE_PLACEHOLDER }),
      };
    }),
    model: containsEmbeddedImageData(snapshot.model) ? CHAT_SESSION_IMAGE_PLACEHOLDER : snapshot.model,
    reasoningEffort: containsEmbeddedImageData(snapshot.reasoningEffort) ? CHAT_SESSION_IMAGE_PLACEHOLDER : snapshot.reasoningEffort,
    input: containsEmbeddedImageData(snapshot.input) ? CHAT_SESSION_IMAGE_PLACEHOLDER : snapshot.input,
  };
}

function parseMessage(value: unknown): ChatMessage | null {
  if (!isRecord(value) || !isRole(value.role)) return null;
  if (typeof value.content === "string") {
    if (value.content.length > MAX_MESSAGE_CHARS || containsForbiddenData(value.content)) return null;
    return { role: value.role, content: value.content };
  }
  if (!Array.isArray(value.content) || value.content.length > MAX_MESSAGE_PARTS) return null;

  const parts: ChatContentPart[] = [];
  for (const candidate of value.content) {
    if (!isRecord(candidate) || typeof candidate.type !== "string") return null;
    if (candidate.type === "text") {
      if (typeof candidate.text !== "string" || candidate.text.length > MAX_MESSAGE_CHARS || containsForbiddenData(candidate.text)) return null;
      parts.push({ type: "text", text: candidate.text });
      continue;
    }
    if (candidate.type !== "image_url" || !isRecord(candidate.image_url)) return null;
    const url = candidate.image_url.url;
    if (typeof url !== "string" || url.length > MAX_IMAGE_URL_CHARS || isDataUrl(url)) return null;
    // Image bytes are page-local by design; only retain an explicit text marker.
    parts.push({ type: "text", text: CHAT_SESSION_IMAGE_PLACEHOLDER });
  }
  const message: ChatMessage = { role: value.role, content: parts.length ? parts : "" };
  return messageCharCount(message) <= MAX_MESSAGE_CHARS ? message : null;
}

function parseSnapshot(value: unknown): ChatSessionSnapshot | null {
  if (!isRecord(value) || value.version !== CHAT_SESSION_SCHEMA_VERSION) return null;
  if (!Array.isArray(value.messages) || value.messages.length > MAX_MESSAGES) return null;
  if (typeof value.model !== "string" || value.model.length > MAX_MODEL_CHARS || containsForbiddenData(value.model)) return null;
  if (typeof value.reasoningEffort !== "string" || value.reasoningEffort.length > MAX_REASONING_CHARS || containsForbiddenData(value.reasoningEffort)) return null;
  if (typeof value.input !== "string" || value.input.length > MAX_INPUT_CHARS || containsForbiddenData(value.input)) return null;

  const messages: ChatMessage[] = [];
  let totalCharacters = 0;
  for (const candidate of value.messages) {
    const message = parseMessage(candidate);
    if (!message) return null;
    totalCharacters += messageCharCount(message);
    if (totalCharacters > MAX_TOTAL_CHARS) return null;
    messages.push(message);
  }
  return {
    version: CHAT_SESSION_SCHEMA_VERSION,
    messages,
    model: value.model,
    reasoningEffort: value.reasoningEffort,
    input: value.input,
  };
}

export function readChatSession(): ChatSessionReadResult {
  if (typeof window === "undefined") return { status: "unavailable" };
  try {
    const raw = window.sessionStorage.getItem(CHAT_SESSION_STORAGE_KEY);
    if (!raw) return { status: "empty" };
    const snapshot = parseSnapshot(JSON.parse(raw));
    return snapshot ? { status: "restored", snapshot } : { status: "invalid" };
  } catch {
    return { status: "unavailable" };
  }
}

export function writeChatSession(snapshot: ChatSessionSnapshot): boolean {
  if (typeof window === "undefined") return false;
  try {
    window.sessionStorage.setItem(CHAT_SESSION_STORAGE_KEY, JSON.stringify(snapshot));
    return true;
  } catch {
    return false;
  }
}

export function clearChatSession(): boolean {
  if (typeof window === "undefined") return false;
  try {
    window.sessionStorage.removeItem(CHAT_SESSION_STORAGE_KEY);
    return true;
  } catch {
    return false;
  }
}
