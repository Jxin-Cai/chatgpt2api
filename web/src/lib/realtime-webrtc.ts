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
};

export type RealtimeConnectionQuality = {
  roundTripTimeMs?: number;
  jitterMs?: number;
  packetsLost?: number;
  packetsReceived?: number;
  concealedSamples?: number;
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

type RealtimeWebRTCHandlers = {
  onEvent: (event: RealtimeEvent) => void;
  onConnectionState: (state: RTCPeerConnectionState) => void;
  onRemoteStream?: (stream: MediaStream) => void;
  onQuality?: (quality: RealtimeConnectionQuality) => void;
  onMicrophoneEnded?: () => void;
  onMicrophoneState?: (state: "live" | "muted", settings: MediaTrackSettings) => void;
};

const CONNECTION_TIMEOUT_MS = 15_000;
const ICE_GATHERING_TIMEOUT_MS = 5_000;
const DATA_CHANNEL_TIMEOUT_MS = 10_000;

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

export class RealtimeWebRTCConnection {
  private pc: RTCPeerConnection | null = null;
  private dataChannel: RTCDataChannel | null = null;
  private microphone: MediaStream | null = null;
  private remoteStream: MediaStream | null = null;
  private audioElement: HTMLAudioElement | null = null;
  private signalingAbort: AbortController | null = null;
  private statsTimer: number | null = null;
  private sessionReport: { authorization: string; baseUrl: string; callId: string } | null = null;
  private closed = true;

  constructor(private readonly handlers: RealtimeWebRTCHandlers) {}

  async connect(options: ConnectOptions): Promise<{
    location: string;
    attemptId: string;
    requestId: string;
    sessionHandle: string;
    resumeHandle: string;
  }> {
    this.close();
    this.closed = false;

    const pc = new RTCPeerConnection();
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
      pc.addTrack(track, microphone);
      notifyMicrophoneState();
    });
    pc.addTransceiver("video", { direction: "sendonly" });

    const dc = pc.createDataChannel("", { negotiated: true, id: 0, ordered: true });
    this.dataChannel = dc;
    dc.onmessage = (message) => {
      if (typeof message.data !== "string") return;
      try {
        const event = decodeDataChannelMessage(message.data);
        const payload = event.payload;
        this.handlers.onEvent(
          payload && typeof payload === "object"
            ? { type: event.type, ...(payload as Record<string, unknown>) }
            : event,
        );
      } catch {
        this.handlers.onEvent({ type: "datachannel.message", raw: message.data.slice(0, 1000) });
      }
    };
    dc.onopen = () => {
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
      if (this.signalingAbort === signalingAbort) this.signalingAbort = null;
      throw await signalingErrorFromResponse(secretResponse, "实时信令失败", options.attemptId || "");
    }
    const secret = (await secretResponse.json()) as { value?: string };
    if (!secret.value) {
      if (this.signalingAbort === signalingAbort) this.signalingAbort = null;
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
    if (this.signalingAbort === signalingAbort) this.signalingAbort = null;
    if (!callResponse.ok) {
      throw await signalingErrorFromResponse(callResponse, "实时信令失败", options.attemptId || "");
    }
    const answerSdp = await callResponse.text();
    if (!answerSdp.trim()) throw new Error("实时信令返回了无效响应");
    const location = callResponse.headers.get("Location") || "";
    const callId = callResponse.headers.get("X-Attempt-Id")
      || location.split("/").filter(Boolean).pop()
      || options.attemptId
      || "";
    const sessionHandle = callResponse.headers.get("X-Session-Handle")
      || callResponse.headers.get("X-Resume-Handle")
      || "";

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
    return {
      location,
      attemptId: callId,
      requestId: callResponse.headers.get("X-Request-ID") || "",
      sessionHandle,
      resumeHandle: sessionHandle,
    };
  }

  async reportQuotaExhausted(details?: {
    reason?: string;
    restoreAt?: string;
    retryAfterSeconds?: number;
  }): Promise<void> {
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
    this.sessionReport = null;
    if (this.dataChannel?.readyState === "open") {
      this.setMicrophoneEnabled(false);
    }
    this.dataChannel?.close();
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
      this.pc.close();
      this.pc = null;
    }
  }

  private sendWrapped(event: RealtimeEvent): void {
    if (this.dataChannel?.readyState !== "open") return;
    this.dataChannel.send(JSON.stringify({ type: "data_message", data: JSON.stringify(event) }));
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
    this.statsTimer = window.setInterval(() => void sample(), 5_000);
  }
}
