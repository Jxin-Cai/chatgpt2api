"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Mic, MicOff, Phone, PhoneOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { RealtimeEvent, RealtimeWebRTCConnection } from "@/lib/realtime-webrtc";
import { getStoredAuthSession } from "@/store/auth";
import webConfig from "@/constants/common-env";

type LogEntry = {
  id: number;
  time: string;
  dir: "send" | "recv" | "info" | "error";
  text: string;
};

type TranscriptEntry = {
  id: number;
  role: "user" | "assistant";
  text: string;
};

const VOICES = [
  { value: "ember", label: "Ember (自信乐观)" },
  { value: "glimmer", label: "Sol (聪慧随性)" },
  { value: "breeze", label: "Breeze (活泼认真)" },
  { value: "cove", label: "Cove (沉稳直率)" },
  { value: "juniper", label: "Juniper (开放豁达)" },
  { value: "maple", label: "Maple (开朗直率)" },
  { value: "orbit", label: "Spruce (冷静坚定)" },
  { value: "vale", label: "Vale (聪颖好奇)" },
  { value: "fathom", label: "Arbor (随和多才)" },
];

const MAX_QUOTA_RETRIES = 2;

function ts() {
  return new Date().toLocaleTimeString("en-US", { hour12: false, fractionalSecondDigits: 2 });
}

export function RealtimePanel() {
  const [voice, setVoice] = useState("ember");
  const [connected, setConnected] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [micActive, setMicActive] = useState(false);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [status, setStatus] = useState("未连接");
  const [textInput, setTextInput] = useState("");

  const realtimeRef = useRef<RealtimeWebRTCConnection | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const animFrameRef = useRef<number>(0);
  const terminalErrorRef = useRef<string>("");
  const quotaRetryCountRef = useRef(0);
  const quotaRetryScheduledRef = useRef(false);
  const quotaRetryTimerRef = useRef<number | null>(null);
  const connectRef = useRef<(retry?: boolean) => Promise<void>>(async () => {});
  const logIdRef = useRef(0);
  const transcriptIdRef = useRef(0);
  const logsEndRef = useRef<HTMLDivElement | null>(null);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);

  const addLog = useCallback((dir: LogEntry["dir"], text: string) => {
    const entry: LogEntry = { id: logIdRef.current++, time: ts(), dir, text };
    setLogs((prev) => [...prev.slice(-200), entry]);
  }, []);

  const addTranscript = useCallback((role: "user" | "assistant", text: string) => {
    setTranscript((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.role === role) {
        return [...prev.slice(0, -1), { ...last, text: last.text + text }];
      }
      return [...prev, { id: transcriptIdRef.current++, role, text }];
    });
  }, []);

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [transcript]);

  const handleRealtimeEvent = useCallback(
    (data: RealtimeEvent) => {
      const type = data.type || "unknown";
      if (type === "state_update") {
        const stateLabels: Record<string, string> = {
          listening: "上游正在聆听",
          thinking: "上游正在思考",
          speaking: "上游正在回答",
          idle: "会话已建立 — 可以说话",
        };
        const upstreamState = typeof data.new_state === "string" ? data.new_state : "unknown";
        setStatus(stateLabels[upstreamState] || `上游状态：${upstreamState}`);
        addLog("recv", `state_update: ${String(data.previous_state || "?")} → ${upstreamState}`);
      } else if (type === "response.audio_transcript.delta") {
        addTranscript("assistant", String(data.delta || ""));
      } else if (type === "response.text.delta") {
        addTranscript("assistant", String(data.delta || ""));
      } else if (type === "conversation.item.input_audio_transcription.delta") {
        addTranscript("user", String(data.delta || ""));
      } else if (type === "conversation.item.input_audio_transcription.completed") {
        addTranscript("user", String(data.transcript || ""));
      } else if (type === "usage_update" || type === "goodbye") {
        const rateLimit = data.rate_limit_message as Record<string, unknown> | undefined;
        const exceeded = rateLimit?.exceed_limit_message as Record<string, unknown> | undefined;
        const quotaEnded = type === "goodbye" && data.reason === "cap_reached";
        if ((exceeded || quotaEnded) && quotaRetryCountRef.current < MAX_QUOTA_RETRIES && !quotaRetryScheduledRef.current) {
          quotaRetryScheduledRef.current = true;
          quotaRetryCountRef.current += 1;
          addLog("info", `语音额度不足，自动切换账号 (${quotaRetryCountRef.current}/${MAX_QUOTA_RETRIES})`);
          quotaRetryTimerRef.current = window.setTimeout(() => {
            quotaRetryTimerRef.current = null;
            void connectRef.current(true);
          }, 100);
        }
      } else if (type === "error") {
        const error = data.error as Record<string, unknown> | undefined;
        const message = String(error?.message || "未知错误");
        terminalErrorRef.current = message;
        setStatus(`错误：${message}`);
        addLog("error", `ERROR: ${JSON.stringify(error)}`);
      } else {
        addLog("recv", `${type}: ${JSON.stringify(data).substring(0, 150)}`);
        if (typeof data.transcript === "string") addTranscript("assistant", data.transcript);
      }
    },
    [addLog, addTranscript],
  );

  const canUseMic = typeof window !== "undefined" && !!navigator.mediaDevices?.getUserMedia;

  const drawWaveform = useCallback(() => {
    const canvas = canvasRef.current;
    const analyser = analyserRef.current;
    if (!canvas || !analyser) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const data = new Uint8Array(analyser.frequencyBinCount);

    const draw = () => {
      animFrameRef.current = requestAnimationFrame(draw);
      analyser.getByteTimeDomainData(data);
      ctx.fillStyle = "rgba(0,0,0,0.05)";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#22c55e";
      ctx.beginPath();
      const sliceWidth = canvas.width / data.length;
      let x = 0;
      for (let i = 0; i < data.length; i++) {
        const v = data[i] / 128.0;
        const y = (v * canvas.height) / 2;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
        x += sliceWidth;
      }
      ctx.lineTo(canvas.width, canvas.height / 2);
      ctx.stroke();
    };
    canvas.width = canvas.offsetWidth * 2;
    canvas.height = canvas.offsetHeight * 2;
    draw();
  }, []);

  const startWaveform = useCallback((stream: MediaStream) => {
    const ctx = new AudioContext();
    audioCtxRef.current = ctx;
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    analyserRef.current = analyser;
    drawWaveform();
  }, [drawWaveform]);

  const stopWaveform = useCallback(() => {
    if (animFrameRef.current) {
      cancelAnimationFrame(animFrameRef.current);
      animFrameRef.current = 0;
    }
    analyserRef.current = null;
    if (audioCtxRef.current) {
      void audioCtxRef.current.close();
      audioCtxRef.current = null;
    }
  }, []);

  const startMic = useCallback(() => {
    realtimeRef.current?.setMicrophoneEnabled(true);
    setMicActive(true);
    addLog("info", "麦克风已开启");
  }, [addLog]);

  const stopMic = useCallback(() => {
    realtimeRef.current?.setMicrophoneEnabled(false);
    setMicActive(false);
    addLog("info", "麦克风已静音");
  }, [addLog]);

  const sendTextMessage = useCallback((text: string) => {
    if (!realtimeRef.current || !text.trim()) return;
    const event = {
      type: "conversation.item.create",
      item: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: text.trim() }],
      },
    };
    realtimeRef.current.sendEvent(event);
    realtimeRef.current.sendEvent({ type: "response.create" });
    addTranscript("user", text.trim());
    addLog("send", `text: ${text.trim().substring(0, 50)}`);
    setTextInput("");
  }, [addTranscript, addLog]);

  const disconnect = useCallback(() => {
    if (quotaRetryTimerRef.current !== null) {
      window.clearTimeout(quotaRetryTimerRef.current);
      quotaRetryTimerRef.current = null;
    }
    quotaRetryScheduledRef.current = false;
    realtimeRef.current?.close();
    realtimeRef.current = null;
    stopWaveform();
    setConnected(false);
    setConnecting(false);
    setMicActive(false);
    setStatus("已断开");
  }, [stopWaveform]);

  const connect = useCallback(async (retry = false) => {
    const session = await getStoredAuthSession();
    if (!session) {
      addLog("error", "未登录，请先登录");
      return;
    }
    realtimeRef.current?.close();
    stopWaveform();
    setConnected(false);
    setMicActive(false);
    setConnecting(true);
    setStatus(retry ? "正在切换语音账号..." : "正在建立 WebRTC...");
    terminalErrorRef.current = "";
    quotaRetryScheduledRef.current = false;
    if (!retry) quotaRetryCountRef.current = 0;

    const signalingUrl = webConfig.apiUrl
      ? new URL("/v1/realtime/sessions", webConfig.apiUrl).toString()
      : "/v1/realtime/sessions";
    const connection = new RealtimeWebRTCConnection({
      onEvent: handleRealtimeEvent,
      onConnectionState: (state) => {
        addLog("info", `WebRTC: ${state}`);
        if (state === "failed") {
          setConnected(false);
          setStatus("WebRTC 连接失败");
        }
      },
    });
    realtimeRef.current = connection;

    try {
      addLog("info", retry ? "正在切换账号并重连..." : "正在建立端到端 WebRTC...");
      const result = await connection.connect({
        authorization: `Bearer ${session.key}`,
        voice,
        signalingUrl,
      });
      if (realtimeRef.current !== connection) return;
      setConnected(true);
      setConnecting(false);
      setMicActive(true);
      setStatus("会话已建立 — 可以说话");
      addLog("recv", `session.created (${result.location})`);
      const microphone = connection.getMicrophoneStream();
      if (microphone) startWaveform(microphone);
    } catch (error) {
      if (realtimeRef.current !== connection) return;
      connection.close();
      realtimeRef.current = null;
      setConnected(false);
      setConnecting(false);
      setMicActive(false);
      const message = error instanceof Error ? error.message : String(error);
      terminalErrorRef.current = message;
      setStatus(`错误：${message}`);
      addLog("error", message);
    }
  }, [voice, addLog, handleRealtimeEvent, startWaveform, stopWaveform]);

  useEffect(() => {
    connectRef.current = connect;
    return () => {
      if (quotaRetryTimerRef.current !== null) {
        window.clearTimeout(quotaRetryTimerRef.current);
        quotaRetryTimerRef.current = null;
      }
      realtimeRef.current?.close();
      stopWaveform();
    };
  }, [connect, stopWaveform]);

  return (
    <div className="flex h-[calc(100vh-160px)] min-h-[500px] gap-4">
      {/* 左侧：对话区 */}
      <div className="flex flex-1 flex-col overflow-hidden rounded-xl border border-stone-200 bg-white dark:border-white/10 dark:bg-stone-900">
        {/* 对话头部 */}
        <div className="flex items-center justify-between border-b border-stone-100 px-4 py-3 dark:border-white/10">
          <div className="flex items-center gap-3">
            <div className={`size-2 rounded-full ${connected ? "bg-green-500" : "bg-stone-300"}`} />
            <span className="text-sm font-medium text-stone-700 dark:text-stone-200">{status}</span>
          </div>
          <div className="flex items-center gap-2">
            {micActive && (
              <Button size="sm" variant="ghost" onClick={stopMic} className="text-red-500 hover:text-red-600">
                <MicOff className="mr-1 size-4" />
                静音
              </Button>
            )}
            {connected && !micActive && (
              <Button size="sm" variant="ghost" onClick={startMic} className="text-green-600">
                <Mic className="mr-1 size-4" />
                开麦
              </Button>
            )}
          </div>
        </div>

        {/* 对话内容 */}
        <div className="flex-1 overflow-y-auto px-4 py-4">
          {transcript.length === 0 && (
            <div className="flex h-full items-center justify-center text-sm text-stone-400">
              {connected ? "开始说话，对话内容将在此显示..." : "点击下方按钮开始语音对话"}
            </div>
          )}
          <div className="space-y-4">
            {transcript.map((entry) => (
              <div key={entry.id} className={`flex ${entry.role === "user" ? "justify-end" : "justify-start"}`}>
                <div
                  className={`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
                    entry.role === "user"
                      ? "bg-stone-900 text-white dark:bg-white dark:text-stone-900"
                      : "bg-stone-100 text-stone-800 dark:bg-white/10 dark:text-stone-100"
                  }`}
                >
                  {entry.text}
                </div>
              </div>
            ))}
            <div ref={transcriptEndRef} />
          </div>
        </div>

        {/* 底部：文字输入 + 波形 + 控制 */}
        <div className="border-t border-stone-100 px-4 py-3 dark:border-white/10">
          {connected && (
            <form
              className="mb-3 flex gap-2"
              onSubmit={(e) => { e.preventDefault(); sendTextMessage(textInput); }}
            >
              <input
                type="text"
                value={textInput}
                onChange={(e) => setTextInput(e.target.value)}
                placeholder={canUseMic ? "输入文字消息（或直接说话）..." : "输入文字消息（当前环境麦克风不可用，需 HTTPS）..."}
                className="flex-1 rounded-lg border border-stone-200 bg-stone-50 px-3 py-2 text-sm outline-none focus:border-stone-400 dark:border-white/10 dark:bg-stone-800 dark:text-white"
              />
              <Button type="submit" size="sm" disabled={!textInput.trim()} className="rounded-lg px-4">
                发送
              </Button>
            </form>
          )}
          {micActive && <canvas ref={canvasRef} className="mb-3 h-12 w-full rounded-lg bg-stone-50 dark:bg-stone-800" />}
          <div className="flex items-center gap-3">
            <Select value={voice} onValueChange={setVoice} disabled={connected}>
              <SelectTrigger className="w-48">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {VOICES.map((v) => (
                  <SelectItem key={v.value} value={v.value}>{v.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <div className="flex-1" />
            {!connected ? (
              <Button onClick={() => void connect()} disabled={connecting} className="gap-2 rounded-full bg-green-600 px-6 text-white hover:bg-green-700">
                <Phone className="size-4" />
                {connecting ? "连接中..." : "开始对话"}
              </Button>
            ) : (
              <Button onClick={disconnect} variant="destructive" className="gap-2 rounded-full px-6">
                <PhoneOff className="size-4" />
                结束对话
              </Button>
            )}
          </div>
          {!canUseMic && connected && (
            <p className="mt-2 text-xs text-amber-600 dark:text-amber-400">
              ⚠ 当前为 HTTP 环境，浏览器禁用了麦克风。请通过 HTTPS 访问以使用语音输入，或使用上方文字输入。
            </p>
          )}
        </div>
      </div>

      {/* 右侧：事件日志 */}
      <div className="flex w-80 flex-col overflow-hidden rounded-xl border border-stone-200 bg-white dark:border-white/10 dark:bg-stone-900">
        <div className="border-b border-stone-100 px-4 py-3 dark:border-white/10">
          <Label className="text-xs font-semibold uppercase tracking-wider text-stone-500">事件日志</Label>
        </div>
        <div className="flex-1 overflow-y-auto px-3 py-2 font-mono text-[11px] leading-5">
          {logs.map((entry) => (
            <div
              key={entry.id}
              className={`border-b border-stone-50 py-0.5 dark:border-white/5 ${
                entry.dir === "send" ? "text-blue-600 dark:text-blue-400" :
                entry.dir === "recv" ? "text-green-600 dark:text-green-400" :
                entry.dir === "error" ? "text-red-500" :
                "text-stone-500 dark:text-stone-400"
              }`}
            >
              <span className="text-stone-400">[{entry.time}]</span>{" "}
              {entry.dir === "send" ? "→ " : entry.dir === "recv" ? "← " : ""}
              {entry.text}
            </div>
          ))}
          <div ref={logsEndRef} />
        </div>
      </div>
    </div>
  );
}
