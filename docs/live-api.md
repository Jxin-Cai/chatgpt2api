# GPT-Live 兼容语音服务

本项目为 ChatGPT Web Voice 提供 **GPT-Live 语音会话协议子集**。新集成使用 `/v1/live/sessions`；旧 `/v1/realtime/*` 保留给已有客户端。

对外的请求、音频事件、字幕和生命周期采用官方 Live 形状。底层仍是 ChatGPT Web Voice：`gpt-live-1` 是兼容名称，不表示实际运行官方 GPT-Live 权重。通过下文的标准字段集成后，迁往官方时可以保留协议处理代码，替换可信服务器中的服务地址、凭据，并验证能力差异。

核对基准：2026-09-30 [官方 Live 指南](https://developers.openai.com/api/docs/guides/live)、[WebRTC](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live)、[WebSocket](https://developers.openai.com/api/docs/guides/voice-websockets?api=live)，以及 [openai-python 的 Live 实现](https://github.com/openai/openai-python/blob/58aca1dcfd8d04a3c6352fa2c34b3035ea850f57/src/openai/resources/live/live.py)。

## 接口与鉴权

| 端点 | 用途 |
| --- | --- |
| `POST /v1/live/sessions` | JSON SDP 交换，创建 WebRTC 会话 |
| `WS /v1/live/sessions` | 以 `session.start` 启动的主 WebSocket |
| `GET /v1/live/capabilities` | 本项目扩展：查询映射、支持范围、音频限制；官方没有此端点 |

HTTP 与服务器 WebSocket 使用 `Authorization: Bearer <项目 auth-key>`。这里的 auth-key 是本项目凭据，上游账号令牌不会返回给客户端。不要在 URL 中放凭据或模型参数。

调试页在本项目 WS 回退路径中通过 `openai-insecure-api-key.<auth-key>` 子协议传递应用凭据；这是浏览器兼容扩展，不能当成官方 Live 浏览器鉴权方案。切换官方时将 OpenAI Key 保留在可信服务器，浏览器使用 WebRTC，通过自己的服务器交换 SDP。

## WebRTC 接入

浏览器流程：获取麦克风 → 创建 PeerConnection → 创建 `oai-events` 数据通道并注册监听 → 收集 SDP/ICE → 由可信服务器提交下面的 JSON → 应用 `transport.sdp` → 等待 `session.started`。

```http
POST /v1/live/sessions
Authorization: Bearer <auth-key>
Content-Type: application/json
```

```json
{
  "session": {
    "model": "gpt-live-1",
    "audio": { "output": { "voice": "marin" } }
  },
  "transport": {
    "type": "webrtc",
    "sdp": "<browser SDP offer>"
  }
}
```

成功响应 HTTP 201：

```json
{
  "session": { "id": "live_..." },
  "transport": { "type": "webrtc", "sdp": "<SDP answer>" }
}
```

- 数据通道使用 `pc.createDataChannel("oai-events")`，不要使用旧版空标签、`negotiated: true` 或 `data_message` 双层封装。
- 音频通过媒体轨道传输；不要设置 `session.audio.format`，也不要在数据通道发送 `session.input_audio.append`。
- HTTP 请求已经启动会话，**不要再次发送 `session.start`**。
- 所有应用命令都在收到 `session.started` 后发送。DataChannel 打开不等于应用会话已就绪。
- 当前实现由服务器终止浏览器 WebRTC，再桥接上游 WebRTC；这使私有事件不会泄露到公共数据通道。和旧直连路径相比，会多一次媒体中继与编解码。

浏览器核心代码（`/api/voice-session` 是应用自己的可信服务器路由）：

```javascript
const peer = new RTCPeerConnection({
  bundlePolicy: "max-bundle",
  iceServers: [{ urls: "stun:stun.cloudflare.com:3478" }],
});
const audio = new Audio();
audio.autoplay = true;
audio.controls = true;
document.body.append(audio);
peer.ontrack = ({ streams }) => { audio.srcObject = streams[0]; };
const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
mic.getTracks().forEach(track => peer.addTrack(track, mic));
const events = peer.createDataChannel("oai-events");
let ready = false;
let closeTimer;
function cleanup() {
  clearTimeout(closeTimer);
  mic.getTracks().forEach(track => track.stop());
  events.close();
  peer.close();
}
events.onmessage = ({ data }) => {
  const event = JSON.parse(data);
  if (event.type === "session.started") ready = true;
  if (event.type === "session.input_transcript.delta" ||
      event.type === "session.output_transcript.delta") {
    console.log(event.type, event.delta, event.start_ms, event.end_ms);
  }
  if (event.type === "error") console.error(event.error);
  if (event.type === "session.closed") {
    console.log("final usage", event.usage);
    cleanup();
  }
};
await peer.setLocalDescription(await peer.createOffer());
if (peer.iceGatheringState !== "complete") {
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      peer.removeEventListener("icegatheringstatechange", changed);
      if (/^a=candidate:.* typ (?:srflx|relay)(?:\s|$)/m.test(peer.localDescription?.sdp ?? "")) resolve();
      else reject(new Error("ICE gathering timed out"));
    }, 10000);
    function changed() {
      if (peer.iceGatheringState === "complete") {
        clearTimeout(timeout);
        peer.removeEventListener("icegatheringstatechange", changed);
        resolve();
      }
    }
    peer.addEventListener("icegatheringstatechange", changed);
    changed();
  });
}
const response = await fetch("/api/voice-session", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    session: { model: "gpt-live-1", audio: { output: { voice: "marin" } } },
    transport: { type: "webrtc", sdp: peer.localDescription.sdp },
  }),
});
if (!response.ok) { cleanup(); throw new Error("Voice setup failed"); }
const result = await response.json();
await peer.setRemoteDescription({ type: "answer", sdp: result.transport.sdp });

// Bind to the application's end-call button. Do not call immediately after setup.
function endConversation() {
  if (!ready) { cleanup(); return; }
  ready = false;
  events.send(JSON.stringify({ type: "session.close" }));
  closeTimer = setTimeout(() => {
    console.error("session.closed was not received; final usage is unconfirmed");
    cleanup();
  }, 15000);
}
```

应用服务器需要验证用户身份，再将该请求转发到本项目；迁往官方时，改为 `https://api.openai.com/v1/live/sessions` 并使用服务器保存的官方项目 Key。不要做无条件自动 POST 重试，以免创建重复会话。

## WebSocket 接入

连接 `ws://localhost:8000/v1/live/sessions`（部署后使用 `wss://`），首条消息：

```json
{
  "type": "session.start",
  "event_id": "start_1",
  "session": {
    "model": "gpt-live-1",
    "audio": {
      "format": { "type": "audio/pcm", "rate": 24000 },
      "output": { "voice": "marin" }
    }
  }
}
```

等待 `session.started` 后，按真实时间节拍发送麦克风音频：

```json
{"type":"session.input_audio.append","audio":"<base64 PCM16 bytes>"}
```

音频是 **单声道、16 位小端、有符号 PCM**，支持 16000 或 24000 Hz，默认 24000；输入与输出格式相同。不要携带 WAV 头。发送字节数必须为偶数；建议每包 20–40ms，本适配器限制单包最多 250ms。采样率不符时必须实际重采样，不能只改字段。内部 48kHz 转换由流式重采样器完成。

持续发送音频（包括未静音时的静音片段），不发送 Realtime 的 commit 或用 `response.create` 触发语音。音频 append 没有确认事件。

可运行的服务器端 Python 示例：

```python
import asyncio
import base64
import json
import os
import wave

from websockets.asyncio.client import connect


async def main():
    url = os.environ.get("LIVE_WS_URL", "ws://localhost:8000/v1/live/sessions")
    # Local service: project auth-key. Official service: approved OpenAI project key.
    key = os.environ["LIVE_API_KEY"]
    with wave.open("input.wav", "rb") as source:
        assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, 24000)
        pcm = source.readframes(source.getnframes())
    async with connect(url, additional_headers={"Authorization": f"Bearer {key}"}) as ws:
        await ws.send(json.dumps({"type": "session.start", "session": {
            "model": "gpt-live-1",
            "audio": {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": "marin"}},
        }}))
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        if first["type"] != "session.started":
            raise RuntimeError(first)

        async def receive():
            # output.pcm is raw PCM, not a WAV. A live application should play
            # chunks as they arrive, using a bounded playback queue.
            with open("output.pcm", "wb") as output:
                async for raw in ws:
                    message = json.loads(raw)
                    if message["type"] == "session.output_audio.delta":
                        output.write(base64.b64decode(message["delta"]))
                    elif message["type"] == "session.closed":
                        print("final usage", message["usage"])
                        return
                    elif message["type"] == "error":
                        raise RuntimeError(message["error"])
                    else:
                        print(message)
            raise RuntimeError("Connection ended without session.closed")

        receiver = asyncio.create_task(receive())
        try:
            # Demo only: append ten seconds of silence to leave reply time.
            # In production keep microphone capture running until the user ends.
            samples = pcm + bytes(24000 * 2 * 10)
            for offset in range(0, len(samples), 960):  # 20ms
                if receiver.done():
                    await receiver
                    return
                chunk = samples[offset:offset + 960]
                await ws.send(json.dumps({"type": "session.input_audio.append", "audio": base64.b64encode(chunk).decode()}))
                await asyncio.sleep(len(chunk) / 48000)
            await ws.send(json.dumps({"type": "session.close", "event_id": "end_1"}))
            await asyncio.wait_for(receiver, timeout=15)
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)


asyncio.run(main())
```

已有支持 Live 的官方 SDK 也可以对接这些字段：例如 `AsyncOpenAI(base_url="http://localhost:8000/v1", api_key=...).live.connect(max_retries=0)`。项目本身不依赖或固定某个官方 SDK 版本；本轮验证覆盖线协议和真实本机 WebRTC，不等同于所有 SDK 版本认证。

## 支持的命令与事件

| 客户端命令 | 行为 / 服务端事件 |
| --- | --- |
| `session.start` | 仅主 WS 首条消息；返回 `session.started`，含完整 session 快照 |
| `session.input_audio.append` | 仅主 WS；输入音频，无 ACK |
| `session.input_audio.mute` / `unmute` | 清理残留输入、更新上游麦克风状态，返回 `session.input_audio.muted` / `unmuted` |
| `session.instructions.append` | 使用 `content` 字符串和显式 `delegation_id: null`；返回 `session.instructions.appended` |
| `session.commentary.append` | 提交需要向用户表达的上下文；返回 `session.commentary.appended` |
| `session.update` | 仅空更新可被确认；不支持的更新显式报错，不假装成功 |
| `session.close` | 释放上游并返回 `session.closed`，随后关闭传输 |

服务端语音事件为 `session.output_audio.delta`（仅 WS）、`session.input_transcript.delta`、`session.output_transcript.delta`。所有普通事件包含 `event_id`；命令确认带 `client_event_id`。错误使用 `error: {type, code, message, param?, client_event_id?}`。

字幕逐片追加，保留空格和重复词。每个角色可同时产生字幕。Live 没有每轮 `response.audio.done`、`response.done` 或字幕 done；不要用文本流结束判定播放结束。调试页以实际输出音量更新“正在回答”，本地字幕分组只是显示策略。

**当前近似行为：**

- 初始 `instructions`、`input` 和运行时 append 经内部 `relay_message` 文本上下文注入。它们不是原生 developer 角色或 Live 内部指令通道，无法保证静默初始化、逐字播报或与官方相同的遵从性。ACK 只表示已交给上游数据通道，不证明模型执行或用户听到。
- 上游未提供可靠的字幕对齐时间；`start_ms` / `end_ms` 是自本地会话启动起的到达时间点，不是字词的真实发声时间，不能用于精准音画同步。
- `usage.seconds` 是本地活动会话的累计时长，不是上游账单或 token 使用量。收到 `session.closed` 才确认本地最终时长；连接先断开时最终用量未确认。
- `gpt-live-1` 和官方声音名均为协议兼容名称，不承诺相同模型权重、音色或全双工行为。

## 能力边界与声音

以下能力明确不支持：Responses/client delegation、`response.*` 后端工具事件、静默 `session.thinking.append`、自定义声音对象、G.711、SIP、sideband、`store: true`、录音下载、fork、前端事件权限配置。未知字段和未实现命令返回错误，而非静默忽略。`delegation` 省略或为 null 时，本适配器只提供语音，不会生成客户端委派任务。

声音映射：

| 对外声音名 | 实际 Web Voice |
| --- | --- |
| `marin` / `verse` | Ember |
| `cedar` | Arbor / fathom |
| `alloy` | Breeze |
| `ash` | Cove |
| `ballad` | Maple |
| `coral` | Juniper |
| `echo` | Spruce / orbit |
| `sage` | Vale |
| `shimmer` | Sol / glimmer |

声音在创建时选择，不能在会话中修改。官方 Live 支持的其他声音不自动映射；不支持的名字返回 `unsupported_voice`。本项目不支持上传声音样本；官方自定义声音需要单独获批，详见[官方自定义声音指南](https://developers.openai.com/api/docs/guides/custom-voices)。

## 播放、重连与部署

调试页优先 Live WebSocket，启动失败时回退 Live WebRTC。WebSocket 避免浏览器到服务器的额外 ICE 握手和一次 Opus 解码/重编码；服务端到上游仍使用 WebRTC。两种公开 Live 传输均保留。

WS 播放初始预填为 180ms，播放保持原速。最多保留 2 秒音频以吸收 TCP 短暂积压，只有真正超出队列容量时才裁剪；不再在 600ms 时清空队列切掉词句。AudioContext 使用设备原生采样率和 interactive 提示，实际 PCM 输入输出仍按协商的 16/24kHz 重采样。音频激活与 AudioWorklet 加载都有超时，工作线程加载失败会回退到备用采集与播放节点。

WebRTC 备用链路接收缓冲初始目标 120ms、上限 300ms，兼顾到包抖动和未丢包时的播放补偿。服务端恢复 aiortc 原生接收缓冲，不再通过私有接口用固定 3 包窗口跳过语音。上游输入轨道有 60ms 有界预填，减少到包间隔波动造成的断音。这些是缓冲目标，不是端到端响应时间保证。

为减少偶发爆音，Live WebRTC 在服务器输出轨道增加最多约 60ms 的启动/断流恢复预填；队列仍限制为 300ms，不加速播放。解码后的 20ms PCM 帧直接进入媒体轨道，不经过 WS 的 40ms JSON 聚合和 DTX 补静音。启动、断流、清空或溢出后的拼接使用 5ms 平滑过渡，正常连续帧保持原样。WS AudioWorklet 同样平滑断点，并在缺帧后重新预填，避免反复播放极短碎片。短尾音通过有界等待或本地播放结束提示释放。

排查“啪/滋”声时，区分浏览器 `packetsLost` / `concealedSamples` 与服务器输出队列问题：网络丢包为零也不能证明音频连续。会话结束时的 `[live-audio]` 日志记录队列空转次数、丢弃帧数和麦克风 RTP 缺包/迟到包；`[realtime] Session closed` 记录上游 RTP 缺包/迟到包和输入队列丢帧。空转也可能发生在正常停顿，需结合有声片段分析。合成音频回归覆盖 25ms 到达抖动、单包/连续丢包、乱序、序号回绕、断流恢复、打断清空、溢出、短尾音和 DTX 间隔。

字幕按序保留；音频连续发送时，控制事件优先、字幕公平调度。识别到上游 speech-start 打断后，清除服务器旧音频，等待新的 speaking 状态；WS 已经发出的音频无法撤回，低播放水位有助于降低残留播放时间。严重拥塞会关闭连接，避免无提示丢弃关键字幕并继续展示损坏的会话。

网络恢复新建 Live 会话，并通过标准 `session.input` 携带最近最多 8 条、每条最多 600 字符的显示历史，同时保持用户静音选择。它不是上游原会话恢复，无法恢复尚未收到的字幕、精确音频时间线或模型内部状态。不自动重放未确认的文字命令，避免重复执行；原始音频也不重播。

服务器需支持 WebSocket Upgrade，并允许 WebRTC UDP/ICE 媒体通信。代理仅转发 HTTP/WSS 并不足以支持新的服务器 WebRTC 媒体桥；受限网络可使用 WS。`CHATGPT2API_LIVE_ICE_SERVERS` 可配置服务器 ICE/STUN/TURN（JSON 数组，例如 `[{"urls":"stun:stun.example.com:3478"}]`）；TURN 凭据保存在服务器环境中。

### Linux Docker 部署检查

更新源码之后，必须重新构建并重建容器。旧容器没有 `/v1/live/sessions` 时，POST 会落到只接受 GET/HEAD 的网页兜底路由，返回 `405 Method Not Allowed`。检查运行实例的 `/openapi.json` 是否包含该 POST 路由；`/version` 的版本号不一定随每次代码提交变化，不能单独用来确认升级成功。

```bash
docker compose build app
docker compose up -d --no-deps app
```

桥接网络只发布 `3321:80` 时，即使信令返回 201，媒体仍可能停在 ICE checking。仅出现 `host` 和 `srflx` 候选不代表容器 UDP 端口可从公网到达。可使用 `docker-compose.live.yml` 为服务端添加受认证的 TURN 中继，保留现有 HTTP 地址和 Docker 网络。该覆盖文件用于具有公网网卡的 Linux VPS；TURN 监听 Docker 网桥网关，公网媒体使用 UDP 49160–49259。主机和云防火墙需要允许该媒体端口范围。

创建权限为 0600、不要提交的 `.env.live`，设置 `LIVE_TURN_LISTEN_IP`（app 所在 Docker 网桥网关）、`LIVE_TURN_RELAY_IP`（VPS 公网网卡 IPv4）和 `LIVE_TURN_PASSWORD`（使用 `secrets.token_urlsafe(32)` 生成）。密码只通过环境变量注入，不下发浏览器。若 VPS 公网地址由额外 NAT 映射，需要另行配置 TURN external-ip。

```bash
docker compose --env-file .env.live -f docker-compose.yml -f docker-compose.live.yml config --quiet
docker compose --env-file .env.live -f docker-compose.yml -f docker-compose.live.yml up -d
```

后续升级继续使用相同覆盖文件和环境文件。浏览器也需配置可达的 STUN 服务，将公网 `srflx` 候选写入 offer，否则服务器 TURN 无法为浏览器公网 IP 建立权限。多网卡设备可能持续收集不可达网卡的候选；在收集超时时已有 `srflx`/`relay` 候选的情况下，可以提交当前 SDP，而不必放弃整个连接。

不要仅验证 HTTP 成功：应确认浏览器收到 `session.started`、指令 ACK、字幕和非零音频数据，结束时收到 `session.closed`。TURN 部署参数参考 [Coturn 官方 Docker 文档](https://github.com/coturn/coturn/blob/master/docker/coturn/README.md)。

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `CHATGPT2API_LIVE_MAX_SESSIONS` | 32 | 每进程活动/预留会话总数 |
| `CHATGPT2API_LIVE_MAX_SESSIONS_PER_USER` | 4 | 每身份每进程活动/预留会话数 |
| `CHATGPT2API_REALTIME_SIGNALING_CONCURRENCY` | 8 | 与旧路径共享的上游信令并发 |
| `CHATGPT2API_REALTIME_SIGNALING_RATE_PER_MINUTE` | 20 | 与旧路径共享的每身份创建频率 |

会话最长 2 小时。首次 WS 命令等待 10 秒，服务器上游准备超时 45 秒；反向代理的连接建立等待应覆盖这些时间。每事件上限 512000 字节。

并发计数、短期凭据和活动会话记录在进程内；多 worker/多实例的限流不是全局限流。需要全局配额时，应在入口实施统一限流。旧 Realtime 的 ephemeral key 交换需要粘性路由或共享状态，不能把多进程当成已解决的持久会话系统。

## 从旧 Realtime 或迁往官方

| 旧用法 | Live 用法 |
| --- | --- |
| `/v1/realtime/client_secrets` + `/calls` | `POST /v1/live/sessions`，JSON session + transport |
| `WS /v1/realtime?voice=...` | `WS /v1/live/sessions`，首条 `session.start` 配置声音和模型 |
| `input_audio_buffer.append`，48kHz | `session.input_audio.append`，WS 使用 16/24kHz |
| `response.audio.delta` | `session.output_audio.delta` |
| `chat_message_delta` / `state_update` | 标准字幕事件；播放状态由客户端测量 |
| `data_message` JSON 套 JSON | 单层 Live JSON 事件 |
| 立即关闭连接 | `session.close` → 等待 `session.closed` → 关闭 |

切换官方服务时：

1. 在可信服务器替换 base URL 和项目 API Key；仍使用 `gpt-live-1`。
2. 移除对本项目 `/capabilities`、浏览器 WS 子协议、声音试听文件和所有 `/realtime/*` 扩展的依赖。
3. 保留标准连接、音频、字幕、静音和关闭代码，重新验证提示词与历史注入行为、声音效果、打断、用量统计。
4. 只有在官方项目开通相应能力后，才加入 delegation、自定义声音、sideband 等功能。不要把本适配器错误返回当成可以忽略的成功。

## 验证

```bash
.venv/bin/python -m pytest -q test/test_live.py test/test_live_audio_quality.py test/test_audio_receive_buffer.py test/test_realtime.py test/test_realtime_signaling.py test/test_realtime_signaling_guard.py
node web/node_modules/typescript/bin/tsc --noEmit -p web/tsconfig.json
node web/test/live-client.test.mjs
```

测试使用合成上游，不消耗真实语音账号。包含线协议校验、错误关联、采样率/频率保持、增量字幕、控制事件优先、长通话额度绑定以及 aiortc 本机 WebRTC 握手/数据通道。真实 ChatGPT 上游、不同浏览器设备和公网 TURN 链路仍需联调，不能以这些测试代替端到端音质与延迟测量。
