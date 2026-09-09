import {
  type ConnectOptions,
  type RealtimeConnection,
  type RealtimeConnectionHandlers,
  type RealtimeConnectionResult,
  type RealtimeEvent,
} from "@/lib/realtime-webrtc";

const SAMPLE_RATE = 48_000;
const CAPTURE_CHUNK_SAMPLES = 1_920; // 40ms
const PLAYOUT_LEAD_SECONDS = 0.42;
const PLAYOUT_MAX_LEAD_SECONDS = 0.84;
const CONNECTION_TIMEOUT_MS = 35_000;

class LinearPcmResampler {
  private leftover = new Float32Array(0);
  private position = 0;

  constructor(
    private readonly sourceRate: number,
    private readonly targetRate: number,
  ) {}

  reset(): void {
    this.leftover = new Float32Array(0);
    this.position = 0;
  }

  push(pcm: Int16Array): Float32Array {
    const incoming = new Float32Array(pcm.length);
    for (let index = 0; index < pcm.length; index += 1) incoming[index] = pcm[index] / 32768;
    if (this.sourceRate === this.targetRate) return incoming;

    const combined = new Float32Array(this.leftover.length + incoming.length);
    combined.set(this.leftover);
    combined.set(incoming, this.leftover.length);
    const ratio = this.sourceRate / this.targetRate;
    const outLength = Math.floor((combined.length - this.position - 1) / ratio);
    if (outLength <= 0) {
      this.leftover = combined;
      return new Float32Array(0);
    }
    const out = new Float32Array(outLength);
    let position = this.position;
    for (let index = 0; index < outLength; index += 1) {
      const i0 = Math.floor(position);
      const frac = position - i0;
      out[index] = combined[i0] * (1 - frac) + combined[i0 + 1] * frac;
      position += ratio;
    }
    const consumed = Math.floor(position);
    this.leftover = combined.slice(consumed);
    this.position = position - consumed;
    return out;
  }
}

class BufferSourceScheduler {
  private nextAt = 0;
  private pending: Float32Array[] = [];
  private pendingSamples = 0;
  private buffering = true;

  constructor(
    private readonly context: AudioContext,
    private readonly output: AudioNode,
    private readonly prefillSamples: number,
    private readonly activeSources: Set<AudioBufferSourceNode>,
  ) {}

  push(samples: Float32Array): void {
    if (this.buffering) {
      this.pending.push(samples);
      this.pendingSamples += samples.length;
      if (this.pendingSamples >= this.prefillSamples) this.flushPending();
      return;
    }
    if (this.nextAt < this.context.currentTime + 0.02) {
      this.buffering = true;
      this.pending = [samples];
      this.pendingSamples = samples.length;
      this.nextAt = 0;
      return;
    }
    this.start(samples, this.nextAt);
    this.nextAt += samples.length / this.context.sampleRate;
  }

  end(): void {
    if (this.buffering && this.pendingSamples > 0) this.flushPending();
  }

  stop(): void {
    this.pending = [];
    this.pendingSamples = 0;
    this.buffering = true;
    this.nextAt = 0;
  }

  private flushPending(): void {
    let at = this.context.currentTime + 0.02;
    for (const chunk of this.pending) {
      this.start(chunk, at);
      at += chunk.length / this.context.sampleRate;
    }
    this.nextAt = at;
    this.pending = [];
    this.pendingSamples = 0;
    this.buffering = false;
  }

  private start(samples: Float32Array, when: number): void {
    const buffer = this.context.createBuffer(1, samples.length, this.context.sampleRate);
    buffer.getChannelData(0).set(samples);
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.output);
    source.start(when);
    this.activeSources.add(source);
    source.onended = () => {
      source.disconnect();
      this.activeSources.delete(source);
    };
  }
}

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
  const evenLength = binary.length & ~1;
  const buffer = new ArrayBuffer(evenLength);
  const bytes = new Uint8Array(buffer);
  for (let index = 0; index < evenLength; index += 1) bytes[index] = binary.charCodeAt(index);
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
  private playbackNode: AudioWorkletNode | null = null;
  private playbackStreamDestination: MediaStreamAudioDestinationNode | null = null;
  private remoteStream: MediaStream | null = null;
  private activeSources = new Set<AudioBufferSourceNode>();
  private fallbackScheduler: BufferSourceScheduler | null = null;
  private playbackResampler: LinearPcmResampler | null = null;
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
        if (event.type === "response.audio.done") {
          this.endScheduledAudio();
          this.handlers.onEvent(event);
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
    this.playbackNode?.disconnect();
    this.playbackGain?.disconnect();
    this.micSource = null;
    this.captureNode = null;
    this.captureSink = null;
    this.playbackNode = null;
    this.playbackGain = null;
    this.playbackResampler = null;
    this.fallbackScheduler = null;
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

    this.playbackResampler = new LinearPcmResampler(SAMPLE_RATE, context.sampleRate);
    this.fallbackScheduler = new BufferSourceScheduler(
      context,
      playbackGain,
      Math.round(context.sampleRate * PLAYOUT_LEAD_SECONDS),
      this.activeSources,
    );

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
        class RealtimePcmPlayback extends AudioWorkletProcessor {
          constructor(options) {
            super();
            const opts = (options && options.processorOptions) || {};
            const ring = Math.max(128, opts.ringSamples || 144000);
            this.buffer = new Float32Array(ring);
            this.prefill = Math.max(128, opts.prefillSamples || Math.round(ring * 0.18));
            this.maxPrefill = Math.max(this.prefill, opts.maxPrefillSamples || this.prefill * 2);
            this.read = 0;
            this.write = 0;
            this.available = 0;
            this.playing = false;
            this.ending = false;
            this.port.onmessage = (event) => {
              const data = event.data;
              if (data && data.type === 'stop') {
                this.read = 0;
                this.write = 0;
                this.available = 0;
                this.playing = false;
                this.ending = false;
                return;
              }
              if (data && data.type === 'end') {
                this.ending = true;
                if (this.available > 0) this.playing = true;
                return;
              }
              let samples = data instanceof Float32Array ? data : new Float32Array(data);
              const space = this.buffer.length - this.available;
              if (space <= 0) return;
              if (samples.length > space) samples = samples.subarray(0, space);
              let offset = 0;
              while (offset < samples.length) {
                const count = Math.min(samples.length - offset, this.buffer.length - this.write);
                this.buffer.set(samples.subarray(offset, offset + count), this.write);
                this.write = (this.write + count) % this.buffer.length;
                this.available += count;
                offset += count;
              }
              if (!this.playing && !this.ending && this.available >= this.prefill) this.playing = true;
            };
          }
          process(_, outputs) {
            const output = outputs[0] && outputs[0][0];
            if (!output) return true;
            if (!this.playing) {
              if (!this.ending && this.available >= this.prefill) this.playing = true;
              else {
                output.fill(0);
                return true;
              }
            }
            for (let i = 0; i < output.length; i += 1) {
              if (this.available > 0) {
                output[i] = this.buffer[this.read];
                this.read = (this.read + 1) % this.buffer.length;
                this.available -= 1;
              } else if (this.ending) {
                output.fill(0, i);
                this.playing = false;
                this.ending = false;
                break;
              } else {
                output.fill(0, i);
                this.playing = false;
                this.prefill = Math.min(this.maxPrefill, this.prefill + output.length * 8);
                break;
              }
            }
            return true;
          }
        }
        registerProcessor('realtime-pcm-capture', RealtimePcmCapture);
        registerProcessor('realtime-pcm-playback', RealtimePcmPlayback);
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
      const playback = new AudioWorkletNode(context, "realtime-pcm-playback", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: {
          ringSamples: Math.max(context.sampleRate * 3, 144_000),
          prefillSamples: Math.round(context.sampleRate * PLAYOUT_LEAD_SECONDS),
          maxPrefillSamples: Math.round(context.sampleRate * PLAYOUT_MAX_LEAD_SECONDS),
        },
      });
      playback.connect(playbackGain);
      this.playbackNode = playback;
    } catch {
      this.playbackNode = null;
    }
    if (this.captureNode) return;
    try {
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
    } catch {
      // 采集节点不可用时仍允许纯播放/文字输入。
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
    const resampler = this.playbackResampler;
    if (!resampler) return;
    const samples = resampler.push(pcm);
    if (!samples.length) return;

    if (this.playbackNode) {
      this.playbackNode.port.postMessage(samples, [samples.buffer]);
      return;
    }
    this.fallbackScheduler?.push(samples);
  }

  private endScheduledAudio(): void {
    this.playbackNode?.port.postMessage({ type: "end" });
    this.fallbackScheduler?.end();
  }

  private stopScheduledAudio(): void {
    this.playbackNode?.port.postMessage({ type: "stop" });
    this.fallbackScheduler?.stop();
    this.playbackResampler?.reset();
    for (const source of this.activeSources) {
      try {
        source.stop();
      } catch {
        // The source may already have ended between iteration and stop().
      }
      source.disconnect();
    }
    this.activeSources.clear();
  }
}
