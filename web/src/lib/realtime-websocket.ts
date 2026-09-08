import {
  type ConnectOptions,
  type RealtimeConnection,
  type RealtimeConnectionHandlers,
  type RealtimeConnectionResult,
  type RealtimeEvent,
} from "@/lib/realtime-webrtc";

const SAMPLE_RATE = 48_000;
const CAPTURE_CHUNK_SAMPLES = 1_920; // 40ms
const PLAYOUT_LEAD_SECONDS = 0.22;
const CONNECTION_TIMEOUT_MS = 35_000;

function websocketUrl(baseUrl: string, voice: string): string {
  const url = new URL("/v1/realtime", baseUrl || window.location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("voice", voice);
  return url.toString();
}

function bytesToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function base64ToPcm16(value: string): Int16Array {
  const binary = atob(value);
  const buffer = new ArrayBuffer(binary.length);
  const bytes = new Uint8Array(buffer);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return new Int16Array(buffer);
}

export class RealtimeWebSocketConnection implements RealtimeConnection {
  private socket: WebSocket | null = null;
  private microphone: MediaStream | null = null;
  private audioContext: AudioContext | null = null;
  private micSource: MediaStreamAudioSourceNode | null = null;
  private captureNode: AudioNode | null = null;
  private captureSink: GainNode | null = null;
  private playbackGain: GainNode | null = null;
  private playbackStreamDestination: MediaStreamAudioDestinationNode | null = null;
  private remoteStream: MediaStream | null = null;
  private activeSources = new Set<AudioBufferSourceNode>();
  private nextPlaybackAt = 0;
  private microphoneEnabled = true;
  private connected = false;
  private closed = true;

  constructor(private readonly handlers: RealtimeConnectionHandlers) {}

  async connect(options: ConnectOptions): Promise<RealtimeConnectionResult> {
    this.close();
    this.closed = false;
    this.handlers.onConnectionState("connecting");

    const microphone = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        sampleRate: SAMPLE_RATE,
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
    this.bindMicrophoneState(microphone);
    await this.setupAudioGraph(microphone);

    const token = options.authorization.replace(/^Bearer\s+/i, "").trim();
    if (!token) throw new Error("实时中继缺少认证信息");
    const socket = new WebSocket(
      websocketUrl(options.baseUrl, options.voice),
      [`openai-insecure-api-key.${token}`],
    );
    this.socket = socket;

    return await new Promise<RealtimeConnectionResult>((resolve, reject) => {
      let settled = false;
      const timeout = window.setTimeout(() => {
        if (settled) return;
        settled = true;
        reject(new Error("平滑语音中继连接超时"));
        this.close();
      }, CONNECTION_TIMEOUT_MS);

      const finish = (result: RealtimeConnectionResult) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeout);
        resolve(result);
      };
      const fail = (message: string) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeout);
        reject(new Error(message));
      };

      socket.onmessage = (message) => {
        if (typeof message.data !== "string") return;
        let event: RealtimeEvent;
        try {
          event = JSON.parse(message.data) as RealtimeEvent;
        } catch {
          return;
        }
        if (event.type === "response.audio.delta" && typeof event.delta === "string") {
          this.scheduleAudio(event.delta);
          return;
        }
        if (event.type === "input_audio_buffer.speech_started") this.stopScheduledAudio();
        this.handlers.onEvent(event);
        if (event.type === "session.created") {
          this.connected = true;
          this.handlers.onConnectionState("connected");
          this.handlers.onQuality?.({
            roundTripTimeMs: 0,
            jitterMs: 0,
            packetLossPercent: 0,
            concealedSamplePercent: 0,
            candidateType: "relay",
          });
          const session = event.session as { id?: string } | undefined;
          finish({
            location: session?.id || "/v1/realtime",
            attemptId: "",
            requestId: "",
            sessionHandle: "",
            resumeHandle: "",
          });
        } else if (event.type === "error" && !this.connected) {
          const error = event.error as { message?: string } | undefined;
          fail(error?.message || "平滑语音中继连接失败");
        }
      };
      socket.onerror = () => fail("平滑语音中继连接失败");
      socket.onclose = () => {
        const wasConnected = this.connected;
        this.connected = false;
        if (!this.closed) this.handlers.onConnectionState(wasConnected ? "disconnected" : "failed");
        fail("平滑语音中继已关闭");
      };
    });
  }

  async reportQuotaExhausted(): Promise<void> {
    // The relay owns its upstream account and performs quota rotation server-side.
  }

  sendEvent(event: RealtimeEvent): void {
    this.send(event);
  }

  sendTextMessage(text: string): string {
    if (!text.trim()) throw new Error("文字消息不能为空");
    if (this.socket?.readyState !== WebSocket.OPEN) throw new Error("实时事件通道未连接");
    const messageId = crypto.randomUUID();
    this.send({
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
    this.microphoneEnabled = enabled;
    this.microphone?.getAudioTracks().forEach((track) => {
      track.enabled = enabled;
    });
    if (!enabled) this.send({ type: "input_audio_buffer.clear" });
  }

  getMicrophoneStream(): MediaStream | null {
    return this.microphone;
  }

  getRemoteStream(): MediaStream | null {
    return this.remoteStream;
  }

  close(): void {
    this.closed = true;
    this.connected = false;
    const socket = this.socket;
    this.socket = null;
    if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "client closed");
    this.stopScheduledAudio();
    this.micSource?.disconnect();
    this.captureNode?.disconnect();
    this.captureSink?.disconnect();
    this.playbackGain?.disconnect();
    this.micSource = null;
    this.captureNode = null;
    this.captureSink = null;
    this.playbackGain = null;
    this.playbackStreamDestination = null;
    this.remoteStream = null;
    this.microphone?.getTracks().forEach((track) => track.stop());
    this.microphone = null;
    if (this.audioContext) void this.audioContext.close();
    this.audioContext = null;
  }

  private bindMicrophoneState(stream: MediaStream): void {
    stream.getAudioTracks().forEach((track) => {
      const notify = () => {
        if (!this.closed) this.handlers.onMicrophoneState?.(track.muted ? "muted" : "live", track.getSettings());
      };
      track.addEventListener("mute", notify);
      track.addEventListener("unmute", notify);
      track.addEventListener("ended", () => {
        if (!this.closed) this.handlers.onMicrophoneEnded?.();
      }, { once: true });
      notify();
    });
  }

  private async setupAudioGraph(stream: MediaStream): Promise<void> {
    const context = new AudioContext({ sampleRate: SAMPLE_RATE, latencyHint: "playback" });
    this.audioContext = context;
    await context.resume();

    const playbackGain = context.createGain();
    playbackGain.connect(context.destination);
    const streamDestination = context.createMediaStreamDestination();
    playbackGain.connect(streamDestination);
    this.playbackGain = playbackGain;
    this.playbackStreamDestination = streamDestination;
    this.remoteStream = streamDestination.stream;
    this.handlers.onRemoteStream?.(streamDestination.stream);

    const source = context.createMediaStreamSource(stream);
    this.micSource = source;
    const sink = context.createGain();
    sink.gain.value = 0;
    sink.connect(context.destination);
    this.captureSink = sink;

    try {
      const processorSource = `
        class RealtimePcmCapture extends AudioWorkletProcessor {
          constructor() {
            super();
            this.pending = new Float32Array(${CAPTURE_CHUNK_SAMPLES});
            this.offset = 0;
          }
          process(inputs) {
            const input = inputs[0] && inputs[0][0];
            if (!input) return true;
            let sourceOffset = 0;
            while (sourceOffset < input.length) {
              const count = Math.min(input.length - sourceOffset, this.pending.length - this.offset);
              this.pending.set(input.subarray(sourceOffset, sourceOffset + count), this.offset);
              sourceOffset += count;
              this.offset += count;
              if (this.offset === this.pending.length) {
                const pcm = new Int16Array(this.pending.length);
                for (let i = 0; i < this.pending.length; i += 1) {
                  const sample = Math.max(-1, Math.min(1, this.pending[i]));
                  pcm[i] = sample < 0 ? sample * 32768 : sample * 32767;
                }
                this.port.postMessage(pcm.buffer, [pcm.buffer]);
                this.pending = new Float32Array(${CAPTURE_CHUNK_SAMPLES});
                this.offset = 0;
              }
            }
            return true;
          }
        }
        registerProcessor('realtime-pcm-capture', RealtimePcmCapture);
      `;
      const moduleUrl = URL.createObjectURL(new Blob([processorSource], { type: "text/javascript" }));
      await context.audioWorklet.addModule(moduleUrl);
      URL.revokeObjectURL(moduleUrl);
      const processor = new AudioWorkletNode(context, "realtime-pcm-capture", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
      });
      processor.port.onmessage = (message: MessageEvent<ArrayBuffer>) => this.sendPcm(message.data);
      source.connect(processor);
      processor.connect(sink);
      this.captureNode = processor;
    } catch {
      const processor = context.createScriptProcessor(2_048, 1, 1);
      processor.onaudioprocess = (event) => {
        const input = event.inputBuffer.getChannelData(0);
        const pcm = new Int16Array(input.length);
        for (let index = 0; index < input.length; index += 1) {
          const sample = Math.max(-1, Math.min(1, input[index]));
          pcm[index] = sample < 0 ? sample * 32768 : sample * 32767;
        }
        this.sendPcm(pcm.buffer);
      };
      source.connect(processor);
      processor.connect(sink);
      this.captureNode = processor;
    }
  }

  private sendPcm(buffer: ArrayBuffer): void {
    if (!this.microphoneEnabled || this.socket?.readyState !== WebSocket.OPEN) return;
    this.send({ type: "input_audio_buffer.append", audio: bytesToBase64(buffer) });
  }

  private send(event: RealtimeEvent): void {
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    this.socket.send(JSON.stringify(event));
  }

  private scheduleAudio(encoded: string): void {
    const context = this.audioContext;
    const output = this.playbackGain;
    if (!context || !output || this.closed) return;
    const pcm = base64ToPcm16(encoded);
    if (!pcm.length) return;
    const buffer = context.createBuffer(1, pcm.length, SAMPLE_RATE);
    const channel = buffer.getChannelData(0);
    for (let index = 0; index < pcm.length; index += 1) channel[index] = pcm[index] / 32768;

    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(output);
    const now = context.currentTime;
    if (this.nextPlaybackAt < now + 0.04) this.nextPlaybackAt = now + PLAYOUT_LEAD_SECONDS;
    source.start(this.nextPlaybackAt);
    this.nextPlaybackAt += buffer.duration;
    this.activeSources.add(source);
    source.onended = () => {
      source.disconnect();
      this.activeSources.delete(source);
    };
  }

  private stopScheduledAudio(): void {
    for (const source of this.activeSources) {
      try {
        source.stop();
      } catch {
        // The source may already have ended between iteration and stop().
      }
      source.disconnect();
    }
    this.activeSources.clear();
    this.nextPlaybackAt = 0;
  }
}
