# Jarvis 同花顺 iFinD MCP 接入方案

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-07-03 |
| 状态 | Draft |
| 目标 | 让 Jarvis 接入同花顺 iFinD MCP 金融数据服务，用于 A 股、基金、宏观、新闻公告、债券、港美股、指数板块等投研取数 |
| 相关模块 | `app/tools/mcp`, `app/tools/runtime.py`, `app/config.py`, `skills/stock-analysis-planning` |
| 官方入口 | `https://mcp.51ifind.com/` |

## 1. 调研结论

同花顺 iFinD MCP 是一个远程 HTTP MCP 金融数据服务。官网标题为 `iFinD MCP - AI 金融数据服务`，官方前端和 Skill 包给出的实际服务地址为：

```text
https://api-mcp.51ifind.com:8643/ds-mcp-servers/{server_name}
```

官方提供 7 个 MCP server：

| 业务域 | Jarvis server 建议名 | 官方 server path | 主要工具 |
| --- | --- | --- | --- |
| A 股 | `ifind_stock` | `hexin-ifind-ds-stock-mcp` | `search_stocks`, `get_stock_summary`, `get_stock_info`, `get_stock_performance`, `get_stock_shareholders`, `get_stock_financials`, `get_risk_indicators`, `get_stock_events`, `get_esg_data`, `stock_highfreq_quotes` |
| 基金 | `ifind_fund` | `hexin-ifind-ds-fund-mcp` | `search_funds`, `get_fund_profile`, `get_fund_market_performance`, `get_fund_ownership`, `get_fund_portfolio`, `get_fund_financials`, `get_fund_company_info`, `fund_highfreq_quotes` |
| 宏观/行业经济 | `ifind_edb` | `hexin-ifind-ds-edb-mcp` | `search_edb`, `get_edb_data` |
| 新闻公告 | `ifind_news` | `hexin-ifind-ds-news-mcp` | `search_news`, `search_notice`, `search_trending_news` |
| 债券 | `ifind_bond` | `hexin-ifind-ds-bond-mcp` | `bond_basic_info`, `bond_market_data`, `bond_financial_data`, `bond_special_data`, `bond_highfreq_quotes` |
| 港美股 | `ifind_global_stock` | `hexin-ifind-ds-global-stock-mcp` | `search_global_stocks`, `global_stock_profile`, `global_stock_quotes`, `global_stock_financial`, `global_stock_events` |
| 指数板块 | `ifind_index` | `hexin-ifind-ds-index-mcp` | `index_data`, `sector_data`, `index_highfreq_quotes` |

鉴权方式不是 Bearer token。官方脚本用的是：

```http
Authorization: <auth_token>
Accept: application/json, text/event-stream
Content-Type: application/json
```

Token 获取路径：`mcp.51ifind.com` 登录后进入个人中心/密钥管理，创建 API Key。官方 Skill 的配置文件字段名是 `auth_token`，占位值是 `your ifind-mcp key`。

## 2. 套餐与权限

官网前端当前展示的版本权益：

| 版本 | 用量 | 并发 | 价格/说明 |
| --- | --- | --- | --- |
| 试用版 | 2000 次请求 | 2 次/秒 | 适合验证 MCP 接入和基础查数 |
| 个人版 | 5000 次/月 | 5 次/秒 | 月卡 `¥40`，季卡 `¥120`，年卡 `¥399` |
| 企业版 | 100 万次/月 | 10 次/秒 | 月卡 `¥5,000`，季卡 `¥15,000`，年卡 `¥50,000` |

个人版相对试用版的关键增量：港美股智能选股、热点资讯搜索，以及更稳定的月度用量和 5 次/秒并发。企业版额外开放 EDB 指标检索并提升用量/并发。

工具权限不是固定的，官方文档明确建议用 `tools/list` 或 Skill 包的 `listTools/list_tools` 获取当前密钥对应的真实工具清单。因此 Jarvis 不应假设所有工具都永久可用，首次接入和续费后都应跑一次 `tools/list` 记录实际列表。

## 3. 推荐接入方式

Jarvis 已有 remote HTTP MCP bridge，可以直接接 iFinD 的 remote MCP endpoint，不需要再套一层本地 gateway：

```text
iFinD remote MCP endpoint
  -> Jarvis HttpMcpClient
  -> McpToolManager
  -> ToolDefinition
  -> build_llm_tools / execute_tool
```

这比安装官方 `ifind-finance-data` Skill 更适合 Jarvis，因为：

1. Jarvis 已经能从 HTTP MCP `tools/list` 自动生成 `mcp__{server}__{tool}` 工具。
2. 统一走现有 `enabled_tools` 白名单、timeout、结果转换和工具缓存。
3. 不需要让 LLM 生成临时 Node/Python 脚本，也不会把密钥写入 Skill 目录。

官方 Skill 包仍然有价值，建议保留为参考资料和故障定位备选：

```text
https://s.thsi.cn/cd/ifind-java-ds-bff-web-container/ifind-mcp-web/skills/ifind-finance-data-1.3.0.zip
```

它包含 `call-node.js`、`call.py`、`mcp_config.json` 和 8 份参考文档，可用于核对工具名、参数形态和官方调用行为。

## 4. Jarvis 配置方案

推荐把密钥放在环境变量，MCP server 配置只引用环境变量，不把真实 token 提交进仓库。

`.env` 本地增加：

```dotenv
JARVIS_MCP_ENABLED=true
JARVIS_MCP_CONFIG_PATH=data/mcp_servers.yaml
JARVIS_IFIND_MCP_TOKEN=你的 iFinD MCP API Key
```

`data/mcp_servers.yaml` 示例：

```yaml
mcpServers:
  ifind_stock:
    transport: streamable_http
    url: https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-stock-mcp
    enabled: true
    required: false
    startup_timeout_sec: 10
    tool_timeout_sec: 60
    env_http_headers:
      Authorization: JARVIS_IFIND_MCP_TOKEN
    enabled_tools:
      - search_stocks
      - get_stock_summary
      - get_stock_info
      - get_stock_performance
      - get_stock_shareholders
      - get_stock_financials
      - get_risk_indicators
      - get_stock_events
      - get_esg_data
      - stock_highfreq_quotes

  ifind_fund:
    transport: streamable_http
    url: https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-fund-mcp
    enabled: true
    required: false
    startup_timeout_sec: 10
    tool_timeout_sec: 60
    env_http_headers:
      Authorization: JARVIS_IFIND_MCP_TOKEN
    enabled_tools:
      - search_funds
      - get_fund_profile
      - get_fund_market_performance
      - get_fund_ownership
      - get_fund_portfolio
      - get_fund_financials
      - get_fund_company_info
      - fund_highfreq_quotes

  ifind_edb:
    transport: streamable_http
    url: https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-edb-mcp
    enabled: true
    required: false
    startup_timeout_sec: 10
    tool_timeout_sec: 60
    env_http_headers:
      Authorization: JARVIS_IFIND_MCP_TOKEN
    enabled_tools:
      - search_edb
      - get_edb_data

  ifind_news:
    transport: streamable_http
    url: https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-news-mcp
    enabled: true
    required: false
    startup_timeout_sec: 10
    tool_timeout_sec: 60
    env_http_headers:
      Authorization: JARVIS_IFIND_MCP_TOKEN
    enabled_tools:
      - search_news
      - search_notice
      - search_trending_news

  ifind_bond:
    transport: streamable_http
    url: https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-bond-mcp
    enabled: true
    required: false
    startup_timeout_sec: 10
    tool_timeout_sec: 60
    env_http_headers:
      Authorization: JARVIS_IFIND_MCP_TOKEN
    enabled_tools:
      - bond_basic_info
      - bond_market_data
      - bond_financial_data
      - bond_special_data
      - bond_highfreq_quotes

  ifind_global_stock:
    transport: streamable_http
    url: https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-global-stock-mcp
    enabled: true
    required: false
    startup_timeout_sec: 10
    tool_timeout_sec: 60
    env_http_headers:
      Authorization: JARVIS_IFIND_MCP_TOKEN
    enabled_tools:
      - search_global_stocks
      - global_stock_profile
      - global_stock_quotes
      - global_stock_financial
      - global_stock_events

  ifind_index:
    transport: streamable_http
    url: https://api-mcp.51ifind.com:8643/ds-mcp-servers/hexin-ifind-ds-index-mcp
    enabled: true
    required: false
    startup_timeout_sec: 10
    tool_timeout_sec: 60
    env_http_headers:
      Authorization: JARVIS_IFIND_MCP_TOKEN
    enabled_tools:
      - index_data
      - sector_data
      - index_highfreq_quotes
```

Jarvis 对外暴露后的工具名形态：

```text
mcp__ifind_stock__search_stocks
mcp__ifind_stock__get_stock_financials
mcp__ifind_news__search_notice
mcp__ifind_index__index_data
```

## 5. 首次 smoke test

办完个人版并配置 token 后，先不要直接让模型自由调用，按下面顺序验证。

2026-07-03 已用当前本地账号完成一次真实验证：

| server | tools/list 结果 | 备注 |
| --- | --- | --- |
| `ifind_stock` | 10 个：`get_stock_summary`, `search_stocks`, `get_stock_performance`, `get_stock_info`, `get_stock_shareholders`, `get_stock_financials`, `get_risk_indicators`, `get_stock_events`, `get_esg_data`, `stock_highfreq_quotes` | 与官网 A 股清单一致 |
| `ifind_fund` | 7 个：`get_fund_profile`, `get_fund_market_performance`, `get_fund_ownership`, `get_fund_portfolio`, `get_fund_financials`, `get_fund_company_info`, `fund_highfreq_quotes` | 当前未返回 `search_funds` |
| `ifind_edb` | 1 个：`get_edb_data` | 当前未返回 `search_edb` |
| `ifind_news` | 2 个：`search_notice`, `search_news` | 当前未返回 `search_trending_news` |
| `ifind_bond` | 5 个：`bond_basic_info`, `bond_market_data`, `bond_financial_data`, `bond_special_data`, `bond_highfreq_quotes` | 与官网债券清单一致 |
| `ifind_global_stock` | 4 个：`global_stock_profile`, `global_stock_quotes`, `global_stock_financial`, `global_stock_events` | 当前未返回 `search_global_stocks` |
| `ifind_index` | 3 个：`index_data`, `sector_data`, `index_highfreq_quotes` | 与官网指数板块清单一致 |

Jarvis `McpToolManager` 已注册 32 个 `mcp__ifind_*__...` 工具；`mcp__ifind_stock__get_stock_info` 端到端调用成功，示例查询 `{"query": "贵州茅台上市时间"}` 返回 `600519.SH / 贵州茅台 / 20010827`。

### 5.1 配置加载验证

```powershell
$env:JARVIS_MCP_ENABLED = "true"
$env:JARVIS_MCP_CONFIG_PATH = "data/mcp_servers.yaml"
$env:JARVIS_IFIND_MCP_TOKEN = "<真实 token>"
.venv\Scripts\python.exe -m pytest tests/test_tools_mcp_config.py -q
```

这个测试只能证明 Jarvis 配置解析逻辑未坏，不能证明 iFinD 服务可用。

### 5.2 工具发现验证

建议新增一个临时脚本或用 Python REPL 调用：

```python
from app.config import Settings
from app.tools.mcp.config import load_mcp_server_configs
from app.tools.mcp.http_client import HttpMcpClient

settings = Settings(mcp_enabled=True, mcp_config_path="data/mcp_servers.yaml")
configs = load_mcp_server_configs(settings)

for config in configs:
    client = HttpMcpClient(config)
    try:
        tools = client.list_tools()
        print(config.name, [tool.get("name") for tool in tools])
    finally:
        client.close()
```

预期每个 server 至少能返回白名单内的工具。若出现 `401` 或鉴权错误，优先检查 `Authorization` 是否直接等于 iFinD API Key，不能加 `Bearer `。

### 5.3 最小调用验证

先用低成本、单主体、单指标查询，避免一上来消耗大请求：

```text
server: ifind_stock
tool: get_stock_info
arguments:
  query: "贵州茅台上市时间"
```

然后再验证个人版增量工具：

```text
server: ifind_global_stock
tool: search_global_stocks
arguments:
  query: "汽车行业且市盈率低于50"
  market: "港股"
```

最后验证新闻热点：

```text
server: ifind_news
tool: search_trending_news
arguments:
  keyword: "智能体"
  industry_name: "计算机"
  time_scope: "24小时"
  size: 5
```

## 6. 参数使用约定

大部分 iFinD 工具采用自然语言 `query` 参数，适合 Jarvis 把用户问题压成明确的“主体 + 时间 + 指标”：

```json
{"query": "同花顺、东方财富、大智慧、恒生电子在2025-09-30的净利润增速、ROE、ROA"}
```

高频实时行情工具例外，使用结构化参数，不用 `query`：

```json
{
  "symbols": "300033.SZ,300059,贵州茅台",
  "indicators": "最新价,涨跌幅,成交量,成交额",
  "data_mode": "real_time"
}
```

使用约束：

1. 股票/基金查询支持多主体、多指标，但一次请求主体数和指标数建议都控制在 5 个以内。
2. 日内高频/实时行情仅适用于交易日日内，不应用来查历史日频、财报或基本资料。
3. 债券高频实时行情仅支持交易所债券，不覆盖银行间市场。
4. 宏观 EDB 不确定具体指标时，先 `search_edb` 再 `get_edb_data`。
5. 新闻公告服务返回相关段落，不等于完整公告全文；如果要引用公告全文，仍需补公告原文来源。

## 7. 与 Jarvis 现有能力的关系

### 7.1 替代 Tushare 的一部分投研取数

当前 `skills/stock-analysis-planning` 偏向 Tushare MCP。iFinD 接入后建议改成：

1. A 股日行情、财务、股东、事件、ESG：优先 iFinD，Tushare 作为交叉验证或缺口补充。
2. 实时/高频行情：优先 iFinD，因为官方明确覆盖 A 股、基金、债券、指数的日内高频/实时行情。
3. 港美股、新闻公告、热点事件、债券、指数板块：优先 iFinD。
4. 需要严格可复现的数据表字段时，仍保留 Tushare/交易所公告等结构化来源。

### 7.2 结果可信度

iFinD MCP 的自然语言取数很适合快速投研，但 Jarvis 输出正式结论时仍应保留：

1. 数据日期。
2. 指标口径。
3. 工具名和查询参数。
4. 如果是实时行情，标注查询时间。
5. 如果用于投资判断，补充“非投资建议”和风险说明。

## 8. 风险与待办

### 8.1 MCP 协议版本

官方 Skill 初始化参数使用：

```json
{"protocolVersion": "2025-03-26"}
```

Jarvis 当前 `HttpMcpClient` 写死：

```text
protocolVersion: 2024-11-05
MCP-Protocol-Version: 2024-11-05
```

如果 iFinD server 不兼容旧版本，`initialize` 会失败。建议 smoke test 时重点观察 initialize 响应；如果失败，把 MCP protocol version 做成 `McpServerConfig` 可配置项，例如：

```yaml
protocol_version: "2025-03-26"
```

### 8.2 鉴权头格式

Jarvis 已支持：

```yaml
env_http_headers:
  Authorization: JARVIS_IFIND_MCP_TOKEN
```

不要使用 `bearer_token_env_var`，因为官方脚本没有 `Bearer ` 前缀。

### 8.3 请求额度和并发

个人版 5000 次/月、5 次/秒。Jarvis 的 DAG 或多节点并发调用可能在批量分析时打满额度。建议：

1. 对 iFinD MCP 工具设置较小并发，默认串行或最多 2 路。
2. 缓存 `tools/list` 结果，避免每个任务重复发现。
3. 对同一个股票/指标/日期的查询做任务内复用。
4. 避免 Planner 生成“全市场大范围扫描”类请求，除非用户明确要求。

### 8.4 日志脱敏

`Authorization` 不能进入 prompt、trace、错误日志或飞书消息。当前 Jarvis MCP config 只把 header 放在请求层，文档层要求：

1. `.env` 不提交。
2. `data/mcp_servers.yaml` 不写真实 token。
3. 报错只展示状态码和错误类型，不打印 request headers。

## 9. 建议落地步骤

1. 开通或试用 iFinD MCP，进入个人中心创建 API Key。
2. 本地 `.env` 增加 `JARVIS_IFIND_MCP_TOKEN`、`JARVIS_MCP_ENABLED=true`、`JARVIS_MCP_CONFIG_PATH=data/mcp_servers.yaml`。
3. 新建本地 `data/mcp_servers.yaml`，先只启用 `ifind_stock` 和 `ifind_news` 两个 server。
4. 跑 `tools/list` smoke test，确认协议版本和鉴权头可用。
5. 验证 `get_stock_info`、`search_notice` 两个低风险工具。
6. 再启用其余 server，并记录当前账号真实可用工具。
7. 更新 `skills/stock-analysis-planning`，把 iFinD 作为 A 股/港美股/新闻公告/债券/指数的优先数据源。
8. 如果 `initialize` 因协议版本失败，先补 `protocol_version` 配置项再继续。

## 10. 资料来源

1. 官方首页：`https://mcp.51ifind.com/`
2. 官方静态前端资源：`https://s.thsi.cn/cd/ifind-java-ds-bff-web-container/ifind-mcp-web/assets/index-CBozMg8y.js`
3. 官方 Skill 安装引导：`https://mcp.51ifind.com/gwstatic/static/ds_web/ifind-mcp-web/skills/SKILL_INSTALL_GUIDE.md`
4. 官方 Skill 包：`https://s.thsi.cn/cd/ifind-java-ds-bff-web-container/ifind-mcp-web/skills/ifind-finance-data-1.3.0.zip`
