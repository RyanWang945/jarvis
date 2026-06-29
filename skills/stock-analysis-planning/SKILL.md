---
name: A股股票指标规划
description: 用于 A 股股票基础行情、股本、财务指标、估值指标和简单派生指标计算的 planner skill。
when_to_use: 用户要求分析 A 股、股票代码、上市公司行情估值、PE/PB/市值/ROE/毛利率/净利率等简单金融指标，尤其需要结合 Tushare MCP 或公开行情数据计算。
skill_type: planner
user_invocable: false
disable_model_invocation: true
routing_summary: 适用于 A 股股票分析、行情估值、股本市值、财务指标和简单派生指标计算；优先使用 Tushare MCP 获取结构化数据。
planning_guidance: |
  A 股股票分析任务应优先规划结构化市场数据节点，使用可用 MCP 工具（尤其是 tushareMcp）获取行情、股本和财务数据；网页搜索只作为公告、行业、风险和交叉验证补充。

  如果 runtime_context 的可用工具包含 tushareMcp 相关 MCP 工具，Planner 应生成一个 react mode=write 节点 collect_market_data，用于：
  - 确认证券身份：股票代码、交易所、公司简称、上市日期。
  - 获取交易日行情：收盘价、涨跌幅、成交量、成交额。
  - 获取每日指标或股本结构：总股本、流通股本、总市值、流通市值、PE、PB（如 MCP 提供）。
  - 获取最近年度和最近报告期财务指标：营业收入、归母净利润、扣非净利润、总资产、净资产、EPS、ROE、毛利率、净利率等。
  - 产出内部 artifact market_data.md，不创建面向用户的报告。

  market_data.md 使用 Markdown 表格，但表格列应尽量机器可读：
  - metric：指标英文名，例如 close, total_share, total_mv, revenue, net_profit_parent, eps, pe_ttm, pb, roe。
  - value：数值，必须是阿拉伯数字，不要混入单位。
  - unit：统一单位，例如 CNY/share, share, 100m_CNY, CNY, pct, times。
  - date_or_period：交易日或报告期。
  - source：例如 tushare:daily, tushare:daily_basic, tushare:fina_indicator, filing, web。
  - note：口径说明或待核验说明。

  简单 A 股指标计算公式：
  - 总市值（亿元）= 收盘价（元/股） × 总股本（股） / 100,000,000。
  - 流通市值（亿元）= 收盘价（元/股） × 流通股本（股） / 100,000,000。
  - PE（静态）= 总市值（元） / 最近年度归母净利润（元）。
  - PE（TTM）= 总市值（元） / TTM归母净利润（元）。
  - PB = 总市值（元） / 归母净资产（元）。
  - EPS = 归母净利润（元） / 总股本（股）。
  - ROE = 归母净利润（元） / 归母净资产（元） × 100%。
  - 毛利率 = (营业收入 - 营业成本) / 营业收入 × 100%。
  - 净利率 = 归母净利润 / 营业收入 × 100%。
  - 股息率 = 每股现金分红 / 股价 × 100%。

  单位换算必须明确：
  - 1 亿股 = 100,000,000 股。
  - 1 万股 = 10,000 股。
  - 1 亿元 = 100,000,000 元。
  - 1 万元 = 10,000 元。
  - billion CNY = 10 亿元人民币。

  派生指标必须标记 source=computed，并在 note 中写出公式，例如：31.30 * 400010000 / 100000000 = 125.20。
  如果 MCP 直接返回的 total_mv 与公式计算值数量级冲突，必须在 market_data.md 中标记 conflict，不要静默合并。
  对日期不同导致的差异，必须保留各自日期，不要把不同交易日的股价和股本/市值混成一个确定结论。
---

这是仅供 Planner 使用的 skill。运行时 agent 不应通过 Skill 工具加载它。
