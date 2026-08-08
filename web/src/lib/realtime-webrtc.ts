export type RealtimeEvent = {
  type: string;
  [key: string]: unknown;
};

type ConnectOptions = {
  authorization: string;
  voice: string;
  signalingUrl: string;
  attemptId?: string;
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
  private sessionReport: { authorization: string; signalingUrl: string; attemptId: string } | null = null;
  private closed = true;

  constructor(private readonly handlers: RealtimeWebRTCHandlers) {}

  async connect(options: ConnectOptions): Promise<{ location: string; attemptId: string; requestId: string }> {
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
    const response = await fetch(options.signalingUrl, {
      method: "POST",
      headers: {
        Authorization: options.authorization,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        sdp: pc.localDescription.sdp,
        voice: options.voice,
        attempt_id: options.attemptId,
      }),
      signal: signalingAbort.signal,
    });
    if (this.signalingAbort === signalingAbort) this.signalingAbort = null;
    const responseText = await response.text();
    let result: {
      sdp?: string;
      location?: string;
      attempt_id?: string;
      request_id?: string;
      detail?: string;
      error?: { message?: string; retryable?: boolean; retry_after_ms?: number };
    } = {};
    try {
      result = JSON.parse(responseText) as typeof result;
    } catch {
      if (!response.ok) {
        throw new Error(`实时信令失败 (HTTP ${response.status})`);
      }
      throw new Error("实时信令返回了无效响应");
    }
    if (!response.ok || !result.sdp) {
      const message = result.error?.message || result.detail || `实时信令失败 (HTTP ${response.status})`;
      throw new RealtimeSignalingError(
        message,
        response.status,
        Boolean(result.error?.retryable),
        result.error?.retry_after_ms || 0,
        result.attempt_id || options.attemptId || "",
      );
    }

    // Quota events can arrive as soon as the DataChannel opens, before connect()
    // finishes awaiting both transports. Make the report context available first.
    const attemptId = result.attempt_id || options.attemptId || "";
    this.sessionReport = {
      authorization: options.authorization,
      signalingUrl: options.signalingUrl.replace(/\/$/, ""),
      attemptId,
    };
    if (this.closed) throw new Error("连接已取消");
    await pc.setRemoteDescription({ type: "answer", sdp: normalizeSdpLineEndings(result.sdp) });
    await Promise.all([waitForConnection(pc), waitForDataChannel(dc)]);
    return {
      location: result.location || "",
      attemptId,
      requestId: result.request_id || response.headers.get("X-Request-ID") || "",
    };
  }

  async reportQuotaExhausted(details?: {
    reason?: string;
    restoreAt?: string;
    retryAfterSeconds?: number;
  }): Promise<void> {
    const report = this.sessionReport;
    if (!report?.attemptId) return;
    try {
      await fetch(`${report.signalingUrl}/${encodeURIComponent(report.attemptId)}/quota-exhausted`, {
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
