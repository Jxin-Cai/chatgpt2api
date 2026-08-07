export type RealtimeEvent = {
  type: string;
  [key: string]: unknown;
};

type ConnectOptions = {
  authorization: string;
  voice: string;
  signalingUrl: string;
};

type RealtimeWebRTCHandlers = {
  onEvent: (event: RealtimeEvent) => void;
  onConnectionState: (state: RTCPeerConnectionState) => void;
};

const CONNECTION_TIMEOUT_MS = 15_000;
const ICE_GATHERING_TIMEOUT_MS = 5_000;

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

export class RealtimeWebRTCConnection {
  private pc: RTCPeerConnection | null = null;
  private dataChannel: RTCDataChannel | null = null;
  private microphone: MediaStream | null = null;
  private audioElement: HTMLAudioElement | null = null;
  private closed = true;

  constructor(private readonly handlers: RealtimeWebRTCHandlers) {}

  async connect(options: ConnectOptions): Promise<{ location: string }> {
    this.close();
    this.closed = false;

    const pc = new RTCPeerConnection();
    this.pc = pc;
    pc.onconnectionstatechange = () => this.handlers.onConnectionState(pc.connectionState);

    const audio = new Audio();
    audio.autoplay = true;
    audio.setAttribute("playsinline", "");
    this.audioElement = audio;
    pc.ontrack = (event) => {
      const [stream] = event.streams;
      if (stream) {
        audio.srcObject = stream;
        void audio.play().catch(() => undefined);
      }
    };

    const microphone = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    if (this.closed) {
      microphone.getTracks().forEach((track) => track.stop());
      throw new Error("连接已取消");
    }
    this.microphone = microphone;
    microphone.getAudioTracks().forEach((track) => pc.addTrack(track, microphone));
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

    const response = await fetch(options.signalingUrl, {
      method: "POST",
      headers: {
        Authorization: options.authorization,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ sdp: pc.localDescription.sdp, voice: options.voice }),
    });
    const responseText = await response.text();
    let result: { sdp?: string; location?: string; detail?: string } = {};
    try {
      result = JSON.parse(responseText) as typeof result;
    } catch {
      if (!response.ok) {
        throw new Error(`实时信令失败 (HTTP ${response.status})`);
      }
      throw new Error("实时信令返回了无效响应");
    }
    if (!response.ok || !result.sdp) {
      throw new Error(result.detail || `实时信令失败 (HTTP ${response.status})`);
    }

    if (this.closed) throw new Error("连接已取消");
    await pc.setRemoteDescription({ type: "answer", sdp: normalizeSdpLineEndings(result.sdp) });
    await waitForConnection(pc);
    return { location: result.location || "" };
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

  close(): void {
    this.closed = true;
    if (this.dataChannel?.readyState === "open") {
      this.setMicrophoneEnabled(false);
    }
    this.dataChannel?.close();
    this.dataChannel = null;
    this.microphone?.getTracks().forEach((track) => track.stop());
    this.microphone = null;
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
}
