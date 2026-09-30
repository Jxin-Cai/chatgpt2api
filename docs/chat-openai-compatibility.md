# Chat 与 OpenAI 协议审计

核对日期：2026-09-30。结论：本项目提供 ChatGPT Web 的部分能力转换，不能称为“完整支持最新官方 OpenAI API”，也不能仅凭响应中的 `model` 判断实际权重版本。当前只保留六个文本型号，再与账号 Web 目录取交集；白名单不代表账号已获使用权限。

## 官方更新与模型身份

- 官方 [GPT-5.6 与 GPT-6 Pro in ChatGPT](https://help.openai.com/en/articles/20001354-gpt-56-and-gpt-6-pro-in-chatgpt)明确区分聊天 GPT-6 Pro 与 Work/Codex GPT-6 Astra；聊天 Pro 由 Astra 驱动，不代表它们的路由、权限及额度相同。

- 官方 [`chat-latest`](https://developers.openai.com/api/docs/models/chat-latest) 是持续更新的 ChatGPT Instant 模型别名。它不是 Work/Codex 全部模型的通用别名，也没有官方依据把它映射到本项目的 `auto` 或某个 `-wm` slug。
- [2026-09-29 ChatGPT 更新](https://help.openai.com/en/articles/6825453-chatgpt-release-notes)公布 GPT-6.1 Sol 在 Work/Codex 分批上线。订阅档位相同不代表账号已经获得相同权限。
- [API 更新日志](https://developers.openai.com/api/docs/changelog)也列出了 GPT-6.1 Sol，以及 GPT-6 Astra 的 Ultrafast 等功能；其中 API service tier、托管工具和代理能力，不会因为 Web 模型同名就自动成为本项目的能力。
- 官方 [Chat Completions 规范](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)定义了生成参数、工具及结构化输出等行为。兼容响应 JSON 的形状并不足以保证实现这些语义。

浏览器登录态的只读目录已核验：`gpt-6-pro`、`gpt-5-6-instant`、`gpt-5-6-thinking` 的 `is_work_mode_model` 为 false；`gpt-6.1-sol-wm`、`gpt-6-astra-wm`、`gpt-6-sol-wm` 为 true。`-wm` 表示 Work Mode，不是 Web 的通用后缀。仅查看目录，不发送生成请求，也未保存或导出浏览器凭据。仓库账号池另有 1 个活动账号，其目录请求返回 HTTP 401，因此尚未确认服务自身凭据的可用型号或实际生成能力。

## 保留的文本模型

只保留以下六个型号，`/v1/models` 与账号真实目录取交集，每个型号只列一次，实际可能少于六个。

| 对外规范名称 | 路径 | 当前核验的上游 slug |
| --- | --- | --- |
| `gpt-6-pro` | 普通聊天 Pro | `gpt-6-pro` |
| `gpt-5.6-instant` | 普通聊天 Instant | `gpt-5-6-instant` |
| `gpt-5.6-thinking` | 普通聊天 Thinking | `gpt-5-6-thinking` |
| `gpt-6.1-sol` | Work | `gpt-6.1-sol-wm` |
| `gpt-6-astra` | Work | `gpt-6-astra-wm` |
| `gpt-6-sol` | Work | `gpt-6-sol-wm` |

客户端可使用规范名或目录对应的版本号短横线拼写。Work 型号也可传带 `-wm` 的别名，上游只能使用目录公布的 `-wm` 目标；普通聊天不能加此后缀，不会把 GPT-6 Pro 替换成 Astra Work。目录明确提供 `is_work_mode_model` 时，还会校验模式一致性。

其他文本模型即使在上游目录中仍存在也不再支持，包括 `auto`、`chat-latest`、GPT-6 Luna、GPT-5.6 Sol/Terra/Luna Work、GPT-5.5 及历史 API 搜索型号。Chat、Responses、Messages 都在生成或读取响应缓存前拒绝这些请求。省略型号时默认 GPT-6.1 Sol，缺少权限时返回不可用，不自动选择其他型号。前端 Chat 使用六个型号的固定下拉框，标明聊天或 Work，并限制 Pro/Instant 的思考参数，旧会话中的已移除型号会重置为默认值。图片模型目录独立管理。

## 实现路径

`api/ai.py` 接收请求，经 `LoggedCall` 处理日志及 SSE，再交给 `openai_v1_chat_complete.py`。文本和函数请求经 `conversation.py` 转换为 Web conversation；`model_service.py` 决定模型别名及可用账号，`openai_backend_api.py` 负责上游请求和 Work Mode handoff。

函数调用是提示词桥接：模型生成调用描述，服务转换成 `tool_calls`，客户端执行后传回工具结果。搜索使用单独的 Web 原生搜索链路。图片走独立图片分支。默认的短缓存可直接重放已完成响应，缓存命中不发起新的上游推理。

| 能力 | 本项目行为与边界 |
| --- | --- |
| 模型发现 | 逐账号读取目录，缓存 5 分钟；过滤为六个型号，只展示规范名称 |
| 最新型号 | 支持版本号点号/短横线与 `-wm` 的组合解析，例如 `gpt-6.1-sol` → `gpt-6-1-sol-wm`，前提是账号确实公布该目标 |
| 文本与图片输入 | 支持文本对话及已有图片上传转换；`developer` 转换为 Web `system` |
| Chat 函数调用 | 支持客户端函数往返及流式协议包装，生成参数依赖提示词，不提供官方严格 schema 保证 |
| 网页搜索 | Chat/Responses 按请求模型路由，不再固定使用 GPT-5.6 Sol；专用 `/v1/search` 默认使用 GPT-6.1 Sol，并检查账号权限 |
| 推理档位 | Thinking/Work：minimal/low → min，medium → standard，high → extended，xhigh/max → max；聊天 Pro 仅 standard，Instant 无档位。`none/auto` 不强制档位，不保证关闭推理 |
| 推理摘要与用量 | 返回 Web 可见 recap；token 由本地估算，不能代表完整隐藏推理或实际账单 |
| SSE | role/content/tool_calls/finish/usage/[DONE] 包装；失败返回 OpenAI error，不输出成功 finish/usage |
| 输出限制、采样、JSON schema | 文本 Chat 显式返回 400，不再接收后忽略；未实现服务端生成预算或严格输出约束 |
| Responses | 文本、搜索及图片的部分转换；本次统一思考参数解析及搜索模型路由，未补齐通用函数调用、previous_response_id 状态、后台任务及全部生命周期事件 |
| Work/Codex 平台工具 | 不提供完整的 shell、computer use、MCP、multi-agent、长期任务或 Ultrafast service tier |

## 本次修复

1. 按单个账号缓存和筛选模型权限；凭据替换即使账号数量不变也会刷新目录。使用凭据指纹作为缓存身份，不向客户端暴露 token。
2. 临时故障的旧目录最多保留到上次成功后的 15 分钟；401/403 立即清除对应账号的旧能力。默认最多 4 路并发获取目录；账号池较大时首次请求会比按订阅抽样更慢。
3. 组合解析模型别名，区分普通聊天原生 slug 与 Work 的 `-wm` 目标；两类路由不互相替代，未知型号返回 404 `model_not_found`。移除要求模型自称指定型号的系统提示。
4. 搜索传递调用者选择的型号，避免返回值回显新模型、上游实际执行旧模型。
5. 统一 Chat/Responses 思考强度解析，修复 null 遮蔽其他配置及 `none` 被全局默认覆盖的问题；按当前目录的四个 Web 档位近似转换，并保护 Pro/Instant 不继承不支持的全局默认档位。
6. 文本 Chat 校验消息与工具，拒绝当前无法实现的 OpenAI 生成参数，并返回可定位字段的错误。图片分支继续使用原有图片参数。
7. 复用并关闭首个文本上游会话，消除创建后未使用的连接；统一 SSE 异常的 OpenAI error 形状。

## 验证与使用

回归测试使用模拟 Web 目录和上游事件，覆盖 Chat Pro 与 Astra Work 隔离、Pro/Instant 参数限制、组合别名、相同订阅的权限差异、账号替换、目录失效、未知型号、思考档位、函数及搜索路由、HTTP 错误、SSE 完成和中断。运行：

```bash
.venv/bin/python -m unittest \
  test.test_model_catalog_service test.test_text_model_routing \
  test.test_chat_request_validation test.test_chat_completion_cache \
  test.test_chat_completion_tools test.test_work_mode_handoff \
  test.test_protocol_compatibility -q
```

接入时先查询 `/v1/models`，从保留的六个文本型号中选择账号可用的 ID；旧文本模型请求返回 404。若客户端默认附带 `temperature`、`max_tokens`、JSON schema 等字段，需要移除或改用真正支持这些控制的后端。目录和路由测试不等于上游实测，凭据恢复后还需验证实际生成及账号额度。
