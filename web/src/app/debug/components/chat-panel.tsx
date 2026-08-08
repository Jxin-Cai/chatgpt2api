"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { BrainCircuit, ChevronDown, Download, ImagePlus, LoaderCircle, MessageSquareText, RotateCcw, Send, Sparkles, Square, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { streamRequest } from "@/lib/request";

import { ChatMarkdown } from "./chat-markdown";
import { ChatMessageActions } from "./chat-message-actions";
import { clearChatSession, createChatSessionSnapshot, readChatSession, writeChatSession } from "./chat-session-storage";
import { pretty, type ChatContentPart, type ChatMessage, type ChatRawResponse, type ChatStreamingSummary } from "./types";

type SelectedImage = {
  id: string;
  name: string;
  size: number;
  url: string;
};

const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const DEFAULT_CHAT_INPUT = "你好，先记住我的项目叫 chatgpt2api。";

const SUGGESTIONS = [
  "总结一下这个项目的定位",
  "帮我设计一个清晰的 API 使用示例",
  "列出下一步最值得做的三件事",
] as const;

type SessionMemoryStatus = "restoring" | "restored" | "saved" | "unavailable" | "new";

const SESSION_MEMORY_LABELS: Record<SessionMemoryStatus, string> = {
  restoring: "恢复中",
  restored: "已恢复",
  saved: "已保存",
  unavailable: "不可用",
  new: "新会话",
};

const SESSION_MEMORY_ANNOUNCEMENTS: Record<SessionMemoryStatus, string> = {
  restoring: "正在检查此标签页的会话记忆。",
  restored: "会话已从此标签页恢复。",
  saved: "会话已保存到当前标签页。",
  unavailable: "当前标签页的会话记忆不可用，内容仍可继续使用。",
  new: "已建立新会话，旧内容已清理。",
};

function readImage(file: File): Promise<SelectedImage> {
  return new Promise((resolve, reject) => {
    if (!file.type.startsWith("image/")) {
      reject(new Error(`${file.name} 不是图片文件`));
      return;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      reject(new Error(`${file.name} 超过 10MB`));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result || "");
      if (!url.startsWith("data:image/")) {
        reject(new Error(`${file.name} 读取失败`));
        return;
      }
      resolve({
        id: `${file.name}-${file.size}-${file.lastModified}-${Math.random().toString(16).slice(2)}`,
        name: file.name,
        size: file.size,
        url,
      });
    };
    reader.onerror = () => reject(reader.error || new Error(`${file.name} 读取失败`));
    reader.readAsDataURL(file);
  });
}

function messageText(message: ChatMessage): string {
  if (typeof message.content === "string") {
    return message.content;
  }
  return message.content
    .filter((part): part is { type: "text"; text: string } => part.type === "text")
    .map((part) => part.text)
    .join("");
}

function messageImages(message: ChatMessage): string[] {
  if (!Array.isArray(message.content)) {
    return [];
  }
  return message.content
    .filter((part): part is { type: "image_url"; image_url: { url: string } } => part.type === "image_url")
    .map((part) => part.image_url.url);
}

type ChatStreamChunk = {
  model?: string;
  choices?: Array<{
    delta?: { content?: unknown };
    finish_reason?: string | null;
  }>;
  error?: unknown;
};

type StreamStatus = "idle" | "prepared" | "linked" | "streaming" | "complete" | "interrupted" | "fault";

type StreamTelemetry = {
  status: StreamStatus;
  chunkCount: number;
  firstTokenMs: number | null;
  elapsedMs: number;
  finishReason: string | null;
};

type StreamUiState = StreamTelemetry & {
  assistantIndex: number;
  text: string;
  startedAt: number;
};

type StreamRunOptions = {
  requestMessages: ChatMessage[];
  displayMessages: ChatMessage[];
  assistantIndex: number;
  clearComposer: boolean;
  fallbackMessage?: ChatMessage;
};

const INITIAL_STREAM_TELEMETRY: StreamTelemetry = {
  status: "idle",
  chunkCount: 0,
  firstTokenMs: null,
  elapsedMs: 0,
  finishReason: null,
};

const STREAM_STATUS_ANNOUNCEMENTS: Record<StreamStatus, string> = {
  idle: "",
  prepared: "Arc 已准备请求。",
  linked: "Arc 已建立连接，正在等待首个 token。",
  streaming: "Arc 正在输出回应。",
  complete: "Arc 回答已完成。",
  interrupted: "Arc 回答已中断，已保留当前内容。",
  fault: "Arc 回答遇到错误。",
};

function streamErrorMessage(value: unknown): string {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "实时回答失败";
  const item = value as { message?: unknown; detail?: unknown; error?: unknown };
  if (typeof item.message === "string") return item.message;
  if (typeof item.detail === "string") return item.detail;
  if (item.error) return streamErrorMessage(item.error);
  return "实时回答失败";
}

function createChatSseParser(onFrame: (data: string) => void) {
  let buffer = "";
  let dataLines: string[] = [];

  const processLine = (rawLine: string) => {
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    if (!line) {
      if (dataLines.length) {
        onFrame(dataLines.join("\n"));
        dataLines = [];
      }
      return;
    }
    if (line.startsWith(":")) return;
    if (!line.startsWith("data:")) return;
    const value = line.slice(5).startsWith(" ") ? line.slice(6) : line.slice(5);
    dataLines.push(value);
  };

  return {
    push(chunk: string) {
      buffer += chunk;
      let newlineIndex = buffer.indexOf("\n");
      while (newlineIndex >= 0) {
        processLine(buffer.slice(0, newlineIndex));
        buffer = buffer.slice(newlineIndex + 1);
        newlineIndex = buffer.indexOf("\n");
      }
    },
    flush() {
      if (buffer) processLine(buffer);
      buffer = "";
      if (dataLines.length) {
        onFrame(dataLines.join("\n"));
        dataLines = [];
      }
    },
  };
}

function streamSummary(stream: StreamUiState, model: string, error?: string): ChatStreamingSummary {
  return {
    kind: "stream",
    model,
    status: stream.status === "complete" || stream.status === "interrupted" || stream.status === "fault"
      ? stream.status
      : "fault",
    finish_reason: stream.finishReason,
    char_count: stream.text.length,
    chunk_count: stream.chunkCount,
    first_token_ms: stream.firstTokenMs,
    elapsed_ms: Math.max(0, Math.round(stream.elapsedMs)),
    content: stream.text,
    ...(error ? { error } : {}),
  };
}

export function ChatPanel() {
  const [model, setModel] = useState("auto");
  const [reasoningEffort, setReasoningEffort] = useState("");
  const [input, setInput] = useState(DEFAULT_CHAT_INPUT);
  const [selectedImages, setSelectedImages] = useState<SelectedImage[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [raw, setRaw] = useState<ChatRawResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [streamTelemetry, setStreamTelemetry] = useState<StreamTelemetry>(INITIAL_STREAM_TELEMETRY);
  const conversationEndRef = useRef<HTMLDivElement | null>(null);
  const sendingRef = useRef(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const streamUiRef = useRef<StreamUiState | null>(null);
  const streamFlushFrameRef = useRef(0);
  const sessionHydratedRef = useRef(false);
  const sessionSaveTimerRef = useRef<number | null>(null);
  const [sessionHydrated, setSessionHydrated] = useState(false);
  const [sessionDirty, setSessionDirty] = useState(false);
  const [inputEdited, setInputEdited] = useState(false);
  const [sessionMemoryStatus, setSessionMemoryStatus] = useState<SessionMemoryStatus>("restoring");
  const [newSessionDialogOpen, setNewSessionDialogOpen] = useState(false);

  const scrollToLatest = useCallback(() => {
    window.requestAnimationFrame(() => {
      const reduceMotion = typeof window.matchMedia === "function"
        && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      conversationEndRef.current?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "end" });
    });
  }, []);

  useEffect(() => {
    scrollToLatest();
  }, [loading, messages.length, scrollToLatest]);

  useEffect(() => {
    if (sessionHydratedRef.current) return;
    sessionHydratedRef.current = true;
    const result = readChatSession();
    if (result.status === "restored") {
      setMessages(result.snapshot.messages);
      setModel(result.snapshot.model || "auto");
      setReasoningEffort(result.snapshot.reasoningEffort);
      setInput(result.snapshot.input);
      setInputEdited(result.snapshot.input !== DEFAULT_CHAT_INPUT);
      setSessionMemoryStatus("restored");
    } else if (result.status === "unavailable" || result.status === "invalid") {
      setSessionMemoryStatus("unavailable");
    } else {
      setSessionMemoryStatus("restored");
    }
    setSessionDirty(false);
    setSessionHydrated(true);
  }, []);

  useEffect(() => {
    if (!sessionHydrated || !sessionDirty || loading) return;
    if (sessionSaveTimerRef.current !== null) window.clearTimeout(sessionSaveTimerRef.current);
    sessionSaveTimerRef.current = window.setTimeout(() => {
      sessionSaveTimerRef.current = null;
      const snapshot = createChatSessionSnapshot({ messages, model, reasoningEffort, input });
      setSessionMemoryStatus(writeChatSession(snapshot) ? "saved" : "unavailable");
    }, 500);
    return () => {
      if (sessionSaveTimerRef.current !== null) {
        window.clearTimeout(sessionSaveTimerRef.current);
        sessionSaveTimerRef.current = null;
      }
    };
  }, [input, loading, messages, model, reasoningEffort, sessionDirty, sessionHydrated]);

  useEffect(() => () => {
    if (sessionSaveTimerRef.current !== null) window.clearTimeout(sessionSaveTimerRef.current);
  }, []);

  const flushStreamUi = useCallback(() => {
    streamFlushFrameRef.current = 0;
    const stream = streamUiRef.current;
    if (!stream) return;
    setMessages((current) => {
      const currentMessage = current[stream.assistantIndex];
      if (!currentMessage || currentMessage.role !== "assistant") return current;
      const next = [...current];
      next[stream.assistantIndex] = { ...currentMessage, content: stream.text };
      return next;
    });
    setStreamTelemetry({
      status: stream.status,
      chunkCount: stream.chunkCount,
      firstTokenMs: stream.firstTokenMs,
      elapsedMs: stream.elapsedMs,
      finishReason: stream.finishReason,
    });
    scrollToLatest();
  }, [scrollToLatest]);

  const scheduleStreamUi = useCallback(() => {
    if (streamFlushFrameRef.current) return;
    streamFlushFrameRef.current = window.requestAnimationFrame(flushStreamUi);
  }, [flushStreamUi]);

  const flushPendingStreamUi = useCallback(() => {
    if (streamFlushFrameRef.current) {
      window.cancelAnimationFrame(streamFlushFrameRef.current);
      streamFlushFrameRef.current = 0;
    }
    flushStreamUi();
  }, [flushStreamUi]);

  useEffect(() => () => {
    if (streamFlushFrameRef.current) window.cancelAnimationFrame(streamFlushFrameRef.current);
    abortControllerRef.current?.abort();
  }, []);

  const handleImagesChange = async (files: FileList | null) => {
    if (!files?.length) return;
    setError("");
    try {
      const images = await Promise.all(Array.from(files).map(readImage));
      setSelectedImages((current) => [...current, ...images].slice(0, 4));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const runStream = async ({
    requestMessages,
    displayMessages,
    assistantIndex,
    clearComposer,
    fallbackMessage,
  }: StreamRunOptions) => {
    if (loading || sendingRef.current) return;
    sendingRef.current = true;
    const controller = new AbortController();
    abortControllerRef.current = controller;
    const startedAt = performance.now();
    const modelName = model.trim() || "auto";
    const stream: StreamUiState = {
      ...INITIAL_STREAM_TELEMETRY,
      status: "prepared",
      assistantIndex,
      text: "",
      startedAt,
    };
    streamUiRef.current = stream;
    setMessages(displayMessages);
    if (clearComposer) {
      setInput("");
      setInputEdited(false);
      setSelectedImages([]);
    }
    setRaw(null);
    setLoading(true);
    setStreamTelemetry({ ...INITIAL_STREAM_TELEMETRY, status: "prepared" });
    setError("");
    let activeReader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    try {
      const body = {
        model: modelName,
        messages: requestMessages,
        ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}),
        stream: true,
      };
      const response = await streamRequest("/v1/chat/completions", {
        method: "POST",
        body,
        signal: controller.signal,
      });
      if (!response.body) throw new Error("实时回答没有返回可读取的数据流");

      stream.status = "linked";
      stream.elapsedMs = performance.now() - startedAt;
      scheduleStreamUi();
      const reader = response.body.getReader();
      activeReader = reader;
      const decoder = new TextDecoder();
      let doneSeen = false;
      const parser = createChatSseParser((data) => {
        if (!data.trim()) return;
        if (data.trim() === "[DONE]") {
          doneSeen = true;
          return;
        }
        let payload: ChatStreamChunk;
        try {
          payload = JSON.parse(data) as ChatStreamChunk;
        } catch {
          throw new Error("实时回答返回了无法解析的事件");
        }
        if (payload.error) {
          throw new Error(streamErrorMessage(payload.error));
        }
        const choice = payload.choices?.[0];
        if (!choice) return;
        stream.chunkCount += 1;
        if (choice.finish_reason !== undefined && choice.finish_reason !== null) {
          stream.finishReason = choice.finish_reason;
        }
        const delta = typeof choice.delta?.content === "string" ? choice.delta.content : "";
        if (delta) {
          stream.text += delta;
          if (stream.firstTokenMs === null) {
            stream.firstTokenMs = Math.round(performance.now() - startedAt);
          }
          stream.status = "streaming";
        }
        stream.elapsedMs = performance.now() - startedAt;
        scheduleStreamUi();
      });

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        parser.push(decoder.decode(value, { stream: true }));
        if (doneSeen) {
          await reader.cancel();
          break;
        }
      }
      parser.push(decoder.decode());
      parser.flush();
      if (!doneSeen && stream.finishReason === null && !controller.signal.aborted) {
        throw new Error("实时回答流提前结束，未收到完成标记");
      }
      stream.status = "complete";
      stream.elapsedMs = performance.now() - startedAt;
      flushPendingStreamUi();
      setRaw(streamSummary(stream, modelName));
      if (!stream.text) {
        setMessages((current) => fallbackMessage
          ? current.map((message, index) => index === assistantIndex ? fallbackMessage : message)
          : current.filter((_, index) => index !== assistantIndex));
      }
    } catch (err) {
      const interrupted = controller.signal.aborted
        || (err instanceof DOMException && err.name === "AbortError");
      stream.status = interrupted ? "interrupted" : "fault";
      stream.elapsedMs = performance.now() - startedAt;
      flushPendingStreamUi();
      const message = err instanceof Error ? err.message : String(err);
      setRaw(streamSummary(stream, modelName, interrupted ? undefined : message));
      if (!stream.text) {
        setMessages((current) => fallbackMessage
          ? current.map((item, index) => index === assistantIndex ? fallbackMessage : item)
          : current.filter((_, index) => index !== assistantIndex));
      }
      if (!interrupted) setError(message);
    } finally {
      if (activeReader) {
        void activeReader.cancel().catch(() => undefined);
        activeReader = null;
      }
      if (abortControllerRef.current === controller) abortControllerRef.current = null;
      streamUiRef.current = null;
      sendingRef.current = false;
      setLoading(false);
    }
  };

  const sendChat = async () => {
    if (loading || sendingRef.current) return;
    const text = input.trim();
    if (!text && !selectedImages.length) return;
    const content: string | ChatContentPart[] = selectedImages.length
      ? [
          ...(text ? [{ type: "text" as const, text }] : []),
          ...selectedImages.map((image) => ({ type: "image_url" as const, image_url: { url: image.url } })),
        ]
      : text;
    const requestMessages: ChatMessage[] = [...messages, { role: "user", content }];
    const assistantIndex = requestMessages.length;
    setSessionDirty(true);
    await runStream({
      requestMessages,
      displayMessages: [...requestMessages, { role: "assistant", content: "" }],
      assistantIndex,
      clearComposer: true,
    });
  };

  const regenerateAssistant = async (assistantIndex: number) => {
    if (loading || sendingRef.current) return;
    if (assistantIndex !== messages.length - 1) return;
    const target = messages[assistantIndex];
    if (!target || target.role !== "assistant") return;
    const requestMessages = messages.slice(0, assistantIndex);
    setSessionDirty(true);
    await runStream({
      requestMessages,
      displayMessages: [...requestMessages, { role: "assistant", content: "" }],
      assistantIndex,
      clearComposer: false,
      fallbackMessage: target,
    });
  };

  const stopGeneration = () => {
    abortControllerRef.current?.abort();
  };

  const hasSessionContent = messages.length > 0 || inputEdited || selectedImages.length > 0;

  const resetSession = () => {
    if (loading) return;
    if (sessionSaveTimerRef.current !== null) {
      window.clearTimeout(sessionSaveTimerRef.current);
      sessionSaveTimerRef.current = null;
    }
    const cleared = clearChatSession();
    setMessages([]);
    setModel("auto");
    setReasoningEffort("");
    setSelectedImages([]);
    setRaw(null);
    setStreamTelemetry(INITIAL_STREAM_TELEMETRY);
    streamUiRef.current = null;
    setError("");
    setInput(DEFAULT_CHAT_INPUT);
    setInputEdited(false);
    setSessionDirty(false);
    setSessionMemoryStatus(cleared ? "new" : "unavailable");
    setNewSessionDialogOpen(false);
  };

  const requestNewSession = () => {
    if (loading) return;
    if (hasSessionContent) {
      setNewSessionDialogOpen(true);
      return;
    }
    resetSession();
  };

  const exportConversation = () => {
    if (!messages.length) return;
    const lines = ["# Arc conversation", ""];
    messages.forEach((message, index) => {
      const role = message.role === "assistant" ? "ARC" : message.role.toUpperCase();
      lines.push(`## ${role} · ${String(index + 1).padStart(2, "0")}`, "");
      const text = messageText(message).trimEnd();
      if (text) lines.push(text, "");
      messageImages(message).forEach((url, imageIndex) => {
        lines.push(`![${role} image ${imageIndex + 1}](${url})`, "");
      });
    });
    const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    const stamp = new Date().toISOString().replace(/[^0-9A-Za-z_-]/g, "").slice(0, 14) || "export";
    anchor.href = objectUrl;
    anchor.download = `arc-chat-${stamp}.md`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  };

  const sessionState = loading
    ? "PROCESSING"
    : streamTelemetry.status === "interrupted"
      ? "INTERRUPTED"
      : error || streamTelemetry.status === "fault"
        ? "FAULT"
        : messages.length
          ? "READY"
          : "STANDBY";
  const userMessageCount = messages.filter((message) => message.role === "user").length;
  const lastMessageIndex = messages.length - 1;
  const lastMessage = lastMessageIndex >= 0 ? messages[lastMessageIndex] : undefined;
  const latestAssistantIndex = lastMessage?.role === "assistant"
    && (messageText(lastMessage).trim() || messageImages(lastMessage).length)
    ? lastMessageIndex
    : -1;

  return (
    <div className="chat-panel relative grid min-h-[680px] min-w-0 overflow-hidden rounded-[30px] border border-white/10 text-white shadow-[0_30px_100px_rgba(12,18,48,0.24)] lg:grid-cols-[320px_minmax(0,1fr)]">
      <div className="chat-panel__scrim pointer-events-none absolute inset-0" />

      <aside className="chat-command-deck relative z-10 flex min-h-0 min-w-0 flex-col border-b border-white/10 p-5 md:p-6 lg:border-r lg:border-b-0">
        <div className="flex items-start justify-between gap-4 border-b border-white/10 pb-5">
          <div>
            <p className="chat-kicker">ARC / CHAT DEBUG</p>
            <h2 className="mt-2 text-lg font-medium tracking-[-0.02em] text-white/90">Command deck</h2>
            <p className="mt-1 text-xs leading-5 text-white/42">配置请求参数，观察会话状态</p>
          </div>
          <span className="chat-command-mark" aria-hidden="true">
            <BrainCircuit className="size-5" />
          </span>
        </div>

        <div className="min-h-0 flex-1 space-y-5 overflow-auto py-5 pr-1">
          <div className="space-y-2">
            <Label htmlFor="chat-model" className="chat-field-label">Model</Label>
            <Input
              id="chat-model"
              value={model}
              onChange={(event) => {
                setModel(event.target.value);
                setSessionDirty(true);
              }}
              disabled={loading}
              className="h-11 rounded-xl border-white/10 bg-white/[0.055] text-sm text-white shadow-none transition-colors duration-200 placeholder:text-white/25 focus-visible:border-cyan-300/45 focus-visible:ring-cyan-300/20"
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="chat-reasoning-effort" className="chat-field-label">思考强度</Label>
            <Select value={reasoningEffort || "default"} onValueChange={(value) => {
              setReasoningEffort(value === "default" ? "" : value);
              setSessionDirty(true);
            }} disabled={loading}>
              <SelectTrigger id="chat-reasoning-effort" className="h-11 cursor-pointer rounded-xl border-white/10 bg-white/[0.055] text-sm text-white shadow-none transition-colors duration-200 hover:bg-white/[0.09] focus-visible:border-cyan-300/45 focus-visible:ring-cyan-300/20">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">默认</SelectItem>
                <SelectItem value="low">低</SelectItem>
                <SelectItem value="medium">中</SelectItem>
                <SelectItem value="high">高</SelectItem>
                <SelectItem value="xhigh">超高</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="chat-state-grid" aria-label="会话状态">
            <div className="chat-state-cell">
              <span className="chat-state-label">AI STATE</span>
              <strong className="chat-state-value">{sessionState}</strong>
            </div>
            <div className="chat-state-cell">
              <span className="chat-state-label">MESSAGES</span>
              <strong className="chat-state-value">{messages.length.toString().padStart(2, "0")}</strong>
            </div>
          </div>

          <div className="space-y-2 rounded-xl border border-white/8 bg-white/[0.025] p-3">
            <div className="flex items-center justify-between text-[10px] uppercase tracking-[0.18em] text-white/32">
              <span>Session</span>
              <span className="text-cyan-200/65">{userMessageCount ? `${userMessageCount} user turns` : "ready"}</span>
            </div>
            <div className="flex items-center gap-2 text-xs text-white/48">
              <MessageSquareText className="size-4 text-cyan-300/65" />
              <span>{selectedImages.length ? `${selectedImages.length}/4 images queued` : "Text and image input ready"}</span>
            </div>
          </div>

          <div className="chat-session-memory" aria-label="会话记忆状态">
            <div className="flex items-center justify-between gap-3">
              <span className="chat-state-label">SESSION MEMORY</span>
              <span className={`chat-session-memory__status chat-session-memory__status--${sessionMemoryStatus}`}>
                {SESSION_MEMORY_LABELS[sessionMemoryStatus]}
              </span>
            </div>
            <p className="mt-2 text-[11px] leading-5 text-white/35">仅此标签页 · 图片不保存</p>
            <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
              {SESSION_MEMORY_ANNOUNCEMENTS[sessionMemoryStatus]}
            </div>
          </div>

          <Button
            type="button"
            variant="ghost"
            onClick={requestNewSession}
            disabled={loading}
            className="min-h-11 w-full cursor-pointer justify-start rounded-xl border border-white/8 px-3 text-sm text-white/58 transition-colors duration-200 hover:bg-white/[0.07] hover:text-white disabled:cursor-not-allowed disabled:opacity-35"
          >
            <RotateCcw className="size-4" />
            新建会话
          </Button>

          <details className="chat-raw-details group">
            <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 rounded-xl px-3 text-[11px] uppercase tracking-[0.16em] text-white/38 transition-colors duration-200 hover:bg-white/[0.05] hover:text-white/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300/70 [&::-webkit-details-marker]:hidden">
              <span className="flex items-center gap-2"><ChevronDown className="size-3 transition-transform duration-200 group-open:rotate-180" />Raw response</span>
              <span className="text-[10px] normal-case tracking-normal text-white/24">JSON</span>
            </summary>
            <pre className="mt-2 max-h-64 overflow-auto rounded-xl border border-white/8 bg-black/20 p-3 font-mono text-[10px] leading-5 whitespace-pre-wrap break-words text-white/52">{raw ? pretty(raw) : "{\n  \"messages\": []\n}"}</pre>
          </details>
        </div>
      </aside>

      <section className="chat-conversation relative z-10 flex min-h-0 min-w-0 flex-col">
        <header className="flex items-center justify-between gap-3 border-b border-white/10 px-5 py-5 md:px-8">
          <div>
            <p className="chat-kicker">ARC / CONVERSATION STREAM</p>
            <h2 className="mt-2 text-base font-medium text-white/90">对话</h2>
          </div>
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="ghost"
              onClick={exportConversation}
              disabled={loading || !messages.length}
              className="min-h-11 min-w-11 cursor-pointer rounded-xl border border-white/8 px-3 text-xs text-white/56 transition-colors duration-200 hover:bg-white/[0.07] hover:text-white disabled:cursor-not-allowed disabled:opacity-35"
              aria-label="导出 Markdown"
            >
              <Download className="size-4" />
              <span className="ml-2 hidden sm:inline">导出 Markdown</span>
            </Button>
            <div className="flex items-center gap-2 rounded-full border border-white/8 bg-white/[0.035] px-3 py-1.5 text-[10px] uppercase tracking-[0.16em] text-white/38">
              <Sparkles className="size-3 text-cyan-200/70" />
              <span>{messages.length} events</span>
            </div>
          </div>
        </header>

        <div className="chat-conversation__scroll min-h-0 flex-1 overflow-y-auto px-5 py-6 md:px-8">
          {messages.length ? (
            <div className="mx-auto flex w-full max-w-3xl flex-col gap-7">
              {messages.map((message, index) => {
                const isUser = message.role === "user";
                const images = messageImages(message);
                const text = messageText(message);
                const hasContent = Boolean(text.trim() || images.length);
                const actionText = text || images.map((_, imageIndex) => `[Arc image ${imageIndex + 1}]`).join("\n");
                return (
                  <article key={`${message.role}-${index}`} className={`chat-message ${isUser ? "chat-message--user" : "chat-message--assistant"}`}>
                    <div className="chat-message__meta">
                      <span className="chat-message__role">{isUser ? "USER" : "ARC"}</span>
                      <span className="chat-message__kind">{isUser ? "REQUEST" : "RESPONSE"} · {String(index + 1).padStart(2, "0")}</span>
                    </div>
                    <div className="chat-message__bubble">
                      {images.length ? (
                        <div className="flex flex-wrap gap-2">
                          {images.map((url, imageIndex) => (
                            <img
                              key={`${index}-${imageIndex}`}
                              src={url}
                              alt={`${isUser ? "用户" : "Arc"}消息中的图片 ${imageIndex + 1}`}
                              className="chat-message__image"
                            />
                          ))}
                        </div>
                      ) : null}
                      {text ? (isUser ? <p className="chat-message__text">{text}</p> : <ChatMarkdown content={text} />) : null}
                    </div>
                    {!isUser && hasContent ? (
                      <ChatMessageActions
                        text={actionText}
                        canRegenerate={index === latestAssistantIndex}
                        disabled={loading}
                        onRegenerate={() => void regenerateAssistant(index)}
                      />
                    ) : null}
                  </article>
                );
              })}
            </div>
          ) : (
            <div className="chat-empty-state mx-auto flex h-full min-h-[360px] w-full max-w-2xl flex-col items-center justify-center text-center">
              <div className="chat-arc-mark" aria-hidden="true">
                <span className="chat-arc-mark__ring chat-arc-mark__ring--outer" />
                <span className="chat-arc-mark__ring chat-arc-mark__ring--inner" />
                <span className="chat-arc-mark__core" />
              </div>
              <p className="chat-kicker mt-7">ARC / STANDBY</p>
              <h3 className="mt-3 text-xl font-medium tracking-[-0.02em] text-white/90">准备好开始一次对话</h3>
              <p className="mt-2 max-w-md text-sm leading-6 text-white/44">输入问题、粘贴接口上下文，或附上图片。建议只会填入输入框，不会自动发送。</p>
              <div className="mt-6 flex w-full flex-wrap justify-center gap-2">
                {SUGGESTIONS.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    onClick={() => {
                      setInput(suggestion);
                      setInputEdited(true);
                      setSessionDirty(true);
                    }}
                    className="min-h-11 cursor-pointer rounded-full border border-white/10 bg-white/[0.04] px-4 text-xs text-white/62 transition-colors duration-200 hover:border-cyan-300/35 hover:bg-cyan-300/[0.08] hover:text-cyan-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300/70"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="chat-processing-slot mx-auto w-full max-w-3xl">
            {streamTelemetry.status !== "idle" ? (
              <div className="chat-processing-indicator">
                <span className="chat-processing-indicator__mark">
                  {loading ? <LoaderCircle className="size-4 animate-spin" /> : <Sparkles className="size-4" />}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="chat-processing-indicator__label">ARC / STREAM TELEMETRY</span>
                  <span className="mt-1 block truncate text-xs text-white/48">
                    {streamTelemetry.status === "prepared" ? "请求已准备，正在建立连接…" :
                      streamTelemetry.status === "linked" ? "连接已建立，等待首个 token…" :
                        streamTelemetry.status === "streaming" ? "正在接收增量内容…" :
                          streamTelemetry.status === "complete" ? "本轮回答已完成" :
                            streamTelemetry.status === "interrupted" ? "生成已中断，已保留当前内容" : "流式回答遇到错误"}
                  </span>
                  <span className="chat-processing-steps" aria-hidden="true">
                    <span className="chat-processing-step chat-processing-step--done">REQUEST PREPARED</span>
                    <span className={`chat-processing-step ${streamTelemetry.firstTokenMs !== null ? "chat-processing-step--done" : ""}`}>
                      {streamTelemetry.firstTokenMs !== null ? `LINKED / FIRST TOKEN · ${streamTelemetry.firstTokenMs}ms` : "LINKED / WAITING"}
                    </span>
                    <span className={`chat-processing-step ${streamTelemetry.status === "streaming" || streamTelemetry.status === "complete" ? "chat-processing-step--done" : ""}`}>
                      STREAMING · {streamTelemetry.chunkCount} chunks
                    </span>
                    <span className={`chat-processing-step ${streamTelemetry.status === "complete" ? "chat-processing-step--done" : streamTelemetry.status === "interrupted" ? "chat-processing-step--interrupted" : streamTelemetry.status === "fault" ? "chat-processing-step--fault" : ""}`}>
                      {streamTelemetry.status === "complete" ? `COMPLETE · ${streamTelemetry.elapsedMs}ms` :
                        streamTelemetry.status === "interrupted" ? "INTERRUPTED" :
                          streamTelemetry.status === "fault" ? "FAULT" : "IN PROGRESS"}
                    </span>
                  </span>
                </span>
              </div>
            ) : null}
            {streamTelemetry.status !== "idle" ? (
              <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
                {STREAM_STATUS_ANNOUNCEMENTS[streamTelemetry.status]}
              </div>
            ) : null}
          </div>
          <div ref={conversationEndRef} aria-hidden="true" />
        </div>

        <div className="chat-composer-wrap sticky bottom-0 z-20 border-t border-white/10 px-4 py-4 md:px-8 md:py-5">
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void sendChat();
            }}
            className="chat-composer-shell mx-auto w-full max-w-3xl"
          >
            <div className="flex items-center justify-between gap-3 px-3 pt-3 text-[10px] uppercase tracking-[0.18em] text-white/32">
              <span className="flex items-center gap-2"><MessageSquareText className="size-3.5 text-cyan-200/65" />Message composer</span>
              <span className="hidden normal-case tracking-normal text-white/24 sm:inline">Ctrl / ⌘ + Enter 发送</span>
            </div>
            <Textarea
              id="chat-input"
              value={input}
              onChange={(event) => {
                setInput(event.target.value);
                setInputEdited(true);
                setSessionDirty(true);
              }}
              onKeyDown={(event) => {
                if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
                  event.preventDefault();
                  void sendChat();
                }
              }}
              aria-label="输入消息"
              placeholder="输入消息，描述你要调试的请求…"
              disabled={loading}
              className="chat-composer-textarea min-h-28 resize-y border-0 bg-transparent px-3 py-3 text-sm leading-6 text-white shadow-none outline-none placeholder:text-white/25 focus-visible:ring-0 disabled:cursor-not-allowed disabled:opacity-60"
            />

            {selectedImages.length ? (
              <div className="grid grid-cols-2 gap-2 px-3 pb-3 sm:grid-cols-4">
                {selectedImages.map((image) => (
                  <div key={image.id} className="chat-image-preview group relative overflow-hidden rounded-lg border border-white/10 bg-white/[0.04]">
                    <img src={image.url} alt={`待发送图片：${image.name}`} className="aspect-square w-full object-cover" />
                    <button
                      type="button"
                      aria-label={`移除 ${image.name}`}
                      onClick={() => setSelectedImages((current) => current.filter((item) => item.id !== image.id))}
                      className="absolute top-1 right-1 grid size-11 cursor-pointer place-items-center rounded-lg bg-black/65 text-white/75 transition-colors duration-200 hover:bg-black/85 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300"
                    >
                      <X className="size-4" />
                    </button>
                    <span className="absolute inset-x-0 bottom-0 truncate bg-black/70 px-2 py-1 text-[10px] text-white/68">{image.name}</span>
                  </div>
                ))}
              </div>
            ) : null}

            {error ? <div role="alert" className="mx-3 mb-3 rounded-lg border border-rose-300/25 bg-rose-400/[0.08] px-3 py-2 text-xs leading-5 text-rose-100/85">{error}</div> : null}

            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-white/8 px-3 py-3">
              <label htmlFor="chat-images" className="flex min-h-11 cursor-pointer items-center gap-2 rounded-lg border border-white/8 px-3 text-xs text-white/54 transition-colors duration-200 hover:border-cyan-300/35 hover:bg-white/[0.05] hover:text-white/85 focus-within:ring-2 focus-within:ring-cyan-300/70">
                <ImagePlus className="size-4 text-cyan-200/72" />
                <span>添加图片</span>
                <span className="text-[10px] text-white/25">{selectedImages.length}/4</span>
              </label>
              <input id="chat-images" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple className="sr-only" onChange={(event) => {
                void handleImagesChange(event.target.files);
                event.currentTarget.value = "";
              }} />
              <div className="ml-auto flex items-center gap-3">
                <span className="hidden text-[10px] text-white/25 sm:inline">{input.trim() ? `${input.trim().length} chars` : "等待输入"}</span>
                {loading ? (
                  <Button
                    type="button"
                    variant="outline"
                    onClick={stopGeneration}
                    className="min-h-11 cursor-pointer rounded-xl border-rose-200/20 bg-rose-300/[0.06] px-3 text-xs text-rose-100/80 transition-colors duration-200 hover:bg-rose-300/[0.12] hover:text-rose-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-200/70"
                  >
                    <Square className="size-3.5 fill-current" />
                    <span className="ml-2">停止生成</span>
                  </Button>
                ) : null}
                <Button
                  type="submit"
                  disabled={loading || (!input.trim() && !selectedImages.length)}
                  className="min-h-11 min-w-11 cursor-pointer rounded-xl bg-cyan-100 px-4 text-sm font-medium text-[#071321] transition-colors duration-200 hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300 disabled:cursor-not-allowed disabled:opacity-30"
                >
                  {loading ? <LoaderCircle className="size-4 animate-spin" /> : <Send className="size-4" />}
                  <span className="ml-2 hidden sm:inline">发送</span>
                </Button>
              </div>
            </div>
          </form>
        </div>
      </section>
      <Dialog open={newSessionDialogOpen} onOpenChange={(open) => {
        if (!loading) setNewSessionDialogOpen(open);
      }}>
        <DialogContent showCloseButton={false} className="chat-new-session-dialog border-cyan-200/15 bg-[#07101f]/95 text-white shadow-[0_30px_100px_rgba(0,0,0,0.55)]">
          <DialogHeader>
            <p className="chat-kicker">ARC / SESSION CONTROL</p>
            <DialogTitle className="mt-1 text-lg text-white/92">新建会话？</DialogTitle>
            <DialogDescription className="text-sm leading-6 text-white/55">
              当前对话、未发送输入和已选图片会从此标签页清除。这个操作无法撤销。
            </DialogDescription>
          </DialogHeader>
          <div className="chat-new-session-dialog__signal" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          <DialogFooter className="pt-2">
            <DialogClose asChild>
              <Button type="button" variant="ghost" className="min-h-11 cursor-pointer rounded-xl border border-white/10 px-4 text-white/65 transition-colors duration-200 hover:bg-white/[0.08] hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300/70">
                取消
              </Button>
            </DialogClose>
            <Button type="button" onClick={resetSession} disabled={loading} className="min-h-11 cursor-pointer rounded-xl bg-cyan-100 px-4 text-sm font-medium text-[#071321] transition-colors duration-200 hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300/70">
              确认新建
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
