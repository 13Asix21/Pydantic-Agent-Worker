# Agent Bridge 凭证填写指南

> 用途：把 PoC（默认 mock）切到真实环境。你按本文逐项填，填完 `agent-bridge/.env` 即可。
> 原则：**真实密钥只写进 `.env`，绝不提交到 git**（`.env` 已在 `.gitignore`）。本文所有 `______` 是预留位，由你填写。
> 填写日期：______　　填写人：______

---

## 0. 凭证总表（先看要准备几样）

| # | 凭证 | 用途 | 必需性 | 在哪一步用 |
|---|---|---|---|---|
| 1 | GitHub 建仓/推送权限 | 把代码推到 `Pydantic-Agent-Worker` 仓库 | **必需**（一次性） | 项目托管，非运行时 |
| 2 | LLM base_url + key | 让 agent 说真话（否则一直 mock 假回复） | **必需** | 运行时 `AGENT_BRIDGE_LLM__*` |
| 3 | 乐享 endpoint + token | 独立服务直连乐享知识库（脱离 WorkBuddy 网关） | 必需（要验真时） | 运行时 `AGENT_BRIDGE_LEXIANG__*` |
| 4 | GitHub 运行 token | agent 运行时操作 GitHub 仓库（读代码/建 issue 等） | 必需（G3 已验机制） | 运行时 `AGENT_BRIDGE_GITHUB_TOKEN` |
| 5 | MOI/A2A key（可选） | 对外提供 A2A 服务、或调用 MOI 平台智能体 | 可选（阶段4用） | A2A 扩展时 |

> 说明：#1 是**一次性操作权限**，用完可不留；#2–#5 是**运行时凭证**，填进 `.env` 长期用。

---

## 1. GitHub 建仓与推送权限（阻塞项，优先处理）

**现状**：`Pydantic-Agent-Worker` 仓库还不存在（已查 404），当前 GitHub 连接凭证没有建仓权限（403）。本地 `gh` CLI 未安装、git 无凭证 helper。

### 路径 A（推荐，最简单，不泄露 PAT）：网页手动建空仓

1. 打开 https://github.com/new （用 13Asix21 登录）
2. Repository name 填：`Pydantic-Agent-Worker`
3. Visibility 选：**Private**（推荐；要公开以后可改）
4. **不要**勾 Initialize this repository with a README / .gitignore / license（保持完全空仓，避免首次推送冲突）
5. 点 Create repository
6. 建好后把仓库地址填这里：`git@github.com:13Asix21/Pydantic-Agent-Worker.git` ✅（已确认）
7. 建完告诉我，我来配 remote + 首次推送（用我手上的 GitHub 连接尝试；若集成权限不足，转路径 B）

### 路径 B（备用）：给我一个带 repo 权限的 PAT，我用 API 建仓+推送

1. 打开 https://github.com/settings/tokens
2. 点 **Generate new token** → **Generate new token (classic)**
3. Note 填：`agent-bridge-setup`；Expiration 选 `90 days`（或自定义）
4. Scopes 勾选：**`repo`**（整组全选，含 repo:status / repo_deployment / public_repo / repo:invite / security_events）
5. 点 Generate token，**立即复制**（页面只显示一次）
6. 填到下面预留位（填完可删这段，别提交）：

```
GITHUB_SETUP_PAT=______
```

> 安全提示：PAT 等同于账号密码。若走路径 B，用完建议在 https://github.com/settings/tokens  promptly revoke。

---

## 2. LLM 凭证（让 agent 说真话）

**现状**：默认 `provider=mock`，回复是确定性的假 token。**不填也能跑全部测试**，但要真干活必须填。

**推荐用 Genesis 网关**（你们已有的 OpenAI 兼容网关，之前实测 12 个模型全可用）：

| 项 | 填写位 | 说明 |
|---|---|---|
| Base URL | `AGENT_BRIDGE_LLM__BASE_URL=` | 你的 OpenAI 兼容网关地址（格式 `https://<网关>/v1`，向平台管理员获取） |
| API Key | `AGENT_BRIDGE_LLM__API_KEY=` | 你的个人访问令牌或服务账号令牌（以 `sk-` 开头的串） |
| Model | `AGENT_BRIDGE_LLM__MODEL=` | 主备链：**`deepseek-v4.1-flash`** → 备 `qwen3.8-27b` → 三级 `glm-5.3-flash` |
| Provider | `AGENT_BRIDGE_LLM__PROVIDER=` | 填 `openai_compatible` |
| Stream | `AGENT_BRIDGE_LLM__STREAM=` | `true` |

### 获取步骤（若已有令牌可跳过）
1. 打开你的 LLM 平台控制台的令牌管理页（地址问平台管理员，本文不记录内网域名）
2. 生成个人访问令牌，复制 `sk-...` 开头的串
3. 注意：这是敏感信息，**只填 `.env`，不要贴进对话或任何文档**

### 填好预留位（在 `.env` 里）
```
AGENT_BRIDGE_LLM__PROVIDER=openai_compatible
AGENT_BRIDGE_LLM__BASE_URL=https://<你的网关地址>/v1
AGENT_BRIDGE_LLM__API_KEY=______
AGENT_BRIDGE_LLM__MODEL=deepseek-v4.1-flash
AGENT_BRIDGE_LLM__STREAM=true
```

> 重要（来自之前实测）：推理模型（kimi-k3/glm-5.3 系）首 token 会吃 `max_tokens`，生产起步设 ≥512；静态健康标记不可信，以真实调用为准。主备链已定稿，见工作日志。

### 主备链实现方式（P0 官方化，2026-11-19）

主备链已由 PydanticAI 2.70.0 官方 capability 实现，**不再依赖手工配置约定**：

- `ModelFallback(*fallback_models, sticky=True)`：agent 自带 `model` 为首选，按顺序切换 `fallback_models`（默认 `qwen3.8-27b → glm-5.3-flash`），`sticky=True` 让落到备模型的会话保持在该模型以复用 prompt cache。
- `RetryOnModelErrors(max_attempts=3)`：对 429/超时/连接错误先在同一模型做指数退避重试，用尽后再由 ModelFallback 切备。
- 仅在 `provider=openai_compatible` 时注入；`mock` 模式零主备 capability，保持确定性、不需要任何真实凭证。
- 备用模型列表可通过 `AGENT_BRIDGE_LLM__FALLBACK_MODELS`（JSON 数组）或 `config/settings.yaml` 的 `llm.fallback_models` 覆盖；重试次数用 `AGENT_BRIDGE_LLM__RETRY_MAX_ATTEMPTS`。

---

## 3. 乐享知识库凭证（独立服务直连）

**现状**：PoC 里乐享走的是 WorkBuddy 私有网关（`mcp__lexiang__call_tool`），独立 Python 服务**拿不到那个网关**。要脱离 WorkBuddy 直接读写，需要乐享开放 API 的 endpoint + token。

| 项 | 填写位 | 说明 |
|---|---|---|
| Endpoint | `AGENT_BRIDGE_LEXIANG__ENDPOINT=` | 乐享 MCP/开放 API 的 Streamable HTTP 地址 |
| Token | `AGENT_BRIDGE_LEXIANG__TOKEN=` | 访问令牌 |

```
AGENT_BRIDGE_LEXIANG__ENDPOINT=______
AGENT_BRIDGE_LEXIANG__TOKEN=______
```

### 获取步骤（需要你确认乐享侧提供形态）
1. 找乐享平台管理员确认：独立服务接入的 **MCP endpoint URL** 和 **鉴权方式**（Bearer token / API Key / OAuth）
2. 生成服务账号令牌（不要用个人令牌长期跑服务）
3. 拿到后还要把 `config/mcp_servers.yaml` 里乐享那段 transport 从 `mock` 改成 `streamable_http`（我来改，或你按 README 改）

### 测试目标（已实测可用，填好后我跑 live 用例）
- 个人知识库 space_id：`c3f157b3d03d4c7fa46a8fb8e70b1ae7`
- root_entry_id：`571e43a904274b4c9aaa5f7c48e05d77`
- 写入测试条目统一 `[POC-TEST]-<uuid>`，`finally` 自动清理

> 如果短期拿不到独立凭证：**不阻塞**。PoC 的 116 工具目录 + mock transport 已把机制验证完（48 passed），live 用例（6 个）会保持 skip，等凭证到位再跑。

---

## 4. GitHub 运行 token（agent 运行时操作仓库）

**现状**：PoC 的 G3 只验了「双 MCP server 无冲突」，没验真实 GitHub 操作。

| 项 | 填写位 | 说明 |
|---|---|---|
| Token | `AGENT_BRIDGE_GITHUB_TOKEN=` | GitHub MCP server 运行时代理用 |

```
AGENT_BRIDGE_GITHUB_TOKEN=______
```

### 获取步骤
1. https://github.com/settings/tokens → Generate new token (classic)
2. Note 填 `agent-bridge-runtime`；Expiration 选 90 days
3. Scopes 按最小够用勾选：读仓库 `repo:status` `public_repo`；若要 agent 建 issue/PR 再勾 `repo`（完整）
4. 生成复制，填入上面预留位

> 与 #1 的区分：#1 是**建仓推送用**（一次性）；#4 是**服务运行时代理 GitHub 用**（长期）。可以是同一个 token，但生产建议分开、最小权限。

---

## 5. MOI / A2A 凭证（可选，阶段 4 扩展用）

扩展 A2A 与多 agent 协作时可能需要（设计子代理正在细化，先预留）：

| 项 | 预留位 | 说明 |
|---|---|---|
| MOI API Key | `AGENT_BRIDGE_MOI__API_KEY=` | 调用 MOI 平台智能体（A2A 用 X-API-Key） |
| MOI Workspace ID | `AGENT_BRIDGE_MOI__WORKSPACE_ID=` | MOI 工作区 ID |
| 本服务对外 base URL | `AGENT_BRIDGE_A2A__PUBLIC_URL=` | 供外部 agent 发现 Agent Card |

```
AGENT_BRIDGE_MOI__API_KEY=______
AGENT_BRIDGE_MOI__WORKSPACE_ID=______
AGENT_BRIDGE_A2A__PUBLIC_URL=______
```

> 排雷提醒（来自 MOI 实测）：MOI 智能体的 `/agents/{id}/mcp/http` 需要 RuntimeGrant，个人令牌不能替代 → **不能把 MOI 智能体当常驻 MCP，必须走 A2A**。A2A 端点：`POST /v5/workspaces/{ws}/agents/{agent}/a2a`。

---

## 6. 填完后的验证清单

按顺序跑，全部通过才算接好：

```bash
cd E:/ASoftwareManage/Agents/AI本体和知识库的结合/agent-bridge

# 1) 配置能加载、密钥不落库
uv run python -c "from agent_bridge.config.settings import Settings; print(Settings())"

# 2) 全量回归（默认 mock，不依赖凭证，应始终全绿）
uv run python -m pytest tests/ -q

# 3) LLM 真实连通（填完 #2 后）
# 手动跑一次 /agent/run，确认返回非 mock 内容

# 4) 乐享 live 用例（填完 #3 后）
RUN_LIVE=1 uv run python -m pytest tests/test_mcp_proxy.py -m live -q
```

---

## 填写进度勾选

- [ ] #1 GitHub 仓库已建（echo 空仓地址确认）
- [ ] #2 LLM 凭证已填，`provider=openai_compatible`
- [ ] #3 乐享 endpoint + token 已填（或明确暂缓）
- [ ] #4 GitHub 运行 token 已填
- [ ] #5 MOI/A2A 凭证（可选）
- [ ] 验证清单 1–4 全通过
