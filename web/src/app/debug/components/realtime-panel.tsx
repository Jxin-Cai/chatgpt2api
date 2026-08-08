"use client";

import { FormEvent, type CSSProperties, useCallback, useEffect, useRef, useState } from "react";
import { Activity, ChevronDown, Mic, MicOff, Phone, PhoneOff, Send, Wifi } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import webConfig from "@/constants/common-env";
import {
  RealtimeEvent,
  RealtimeSignalingError,
  RealtimeWebRTCConnection,
  type RealtimeConnectionQuality,
} from "@/lib/realtime-webrtc";
import {
  chatTranscriptUpdateFromEvent,
  transcriptUpdateFromEvent,
  type ChatTranscriptCursor,
  type TranscriptUpdate,
} from "@/lib/realtime-transcript";
import { getStoredAuthSession } from "@/store/auth";

type LogEntry = {
  id: number;
  time: string;
  dir: "send" | "recv" | "info" | "error";
  text: string;
};

type TranscriptEntry = {
  id: number;
  sourceId: string;
  role: "user" | "assistant";
  text: string;
  final: boolean;
};

type LivePhase = "offline" | "connecting" | "listening" | "thinking" | "speaking" | "muted" | "error";

const VOICES = [
  { value: "ember", label: "Ember · 自信乐观", preview: "/audio/voice-previews/ember.m4a" },
  { value: "glimmer", label: "Sol · 聪慧随性", preview: "/audio/voice-previews/glimmer.m4a" },
  { value: "breeze", label: "Breeze · 活泼认真", preview: "/audio/voice-previews/breeze.m4a" },
  { value: "cove", label: "Cove · 沉稳直率", preview: "/audio/voice-previews/cove.m4a" },
  { value: "juniper", label: "Juniper · 开放豁达", preview: "/audio/voice-previews/juniper.m4a" },
  { value: "maple", label: "Maple · 开朗直率", preview: "/audio/voice-previews/maple.m4a" },
  { value: "orbit", label: "Spruce · 冷静坚定", preview: "/audio/voice-previews/orbit.m4a" },
  { value: "vale", label: "Vale · 聪颖好奇", preview: "/audio/voice-previews/vale.m4a" },
  { value: "fathom", label: "Arbor · 随和多才", preview: "/audio/voice-previews/fathom.m4a" },
] as const;

const MAX_QUOTA_RETRIES = 2;
const MAX_NETWORK_RETRIES = 3;
const MAX_TRANSCRIPT_ENTRIES = 100;
const MAX_TRANSCRIPT_CHARS = 20_000;

const PHASE_COPY: Record<LivePhase, { title: string; detail: string }> = {
  offline: { title: "准备好开始了吗？", detail: "选择声音，然后开启一段自然对话" },
  connecting: { title: "正在建立声场", detail: "连接麦克风与实时模型…" },
  listening: { title: "我在听", detail: "直接说话，不需要按住按钮" },
  thinking: { title: "正在思考", detail: "你的话已经送达" },
  speaking: { title: "正在回答", detail: "你随时可以开口打断" },
  muted: { title: "麦克风已静音", detail: "点击下方按钮继续说话" },
  error: { title: "连接遇到问题", detail: "查看事件记录后重新开始" },
};

const PHASE_LABEL: Record<LivePhase, string> = {
  offline: "STANDBY",
  connecting: "LINKING",
  listening: "LISTENING",
  thinking: "PROCESSING",
  speaking: "OUTPUT",
  muted: "MUTED",
  error: "FAULT",
};

function timestamp() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false, fractionalSecondDigits: 2 });
}

function rmsLevel(analyser: AnalyserNode | null, samples: Uint8Array): number {
  if (!analyser) return 0;
  analyser.getByteTimeDomainData(samples);
  let sum = 0;
  for (const sample of samples) {
    const value = (sample - 128) / 128;
    sum += value * value;
  }
  return Math.min(1, Math.sqrt(sum / samples.length) * 4.2);
}

function quotaRecoveryFromEvent(data: RealtimeEvent): {
  restoreAt?: string;
  retryAfterSeconds?: number;
} {
  let restoreAt: string | undefined;
  let retryAfterSeconds: number | undefined;
  const visit = (value: unknown, depth: number) => {
    if (depth > 5 || !value || typeof value !== "object") return;
    for (const [key, nested] of Object.entries(value as Record<string, unknown>)) {
      const normalized = key.toLowerCase();
      if (
        !restoreAt
        && typeof nested === "string"
        && ["restore_at", "reset_at", "resets_at"].includes(normalized)
      ) {
        restoreAt = nested;
      } else if (
        retryAfterSeconds === undefined
        && typeof nested === "number"
        && ["retry_after_seconds", "reset_after_seconds", "seconds_until_reset"].includes(normalized)
      ) {
        retryAfterSeconds = Math.max(1, Math.round(nested));
      } else {
        visit(nested, depth + 1);
      }
    }
  };
  visit(data, 0);
  return { restoreAt, retryAfterSeconds };
}

export function RealtimePanel() {
  const [voice, setVoice] = useState("ember");
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [micActive, setMicActive] = useState(false);
  const [phase, setPhase] = useState<LivePhase>("offline");
  const [statusDetail, setStatusDetail] = useState(PHASE_COPY.offline.detail);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [textInput, setTextInput] = useState("");
  const [quality, setQuality] = useState<RealtimeConnectionQuality | null>(null);

  const realtimeRef = useRef<RealtimeWebRTCConnection | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const micAnalyserRef = useRef<AnalyserNode | null>(null);
  const remoteAnalyserRef = useRef<AnalyserNode | null>(null);
  const remoteSourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const meterFrameRef = useRef(0);
  const orbRef = useRef<HTMLDivElement | null>(null);
  const remoteStreamRef = useRef<MediaStream | null>(null);
  const smoothedLevelRef = useRef(0);
  const currentPhaseRef = useRef<LivePhase>("offline");
  const micActiveRef = useRef(false);
  const terminalErrorRef = useRef("");
  const quotaRetryCountRef = useRef(0);
  const quotaRetryScheduledRef = useRef(false);
  const quotaRetryTimerRef = useRef<number | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectCountRef = useRef(0);
  const disconnectRequestedRef = useRef(false);
  const attemptIdRef = useRef("");
  const conversationIdRef = useRef("");
  const parentMessageIdRef = useRef("");
  const connectRef = useRef<(retry?: boolean) => Promise<void>>(async () => {});
  const logIdRef = useRef(0);
  const transcriptIdRef = useRef(0);
  const transcriptTurnRef = useRef(0);
  const activeTurnRef = useRef<Record<"user" | "assistant", string | null>>({ user: null, assistant: null });
  const chatTranscriptCursorRef = useRef<ChatTranscriptCursor | null>(null);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);
  const pendingTranscriptUpdatesRef = useRef<TranscriptUpdate[]>([]);
  const transcriptFlushFrameRef = useRef(0);
  const previewAudioRef = useRef<HTMLAudioElement | null>(null);
  const previewRequestIdRef = useRef(0);
  const previewVoiceRef = useRef<string | null>(null);
  const selectOpenRef = useRef(false);
  const selectInteractionRef = useRef<"pointer" | "keyboard">("pointer");

  const stopVoicePreview = useCallback(() => {
    previewRequestIdRef.current += 1;
    previewVoiceRef.current = null;
    const audio = previewAudioRef.current;
    if (!audio) return;
    audio.pause();
    try {
      audio.currentTime = 0;
    } catch {
      // The media element can still be loading when the selector closes.
    }
  }, []);

  const playVoicePreview = useCallback((item: (typeof VOICES)[number]) => {
    if (!selectOpenRef.current || connected || connecting) return;
    let audio = previewAudioRef.current;
    if (!audio && typeof Audio !== "undefined") {
      audio = new Audio();
      audio.preload = "auto";
      previewAudioRef.current = audio;
    }
    if (!audio) return;
    audio.pause();
    try {
      audio.currentTime = 0;
    } catch {
      // Ignore a seek while the previous preview is still being replaced.
    }
    if (audio.getAttribute("src") !== item.preview) {
      audio.src = item.preview;
      audio.load();
    }
    const requestId = ++previewRequestIdRef.current;
    previewVoiceRef.current = item.value;
    try {
      const playback = audio.play();
      void playback.catch(() => {
        if (requestId !== previewRequestIdRef.current) return;
        audio.pause();
        try {
          audio.currentTime = 0;
        } catch {
          // Ignore a seek after an autoplay rejection.
        }
      });
    } catch {
      // Some browsers can throw synchronously when playback is disallowed.
    }
  }, [connected, connecting]);

  useEffect(() => {
    currentPhaseRef.current = phase;
  }, [phase]);

  useEffect(() => {
    micActiveRef.current = micActive;
  }, [micActive]);

  useEffect(() => {
    if (connected || connecting) stopVoicePreview();
  }, [connected, connecting, stopVoicePreview]);

  const addLog = useCallback((dir: LogEntry["dir"], text: string) => {
    setLogs((previous) => [
      ...previous.slice(-199),
      { id: logIdRef.current++, time: timestamp(), dir, text },
    ]);
  }, []);

  const startTurn = useCallback((role: "user" | "assistant") => {
    const sourceId = `${role}-${++transcriptTurnRef.current}`;
    activeTurnRef.current[role] = sourceId;
    setTranscript((previous) => previous.map((entry) =>
      entry.role === role && !entry.final ? { ...entry, final: true } : entry,
    ));
    return sourceId;
  }, []);

  const applyTranscriptUpdate = useCallback((update: TranscriptUpdate) => {
    if (!update.text && !update.final) return;
    pendingTranscriptUpdatesRef.current.push(update);
    if (transcriptFlushFrameRef.current) return;
    transcriptFlushFrameRef.current = requestAnimationFrame(() => {
      transcriptFlushFrameRef.current = 0;
      const updates = pendingTranscriptUpdatesRef.current.splice(0);
      setTranscript((previous) => {
        let next = previous;
        for (const pending of updates) {
          let sourceId = pending.sourceId || activeTurnRef.current[pending.role];
          let index = sourceId ? next.findIndex((entry) => entry.sourceId === sourceId) : -1;
          if (index < 0) {
            for (let candidate = next.length - 1; candidate >= 0; candidate -= 1) {
              if (next[candidate].role === pending.role && !next[candidate].final) {
                index = candidate;
                break;
              }
            }
          }
          if (index < 0) {
            sourceId = sourceId || `${pending.role}-${++transcriptTurnRef.current}`;
            activeTurnRef.current[pending.role] = sourceId;
            next = [
              ...next.map((entry) => !entry.final ? { ...entry, final: true } : entry),
              {
                id: transcriptIdRef.current++,
                sourceId,
                role: pending.role,
                text: pending.text.slice(-MAX_TRANSCRIPT_CHARS),
                final: pending.final,
              },
            ];
            continue;
          }

          const current = next[index];
          const mergedText = pending.mode === "append"
            ? current.text + pending.text
            : pending.text || current.text;
          next = [...next];
          next[index] = {
            ...current,
            sourceId: sourceId || current.sourceId,
            text: mergedText.slice(-MAX_TRANSCRIPT_CHARS),
            final: pending.final,
          };
        }
        return next.slice(-MAX_TRANSCRIPT_ENTRIES);
      });
    });
  }, []);

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [transcript]);

  const attachRemoteMeter = useCallback((stream: MediaStream) => {
    remoteStreamRef.current = stream;
    const context = audioContextRef.current;
    if (!context) return;
    remoteSourceRef.current?.disconnect();
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.72;
    source.connect(analyser);
    remoteSourceRef.current = source;
    remoteAnalyserRef.current = analyser;
  }, []);

  const stopMetering = useCallback(() => {
    if (meterFrameRef.current) cancelAnimationFrame(meterFrameRef.current);
    meterFrameRef.current = 0;
    remoteSourceRef.current?.disconnect();
    remoteSourceRef.current = null;
    micAnalyserRef.current = null;
    remoteAnalyserRef.current = null;
    remoteStreamRef.current = null;
    if (audioContextRef.current) void audioContextRef.current.close();
    audioContextRef.current = null;
    smoothedLevelRef.current = 0;
    orbRef.current?.style.setProperty("--voice-level", "0");
  }, []);

  const startMetering = useCallback((microphone: MediaStream) => {
    stopMetering();
    const context = new AudioContext();
    audioContextRef.current = context;
    void context.resume();
    const source = context.createMediaStreamSource(microphone);
    const analyser = context.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.72;
    source.connect(analyser);
    micAnalyserRef.current = analyser;
    if (remoteStreamRef.current) attachRemoteMeter(remoteStreamRef.current);

    const micSamples = new Uint8Array(analyser.frequencyBinCount);
    const remoteSamples = new Uint8Array(analyser.frequencyBinCount);
    const draw = () => {
      const activeAnalyser = currentPhaseRef.current === "speaking"
        ? remoteAnalyserRef.current
        : micAnalyserRef.current;
      const target = rmsLevel(
        activeAnalyser,
        activeAnalyser === remoteAnalyserRef.current ? remoteSamples : micSamples,
      );
      const previous = smoothedLevelRef.current;
      const next = target > previous ? previous * 0.46 + target * 0.54 : previous * 0.86 + target * 0.14;
      smoothedLevelRef.current = next;
      orbRef.current?.style.setProperty("--voice-level", next.toFixed(3));
      meterFrameRef.current = requestAnimationFrame(draw);
    };
    draw();
  }, [attachRemoteMeter, stopMetering]);

  const handleRealtimeEvent = useCallback((data: RealtimeEvent) => {
    const type = data.type || "unknown";
    const chatDelta = chatTranscriptUpdateFromEvent(data, chatTranscriptCursorRef.current);
    if (chatDelta) chatTranscriptCursorRef.current = chatDelta.cursor;
    const transcriptUpdate = chatDelta?.update || transcriptUpdateFromEvent(data);
    if (transcriptUpdate) applyTranscriptUpdate(transcriptUpdate);

    if (type === "input_audio_buffer.speech_started") {
      startTurn("user");
      setPhase("listening");
      setStatusDetail("正在捕捉你的声音");
    } else if (type === "input_audio_buffer.speech_stopped") {
      setPhase("thinking");
      setStatusDetail("正在理解你刚才说的话");
    } else if (type === "state_update") {
      const upstreamState = typeof data.new_state === "string" ? data.new_state : "unknown";
      if (upstreamState === "speaking") {
        startTurn("assistant");
        setPhase("speaking");
      } else if (upstreamState === "thinking") {
        setPhase("thinking");
      } else if (upstreamState === "listening" || upstreamState === "idle") {
        setPhase(micActiveRef.current ? "listening" : "muted");
      }
      setStatusDetail(PHASE_COPY[upstreamState as LivePhase]?.detail || `上游状态：${upstreamState}`);
      addLog("recv", `state_update: ${String(data.previous_state || "?")} → ${upstreamState}`);
    } else if (type === "startup_telemetry") {
      const conversationId = data.conversation_id as string | undefined;
      if (conversationId) conversationIdRef.current = conversationId;
      addLog("recv", `startup_telemetry: ${JSON.stringify(data).substring(0, 180)}`);
    } else if (type === "usage_update" || type === "goodbye") {
      const rateLimit = data.rate_limit_message as Record<string, unknown> | undefined;
      const exceeded = rateLimit?.exceed_limit_message as Record<string, unknown> | undefined;
      const quotaEnded = type === "goodbye" && data.reason === "cap_reached";
      if ((exceeded || quotaEnded) && quotaRetryCountRef.current < MAX_QUOTA_RETRIES && !quotaRetryScheduledRef.current) {
        quotaRetryScheduledRef.current = true;
        quotaRetryCountRef.current += 1;
        const recovery = quotaRecoveryFromEvent(data);
        const quotaReport = realtimeRef.current?.reportQuotaExhausted({
          reason: quotaEnded ? "cap_reached" : "usage_limit",
          ...recovery,
        });
        addLog("info", `语音额度不足，自动切换账号 (${quotaRetryCountRef.current}/${MAX_QUOTA_RETRIES})`);
        setPhase("connecting");
        setStatusDetail("当前账号额度不足，正在无感切换");
        quotaRetryTimerRef.current = window.setTimeout(async () => {
          quotaRetryTimerRef.current = null;
          await quotaReport;
          void connectRef.current(true);
        }, 120);
      } else if ((exceeded || quotaEnded) && quotaRetryCountRef.current >= MAX_QUOTA_RETRIES) {
        setPhase("error");
        setStatusDetail("所有可用账号的实时语音额度均已耗尽");
        addLog("error", "所有实时语音账号均已达到上游额度限制");
      }
    } else if (type === "error") {
      const error = data.error as Record<string, unknown> | undefined;
      const message = String(error?.message || "未知错误");
      terminalErrorRef.current = message;
      setPhase("error");
      setStatusDetail(message);
      addLog("error", `ERROR: ${JSON.stringify(error)}`);
    } else if (!transcriptUpdate) {
      addLog("recv", `${type}: ${JSON.stringify(data).substring(0, 180)}`);
    }
  }, [addLog, applyTranscriptUpdate, startTurn]);

  const secureContext = typeof window !== "undefined" && window.isSecureContext;
  const canUseMic = secureContext && !!navigator.mediaDevices?.getUserMedia;

  const startMic = useCallback(() => {
    realtimeRef.current?.setMicrophoneEnabled(true);
    micActiveRef.current = true;
    setMicActive(true);
    setPhase("listening");
    setStatusDetail(PHASE_COPY.listening.detail);
    addLog("info", "麦克风已开启");
  }, [addLog]);

  const stopMic = useCallback(() => {
    realtimeRef.current?.setMicrophoneEnabled(false);
    micActiveRef.current = false;
    setMicActive(false);
    setPhase("muted");
    setStatusDetail(PHASE_COPY.muted.detail);
    addLog("info", "麦克风已静音");
  }, [addLog]);

  const sendTextMessage = useCallback(async (text: string) => {
    const normalized = text.trim();
    if (!realtimeRef.current || !normalized) return;
    const attemptId = attemptIdRef.current;
    const conversationId = conversationIdRef.current;

    applyTranscriptUpdate({
      role: "user",
      text: normalized,
      mode: "replace",
      final: true,
      sourceId: startTurn("user"),
    });
    setPhase("thinking");
    setStatusDetail(PHASE_COPY.thinking.detail);
    addLog("send", `text: ${normalized.substring(0, 60)}`);
    setTextInput("");

    if (!conversationId || !attemptId) {
      addLog("warn", "无 conversation_id，文本无法发送");
      setPhase(micActiveRef.current ? "listening" : "muted");
      setStatusDetail("语音会话尚未就绪，请稍后重试");
      return;
    }

    const session = await getStoredAuthSession();
    if (!session) return;
    const signalingBase = webConfig.apiUrl
      ? new URL("/v1/realtime/sessions", webConfig.apiUrl).toString()
      : "/v1/realtime/sessions";
    try {
      const response = await fetch(`${signalingBase}/${encodeURIComponent(attemptId)}/text`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${session.key}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          text: normalized,
          conversation_id: conversationId,
          parent_message_id: parentMessageIdRef.current || undefined,
        }),
      });
      if (!response.ok) {
        const errText = await response.text();
        addLog("error", `text inject failed: HTTP ${response.status} ${errText.substring(0, 100)}`);
        setPhase(micActiveRef.current ? "listening" : "muted");
        setStatusDetail("文字发送失败，请重试");
        return;
      }
      const reader = response.body?.getReader();
      if (!reader) return;
      const decoder = new TextDecoder();
      let assistantText = "";
      const sourceId = startTurn("assistant");
      setPhase("speaking");
      setStatusDetail(PHASE_COPY.speaking.detail);

      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const raw = line.slice(6);
          if (raw === "[DONE]") continue;
          try {
            const payload = JSON.parse(raw);
            const message = payload?.message;
            if (message?.author?.role === "assistant" && message?.content?.parts) {
              const newText = message.content.parts.join("");
              if (newText && newText !== assistantText) {
                assistantText = newText;
                applyTranscriptUpdate({ role: "assistant", text: assistantText, mode: "replace", final: false, sourceId });
              }
              if (message.id) parentMessageIdRef.current = message.id;
            }
            if (payload?.type === "error") {
              addLog("error", `text response error: ${JSON.stringify(payload.error).substring(0, 120)}`);
            }
          } catch { /* skip non-json lines */ }
        }
      }
      if (assistantText) {
        applyTranscriptUpdate({ role: "assistant", text: assistantText, mode: "replace", final: true, sourceId });
      }
      setPhase(micActiveRef.current ? "listening" : "muted");
      setStatusDetail(PHASE_COPY[micActiveRef.current ? "listening" : "muted"].detail);
    } catch (err) {
      addLog("error", `text inject error: ${err instanceof Error ? err.message : String(err)}`);
      setPhase(micActiveRef.current ? "listening" : "muted");
    }
  }, [addLog, applyTranscriptUpdate, startTurn]);

  const disconnect = useCallback(() => {
    disconnectRequestedRef.current = true;
    if (quotaRetryTimerRef.current !== null) window.clearTimeout(quotaRetryTimerRef.current);
    if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
    quotaRetryTimerRef.current = null;
    reconnectTimerRef.current = null;
    quotaRetryScheduledRef.current = false;
    reconnectCountRef.current = 0;
    attemptIdRef.current = "";
    conversationIdRef.current = "";
    parentMessageIdRef.current = "";
    realtimeRef.current?.close();
    realtimeRef.current = null;
    stopMetering();
    micActiveRef.current = false;
    setConnected(false);
    setConnecting(false);
    setMicActive(false);
    setQuality(null);
    setPhase("offline");
    setStatusDetail(PHASE_COPY.offline.detail);
  }, [stopMetering]);

  const scheduleNetworkReconnect = useCallback((reason: string, minimumDelayMs = 0) => {
    if (disconnectRequestedRef.current || reconnectTimerRef.current !== null) return;
    if (reconnectCountRef.current >= MAX_NETWORK_RETRIES) {
      setPhase("error");
      setStatusDetail("网络恢复失败，请手动重新连接");
      return;
    }
    reconnectCountRef.current += 1;
    const delay = Math.max(
      minimumDelayMs,
      Math.min(6_000, 750 * (2 ** (reconnectCountRef.current - 1))) + Math.round(Math.random() * 250),
    );
    setPhase("connecting");
    setStatusDetail(`${reason}，${Math.ceil(delay / 1000)} 秒后重连`);
    addLog("info", `${reason}，自动重连 ${reconnectCountRef.current}/${MAX_NETWORK_RETRIES}`);
    reconnectTimerRef.current = window.setTimeout(() => {
      reconnectTimerRef.current = null;
      void connectRef.current(false);
    }, delay);
  }, [addLog]);

  const connect = useCallback(async (retry = false) => {
    stopVoicePreview();
    const session = await getStoredAuthSession();
    if (!session) {
      addLog("error", "未登录，请先登录");
      return;
    }
    disconnectRequestedRef.current = false;
    realtimeRef.current?.close();
    stopMetering();
    setConnected(false);
    micActiveRef.current = false;
    setMicActive(false);
    setConnecting(true);
    setPhase("connecting");
    setStatusDetail(retry ? "正在选择下一个可用账号" : PHASE_COPY.connecting.detail);
    terminalErrorRef.current = "";
    quotaRetryScheduledRef.current = false;
    if (!retry) {
      quotaRetryCountRef.current = 0;
      attemptIdRef.current = "";
    }

    const signalingUrl = webConfig.apiUrl
      ? new URL("/v1/realtime/sessions", webConfig.apiUrl).toString()
      : "/v1/realtime/sessions";
    const connection = new RealtimeWebRTCConnection({
      onEvent: handleRealtimeEvent,
      onRemoteStream: attachRemoteMeter,
      onConnectionState: (state) => {
        addLog("info", `WebRTC: ${state}`);
        if (state === "connected" && reconnectTimerRef.current !== null) {
          window.clearTimeout(reconnectTimerRef.current);
          reconnectTimerRef.current = null;
          reconnectCountRef.current = 0;
          setConnected(true);
          setPhase(micActiveRef.current ? "listening" : "muted");
          setStatusDetail("网络连接已恢复");
        } else if (state === "failed") {
          setConnected(false);
          scheduleNetworkReconnect("WebRTC 连接中断");
        } else if (state === "disconnected") {
          setConnected(false);
          scheduleNetworkReconnect("网络连接不稳定");
        }
      },
      onQuality: setQuality,
      onMicrophoneEnded: () => scheduleNetworkReconnect("麦克风设备已断开"),
      onMicrophoneState: (state, settings) => {
        addLog("info", `麦克风: ${state} · ${settings.sampleRate || "?"}Hz · ${settings.channelCount || "?"}ch`);
        if (state === "muted") setStatusDetail("麦克风没有提供音频，请检查系统输入设备和权限");
      },
    });
    realtimeRef.current = connection;

    try {
      addLog("info", retry ? "正在切换账号并重连…" : "正在建立端到端 WebRTC…");
      const result = await connection.connect({
        authorization: `Bearer ${session.key}`,
        voice,
        signalingUrl,
        attemptId: retry ? attemptIdRef.current : undefined,
      });
      if (realtimeRef.current !== connection) return;
      setConnected(true);
      reconnectCountRef.current = 0;
      attemptIdRef.current = result.attemptId;
      setConnecting(false);
      micActiveRef.current = true;
      setMicActive(true);
      setPhase("listening");
      setStatusDetail(PHASE_COPY.listening.detail);
      addLog("recv", `session.created (${result.location}) request=${result.requestId || "-"}`);
      const microphone = connection.getMicrophoneStream();
      if (microphone) startMetering(microphone);
      const remote = connection.getRemoteStream();
      if (remote) attachRemoteMeter(remote);
    } catch (error) {
      if (realtimeRef.current !== connection) return;
      connection.close();
      realtimeRef.current = null;
      setConnected(false);
      setConnecting(false);
      micActiveRef.current = false;
      setMicActive(false);
      setPhase("error");
      const message = error instanceof Error ? error.message : String(error);
      terminalErrorRef.current = message;
      setStatusDetail(message);
      addLog("error", message);
      if (error instanceof RealtimeSignalingError && error.retryable) {
        if (error.attemptId) attemptIdRef.current = error.attemptId;
        scheduleNetworkReconnect(
          `信令暂时不可用${error.status ? ` (HTTP ${error.status})` : ""}`,
          error.retryAfterMs,
        );
      }
    }
  }, [addLog, attachRemoteMeter, handleRealtimeEvent, scheduleNetworkReconnect, startMetering, stopMetering, stopVoicePreview, voice]);

  useEffect(() => {
    connectRef.current = connect;
    const handleOnline = () => {
      if (!realtimeRef.current && !disconnectRequestedRef.current) {
        scheduleNetworkReconnect("网络已恢复");
      }
    };
    const handlePageHide = () => realtimeRef.current?.close();
    window.addEventListener("online", handleOnline);
    window.addEventListener("pagehide", handlePageHide);
    return () => {
      if (quotaRetryTimerRef.current !== null) window.clearTimeout(quotaRetryTimerRef.current);
      if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
      if (transcriptFlushFrameRef.current) cancelAnimationFrame(transcriptFlushFrameRef.current);
      window.removeEventListener("online", handleOnline);
      window.removeEventListener("pagehide", handlePageHide);
      realtimeRef.current?.close();
      stopMetering();
      stopVoicePreview();
    };
  }, [connect, scheduleNetworkReconnect, stopMetering, stopVoicePreview]);

  const submitText = (event: FormEvent) => {
    event.preventDefault();
    void sendTextMessage(textInput);
  };

  const phaseCopy = PHASE_COPY[phase];
  const qualityLabel = !quality || (quality.roundTripTimeMs === undefined && quality.jitterMs === undefined)
    ? "检测中"
    : (quality.roundTripTimeMs || 0) > 350 || (quality.jitterMs || 0) > 80
      ? "网络较差"
      : (quality.roundTripTimeMs || 0) > 180 || (quality.jitterMs || 0) > 40
        ? "网络一般"
        : "网络良好";

  return (
    <section className="realtime-panel relative min-h-[680px] overflow-hidden rounded-[30px] border border-white/10 text-white shadow-[0_30px_100px_rgba(12,18,48,0.24)]">
      <div className="realtime-panel__scrim pointer-events-none absolute inset-0" />
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_35%_15%,rgba(75,98,255,0.15),transparent_35%),radial-gradient(circle_at_70%_78%,rgba(57,211,255,0.08),transparent_32%)]" />

      <header className="relative z-10 flex flex-wrap items-center gap-3 border-b border-white/10 px-5 py-4 md:px-7">
        <div className="flex min-w-0 items-center gap-3">
          <span className={`realtime-status-dot size-2 rounded-full ${connected ? "bg-cyan-300 shadow-[0_0_16px_#67e8f9]" : connecting ? "bg-violet-300" : phase === "error" ? "bg-rose-300" : "bg-white/25"}`} />
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-[0.24em] text-white/38">Arc / realtime voice core</p>
            <p className="truncate text-sm font-medium text-white/88">{phaseCopy.title}</p>
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <span className={`realtime-phase-label hidden rounded-full border px-2.5 py-1 text-[10px] font-semibold tracking-[0.18em] sm:inline-flex realtime-phase-label--${phase}`}>
            {PHASE_LABEL[phase]}
          </span>
          {connected && (
            <span
              className="hidden items-center gap-1.5 rounded-full border border-white/8 bg-white/5 px-2.5 py-1 text-[10px] text-white/42 sm:flex"
              title={quality ? `RTT ${quality.roundTripTimeMs ?? "-"}ms · 抖动 ${quality.jitterMs ?? "-"}ms · ${quality.candidateType ?? "unknown"}` : "正在采集 WebRTC 质量"}
            >
              <Wifi className="size-3 text-cyan-300/75" />
              {qualityLabel}
            </span>
          )}
          <Select
            value={voice}
            onValueChange={setVoice}
            onOpenChange={(open) => {
              selectOpenRef.current = open;
              if (!open) stopVoicePreview();
            }}
            disabled={connected || connecting}
          >
            <SelectTrigger
              aria-label="选择声音"
              onPointerDownCapture={(event) => {
                if (event.button === 0) selectInteractionRef.current = "pointer";
              }}
              onKeyDownCapture={(event) => {
                if (["Enter", " ", "ArrowDown", "ArrowUp", "Home", "End", "F4"].includes(event.key)) {
                  selectInteractionRef.current = "keyboard";
                }
              }}
              className="h-11 w-[190px] max-w-[calc(100vw-2.5rem)] border-white/10 bg-white/6 text-xs text-white shadow-none transition-colors duration-200 hover:bg-white/10"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent
              onKeyDownCapture={(event) => {
                if (event.key !== "Escape") {
                  selectInteractionRef.current = "keyboard";
                }
              }}
            >
              {VOICES.map((item) => (
                <SelectItem
                  key={item.value}
                  value={item.value}
                  title="悬停或聚焦试听"
                  onPointerEnter={() => playVoicePreview(item)}
                  onPointerLeave={stopVoicePreview}
                  onFocus={() => {
                    if (selectInteractionRef.current === "keyboard") playVoicePreview(item);
                  }}
                >
                  {item.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {connected && (
            <Button onClick={disconnect} size="sm" variant="ghost" className="min-h-11 rounded-full text-white/60 transition-colors duration-200 hover:bg-white/10 hover:text-white">
              <PhoneOff className="size-4" />
              <span className="hidden sm:inline">结束</span>
            </Button>
          )}
        </div>
      </header>

      <div className="relative z-10 grid min-h-[610px] lg:grid-cols-[minmax(0,1fr)_390px]">
        <div className="realtime-core-stage relative flex min-h-[560px] flex-col items-center justify-center overflow-hidden border-b border-white/10 px-5 py-12 lg:border-r lg:border-b-0">
          <div className="realtime-core-grid pointer-events-none absolute inset-0" />
          <div className="realtime-core-axis realtime-core-axis--x pointer-events-none absolute left-[8%] right-[8%] top-1/2" />
          <div className="realtime-core-axis realtime-core-axis--y pointer-events-none absolute bottom-[10%] top-[10%] left-1/2" />
          <div className="realtime-core-hud pointer-events-none absolute left-5 right-5 top-5 flex items-center justify-between text-[9px] font-semibold uppercase tracking-[0.22em] text-white/28 md:left-8 md:right-8">
            <span>Core / ARC-01</span>
            <span>{connected ? "LINK ACTIVE" : connecting ? "CALIBRATING" : "STANDBY"}</span>
          </div>

          {!connected && !connecting ? (
            <button
              type="button"
              onClick={() => void connect()}
              disabled={!canUseMic}
              aria-label="开始实时对话"
              className="realtime-start-core group relative grid size-56 place-items-center rounded-full outline-none transition-transform duration-250 hover:scale-[1.02] focus-visible:ring-2 focus-visible:ring-cyan-300 focus-visible:ring-offset-4 focus-visible:ring-offset-[#070a16] disabled:cursor-not-allowed disabled:opacity-45 md:size-64"
            >
              <span className="realtime-start-core__ring realtime-start-core__ring--outer absolute inset-0 rounded-full" />
              <span className="realtime-start-core__ring realtime-start-core__ring--inner absolute inset-[10%] rounded-full" />
              <span className="realtime-start-core__ticks absolute inset-[4%] rounded-full" />
              <span className="realtime-start-core__center relative flex flex-col items-center gap-3">
                <span className="grid size-14 place-items-center rounded-full border border-cyan-100/80 bg-cyan-50 text-[#081021] shadow-[0_0_40px_rgba(111,222,255,.38)] transition-transform duration-250 group-hover:scale-105">
                  <Phone className="size-6" />
                </span>
                <span className="text-sm font-medium tracking-wide text-white/90">开始实时对话</span>
                <span className="text-[9px] font-semibold uppercase tracking-[0.24em] text-cyan-200/50">Initialize voice link</span>
              </span>
            </button>
          ) : (
            <div className="flex flex-col items-center" aria-live="polite">
              <div
                ref={orbRef}
                data-phase={phase}
                role="img"
                className="realtime-orb-shell relative size-56 md:size-64"
                style={{ "--voice-level": 0 } as CSSProperties}
                aria-label={`${PHASE_LABEL[phase]} · ${phaseCopy.title}`}
              >
                <div className="realtime-orb-aura absolute -inset-12 rounded-full" aria-hidden="true" />
                <div className="realtime-core-ring realtime-core-ring--outer absolute inset-[-4%] rounded-full" aria-hidden="true" />
                <div className="realtime-core-ring realtime-core-ring--middle absolute inset-[5%] rounded-full" aria-hidden="true" />
                <div className="realtime-core-ring realtime-core-ring--inner absolute inset-[13%] rounded-full" aria-hidden="true" />
                <div className="realtime-core-ticks absolute inset-[-1%] rounded-full" aria-hidden="true" />
                <div className="realtime-core-radial absolute inset-[8%] rounded-full" aria-hidden="true" />
                <div className="realtime-core-scan absolute inset-[18%] rounded-full" aria-hidden="true" />
                <div className="realtime-orb absolute inset-[21%] overflow-hidden rounded-full" aria-hidden="true">
                  <span className="realtime-orb-light absolute inset-[9%] rounded-full" />
                  <span className="realtime-orb-glint absolute left-[24%] top-[20%] size-[23%] rounded-full" />
                  <span className="realtime-core-pupil absolute inset-[31%] rounded-full" />
                </div>
                <div className="realtime-core-readout pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
                  <span className="text-[10px] font-semibold uppercase tracking-[0.28em] text-white/80">{PHASE_LABEL[phase]}</span>
                  <span className="mt-1 text-[8px] font-medium uppercase tracking-[0.2em] text-white/42">AUDIO REACTIVE</span>
                </div>
              </div>
              <p className="mt-9 text-[10px] font-semibold uppercase tracking-[0.28em] text-white/42">{PHASE_LABEL[phase]} · {connected ? "LINK ACTIVE" : "CALIBRATING"}</p>
              <h2 className="mt-2 text-2xl font-medium tracking-[-0.025em] text-white md:text-3xl">{phaseCopy.title}</h2>
              <p className="mt-2 text-sm text-white/48">{statusDetail}</p>
            </div>
          )}

          <div className="relative mt-10 flex min-h-10 items-center justify-center gap-3">
            {connecting && (
              <span className="flex items-center gap-2 text-xs text-white/48">
                <Activity className="size-4 text-violet-200" />
                正在连接
              </span>
            )}
            {connected && (
              <>
                <button
                  type="button"
                  onClick={micActive ? stopMic : startMic}
                  aria-pressed={micActive}
                  className={`grid size-11 place-items-center rounded-full border transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300 ${
                    micActive
                      ? "border-white/12 bg-white/10 text-white hover:bg-white/16"
                      : "border-rose-300/20 bg-rose-400/12 text-rose-200 hover:bg-rose-400/20"
                  }`}
                  aria-label={micActive ? "静音麦克风" : "开启麦克风"}
                >
                  {micActive ? <Mic className="size-5" /> : <MicOff className="size-5" />}
                </button>
                <span className="text-xs text-white/38">{micActive ? "麦克风开启" : "麦克风静音"}</span>
              </>
            )}
          </div>

          {!canUseMic && (
            <p className="mt-5 max-w-sm text-center text-xs leading-5 text-amber-200/70">
              {!secureContext
                ? "当前页面不是安全上下文。请通过 HTTPS 域名或 localhost 打开，否则浏览器不会提供麦克风。"
                : "浏览器没有提供麦克风能力，请检查网站权限和系统输入设备。"}
            </p>
          )}
        </div>

        <aside className="flex min-h-[520px] min-w-0 flex-col bg-white/[0.035] backdrop-blur-2xl">
          <div className="flex items-center justify-between border-b border-white/8 px-5 py-4">
            <div>
              <p className="text-[10px] font-semibold uppercase tracking-[0.22em] text-white/35">Live transcript</p>
              <h3 className="mt-1 text-sm font-medium text-white/82">实时对话</h3>
            </div>
            {transcript.length > 0 && (
              <button type="button" onClick={() => setTranscript([])} className="min-h-11 cursor-pointer px-2 text-xs text-white/32 transition-colors duration-200 hover:text-white/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300">清空</button>
            )}
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
            {transcript.length === 0 ? (
              <div className="flex h-full min-h-56 flex-col items-center justify-center text-center">
                <div className="mb-4 flex gap-1">
                  {[10, 18, 26, 15, 22].map((height, index) => (
                    <span key={index} className="w-1 rounded-full bg-white/15" style={{ height }} />
                  ))}
                </div>
                <p className="text-sm text-white/42">对话文字会实时出现在这里</p>
                <p className="mt-1 text-xs text-white/24">你和 AI 的语音都会转写</p>
              </div>
            ) : (
              <div className="space-y-5">
                {transcript.map((entry) => (
                  <article key={entry.id} className="group">
                    <div className="mb-1.5 flex items-center gap-2">
                      <span className={`size-1.5 rounded-full ${entry.role === "user" ? "bg-cyan-300" : "bg-violet-300"}`} />
                      <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-white/34">
                        {entry.role === "user" ? "你" : "AI"}
                      </span>
                      {!entry.final && <span className="text-[10px] text-white/22">正在转写…</span>}
                    </div>
                    <p className={`text-[15px] leading-7 ${entry.role === "user" ? "text-white/72" : "text-white/94"}`}>
                      {entry.text}
                      {!entry.final && <span className="ml-1 inline-block h-4 w-[2px] animate-pulse bg-cyan-300/70 align-middle" />}
                    </p>
                  </article>
                ))}
                <div ref={transcriptEndRef} />
              </div>
            )}
          </div>

          <div className="border-t border-white/8 p-4">
            <form onSubmit={submitText} className="flex items-center gap-2 rounded-2xl border border-white/10 bg-white/[0.055] p-1.5 focus-within:border-cyan-300/35 focus-within:bg-white/[0.075]">
              <input
                value={textInput}
                onChange={(event) => setTextInput(event.target.value)}
                disabled={!connected}
                placeholder={connected ? "说点什么，或在这里输入…" : "开始对话后可输入文字"}
                className="min-w-0 flex-1 bg-transparent px-3 py-2 text-sm text-white outline-none placeholder:text-white/25 disabled:cursor-not-allowed"
              />
              <button
                type="submit"
                disabled={!connected || !textInput.trim()}
                className="grid size-11 shrink-0 place-items-center rounded-xl bg-white text-[#11152c] transition-transform duration-200 hover:scale-105 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-300 disabled:cursor-not-allowed disabled:opacity-25 disabled:hover:scale-100"
                aria-label="发送文字"
              >
                <Send className="size-4" />
              </button>
            </form>

            <details className="group mt-3">
              <summary className="flex cursor-pointer list-none items-center gap-2 py-1 text-[11px] text-white/28 transition-colors hover:text-white/55">
                <ChevronDown className="size-3 transition-transform group-open:rotate-180" />
                协议事件 · {logs.length}
              </summary>
              <div className="mt-2 max-h-36 overflow-y-auto rounded-xl border border-white/8 bg-black/20 p-3 font-mono text-[10px] leading-5">
                {logs.length === 0 && <p className="text-white/25">等待连接事件</p>}
                {logs.map((entry) => (
                  <p key={entry.id} className={
                    entry.dir === "error" ? "text-rose-300/80" :
                    entry.dir === "send" ? "text-cyan-300/65" :
                    entry.dir === "recv" ? "text-emerald-300/60" : "text-white/35"
                  }>
                    <span className="text-white/20">[{entry.time}]</span> {entry.text}
                  </p>
                ))}
              </div>
            </details>
          </div>
        </aside>
      </div>
    </section>
  );
}
