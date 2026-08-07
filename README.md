<h1 align="center">ChatGPT2API</h1>


<p align="center">ChatGPT2API 主要是对 ChatGPT 官网相关能力进行逆向整理与封装，提供面向 ChatGPT 图片生成、图片编辑、多图组图编辑场景的 OpenAI 兼容图片 API / 代理，并集成在线画图、号池管理、多种账号导入方式与 Docker 自托管部署能力。</p>

> [!WARNING]
> 免责声明：
>
> 本项目涉及对 ChatGPT 官网文本生成、图片生成与图片编辑等相关接口的逆向研究，仅供个人学习、技术研究与非商业性技术交流使用。
>
> - 严禁将本项目用于任何商业用途、盈利性使用、批量操作、自动化滥用或规模化调用。
> - 严禁将本项目用于破坏市场秩序、恶意竞争、套利倒卖、二次售卖相关服务，以及任何违反 OpenAI 服务条款或当地法律法规的行为。
> - 严禁将本项目用于生成、传播或协助生成违法、暴力、色情、未成年人相关内容，或用于诈骗、欺诈、骚扰等非法或不当用途。
> - 使用者应自行承担全部风险，包括但不限于账号被限制、临时封禁或永久封禁以及因违规使用等所导致的法律责任。
> - 使用本项目即视为你已充分理解并同意本免责声明全部内容；如因滥用、违规或违法使用造成任何后果，均由使用者自行承担。
> - 本项目基于对 ChatGPT 官网相关能力的逆向研究实现，存在账号受限、临时封禁或永久封禁的风险。请勿使用你自己的重要账号、常用账号或高价值账号进行测试。


## 赞助商

<table>
  <tr>
    <td width="190" align="center">
      <a href="https://www.atlascloud.ai/zh?utm_source=github&utm_medium=link&utm_campaign=chatgpt2api"><img src="assets/atlascloud.svg" width="163" alt="Atlas Cloud"></a>
    </td>
    <td>
      <a href="https://www.atlascloud.ai/zh?utm_source=github&utm_medium=link&utm_campaign=chatgpt2api">Atlas Cloud</a> is a full-modal AI inference platform that gives developers a single AI API to access video generation, image generation, and LLM APIs. Instead of managing multiple vendor integrations, you connect once and get unified access to 300+ curated models across all modalities. Check out <a href="https://www.atlascloud.ai/console/coding-plan">Atlas Cloud's new coding plan promotion</a> for more budget-friendly API access.
    </td>
  </tr>
</table>

## 快速开始

### Docker 运行

```bash
git clone git@github.com:basketikun/chatgpt2api.git
cd chatgpt2api
docker compose up -d
```

启动前请先在 `config.json` 中设置 `auth-key`，也可以在 `docker-compose.yml` 中通过 `CHATGPT2API_AUTH_KEY` 覆盖。

- Web 面板：`http://localhost:3000`
- API 地址：`http://localhost:3000/v1`
- 数据目录：`./data`

### WARP / FlareSolverr 稳定代理部署

如果图片链路经常遇到 Cloudflare 拦截，可以启用附带的 WARP + Privoxy + FlareSolverr 方案：

```bash
cp .env.example .env
docker compose -f docker-compose.warp.yml up -d --build
```

该 compose 会启动：

- `warp-proxy`：提供 WARP SOCKS5 出口。
- `privoxy`：把 WARP SOCKS5 转成 HTTP 代理。
- `flaresolverr`：刷新 Cloudflare clearance。
- `init-config`：幂等写入 `proxy_runtime` 默认配置。
- `app`：启动 ChatGPT2API 主服务。

默认只让上游 OpenAI / ChatGPT 请求走稳定代理，账号邮箱、CPA 等辅助链路不会被强制接管。账号自身配置的代理优先级最高，其次是稳定代理运行时，再其次是显式代理和旧版全局代理。

可在 `.env` 中调整端口和代理运行时参数，也可在后台设置页的「稳定代理运行时」面板手动保存、测试代理和测试 clearance。

### 本地开发

启动后端：

```bash
git clone git@github.com:basketikun/chatgpt2api.git
cd chatgpt2api
uv sync
uv run main.py
```

启动前端：

```bash
cd chatgpt2api/web
bun install
bun run dev
```

后续更新新版本：

```bash
docker pull ghcr.io/basketikun/chatgpt2api:latest
docker-compose down
docker-compose up -d

```

### 存储后端配置

支持通过环境变量 `STORAGE_BACKEND` 切换存储方式：

- `json` - 本地 JSON 文件（默认）
- `sqlite` - 本地 SQLite 数据库
- `postgres` - 外部 PostgreSQL（需配置 `DATABASE_URL`）
- `git` - Git 私有仓库（需配置 `GIT_REPO_URL` 和 `GIT_TOKEN`）

示例：使用 PostgreSQL

```yaml
environment:
  - STORAGE_BACKEND=postgres
  - DATABASE_URL=postgresql://user:password@host:5432/dbname
```

## 功能

### API 兼容能力

- 兼容 `POST /v1/images/generations` 图片生成接口
- 兼容 `POST /v1/images/edits` 图片编辑接口
- 兼容面向图片场景的 `POST /v1/chat/completions`
- 兼容面向图片场景的 `POST /v1/responses`
- 提供 Realtime WebRTC 信令、语音列表、能力发现和 WebSocket 音频桥接接口
- `GET /v1/models` 返回 `gpt-image-2`、`codex-gpt-image-2`、`auto`、`gpt-5`、`gpt-5-1`、`gpt-5-2`、`gpt-5-3`、`gpt-5-3-mini`、
  `gpt-5-mini`
- 支持通过 `n` 返回多张生成结果
- 支持生成可编辑 PPT 文件
- 支持生成可编辑 PSD 文件
- 支持 Codex 中的画图接口逆向，仅 `Plus` / `Team` / `Pro` 订阅可用，模型别名为 `codex-gpt-image-2`，如有需要可自行在其他场景映射回
  `gpt-image-2`，用于和官网画图区分；也就意味着同一账号会同时有官网和 Codex 两份生图额度

### 在线画图功能

- 内置在线画图工作台，支持生成、图片编辑与多图组图编辑
- 支持 `gpt-image-2`、`codex-gpt-image-2`、`auto`、`gpt-5`、`gpt-5-1`、`gpt-5-2`、`gpt-5-3`、`gpt-5-3-mini`、`gpt-5-mini` 模型选择
- 编辑模式支持参考图上传
- 前端支持多图生成交互
- 本地保存图片会话历史，支持回看、删除和清空
- 支持服务端缓存图片URL
- 图片生成进度追踪，超时后可继续等待
- 图片懒加载与滚动位置记忆，优化大量图片场景性能

### 号池管理功能

- 自动刷新账号邮箱、类型、额度和恢复时间（异步进度追踪）
- 轮询可用账号执行图片生成与图片编辑
- 遇到 Token 失效类错误时自动剔除无效 Token
- 定时检查限流账号并自动刷新
- 支持密码重新登录恢复异常账号，刷新后可自动重登
- 支持网页端配置全局 HTTP / HTTPS / SOCKS5 / SOCKS5H 代理
- 支持 WARP / FlareSolverr 稳定代理运行时
- 支持搜索、筛选、批量刷新、导出、手动编辑和清理账号
- 支持四种导入方式：本地 CPA JSON 文件导入、远程 CPA 服务器导入、`sub2api` 服务器导入、`access_token` 导入
- 支持在设置页配置 `sub2api` 服务器，筛选并批量导入其中的 OpenAI OAuth 账号

### 实验性 / 规划中

- 详细状态说明见：[功能清单](./docs/feature-status.en.md)

## 效果展示

<table width="100%">
  <tr>
    <td width="50%"><img src="https://i.ibb.co/Jj8nfwwP/image.png" alt="image" border="0"></td>
    <td width="50%"><img src="https://i.ibb.co/pqf235v/image-edit.png" alt="image edit" border="0"></td>
  </tr>
  <tr>
    <td width="50%"><img src="https://i.ibb.co/tPcqtVfd/chery-studio.png" alt="chery studio" border="0"></td>
    <td width="50%"><img src="https://i.ibb.co/PsT9YHBV/account-pool.png" alt="account pool" border="0"></td>
  </tr>
  <tr>
    <td width="50%"><img src="https://i.ibb.co/rRWLG08q/new-api.png" alt="new api" border="0"></td>
  </tr>
</table>

## API

所有 AI 接口都需要请求头：

```http
Authorization: Bearer <auth-key>
```

<details>
<summary><code>GET /v1/models</code></summary>
<br>

返回当前暴露的图片模型列表。

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer <auth-key>"
```

<details>
<summary>说明</summary>
<br>

| 字段   | 说明                                                                                                         |
|:-----|:-----------------------------------------------------------------------------------------------------------|
| 返回模型 | `gpt-image-2`、`codex-gpt-image-2`、`auto`、`gpt-5`、`gpt-5-1`、`gpt-5-2`、`gpt-5-3`、`gpt-5-3-mini`、`gpt-5-mini` |
| 接入场景 | 可接入 Cherry Studio、New API 等上游或客户端                                                                          |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/images/generations</code></summary>
<br>

OpenAI 兼容图片生成接口，用于文生图。

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "一只漂浮在太空里的猫",
    "n": 1,
    "response_format": "b64_json"
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段                | 说明                                                 |
|:------------------|:---------------------------------------------------|
| `model`           | 图片模型，当前可用值以 `/v1/models` 返回结果为准，推荐使用 `gpt-image-2` |
| `prompt`          | 图片生成提示词                                            |
| `n`               | 生成数量，当前后端限制为 `1-4`                                 |
| `response_format` | 当前请求模型中包含该字段，默认值为 `b64_json`                       |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/images/edits</code></summary>
<br>

OpenAI 兼容图片编辑接口，可上传图片文件，也可按官方 JSON 格式传入图片链接并生成编辑结果。

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Authorization: Bearer <auth-key>" \
  -F "model=gpt-image-2" \
  -F "prompt=把这张图改成赛博朋克夜景风格" \
  -F "n=1" \
  -F "image=@./input.png"
```

也可以直接传图片 URL：

```bash
curl http://localhost:8000/v1/images/edits \
  -H "Authorization: Bearer <auth-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-image-2",
    "prompt": "把这张图改成赛博朋克夜景风格",
    "images": [
      {"image_url": "https://example.com/input.png"}
    ]
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段          | 说明                                            |
|:------------|:----------------------------------------------|
| `model`     | 图片模型， `gpt-image-2`                           |
| `prompt`    | 图片编辑提示词                                       |
| `n`         | 生成数量，当前后端限制为 `1-4`                            |
| `image`     | 需要编辑的图片文件，使用 multipart/form-data 上传           |
| `images`    | JSON 图片引用数组，支持 `{"image_url": "https://..."}` |
| `image_url` | 表单模式下也可直接传图片链接，支持重复字段传多张图                     |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/chat/completions</code></summary>
<br>

面向文本、网页搜索与图片场景的 Chat Completions 兼容接口，不是完整通用聊天代理。

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-image-2",
    "messages": [
      {
        "role": "user",
        "content": "生成一张雨夜东京街头的赛博朋克猫"
      }
    ],
    "n": 1
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段                   | 说明                                                                           |
|:---------------------|:-----------------------------------------------------------------------------|
| `model`              | 文本、搜索或图片模型；搜索模型会触发网页搜索兼容逻辑                                                   |
| `messages`           | 消息数组，支持文本、搜索和图片请求内容                                                          |
| `n`                  | 图片生成数量，按当前实现解析为图片数量                                                          |
| `stream`             | 文本、搜索和图片场景均支持，仍在测试                                                           |
| `tools`              | 文本场景支持 `web_search` / `web_search_preview` / `web_search_preview_2025_03_11` |
| `web_search_options` | 传入时会触发网页搜索兼容逻辑                                                               |

<br>
</details>
</details>

<details>
<summary><code>POST /v1/responses</code></summary>
<br>

面向文本、网页搜索和图片生成工具调用的 Responses API 兼容接口，不是完整通用 Responses API 代理。

```bash
curl http://localhost:8000/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <auth-key>" \
  -d '{
    "model": "gpt-5",
    "input": "生成一张未来感城市天际线图片",
    "tools": [
      {
        "type": "image_generation"
      }
    ]
  }'
```

<details>
<summary>字段说明</summary>
<br>

| 字段       | 说明                                                                                      |
|:---------|:----------------------------------------------------------------------------------------|
| `model`  | 响应中会回显该模型字段，搜索和图片生成会走对应兼容逻辑                                                             |
| `input`  | 输入内容；搜索使用最后一条用户文本，图片生成需能解析出提示词                                                          |
| `tools`  | 支持 `image_generation`、`web_search`、`web_search_preview`、`web_search_preview_2025_03_11` |
| `stream` | 已实现，但仍在测试                                                                               |

<br>
</details>
</details>

### 实时语音 API

实时语音接口同样使用项目的 API Key：

```http
Authorization: Bearer <auth-key>
```

| 端点 | 用途 |
|:--|:--|
| `GET /v1/realtime/capabilities` | 获取支持的传输方式、音频格式和相关端点 |
| `GET /v1/realtime/voices` | 获取可用声音列表 |
| `POST /v1/realtime/sessions` | 交换 WebRTC SDP，媒体随后在客户端和上游之间直连 |
| `WS /v1/realtime` | 服务端 WebSocket 音频桥接，适合无法使用 WebRTC 的客户端 |

浏览器、App 和桌面客户端推荐使用 WebRTC。服务端只处理 API Key 鉴权和 SDP
信令，不会把上游账号 Token 暴露给客户端；音频媒体不经过本服务进行
Base64 转码，因此延迟和抖动更低。

信令端点默认按 API Key 身份限制为每分钟 20 次请求，同时最多处理 8 个并发
上游 SDP 交换。可通过以下环境变量调整：

| 环境变量 | 默认值 | 说明 |
|:--|:--|:--|
| `CHATGPT2API_REALTIME_SIGNALING_RATE_PER_MINUTE` | `20` | 单个身份每分钟最多创建的信令请求 |
| `CHATGPT2API_REALTIME_SIGNALING_CONCURRENCY` | `8` | 全局并发 SDP 交换数 |
| `CHATGPT2API_REALTIME_ATTEMPT_TTL_SECONDS` | `300` | 账号重试链的保留时间 |
| `CHATGPT2API_REALTIME_QUOTA_COOLDOWN_SECONDS` | `3600` | DataChannel 确认语音额度耗尽后，暂停选择该账号的时间 |

#### 查询能力和声音

```bash
curl http://localhost:8000/v1/realtime/capabilities \
  -H "Authorization: Bearer <auth-key>"

curl http://localhost:8000/v1/realtime/voices \
  -H "Authorization: Bearer <auth-key>"
```

#### 浏览器 WebRTC 接入

下面是完整的最小接入示例。生产代码还应处理麦克风拒绝授权、连接超时、
ICE 失败和页面卸载时的资源释放。

```js
const baseUrl = "http://localhost:8000";
const apiKey = "<auth-key>";
let previousAttemptId = "";

const pc = new RTCPeerConnection();
const remoteAudio = new Audio();
remoteAudio.autoplay = true;

pc.ontrack = ({ streams: [stream] }) => {
  remoteAudio.srcObject = stream;
};

const microphone = await navigator.mediaDevices.getUserMedia({
  audio: {
    channelCount: 1,
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
});
microphone.getTracks().forEach((track) => pc.addTrack(track, microphone));
pc.addTransceiver("video", { direction: "sendonly" });

// ChatGPT Web Voice 使用协商好的 id=0 DataChannel。
const dc = pc.createDataChannel("", {
  negotiated: true,
  id: 0,
  ordered: true,
});

dc.onmessage = ({ data }) => {
  const outer = JSON.parse(data);
  const event = outer.type === "data_message"
    ? JSON.parse(outer.data)
    : outer;
  console.log("realtime event", event);
};

dc.onopen = () => {
  dc.send(JSON.stringify({
    type: "data_message",
    data: JSON.stringify({
      type: "track_state",
      payload: {
        type: "track_state",
        track_id: "microphone",
        media_type: "audio",
        media_source: "microphone",
        state: "live",
      },
    }),
  }));
};

await pc.setLocalDescription(await pc.createOffer());

// 等待 host ICE candidate 收集完成后再提交 SDP。
if (pc.iceGatheringState !== "complete") {
  await new Promise((resolve) => {
    let timer;
    const finish = () => {
      clearTimeout(timer);
      pc.removeEventListener("icegatheringstatechange", done);
      resolve();
    };
    const done = () => {
      if (pc.iceGatheringState === "complete") {
        finish();
      }
    };
    pc.addEventListener("icegatheringstatechange", done);
    timer = setTimeout(finish, 5000);
  });
}

const response = await fetch(`${baseUrl}/v1/realtime/sessions`, {
  method: "POST",
  headers: {
    Authorization: `Bearer ${apiKey}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    sdp: pc.localDescription.sdp,
    voice: "ember",
    language: "auto",
    // 额度重试时传回上一次响应中的 attempt_id，避免再次选择同一账号。
    attempt_id: previousAttemptId || undefined,
  }),
});

const answer = await response.json();
if (!response.ok) {
  // { error: { code, message, retryable, retry_after_ms, request_id } }
  throw new Error(answer.error?.message || `signaling failed: ${response.status}`);
}
previousAttemptId = answer.attempt_id;
const answerSdp = `${answer.sdp.trim().replace(/\r?\n/g, "\r\n")}\r\n`;
await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
```

成功响应还包含 `attempt_id`、`request_id`，响应头包含 `X-Request-ID`。当
DataChannel 收到语音额度耗尽的 `usage_update` 或 `goodbye` 时，重新创建
PeerConnection，并把 `attempt_id` 放入下一次请求；服务端会在这条重试链中排除
已经尝试过的账号。收到 `429` 或 `503` 时应遵循 `Retry-After` 并加入随机退避。
项目自带调试页会把 `cap_reached` 回报给服务端，使耗尽账号进入临时冷却，后续新
会话也不会继续命中该账号。

文字消息也通过 DataChannel 发送，并使用双层 `data_message` 封装：

```js
function sendRealtimeEvent(event) {
  dc.send(JSON.stringify({
    type: "data_message",
    data: JSON.stringify(event),
  }));
}

sendRealtimeEvent({
  type: "conversation.item.create",
  item: {
    type: "message",
    role: "user",
    content: [{ type: "input_text", text: "你好" }],
  },
});
sendRealtimeEvent({ type: "response.create" });
```

客户端应关注以下事件类别：

- `state_update`：`listening`、`thinking`、`speaking` 等会话状态。
- `input_audio_buffer.speech_started` / `speech_stopped`：用户开始或停止说话。
- `conversation.item.input_audio_transcription.*`：用户语音转写。
- `response.output_audio_transcript.*`、`response.audio_transcript.*`：AI 语音转写。
- `chat_message_delta`：ChatGPT Web Voice 使用的增量消息流；其中包含 user 和
  assistant 的文字 JSON Patch，接入方需要按 message id 顺序合并。
- `usage_update` / `goodbye`：额度状态和会话结束原因。

#### WebSocket 音频桥接

不能使用 WebRTC 时，可以连接：

```text
ws://localhost:8000/v1/realtime?voice=ember
```

非浏览器客户端应通过 `Authorization: Bearer <auth-key>` 请求头鉴权。浏览器原生
WebSocket 无法设置自定义请求头，可使用 `?api_key=<auth-key>`；但 URL 可能进入
代理访问日志，因此浏览器仍推荐使用上面的 WebRTC 方案。

该兼容端点的实际模型标识为 `chatgpt-web-voice`，模型不可通过查询参数切换；声音
必须在建立 WebSocket 时用 `voice` 查询参数选择。连接建立后再通过
`session.update` 切换声音会返回 `unsupported_session_update`，避免客户端误以为
设置已经生效。

输入音频为 `48 kHz / PCM16 / 单声道 / little-endian`，按 Base64 分片发送：

```json
{"type":"input_audio_buffer.append","audio":"<base64-pcm16>"}
```

服务端返回的 `response.audio.delta` 同样是 `48 kHz PCM16` 的 Base64 数据：

```json
{"type":"response.audio.delta","delta":"<base64-pcm16>"}
```

建议每个输入分片控制在 `20–100 ms`。单条 Base64 字符串最大为 `512000`
字符；客户端应持续消费返回消息，并在结束时主动关闭 WebSocket。

## 社区支持

学 AI , 上 L 站：[LinuxDO](https://linux.do)

## Contributors

感谢所有为本项目做出贡献的开发者：

<a href="https://github.com/basketikun/chatgpt2api/graphs/contributors">
  <img alt="Contributors" src="https://contrib.rocks/image?repo=basketikun/chatgpt2api" />
</a>

## Star History

[![Star History Chart](https://api.star-history.com/chart?repos=basketikun/chatgpt2api&type=date&legend=top-left)](https://www.star-history.com/?repos=basketikun%2Fchatgpt2api&type=date&legend=top-left)
