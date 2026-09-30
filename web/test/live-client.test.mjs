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
  btoa: text => Buffer.from(text, 'binary').toString('base64'),
  atob: text => Buffer.from(text, 'base64').toString('binary'),
};
const { RealtimeWebSocketConnection } = await load('../src/lib/realtime-websocket.ts', globals);
const connection = new RealtimeWebSocketConnection({ onEvent() {}, onConnectionState() {} }, 'live');
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
console.log('Live client: transcripts, worklet continuity, underrun recovery, startup gating and resampling passed');
