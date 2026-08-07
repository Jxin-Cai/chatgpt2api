"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Mic, MicOff, Phone, PhoneOff } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
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

function ts() {
  return new Date().toLocaleTimeString("en-US", { hour12: false, fractionalSecondDigits: 2 });
}

function float32ToPcm16(float32: Float32Array): Int16Array {
  const pcm16 = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    const s = Math.max(-1, Math.min(1, float32[i]));
    pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return pcm16;
}

function arrayBufferToBase64(buffer: ArrayBufferLike): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function base64ToArrayBuffer(base64: string): ArrayBuffer {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
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

  const wsRef = useRef<WebSocket | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const scriptNodeRef = useRef<ScriptProcessorNode | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const animFrameRef = useRef<number>(0);
  const playbackTimeRef = useRef<number>(0);
  const terminalErrorRef = useRef<string>("");
  const logIdRef = useRef(0);
  const transcriptIdRef = useRef(0);
  const startMicRef = useRef<() => Promise<void>>(async () => {});
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

  const playAudio = useCallback((pcmBuffer: ArrayBuffer) => {
    if (!audioCtxRef.current) audioCtxRef.current = new AudioContext({ sampleRate: 48000 });
    const ctx = audioCtxRef.current;
    const samples = new Int16Array(pcmBuffer);
    const float32 = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i++) float32[i] = samples[i] / 32768;

    const buffer = ctx.createBuffer(1, float32.length, 48000);
    buffer.getChannelData(0).set(float32);
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);

    const now = ctx.currentTime;
    if (playbackTimeRef.current < now) playbackTimeRef.current = now;
    source.start(playbackTimeRef.current);
    playbackTimeRef.current += buffer.duration;
  }, []);

  const handleMessage = useCallback(
    (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data);
        const type = data.type || "unknown";

        if (type === "response.audio.delta") {
          const pcm = base64ToArrayBuffer(data.delta);
          playAudio(pcm);
          // 不记录每个 audio delta 避免刷屏
        } else if (type === "response.audio.done") {
          addLog("recv", "audio.done");
        } else if (type === "session.created") {
          setStatus("会话已建立 — 可以说话");
          addLog("recv", `session.created (${data.session?.id || ""})`);
          if (typeof navigator.mediaDevices?.getUserMedia === "function") {
            void startMicRef.current();
          } else {
            addLog("info", "HTTP 环境下麦克风不可用，请使用文字输入");
          }
        } else if (type === "session.updated") {
          addLog("recv", `session.updated: voice=${data.session?.voice}`);
        } else if (type === "state_update") {
          const stateLabels: Record<string, string> = {
            listening: "上游正在聆听",
            thinking: "上游正在思考",
            speaking: "上游正在回答",
            idle: "会话已建立 — 可以说话",
          };
          const upstreamState = data.new_state || "unknown";
          setStatus(stateLabels[upstreamState] || `上游状态：${upstreamState}`);
          addLog("recv", `state_update: ${data.previous_state || "?"} → ${upstreamState}`);
        } else if (type === "response.audio_transcript.delta") {
          addTranscript("assistant", data.delta || "");
        } else if (type === "response.text.delta") {
          addTranscript("assistant", data.delta || "");
        } else if (type === "conversation.item.input_audio_transcription.delta") {
          addTranscript("user", data.delta || "");
        } else if (type === "conversation.item.input_audio_transcription.completed") {
          addTranscript("user", data.transcript || "");
        } else if (type === "error") {
          const message = data.error?.message || "未知错误";
          terminalErrorRef.current = message;
          setStatus(`错误：${message}`);
          addLog("error", `ERROR: ${JSON.stringify(data.error)}`);
        } else {
          addLog("recv", `${type}: ${JSON.stringify(data).substring(0, 150)}`);
          // DataChannel 消息可能包含转录
          if (data.transcript) {
            addTranscript("assistant", data.transcript);
          }
        }
      } catch {
        addLog("error", `parse error: ${String(event.data).substring(0, 100)}`);
      }
    },
    [addLog, addTranscript, playAudio],
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

  const startMic = useCallback(async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      addLog("error", "麦克风不可用：需要 HTTPS 或 localhost 才能访问麦克风。可使用下方文字输入。");
      return;
    }
    try {
      if (!audioCtxRef.current) audioCtxRef.current = new AudioContext({ sampleRate: 48000 });
      const ctx = audioCtxRef.current;
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { sampleRate: 48000, channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      micStreamRef.current = stream;
      const source = ctx.createMediaStreamSource(stream);

      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      source.connect(analyser);
      analyserRef.current = analyser;

      const scriptNode = ctx.createScriptProcessor(4096, 1, 1);
      scriptNode.onaudioprocess = (e) => {
        if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
        const float32 = e.inputBuffer.getChannelData(0);
        const pcm16 = float32ToPcm16(float32);
        const b64 = arrayBufferToBase64(pcm16.buffer);
        wsRef.current.send(JSON.stringify({ type: "input_audio_buffer.append", audio: b64 }));
      };
      source.connect(scriptNode);
      scriptNode.connect(ctx.destination);
      scriptNodeRef.current = scriptNode;
      setMicActive(true);
      addLog("info", "麦克风已开启");
      drawWaveform();
    } catch (err) {
      addLog("error", `麦克风错误: ${(err as Error).message}`);
    }
  }, [addLog, drawWaveform]);

  useEffect(() => {
    startMicRef.current = startMic;
  }, [startMic]);

  const stopMic = useCallback(() => {
    if (scriptNodeRef.current) {
      scriptNodeRef.current.disconnect();
      scriptNodeRef.current = null;
    }
    if (micStreamRef.current) {
      micStreamRef.current.getTracks().forEach((t) => t.stop());
      micStreamRef.current = null;
    }
    if (animFrameRef.current) {
      cancelAnimationFrame(animFrameRef.current);
      animFrameRef.current = 0;
    }
    setMicActive(false);
  }, []);

  const sendTextMessage = useCallback((text: string) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN || !text.trim()) return;
    const event = {
      type: "conversation.item.create",
      item: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: text.trim() }],
      },
    };
    wsRef.current.send(JSON.stringify(event));
    wsRef.current.send(JSON.stringify({ type: "response.create" }));
    addTranscript("user", text.trim());
    addLog("send", `text: ${text.trim().substring(0, 50)}`);
    setTextInput("");
  }, [addTranscript, addLog]);

  const connect = useCallback(async () => {
    const session = await getStoredAuthSession();
    if (!session) {
      addLog("error", "未登录，请先登录");
      return;
    }
    setConnecting(true);
    setStatus("连接中...");
    terminalErrorRef.current = "";

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = webConfig.apiUrl ? new URL(webConfig.apiUrl).host : window.location.host;
    const url = `${proto}//${host}/v1/realtime?model=gpt-4o-realtime-preview&authorization=${encodeURIComponent("Bearer " + session.key)}`;

    addLog("info", "正在连接...");
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      setConnecting(false);
      setStatus("已连接 — 正在建立语音通道");
      addLog("info", "WebSocket 已连接");
      ws.send(JSON.stringify({ type: "session.update", session: { voice } }));
      addLog("send", `session.update (voice=${voice})`);
    };
    ws.onmessage = handleMessage;
    ws.onclose = (e) => {
      setConnected(false);
      setConnecting(false);
      if (!terminalErrorRef.current) setStatus(`已断开 (code=${e.code})`);
      addLog("info", `连接关闭: code=${e.code}`);
      stopMic();
    };
    ws.onerror = () => {
      addLog("error", "WebSocket 错误");
    };
  }, [voice, addLog, handleMessage, stopMic]);

  const disconnect = useCallback(() => {
    wsRef.current?.close();
    stopMic();
  }, [stopMic]);

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
              <Button onClick={connect} disabled={connecting} className="gap-2 rounded-full bg-green-600 px-6 text-white hover:bg-green-700">
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
