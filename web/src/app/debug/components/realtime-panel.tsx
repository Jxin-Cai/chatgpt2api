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
  { value: "ember", label: "Ember · 自信乐观" },
  { value: "glimmer", label: "Sol · 聪慧随性" },
  { value: "breeze", label: "Breeze · 活泼认真" },
  { value: "cove", label: "Cove · 沉稳直率" },
  { value: "juniper", label: "Juniper · 开放豁达" },
  { value: "maple", label: "Maple · 开朗直率" },
  { value: "orbit", label: "Spruce · 冷静坚定" },
  { value: "vale", label: "Vale · 聪颖好奇" },
  { value: "fathom", label: "Arbor · 随和多才" },
];

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
  const connectRef = useRef<(retry?: boolean) => Promise<void>>(async () => {});
  const logIdRef = useRef(0);
  const transcriptIdRef = useRef(0);
  const transcriptTurnRef = useRef(0);
  const activeTurnRef = useRef<Record<"user" | "assistant", string | null>>({ user: null, assistant: null });
  const chatTranscriptCursorRef = useRef<ChatTranscriptCursor | null>(null);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);
  const pendingTranscriptUpdatesRef = useRef<TranscriptUpdate[]>([]);
  const transcriptFlushFrameRef = useRef(0);

  useEffect(() => {
    currentPhaseRef.current = phase;
  }, [phase]);

  useEffect(() => {
    micActiveRef.current = micActive;
  }, [micActive]);

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
    } else if (type === "usage_update" || type === "goodbye") {
      const rateLimit = data.rate_limit_message as Record<string, unknown> | undefined;
      const exceeded = rateLimit?.exceed_limit_message as Record<string, unknown> | undefined;
      const quotaEnded = type === "goodbye" && data.reason === "cap_reached";
      if ((exceeded || quotaEnded) && quotaRetryCountRef.current < MAX_QUOTA_RETRIES && !quotaRetryScheduledRef.current) {
        quotaRetryScheduledRef.current = true;
        quotaRetryCountRef.current += 1;
        addLog("info", `语音额度不足，自动切换账号 (${quotaRetryCountRef.current}/${MAX_QUOTA_RETRIES})`);
        setPhase("connecting");
        setStatusDetail("当前账号额度不足，正在无感切换");
        quotaRetryTimerRef.current = window.setTimeout(() => {
          quotaRetryTimerRef.current = null;
          void connectRef.current(true);
        }, 120);
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

  const canUseMic = typeof window !== "undefined" && !!navigator.mediaDevices?.getUserMedia;

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

  const sendTextMessage = useCallback((text: string) => {
    const normalized = text.trim();
    if (!realtimeRef.current || !normalized) return;
    realtimeRef.current.sendEvent({
      type: "conversation.item.create",
      item: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: normalized }],
      },
    });
    realtimeRef.current.sendEvent({ type: "response.create" });
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
        scheduleNetworkReconnect(
          `信令暂时不可用${error.status ? ` (HTTP ${error.status})` : ""}`,
          error.retryAfterMs,
        );
      }
    }
  }, [addLog, attachRemoteMeter, handleRealtimeEvent, scheduleNetworkReconnect, startMetering, stopMetering, voice]);

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
    };
  }, [connect, scheduleNetworkReconnect, stopMetering]);

  const submitText = (event: FormEvent) => {
    event.preventDefault();
    sendTextMessage(textInput);
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
    <section className="relative min-h-[680px] overflow-hidden rounded-[30px] border border-white/10 bg-[#070a16] text-white shadow-[0_30px_100px_rgba(12,18,48,0.24)]">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_35%_15%,rgba(75,98,255,0.19),transparent_35%),radial-gradient(circle_at_70%_78%,rgba(57,211,255,0.10),transparent_32%)]" />

      <header className="relative z-10 flex flex-wrap items-center gap-3 border-b border-white/8 px-5 py-4 md:px-7">
        <div className="flex min-w-0 items-center gap-3">
          <span className={`size-2 rounded-full ${connected ? "bg-cyan-300 shadow-[0_0_16px_#67e8f9]" : connecting ? "animate-pulse bg-violet-300" : "bg-white/25"}`} />
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-[0.24em] text-white/38">Realtime voice lab</p>
            <p className="truncate text-sm font-medium text-white/88">{phaseCopy.title}</p>
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {connected && (
            <span
              className="hidden items-center gap-1.5 rounded-full border border-white/8 bg-white/5 px-2.5 py-1 text-[10px] text-white/42 sm:flex"
              title={quality ? `RTT ${quality.roundTripTimeMs ?? "-"}ms · 抖动 ${quality.jitterMs ?? "-"}ms · ${quality.candidateType ?? "unknown"}` : "正在采集 WebRTC 质量"}
            >
              <Wifi className="size-3 text-cyan-300/75" />
              {qualityLabel}
            </span>
          )}
          <Select value={voice} onValueChange={setVoice} disabled={connected || connecting}>
            <SelectTrigger aria-label="选择声音" className="h-9 w-[190px] border-white/10 bg-white/6 text-xs text-white shadow-none hover:bg-white/10">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {VOICES.map((item) => (
                <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          {connected && (
            <Button onClick={disconnect} size="sm" variant="ghost" className="rounded-full text-white/60 hover:bg-white/10 hover:text-white">
              <PhoneOff className="size-4" />
              <span className="hidden sm:inline">结束</span>
            </Button>
          )}
        </div>
      </header>

      <div className="relative z-10 grid min-h-[610px] lg:grid-cols-[minmax(0,1fr)_390px]">
        <div className="relative flex min-h-[520px] flex-col items-center justify-center overflow-hidden border-b border-white/8 px-5 py-10 lg:border-r lg:border-b-0">
          <div className="pointer-events-none absolute inset-0 opacity-[0.12] [background-image:linear-gradient(rgba(255,255,255,.12)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,.12)_1px,transparent_1px)] [background-size:48px_48px] [mask-image:radial-gradient(circle_at_center,black,transparent_72%)]" />

          {!connected && !connecting ? (
            <button
              type="button"
              onClick={() => void connect()}
              disabled={!canUseMic}
              className="group relative grid size-48 place-items-center rounded-full outline-none transition-transform duration-500 hover:scale-[1.035] focus-visible:ring-2 focus-visible:ring-cyan-300 disabled:cursor-not-allowed disabled:opacity-45"
            >
              <span className="absolute inset-0 rounded-full bg-[conic-gradient(from_210deg,#4f46e5,#22d3ee,#c084fc,#4f46e5)] opacity-90 blur-[1px] transition-transform duration-700 group-hover:rotate-12" />
              <span className="absolute inset-[2px] rounded-full bg-[#0b1025]" />
              <span className="relative flex flex-col items-center gap-3">
                <span className="grid size-14 place-items-center rounded-full bg-white text-[#11152c] shadow-[0_0_40px_rgba(111,222,255,.38)]">
                  <Phone className="size-6" />
                </span>
                <span className="text-sm font-medium tracking-wide">开始实时对话</span>
              </span>
            </button>
          ) : (
            <div className="flex flex-col items-center" aria-live="polite">
              <div
                ref={orbRef}
                data-phase={phase}
                className="realtime-orb-shell relative size-56 md:size-64"
                style={{ "--voice-level": 0 } as CSSProperties}
                aria-label={phaseCopy.title}
              >
                <div className="realtime-orb-aura absolute -inset-12 rounded-full" />
                <div className="realtime-orb absolute inset-0 overflow-hidden rounded-[46%_54%_52%_48%/50%_44%_56%_50%]">
                  <span className="realtime-orb-light absolute inset-[12%] rounded-full" />
                  <span className="realtime-orb-glint absolute left-[27%] top-[22%] size-[24%] rounded-full" />
                </div>
              </div>
              <h2 className="mt-10 text-2xl font-medium tracking-[-0.025em] text-white md:text-3xl">{phaseCopy.title}</h2>
              <p className="mt-2 text-sm text-white/48">{statusDetail}</p>
            </div>
          )}

          <div className="relative mt-10 flex min-h-10 items-center justify-center gap-3">
            {connecting && (
              <span className="flex items-center gap-2 text-xs text-white/48">
                <Activity className="size-4 animate-pulse text-cyan-300" />
                正在连接
              </span>
            )}
            {connected && (
              <>
                <button
                  type="button"
                  onClick={micActive ? stopMic : startMic}
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
              浏览器需要 HTTPS 或 localhost 才能访问麦克风。你仍可在右侧使用文字测试。
            </p>
          )}
        </div>

        <aside className="flex min-h-[520px] flex-col bg-white/[0.035] backdrop-blur-2xl">
          <div className="flex items-center justify-between border-b border-white/8 px-5 py-4">
            <div>
              <p className="text-[10px] font-semibold uppercase tracking-[0.22em] text-white/35">Live transcript</p>
              <h3 className="mt-1 text-sm font-medium text-white/82">实时对话</h3>
            </div>
            {transcript.length > 0 && (
              <button type="button" onClick={() => setTranscript([])} className="text-xs text-white/32 hover:text-white/70">清空</button>
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
                className="grid size-9 shrink-0 place-items-center rounded-xl bg-white text-[#11152c] transition-transform hover:scale-105 disabled:opacity-25 disabled:hover:scale-100"
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
