# Agent Bridge — Phase 0 PoC

「可被调用的 agent 构件」骨架：把 **Skills** 与 **MCP 工具** 收敛到统一 `ToolRegistry`，同时通过 **REST/SSE（人）** 与 **MCP Server（agent）** 两条通道暴露同一个 `AgentRuntime`。PoC 全离线可跑：默认 mock LLM + mock MCP transport，不需要任何密钥或外网。

技术栈：Python 3.13 + PydanticAI 2.70 + FastAPI + MCP 官方 Python SDK + Pydantic Settings + structlog。

## 1. 目录结构

```
agent-bridge/
├── config/
│   ├── settings.yaml          # 静态默认值（凭证禁止写入）
│   ├── mcp_servers.yaml       # 乐享 / GitHub 两个 MCP server 定义
│   ├── logging.yaml           # trace 字段与脱敏键
│   └── lexiang_tools.yaml     # tools/list 不可用时的声明式目录（116 个工具）
├── agent_bridge/
│   ├── config/settings.py     # YAML + AGENT_BRIDGE_* ENV 覆盖
│   ├── tools/                 # 统一注册表 + native web/http 工具
│   ├── skills/                # L1 manifest / L2 正文 / L3 资源脚本 + 沙箱
│   ├── mcp/                   # transport / discovery / proxy / gateway / manager / adapter
│   ├── runtime/               # AgentRuntime / SessionStore / TraceContext
│   └── server/                # REST / SSE / FastMCP / create_app
├── tests/                     # T0–T2 单测 + test_rest / test_mcp_server + e2e/test_gates.py
├── pyproject.toml / uv.lock / .env.example
```

## 2. 安装与启动

```bash
cd agent-bridge
uv sync                                   # 已生成 uv.lock，117 个包
uv run python -m agent_bridge.main         # 默认 127.0.0.1:8000
curl http://127.0.0.1:8000/health
# {"code":0,"data":{"status":"ok","dry_run":true,"tools":15,"llm_provider":"mock"},...}
```

复制 `.env.example` 为 `.env` 可覆盖配置；所有键以 `AGENT_BRIDGE_` 开头，嵌套段用双下划线，例如 `AGENT_BRIDGE_LLM__PROVIDER=openai_compatible`。

## 3. 三种调用方式

### 3.1 REST 同步

```bash
curl -X POST http://127.0.0.1:8000/agent/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"你好","session_id":"demo"}'
# {"code":0,"data":{"text":"Agent Bridge mock 响应：你好","tool_calls":[],...},"message":"ok","trace_id":"..."}
```

### 3.2 REST SSE 流式

```bash
curl -N -X POST http://127.0.0.1:8000/agent/run/stream \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"写一段流式输出","session_id":"demo"}'
# data: {"code":0,"data":{"delta":"Agent Bridge...","session_id":"demo"},...}
# data: [DONE]
```

### 3.3 MCP Server（agent 调用）

服务在 `/mcp` 挂载 Streamable HTTP。外部 MCP client 配置示例：

```json
{"mcpServers": {"agent-bridge": {"url": "http://127.0.0.1:8000/mcp"}}}
```

`tools/list` 可见 `agent_run(prompt, session_id)`；`call_tool` 返回与 REST 相同的 `{code,data,message,trace_id}`。内存中测试客户端：

```python
from mcp.shared.memory import create_connected_server_and_client_session
from agent_bridge.server.mcp_server import create_mcp_server

async with create_connected_server_and_client_session(create_mcp_server(runtime)) as client:
    result = await client.call_tool("agent_run", {"prompt": "你好"})
```

### 3.4 让 LLM 调工具（mock 协议）

默认 mock LLM 识别固定语法 `调用 <工具名> 参数:{json}`，会真实发射 `tool_call` 并由 PydanticAI 自动派发：

```bash
curl -X POST http://127.0.0.1:8000/agent/run -H 'Content-Type: application/json' \
  -d '{"prompt":"调用 native__web_search 参数:{\"query\":\"Agent Bridge\"}"}'
# data.tool_calls = ["native__web_search"]，结果内含确定性 mock 搜索结果
```

## 4. 切换到真实 LLM

默认 `AGENT_BRIDGE_LLM__PROVIDER=mock`（确定性假回复 + 逐块 token）。接 OpenAI-compatible 模型：

```bash
export AGENT_BRIDGE_LLM__PROVIDER=openai_compatible
export AGENT_BRIDGE_LLM__BASE_URL=https://your-endpoint/v1
export AGENT_BRIDGE_LLM__API_KEY=sk-...        # 只从 ENV 注入，禁止写入 config/*.yaml
export AGENT_BRIDGE_LLM__MODEL=your-model
export AGENT_BRIDGE_LLM__STREAM=true
```

凭证缺失时启动会报 `1001` 配置错误；mock 模式永不请求外网。`native__http_request` / `web_search` 默认返回 `mocked: true`，只有 `AGENT_BRIDGE_TOOLS__ENABLE_REAL_HTTP=true` 才真发请求。

## 5. 乐享 live 用例与写操作

默认 `config/mcp_servers.yaml` 两个 server 均为 mock transport，`dry_run=true`（`AGENT_BRIDGE_ALLOW_WRITES=false`）。写工具返回 `code=3003 / dry_run=true`，须同时满足 `allow_writes=true` 与调用方 `confirm_writes=true` 才执行。

拿到独立服务后：把 lexiang 的 `transport` 改为 `streamable_http` / `sse` / `stdio`，并注入：

```bash
export RUN_LIVE=1
export AGENT_BRIDGE_LEXIANG_ENDPOINT=https://...
export AGENT_BRIDGE_LEXIANG_TOKEN=...
uv run python -m pytest tests/test_mcp_proxy.py -m live -q
```

TC-LX-01~08 使用实测 `space_id=c3f157b3d03d4c7fa46a8fb8e70b1ae7`、`root_entry_id=571e43a904274b4c9aaa5f7c48e05d77`；写入条目以 `[POC-TEST]-<uuid>` 命名并在 `finally` 中删除。默认测试（无 live）不依赖任何凭证。

## 6. 统一约定

- 工具名：`<namespace>__<tool_name>`，保留 `lexiang__/github__/skill__/native__`；撞名升级 `<ns>__<category>__<tool>`。
- 响应：`{code, data, message, trace_id}`；SSE 为 `data: {...}\n\n`，结束帧 `data: [DONE]`。
- 错误码：`0` 成功 / `1xxx` 配置 / `2xxx` Skills / `3xxx` MCP（3002 降级、3003 权限拦截）/ `4xxx` 沙箱（4001 穿越、4002 超时、4003 解释器）/ `5xxx` 参数与服务。
- 日志字段：`trace_id/run_id/session_id/tool_name/server/skill/latency_ms/dry_run/code`；token/api_key/prompt 等键脱敏。

## 7. 验收命令

```bash
uv run python -m pytest tests/ -q
uv run python -m pytest tests/test_rest.py -q
uv run python -m pytest tests/test_mcp_server.py -q
uv run python -m pytest tests/e2e/test_gates.py -q
uv run ruff check agent_bridge tests --output-format concise
```

## 8. PoC 边界（明确不做）

A2A、鉴权/限流、持久检查点、容器沙箱、真实凭证与真实 LLM 调用（可配置但默认不开）、多 agent 编排、生产可观测仪表盘。Skills 沙箱是同解释器子进程 + 路径锁，不是容器隔离。
