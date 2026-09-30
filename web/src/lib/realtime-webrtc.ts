export type RealtimeEvent = {
  type: string;
  [key: string]: unknown;
};

export type ConnectOptions = {
  authorization: string;
  voice: string;
  /** API 服务地址；同源部署传空字符串。 */
  baseUrl: string;
  attemptId?: string;
  conversationId?: string;
  parentMessageId?: string;
  resumeHandle?: string;
  microphoneEnabled?: boolean;
  initialHistory?: Array<{ role: "user" | "assistant"; content: Array<{ type: "input_text" | "output_text"; text: string }> }>;
};

export type RealtimeConnectionQuality = {
  roundTripTimeMs?: number;
  jitterMs?: number;
  packetsLost?: number;
  packetsReceived?: number;
  concealedSamples?: number;
  packetLossPercent?: number;
  concealedSamplePercent?: number;
  jitterBufferMs?: number;
  candidateType?: string;
};

export class RealtimeSignalingError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly retryable: boolean,
    readonly retryAfterMs: number,
    readonly attemptId: string,
  ) {
    super(message);
    this.name = "RealtimeSignalingError";
  }
}

export type RealtimeConnectionHandlers = {
  onEvent: (event: RealtimeEvent) => void;
  onConnectionState: (state: RTCPeerConnectionState) => void;
  onRemoteStream?: (stream: MediaStream) => void;
  onQuality?: (quality: RealtimeConnectionQuality) => void;
  onMicrophoneEnded?: () => void;
  onMicrophoneState?: (state: "live" | "muted", settings: MediaTrackSettings) => void;
};

export type RealtimeConnectionResult = {
  location: string;
  attemptId: string;
  requestId: string;
  sessionHandle: string;
  resumeHandle: string;
};

export interface RealtimeConnection {
  connect(options: ConnectOptions): Promise<RealtimeConnectionResult>;
  reportQuotaExhausted(details?: {
    reason?: string;
    restoreAt?: string;
    retryAfterSeconds?: number;
  }): Promise<void>;
  sendEvent(event: RealtimeEvent): void;
  sendTextMessage(text: string): string;
  setMicrophoneEnabled(enabled: boolean): void;
  getMicrophoneStream(): MediaStream | null;
  getRemoteStream(): MediaStream | null;
  close(): void;
}

const CONNECTION_TIMEOUT_MS = 15_000;
const ICE_GATHERING_TIMEOUT_MS = 5_000;
const DATA_CHANNEL_TIMEOUT_MS = 10_000;
const INITIAL_JITTER_BUFFER_MS = 120;
const MAX_JITTER_BUFFER_MS = 300;

type BufferedAudioReceiver = RTCRtpReceiver & {
  jitterBufferTarget?: number | null;
  playoutDelayHint?: number | null;
};

function decodeDataChannelMessage(raw: string): RealtimeEvent {
  const outer = JSON.parse(raw) as RealtimeEvent & { data?: string | RealtimeEvent };
  if (outer.type !== "data_message") return outer;
  if (typeof outer.data === "string") return JSON.parse(outer.data) as RealtimeEvent;
  if (outer.data && typeof outer.data === "object") return outer.data;
  return outer;
}

function normalizeSdpLineEndings(sdp: string): string {
  return `${sdp.trim().replace(/\r?\n/g, "\r\n")}\r\n`;
}

export function realtimeEndpoint(baseUrl: string, path: string): string {
  return baseUrl ? new URL(path, baseUrl).toString() : path;
}

type SignalingErrorBody = {
  attempt_id?: string;
  error?: { message?: string; retryable?: boolean; retry_after_ms?: number };
};

async function signalingErrorFromResponse(
  response: globalThis.Response,
  fallbackMessage: string,
  fallbackAttemptId = "",
): Promise<RealtimeSignalingError> {
  let body: SignalingErrorBody = {};
  try {
    body = (await response.json()) as SignalingErrorBody;
  } catch {
    // 非 JSON 错误响应（如网关错误页）直接落到 fallback 文案。
  }
  return new RealtimeSignalingError(
    body.error?.message || `${fallbackMessage} (HTTP ${response.status})`,
    response.status,
    Boolean(body.error?.retryable),
    body.error?.retry_after_ms || 0,
    body.attempt_id || response.headers.get("X-Attempt-Id") || fallbackAttemptId,
  );
}

function waitForIceGathering(pc: RTCPeerConnection): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    let settled = false;
    let timeout = 0;
    const finish = () => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeout);
      pc.removeEventListener("icegatheringstatechange", onStateChange);
      resolve();
    };
    const onStateChange = () => {
      if (pc.iceGatheringState === "complete") finish();
    };
    pc.addEventListener("icegatheringstatechange", onStateChange);
    timeout = window.setTimeout(finish, ICE_GATHERING_TIMEOUT_MS);
  });
}

function waitForConnection(pc: RTCPeerConnection): Promise<void> {
  if (pc.connectionState === "connected") return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      cleanup();
      reject(new Error(`WebRTC 连接超时 (${pc.connectionState})`));
    }, CONNECTION_TIMEOUT_MS);
    const onStateChange = () => {
      if (pc.connectionState === "connected") {
        cleanup();
        resolve();
      } else if (pc.connectionState === "failed" || pc.connectionState === "closed") {
        cleanup();
        reject(new Error(`WebRTC 连接失败 (${pc.connectionState})`));
      }
    };
    const cleanup = () => {
      window.clearTimeout(timeout);
      pc.removeEventListener("connectionstatechange", onStateChange);
    };
    pc.addEventListener("connectionstatechange", onStateChange);
  });
}

function waitForDataChannel(channel: RTCDataChannel): Promise<void> {
  if (channel.readyState === "open") return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      cleanup();
      reject(new Error(`实时事件通道连接超时 (${channel.readyState})`));
    }, DATA_CHANNEL_TIMEOUT_MS);
    const onOpen = () => {
      cleanup();
      resolve();
    };
    const onClose = () => {
      cleanup();
      reject(new Error("实时事件通道已关闭"));
    };
    const cleanup = () => {
      window.clearTimeout(timeout);
      channel.removeEventListener("open", onOpen);
      channel.removeEventListener("close", onClose);
    };
    channel.addEventListener("open", onOpen);
    channel.addEventListener("close", onClose);
  });
}

export class RealtimeWebRTCConnection implements RealtimeConnection {
  private pc: RTCPeerConnection | null = null;
  private dataChannel: RTCDataChannel | null = null;
  private microphone: MediaStream | null = null;
  private remoteStream: MediaStream | null = null;
  private audioElement: HTMLAudioElement | null = null;
  private signalingAbort: AbortController | null = null;
  private statsTimer: number | null = null;
  private sessionReport: { authorization: string; baseUrl: string; callId: string } | null = null;
  private audioReceiver: BufferedAudioReceiver | null = null;
  private jitterBufferTargetMs = INITIAL_JITTER_BUFFER_MS;
  private previousAudioStats: { packetsReceived: number; packetsLost: number; concealedSamples: number; totalSamplesReceived: number; jitterBufferDelay: number; jitterBufferEmittedCount: number } | null = null;
  private healthyQualitySamples = 0;
  private closed = true;

  constructor(
    private readonly handlers: RealtimeConnectionHandlers,
    private readonly protocol: "realtime" | "live" = "realtime",
  ) {}

  async connect(options: ConnectOptions): Promise<RealtimeConnectionResult> {
    this.close();
    this.closed = false;

    const pc = new RTCPeerConnection({
      bundlePolicy: "max-bundle",
      iceServers: [{ urls: "stun:stun.cloudflare.com:3478" }],
    });
    this.pc = pc;
    pc.onconnectionstatechange = () => {
      this.handlers.onConnectionState(pc.connectionState);
      if (pc.connectionState === "connected") this.startQualitySampling(pc);
    };

    const audio = new Audio();
    audio.autoplay = true;
    audio.setAttribute("playsinline", "");
    this.audioElement = audio;
    pc.ontrack = (event) => {
      const [stream] = event.streams;
      if (stream) {
        if (event.track.kind === "audio") {
          this.audioReceiver = event.receiver as BufferedAudioReceiver;
          this.setJitterBufferTarget(INITIAL_JITTER_BUFFER_MS);
        }
        this.remoteStream = stream;
        audio.srcObject = stream;
        this.handlers.onRemoteStream?.(stream);
        void audio.play().catch(() => undefined);
      }
    };

    const microphone = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        sampleRate: 48000,
        echoCancellation: true,
        noiseSuppression: false,
        autoGainControl: true,
      },
    });
    if (this.closed) {
      microphone.getTracks().forEach((track) => track.stop());
      throw new Error("连接已取消");
    }
    this.microphone = microphone;
    microphone.getAudioTracks().forEach((track) => {
      const notifyMicrophoneState = () => {
        if (!this.closed) this.handlers.onMicrophoneState?.(track.muted ? "muted" : "live", track.getSettings());
      };
      track.addEventListener("mute", notifyMicrophoneState);
      track.addEventListener("unmute", notifyMicrophoneState);
      track.addEventListener("ended", () => {
        if (!this.closed) this.handlers.onMicrophoneEnded?.();
      }, { once: true });
      track.enabled = options.microphoneEnabled !== false;
      pc.addTrack(track, microphone);
      notifyMicrophoneState();
    });
    if (this.protocol === "realtime") pc.addTransceiver("video", { direction: "sendonly" });

    const dc = this.protocol === "live"
      ? pc.createDataChannel("oai-events", { ordered: true })
      : pc.createDataChannel("", { negotiated: true, id: 0, ordered: true });
    let liveStarted = false;
    let liveStartupError = "";
    this.dataChannel = dc;
    dc.onmessage = (message) => {
      if (this.closed) return;
      if (typeof message.data !== "string") return;
      try {
        const event = decodeDataChannelMessage(message.data);
        if (event.type === "session.started") liveStarted = true;
        if (event.type === "error" && !liveStarted) {
          const error = event.error as { message?: string } | undefined;
          liveStartupError = error?.message || "Live 会话启动失败";
        }
        const payload = event.payload;
        this.handlers.onEvent(
          payload && typeof payload === "object"
            ? { type: event.type, ...(payload as Record<string, unknown>) }
            : event,
        );
        if (event.type === "session.closed") this.handlers.onConnectionState("disconnected");
      } catch {
        this.handlers.onEvent({ type: "datachannel.message", raw: message.data.slice(0, 1000) });
      }
    };
    dc.onopen = () => {
      if (this.protocol === "live") return;
      this.sendWrapped({
        type: "track_state",
        payload: {
          type: "track_state",
          track_id: "microphone",
          media_type: "audio",
          media_source: "microphone",
          state: "live",
        },
      });
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await waitForIceGathering(pc);
    if (this.closed) throw new Error("连接已取消");
    if (!pc.localDescription?.sdp) throw new Error("无法生成 WebRTC SDP offer");

    const signalingAbort = new AbortController();
    this.signalingAbort = signalingAbort;
    const signalingTimeout = window.setTimeout(() => signalingAbort.abort(), 60_000);
    signalingAbort.signal.addEventListener("abort", () => window.clearTimeout(signalingTimeout), { once: true });

    let answerSdp: string;
    let location = "";
    let callId = "";
    let sessionHandle = "";
    let requestId = "";
    try {
      if (this.protocol === "live") {
        const response = await fetch(realtimeEndpoint(options.baseUrl, "/v1/live/sessions"), {
          method: "POST",
          headers: { Authorization: options.authorization, "Content-Type": "application/json" },
          body: JSON.stringify({
            session: {
              model: "gpt-live-1",
              audio: { output: { voice: options.voice } },
              ...(options.initialHistory?.length ? { input: options.initialHistory } : {}),
            },
            transport: { type: "webrtc", sdp: pc.localDescription.sdp },
          }),
          signal: signalingAbort.signal,
        });
        if (!response.ok) throw await signalingErrorFromResponse(response, "Live 信令失败");
        const result = await response.json() as { session: { id: string }; transport: { type: string; sdp: string } };
        answerSdp = result.transport.sdp;
        callId = result.session.id;
        location = `/v1/live/sessions/${callId}`;
        requestId = response.headers.get("X-Request-ID") || "";
      } else {
        // 第一步：用项目 API Key 换取 OpenAI GA 形状的 ephemeral key。
        // 续接/重试参数放在 session.chatgpt2api 扩展命名空间中，切换官方
        // API 时删除该字段即可。
        const chatgpt2api: Record<string, string> = {};
        if (options.attemptId) chatgpt2api.attempt_id = options.attemptId;
        if (options.conversationId) chatgpt2api.conversation_id = options.conversationId;
        if (options.parentMessageId) chatgpt2api.parent_message_id = options.parentMessageId;
        if (options.resumeHandle) chatgpt2api.resume_handle = options.resumeHandle;
        const secretResponse = await fetch(realtimeEndpoint(options.baseUrl, "/v1/realtime/client_secrets"), {
          method: "POST",
          headers: {
            Authorization: options.authorization,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            session: {
              type: "realtime",
              audio: { output: { voice: options.voice } },
              ...(Object.keys(chatgpt2api).length > 0 ? { chatgpt2api } : {}),
            },
          }),
          signal: signalingAbort.signal,
        });
        if (!secretResponse.ok) {
          throw await signalingErrorFromResponse(secretResponse, "实时信令失败", options.attemptId || "");
        }
        const secret = (await secretResponse.json()) as { value?: string };
        if (!secret.value) {
          throw new Error("实时信令返回了无效的 ephemeral key");
        }

        // 第二步：官方 GA 形状的 SDP 交换 —— 裸 SDP 进，裸 SDP 出，
        // call id 在 Location 响应头。
        const callResponse = await fetch(realtimeEndpoint(options.baseUrl, "/v1/realtime/calls"), {
          method: "POST",
          headers: {
            Authorization: `Bearer ${secret.value}`,
            "Content-Type": "application/sdp",
          },
          body: pc.localDescription.sdp,
          signal: signalingAbort.signal,
        });
        if (!callResponse.ok) {
          throw await signalingErrorFromResponse(callResponse, "实时信令失败", options.attemptId || "");
        }
        answerSdp = await callResponse.text();
        if (!answerSdp.trim()) throw new Error("实时信令返回了无效响应");
        location = callResponse.headers.get("Location") || "";
        callId = callResponse.headers.get("X-Attempt-Id")
          || location.split("/").filter(Boolean).pop()
          || options.attemptId
          || "";
        sessionHandle = callResponse.headers.get("X-Session-Handle")
          || callResponse.headers.get("X-Resume-Handle")
          || "";

        requestId = callResponse.headers.get("X-Request-ID") || "";
      }
    } finally {
      window.clearTimeout(signalingTimeout);
      if (this.signalingAbort === signalingAbort) this.signalingAbort = null;
    }
    // Quota events can arrive as soon as the DataChannel opens, before connect()
    // finishes awaiting both transports. Make the report context available first.
    this.sessionReport = {
      authorization: options.authorization,
      baseUrl: options.baseUrl,
      callId,
    };
    if (this.closed) throw new Error("连接已取消");
    await pc.setRemoteDescription({ type: "answer", sdp: normalizeSdpLineEndings(answerSdp) });
    await Promise.all([waitForConnection(pc), waitForDataChannel(dc)]);
    if (this.protocol === "live") {
      const deadline = Date.now() + DATA_CHANNEL_TIMEOUT_MS;
      while (!liveStarted) {
        if (liveStartupError) throw new Error(liveStartupError);
        if (this.closed || dc.readyState !== "open" || Date.now() > deadline) throw new Error("未收到 session.started");
        await new Promise(resolve => window.setTimeout(resolve, 25));
      }
      this.setMicrophoneEnabled(options.microphoneEnabled !== false);
    }
    return {
      location,
      attemptId: callId,
      requestId,
      sessionHandle,
      resumeHandle: sessionHandle,
    };
  }

  async reportQuotaExhausted(details?: {
    reason?: string;
    restoreAt?: string;
    retryAfterSeconds?: number;
  }): Promise<void> {
    if (this.protocol === "live") return;
    const report = this.sessionReport;
    if (!report?.callId) return;
    try {
      await fetch(realtimeEndpoint(report.baseUrl, `/v1/realtime/calls/${encodeURIComponent(report.callId)}/quota-exhausted`), {
        method: "POST",
        headers: {
          Authorization: report.authorization,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          reason: details?.reason || "quota_exhausted",
          restore_at: details?.restoreAt,
          retry_after_seconds: details?.retryAfterSeconds,
        }),
        keepalive: true,
      });
    } catch {
      // Retry-chain exclusion still works even if the global cooldown report fails.
    }
  }

  sendEvent(event: RealtimeEvent): void {
    this.sendWrapped(event);
  }

  sendTextMessage(text: string): string {
    if (!text.trim()) throw new Error("文字消息不能为空");
    if (this.dataChannel?.readyState !== "open") throw new Error("实时事件通道未连接");

    const messageId = crypto.randomUUID();
    if (this.protocol === "live") {
      this.sendWrapped({ type: "session.instructions.append", event_id: messageId, delegation_id: null, content: text });
      return messageId;
    }
    this.sendWrapped({
      type: "relay_message",
      payload: {
        type: "relay_message",
        message: {
          id: messageId,
          author: { role: "user" },
          create_time: Date.now() / 1000,
          content: { content_type: "text", parts: [text] },
          metadata: { serialization_metadata: { custom_symbol_offsets: [] } },
          clientMetadata: { isOptimistic: true },
        },
      },
    });
    return messageId;
  }

  setMicrophoneEnabled(enabled: boolean): void {
    this.microphone?.getAudioTracks().forEach((track) => {
      track.enabled = enabled;
    });
    if (this.protocol === "live") {
      this.sendWrapped({ type: enabled ? "session.input_audio.unmute" : "session.input_audio.mute" });
      return;
    }
    this.sendWrapped({
      type: "track_state",
      payload: {
        type: "track_state",
        track_id: "microphone",
        media_type: "audio",
        media_source: "microphone",
        state: enabled ? "live" : "muted",
      },
    });
  }

  getMicrophoneStream(): MediaStream | null {
    return this.microphone;
  }

  getRemoteStream(): MediaStream | null {
    return this.remoteStream;
  }

  close(): void {
    this.closed = true;
    this.signalingAbort?.abort();
    this.signalingAbort = null;
    if (this.statsTimer !== null) window.clearInterval(this.statsTimer);
    this.statsTimer = null;
    this.audioReceiver = null;
    this.previousAudioStats = null;
    this.healthyQualitySamples = 0;
    this.jitterBufferTargetMs = INITIAL_JITTER_BUFFER_MS;
    this.sessionReport = null;
    if (this.dataChannel?.readyState === "open") {
      this.setMicrophoneEnabled(false);
    }
    const closingChannel = this.dataChannel;
    const closingPeer = this.pc;
    if (this.protocol === "live" && closingChannel?.readyState === "open") {
      const finish = () => { closingChannel.close(); closingPeer?.close(); };
      const timeout = window.setTimeout(finish, 3000);
      closingChannel.addEventListener("message", (message) => {
        try {
          if (JSON.parse(message.data).type === "session.closed") {
            window.clearTimeout(timeout);
            finish();
          }
        } catch { /* Ignore malformed terminal events. */ }
      });
      closingChannel.send(JSON.stringify({ type: "session.close" }));
    } else {
      closingChannel?.close();
      closingPeer?.close();
    }
    this.dataChannel = null;
    this.microphone?.getTracks().forEach((track) => track.stop());
    this.microphone = null;
    this.remoteStream = null;
    if (this.audioElement) {
      this.audioElement.pause();
      this.audioElement.srcObject = null;
      this.audioElement = null;
    }
    if (this.pc) {
      this.pc.ontrack = null;
      this.pc.onconnectionstatechange = null;
      this.pc = null;
    }
  }

  private sendWrapped(event: RealtimeEvent): void {
    if (this.dataChannel?.readyState !== "open") return;
    this.dataChannel.send(JSON.stringify(this.protocol === "live"
      ? event
      : { type: "data_message", data: JSON.stringify(event) }));
  }

  private setJitterBufferTarget(targetMs: number): void {
    const receiver = this.audioReceiver;
    if (!receiver) return;
    const clamped = Math.max(INITIAL_JITTER_BUFFER_MS, Math.min(MAX_JITTER_BUFFER_MS, Math.round(targetMs)));
    const targetable = receiver as unknown as Record<string, number | null | undefined>;
    try {
      if ("jitterBufferTarget" in targetable) {
        targetable.jitterBufferTarget = clamped;
      } else if ("playoutDelayHint" in targetable) {
        targetable.playoutDelayHint = clamped / 1000;
      }
      this.jitterBufferTargetMs = clamped;
    } catch {
      // Older browsers expose one of these experimental properties as read-only.
    }
  }

  private startQualitySampling(pc: RTCPeerConnection): void {
    if (!this.handlers.onQuality || this.statsTimer !== null) return;
    const sample = async () => {
      if (this.closed || pc.connectionState !== "connected") return;
      try {
        const report = await pc.getStats();
        const quality: RealtimeConnectionQuality = {};
        report.forEach((stat) => {
          if (stat.type === "inbound-rtp" && stat.kind === "audio") {
            quality.jitterMs = typeof stat.jitter === "number" ? Math.round(stat.jitter * 1000) : undefined;
            quality.packetsLost = typeof stat.packetsLost === "number" ? stat.packetsLost : undefined;
            quality.packetsReceived = typeof stat.packetsReceived === "number" ? stat.packetsReceived : undefined;
            quality.concealedSamples = typeof stat.concealedSamples === "number" ? stat.concealedSamples : undefined;
            const current = {
              packetsReceived: Number(stat.packetsReceived || 0),
              packetsLost: Number(stat.packetsLost || 0),
              concealedSamples: Number(stat.concealedSamples || 0),
              totalSamplesReceived: Number(stat.totalSamplesReceived || 0),
              jitterBufferDelay: Number(stat.jitterBufferDelay || 0),
              jitterBufferEmittedCount: Number(stat.jitterBufferEmittedCount || 0),
            };
            const previous = this.previousAudioStats;
            if (previous) {
              const receivedDelta = Math.max(0, current.packetsReceived - previous.packetsReceived);
              const lostDelta = Math.max(0, current.packetsLost - previous.packetsLost);
              const packetTotal = receivedDelta + lostDelta;
              const sampleDelta = Math.max(0, current.totalSamplesReceived - previous.totalSamplesReceived);
              const concealedDelta = Math.max(0, current.concealedSamples - previous.concealedSamples);
              quality.packetLossPercent = packetTotal > 0 ? lostDelta / packetTotal * 100 : 0;
              quality.concealedSamplePercent = sampleDelta > 0 ? concealedDelta / sampleDelta * 100 : 0;
              const emitted = current.jitterBufferEmittedCount - previous.jitterBufferEmittedCount;
              if (emitted > 0) {
                quality.jitterBufferMs = Math.round(Math.max(0, current.jitterBufferDelay - previous.jitterBufferDelay) / emitted * 1000);
              }

              // Average jitter misses short late-packet bursts. Concealment
              // without packet loss means playback ran ahead of arrivals.
              const lateAudio = (quality.concealedSamplePercent || 0) > 1
                && (quality.packetLossPercent || 0) < 1;
              const desired = Math.min(MAX_JITTER_BUFFER_MS, Math.max(INITIAL_JITTER_BUFFER_MS,
                Math.ceil((quality.jitterMs || 0) * 2 / 20) * 20,
                lateAudio ? this.jitterBufferTargetMs + 40 : INITIAL_JITTER_BUFFER_MS));
              if (desired > this.jitterBufferTargetMs) {
                this.healthyQualitySamples = 0;
                this.setJitterBufferTarget(desired);
              } else {
                this.healthyQualitySamples += 1;
                if (this.healthyQualitySamples >= 15 && this.jitterBufferTargetMs > desired) {
                  this.setJitterBufferTarget(Math.max(desired, this.jitterBufferTargetMs - 40));
                  this.healthyQualitySamples = 0;
                }
              }
            }
            this.previousAudioStats = current;
          } else if (stat.type === "candidate-pair" && stat.state === "succeeded" && stat.nominated) {
            quality.roundTripTimeMs = typeof stat.currentRoundTripTime === "number"
              ? Math.round(stat.currentRoundTripTime * 1000)
              : undefined;
            const local = report.get(stat.localCandidateId);
            if (local && typeof local.candidateType === "string") quality.candidateType = local.candidateType;
          }
        });
        this.handlers.onQuality?.(quality);
      } catch {
        // A stats query can race with close(); the next connected session will restart it.
      }
    };
    void sample();
    this.statsTimer = window.setInterval(() => void sample(), 1_000);
  }
}
