# Jarvis GitHub MCP 接入方案

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-06-11 |
| 状态 | Draft |
| 目标 | 让 Jarvis 通过远程 HTTP MCP 连接 GitHub 能力，并以白名单和权限策略控制工具暴露 |
| 相关模块 | `app/tools/mcp`, `app/tools/runtime.py`, `app/tools/definitions.py`, `app/task_runtime/node_execute_runtime.py` |

## 1. 背景

Jarvis 已经具备 remote HTTP MCP bridge：

```text
MCP server / gateway
  -> HTTP MCP endpoint
  -> Jarvis MCP client
  -> ToolDefinition
  -> build_llm_tools / get_tool_definition / execute_tool
```

FRED MCP 的第一版验证结果说明这条链路成立：Jarvis 可以通过 `mcp__{server}__{tool}` 命名发现远程工具，并把 `tools/call` 结果转成 `ToolExecutionResult`。

GitHub MCP 与 FRED 的差异是风险边界更复杂：

1. GitHub MCP 工具包含仓库读取、issue/PR 读取，也可能包含写文件、创建 issue、更新 PR 等写能力。
2. GitHub token 权限通常比单一只读数据源更大，不能进入 Jarvis prompt、工具参数或日志。
3. GitHub 返回内容可能很长，尤其是文件内容、PR diff、issue 讨论，需要结果截断和引用边界。
4. GitHub 仓库操作与 Jarvis 现有 repository registry、coder runtime、permission policy 有重叠，不能让 MCP 写能力绕过已有审批。

因此 GitHub MCP 接入必须先走只读白名单，后续再逐步开放写能力。

## 2. 结论

推荐架构：

```text
GitHub MCP server / gateway
  - 持有 GITHUB_PERSONAL_ACCESS_TOKEN
  - 对外暴露 HTTP MCP endpoint

Jarvis
  - 只保存 endpoint 和工具白名单
  - 通过 HTTP MCP initialize/tools/list/tools/call 连接
  - 暴露 mcp__github__... 工具给模型
  - 执行风险分级、结果截断和审批
```

Jarvis 不直接持有 GitHub token，不把 GitHub MCP stdio/Docker 启动细节塞进 agent runtime。

第一阶段只支持只读工具：

```yaml
mcpServers:
  github:
    url: http://127.0.0.1:8770/mcp
    enabled_tools:
      - search_repositories
      - get_file_contents
      - list_issues
      - get_issue
      - list_pull_requests
      - get_pull_request
      - get_pull_request_files
```

如果具体 GitHub MCP server 的工具名不同，以 `tools/list` 真实返回为准。

## 3. Transport 选择

### 3.1 推荐：独立 HTTP MCP gateway

GitHub MCP 常见形态是 stdio server，例如 Docker:

```text
docker run --rm -i --env GITHUB_PERSONAL_ACCESS_TOKEN docker.xuanyuan.run/mcp/github:latest
```

Jarvis 不应直接启动这个 stdio server。推荐增加一个独立 gateway：

```text
GitHub MCP stdio server
  <-> HTTP MCP gateway
  <-> Jarvis
```

gateway 职责：

1. 读取 `GITHUB_PERSONAL_ACCESS_TOKEN`。
2. 启动或连接 GitHub MCP stdio server。
3. 对外暴露 `/mcp` HTTP endpoint。
4. 隔离进程生命周期、stderr、重启、健康检查。

Jarvis 职责：

1. 连接 gateway endpoint。
2. 发现工具。
3. 过滤工具。
4. 转发调用。
5. 记录审计。

### 3.2 备选：Jarvis 支持 stdio transport

Jarvis 可以后续补 stdio MCP client，但不作为 GitHub 主线。原因：

1. 会让 Jarvis 重新承担外部 server 生命周期。
2. Docker 命令、token env、stderr、重启都会进入 Jarvis runtime。
3. 与“self-mcp-server 独立启动，Jarvis 只连接”的方向不一致。

## 4. Jarvis 配置

推荐使用文件配置：

```yaml
mcpServers:
  github:
    transport: streamable_http
    url: http://127.0.0.1:8770/mcp
    enabled: true
    required: false
    startup_timeout_sec: 10
    tool_timeout_sec: 60
    enabled_tools:
      - search_repositories
      - get_file_contents
      - list_issues
      - get_issue
      - list_pull_requests
      - get_pull_request
      - get_pull_request_files
    disabled_tools: []
```

Jarvis 环境变量：

```powershell
$env:JARVIS_MCP_ENABLED = "true"
$env:JARVIS_MCP_CONFIG_PATH = "data/mcp_servers.yaml"
```

本地临时测试可以使用 JSON：

```powershell
$env:JARVIS_MCP_ENABLED = "true"
$env:JARVIS_MCP_SERVERS_JSON = '{"mcpServers":{"github":{"url":"http://127.0.0.1:8770/mcp","enabled_tools":["search_repositories"]}}}'
```

GitHub token 只放 gateway/server 环境：

```powershell
$env:GITHUB_PERSONAL_ACCESS_TOKEN = "..."
```

不要放入 Jarvis `.env`，除非 Jarvis 本身就是 gateway 进程；当前方案不是。

## 5. 工具命名

Jarvis 暴露给模型的工具名继续使用 Codex 风格：

```text
mcp__github__search_repositories
mcp__github__get_file_contents
mcp__github__list_pull_requests
```

规则：

1. 原始 server/tool 名先组合为 `mcp__{server}__{tool}`。
2. 非 `[A-Za-z0-9_-]` 字符替换成 `_`。
3. 超过 64 字符时用 sha1 后缀截断。
4. manager 保存 `qualified_name -> server/tool` 映射，调用时不依赖字符串反解。

## 6. 权限和风险分级

GitHub MCP 的第一阶段只暴露只读工具。第二阶段再引入 per-tool risk override。

建议扩展配置：

```yaml
mcpServers:
  github:
    url: http://127.0.0.1:8770/mcp
    tool_policies:
      search_repositories:
        risk_level: low
        execution_mode: direct
      get_file_contents:
        risk_level: low
        execution_mode: direct
      create_issue:
        risk_level: medium
        execution_mode: proposal
      create_pull_request:
        risk_level: high
        execution_mode: proposal
      push_files:
        risk_level: critical
        execution_mode: proposal
```

写能力开放原则：

1. 默认不暴露。
2. 只允许显式白名单工具。
3. `execution_mode=proposal`，必须走 Jarvis 审批。
4. 写仓库内容、创建 PR、改 issue/PR 标签等操作要记录 tool call input/output。
5. 不能绕过 coder runtime 对 repository 修改的策略。

如果用户要求修改代码，优先路径仍然是 Jarvis coder runtime；GitHub MCP 写文件只适合很窄的仓库托管 API 场景。

## 7. 结果处理

GitHub MCP 返回内容可能很大。Jarvis MCP bridge 需要补充结果截断策略：

1. `stdout` 最大字符数，例如 12k 或 24k。
2. 对文件内容、diff、comment body 做字段级截断。
3. 保留 `url`、`html_url`、`path`、`sha`、`number`、`state` 等引用字段。
4. 对二进制或超大文件只返回元数据和截断说明。
5. 工具结果中的 token、authorization header、环境变量名对应值必须脱敏。

建议输出摘要：

```json
{
  "ok": true,
  "summary": "GitHub MCP tool mcp__github__get_file_contents completed.",
  "truncated": true,
  "items": [...]
}
```

## 8. 与 repository registry 的关系

Jarvis 已有 repository registry 和 coder runtime。GitHub MCP 不替代它们。

分工：

| 场景 | 推荐路径 |
| --- | --- |
| 搜索公开仓库 | GitHub MCP |
| 读取远端 issue/PR 元数据 | GitHub MCP |
| 读取少量远端文件 | GitHub MCP |
| 本地仓库代码分析 | coder runtime / repository registry |
| 本地仓库修改、测试、commit | coder runtime |
| 创建 PR、更新 issue | 第二阶段 GitHub MCP proposal |

当用户指定已注册本地仓库，优先使用 repository registry。GitHub MCP 用于远端平台信息和未 clone 仓库读取。

## 9. 实施计划

### Phase 1: 只读接入

1. 准备独立 HTTP MCP gateway，暴露 `http://127.0.0.1:8770/mcp`。
2. 添加 `data/mcp_servers.yaml` 示例配置。
3. 只白名单 GitHub 只读工具。
4. 通过 Jarvis `build_llm_tools()` 验证出现 `mcp__github__...`。
5. 真实调用：
   - `search_repositories`
   - `get_file_contents`
   - `list_pull_requests` 或 `get_pull_request`
6. 验证未白名单工具不出现在模型工具列表。

### Phase 2: 策略化工具风险

1. MCP config 支持 `tool_policies`。
2. `McpToolManager` 创建 `ToolDefinition` 时读取 per-tool policy。
3. `check_tool_policy` 对 proposal MCP tool 做统一拦截。
4. 测试 medium/high/critical 工具不会 direct 执行。

### Phase 3: 结果截断与审计

1. MCP result sanitizer 支持最大字符数。
2. 对常见 GitHub payload 做字段级截断。
3. tool call record 保留 server/tool/qualified name。
4. 错误响应区分 auth、rate limit、not found、permission denied。

### Phase 4: 有限写能力

1. 开放 issue/PR 评论等低风险写操作。
2. 开放 create issue / create pull request。
3. 仓库内容写入仍默认走 coder runtime，除非用户明确要求 GitHub API 写入。
4. 所有写操作必须有用户确认或 proposal approval。

## 10. 测试门禁

### Unit

1. GitHub config 只暴露 `enabled_tools`。
2. `disabled_tools` 能覆盖 `enabled_tools`。
3. `mcp__github__...` 工具名 sanitize/hash 稳定。
4. per-tool policy 能正确映射到 `ToolDefinition`。
5. 长结果会被截断并保留引用字段。

### Local smoke

1. gateway 启动后 `tools/list` 成功。
2. Jarvis `build_llm_tools()` 包含 GitHub 白名单工具。
3. `search_repositories` 查询公开仓库成功。
4. `get_file_contents` 读取公开 README 成功。
5. 未白名单写工具不出现在 `build_llm_tools()`。

### Safety

1. Jarvis 日志不包含 `GITHUB_PERSONAL_ACCESS_TOKEN`。
2. 工具参数 schema 不包含 token 字段。
3. 写工具默认不可见。
4. 写工具即使可见，也必须 proposal。

## 11. 当前建议

先不要改 Jarvis 支持 stdio。下一步应先做一个独立 GitHub HTTP MCP gateway 或确认现有 GitHub MCP server 能直接以 HTTP 模式启动。

Jarvis 侧当前 remote HTTP MCP bridge 已经足够承接 Phase 1，只需要准备 endpoint、白名单配置和真实 smoke test。
