// Exercise the shipped TS client and its actual inline worklet, without devices or API calls.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import ts from 'typescript';

async function load(path, globals = {}) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8');
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const exports = {};
  const context = vm.createContext({ exports, console, Float32Array, Int16Array, Uint8Array, ArrayBuffer, Math, Date, Set, Map, ...globals });
  vm.runInContext(code, context);
  return exports;
}

const { LiveTranscriptGrouper } = await load('../src/lib/realtime-transcript.ts');
const group = new LiveTranscriptGrouper();
const fragment = (type, delta, at) => ({ type, delta, start_ms: at, end_ms: at });
const u1 = group.push(fragment('session.input_transcript.delta', '你', 0));
const a1 = group.push(fragment('session.output_transcript.delta', '嗯', 50));
const u2 = group.push(fragment('session.input_transcript.delta', ' 好 好', 100));
assert.equal(u1.sourceId, u2.sourceId);
assert.notEqual(a1.sourceId, u1.sourceId);
assert.equal(u2.text, ' 好 好');
assert.notEqual(group.push(fragment('session.input_transcript.delta', 'next', 2500)).sourceId, u1.sourceId);
assert.notEqual(new LiveTranscriptGrouper().push(fragment('session.input_transcript.delta', 'new session', 0)).sourceId, u1.sourceId);
assert.equal(group.push({ type: 'session.output_audio.delta', delta: 'AAAA' }), null);

const processors = {};
const registeredNodes = [];
let workletSource;
let requestedLatency;
class Node {
  connect() {}
  disconnect() {}
}
class Context {
  constructor(options) {
    // Simulate a device that does not honor the requested 24kHz context rate.
    this.sampleRate = 48000;
    this.destination = new Node();
    requestedLatency = options.latencyHint;
    this.audioWorklet = { addModule: async () => {
      const worklet = vm.createContext({
        Float32Array, Int16Array, Math, sampleRate: 48000,
        AudioWorkletProcessor: class { constructor() { this.port = { postMessage() {}, onmessage: null }; } },
        registerProcessor: (name, processor) => { processors[name] = processor; },
      });
      vm.runInContext(workletSource, worklet);
    }};
  }
  async resume() {}
  createGain() { return Object.assign(new Node(), { gain: { value: 1 } }); }
  createMediaStreamDestination() { return Object.assign(new Node(), { stream: {} }); }
  createMediaStreamSource() { return new Node(); }
}
class WorkletNode extends Node {
  constructor(context, name, options) {
    super();
    this.processor = new processors[name](options);
    this.port = { postMessage: data => this.processor.port.onmessage?.({ data }) };
    this.processor.port.postMessage = data => this.port.onmessage?.({ data });
    registeredNodes.push(this);
  }
}
const globals = {
  AudioContext: Context, AudioWorkletNode: WorkletNode,
  Blob: class { constructor(parts) { workletSource = parts.join(''); } },
  URL: { createObjectURL: () => 'blob:test', revokeObjectURL() {} },
  WebSocket: { OPEN: 1 },
  window: { setTimeout, clearTimeout },
  btoa: text => Buffer.from(text, 'binary').toString('base64'),
  atob: text => Buffer.from(text, 'base64').toString('binary'),
};
const { RealtimeWebSocketConnection } = await load('../src/lib/realtime-websocket.ts', globals);
const connection = new RealtimeWebSocketConnection({ onEvent() {}, onConnectionState() {} }, 'live');
connection.closed = false;
await connection.setupAudioGraph({});
assert.equal(requestedLatency, 'interactive');
const sent = [];
connection.socket = { readyState: 1, bufferedAmount: 0, send: value => sent.push(JSON.parse(value)) };
connection.connected = false;
connection.sendPcm(new Int16Array(960).buffer);
assert.equal(sent.length, 0, 'Do not stream audio before session.started');
connection.connected = true;
connection.sendPcm(new Int16Array(960).fill(1000).buffer);
assert.equal(sent[0].type, 'session.input_audio.append');
assert.equal(Buffer.from(sent[0].audio, 'base64').length, 960, '48k device frames become real 24k PCM bytes');

// Exercise the exact production worklet's initial prefill, stop and underflow handling.
const player = registeredNodes[1].processor;
const output = [ [new Float32Array(128)] ];
player.port.onmessage({ data: new Float32Array(4000).fill(0.25) });
player.process([], output);
assert.ok(output[0][0].every(value => value === 0), 'wait for prefill');
player.port.onmessage({ data: new Float32Array(5000).fill(0.25) });
player.process([], output);
assert.equal(output[0][0][0], 0, 'start from silence without a hard step');
player.process([], output);
player.process([], output);
assert.ok(output[0][0].every(value => value === 0.25), 'play at the device sample rate');
player.port.onmessage({ data: { type: 'stop' } });
player.process([], output);
assert.equal(output[0][0][0], 0.25, 'interrupt fades the last sample instead of snapping to zero');
assert.ok(Math.max(...output[0][0].slice(1).map((v, i) => Math.abs(v - output[0][0][i]))) < 0.002);
player.process([], output);
player.process([], output);
assert.ok(output[0][0].every(value => value === 0), 'interrupt discards queued speech');
player.port.onmessage({ data: new Float32Array(100).fill(0.5) });
player.port.onmessage({ data: { type: 'end' } });
player.process([], output);
assert.ok(output[0][0].slice(0, 100).some(value => value > 0), 'short final audio is not stuck below prefill');
player.process([], output);
player.process([], output);
player.process([], output);
assert.ok(output[0][0].every(value => value === 0), 'short tail fades to silence');
player.port.onmessage({ data: new Float32Array(100).fill(0.5) });
player.process([], output);
assert.ok(output[0][0].every(value => value === 0), 'underrun recovery waits for refill instead of stuttering');
player.port.onmessage({ data: new Float32Array(9000).fill(0.5) });
player.process([], output);
assert.equal(output[0][0][0], 0, 'recovered audio fades in');
assert.ok(output[0][0].some(value => value > 0));

// A 720ms TCP burst must preserve every sample, rather than cutting speech at 600ms.
player.port.onmessage({ data: { type: 'stop' } });
for (let i = 0; i < 18; i++) player.port.onmessage({ data: new Float32Array(1920).fill(0.25) });
assert.equal(player.available, 18 * 1920, 'ordinary bursts must not drop buffered words');
// A genuinely stale backlog is still bounded by the two-second ring.
player.port.onmessage({ data: { type: 'stop' } });
for (let i = 0; i < 60; i++) player.port.onmessage({ data: new Float32Array(1920).fill(i / 100) });
assert.ok(player.available <= 48000 * 2, 'worklet cannot retain seconds of stale speech');
player.port.onmessage({ data: new Float32Array(48000 * 3).fill(0.75) });
assert.ok(player.available <= 48000 * 2, 'oversized chunks are also bounded');

// Exercise the actual fallback scheduler, including future scheduled sources.
const fallbackContext = {
  sampleRate: 48000, currentTime: 0,
  createBuffer: (_, length) => ({ getChannelData: () => new Float32Array(length) }),
  createBufferSource: () => ({ connect() {}, disconnect() {}, start() {}, stop() {} }),
};
const scheduler = connection.fallbackScheduler;
scheduler.context = fallbackContext;
let stoppedSources = 0;
fallbackContext.createBufferSource = () => ({ connect() {}, disconnect() {}, start() {}, stop() { stoppedSources++; } });
for (let i = 0; i < 18; i++) scheduler.push(new Float32Array(1920));
assert.equal(stoppedSources, 0, 'fallback must preserve a 720ms burst too');
assert.ok(Math.abs(scheduler.nextAt - 0.74) < 1e-6);
for (let i = 0; i < 80; i++) scheduler.push(new Float32Array(1920));
assert.ok(scheduler.nextAt <= 2.02, 'fallback also discards stale scheduled sources');
fallbackContext.currentTime = 5;
scheduler.push(new Float32Array(960));
assert.equal(scheduler.buffering, true, 'fallback refills after an underrun');
scheduler.end();
assert.ok(scheduler.nextAt > 5, 'short tail is released');

// Real quality sampling must not turn unrecoverable packet loss into 600ms delay.
let statsTick = 0;
let jitter = 0.001;
let sampleQuality;
let sampleCallback;
const { RealtimeWebRTCConnection } = await load('../src/lib/realtime-webrtc.ts', {
  window: { setInterval: callback => { sampleCallback = callback; return 1; }, clearInterval() {} },
});
const rtc = new RealtimeWebRTCConnection({ onEvent() {}, onConnectionState() {}, onQuality: q => { sampleQuality = q; } }, 'live');
rtc.closed = false;
rtc.audioReceiver = { jitterBufferTarget: 0 };
rtc.setJitterBufferTarget(120);
const peer = { connectionState: 'connected', getStats: async () => {
  statsTick++;
  return new Map([['audio', { type: 'inbound-rtp', kind: 'audio', jitter,
    packetsReceived: statsTick * 45, packetsLost: statsTick * 5,
    concealedSamples: statsTick * 4800, totalSamplesReceived: statsTick * 48000,
    jitterBufferDelay: statsTick * 4800, jitterBufferEmittedCount: statsTick * 48000,
  }]]);
}};
rtc.startQualitySampling(peer);
const sampleRtc = async () => { sampleCallback(); await new Promise(setImmediate); };
await new Promise(setImmediate);
for (let i = 0; i < 20; i++) await sampleRtc();
assert.equal(rtc.audioReceiver.jitterBufferTarget, 120, 'loss alone must not ratchet buffering');
assert.equal(sampleQuality.jitterBufferMs, 100, 'display current interval buffering');
jitter = 0.12;
await sampleRtc();
assert.equal(rtc.audioReceiver.jitterBufferTarget, 240, 'jitter protection has a conversational cap');
jitter = 0.001;
for (let i = 0; i < 45; i++) await sampleRtc();
assert.equal(rtc.audioReceiver.jitterBufferTarget, 120, 'recover promptly when jitter settles');
console.log('Live client: transcripts, continuity, resampling, bounded worklet/fallback latency and jitter recovery passed');

// Worklet loading and blocked audio activation must never hang before connect's timeout.
class HungWorkletContext extends Context {
  constructor(options) { super(options); this.audioWorklet.addModule = () => new Promise(() => {}); }
  createScriptProcessor() { return new Node(); }
  async close() {}
}
const boundedTimers = { setTimeout: callback => setTimeout(callback, 1), clearTimeout };
const hung = await load('../src/lib/realtime-websocket.ts', { ...globals, AudioContext: HungWorkletContext, window: boundedTimers });
const recovering = new hung.RealtimeWebSocketConnection({ onEvent() {}, onConnectionState() {} }, 'live');
recovering.closed = false;
await recovering.setupAudioGraph({});
assert.ok(recovering.captureNode, 'pending addModule must fall back to capture');
assert.equal(recovering.playbackNode, null);
const canceled = new hung.RealtimeWebSocketConnection({ onEvent() {}, onConnectionState() {} }, 'live');
canceled.closed = false;
const preparing = canceled.setupAudioGraph({});
await Promise.resolve();
canceled.close();
await preparing.catch(error => assert.match(error.message, /连接已取消/));
assert.equal(canceled.captureNode, null, 'canceled startup must not install late audio nodes');
class BlockedAudioContext extends HungWorkletContext { resume() { return new Promise(() => {}); } }
const blocked = await load('../src/lib/realtime-websocket.ts', { ...globals, AudioContext: BlockedAudioContext, window: boundedTimers });
const activation = new blocked.RealtimeWebSocketConnection({ onEvent() {}, onConnectionState() {} }, 'live');
activation.closed = false;
await assert.rejects(activation.setupAudioGraph({}), /重新点击开始/);
console.log('Live startup: pending worklets, canceled initialization and blocked activation are bounded');
