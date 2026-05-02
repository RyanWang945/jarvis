# Jarvis Skills 加载与编排设计

| 项目 | 内容 |
|------|------|
| 作者 | Codex |
| 日期 | 2026-05-02 |
| 版本 | v1.0 |
| 状态 | 设计中 |

---

## 1. 背景

Jarvis 当前的 skill 系统位于 `app/skills/`，核心职责是把磁盘上的 skill 目录（`manifest.yaml` 或 `SKILL.md`）加载进内存索引，供 Agent 按查询条件挑选。这套实现小而全，但也存在明显的天花板：

- skill 只有静态元数据 + 单文本 body，没有结构化资源层。
- 路由（匹配）靠硬编码权重词法匹配，对中文和语义搜索支持差。
- 没有执行层：skill 只是"提示词包"，无法绑定脚本或限定可用工具。
- 工程化能力不足：单例全局状态、错误吞掉、无热加载、无版本与依赖模型。

Jarvis 下一步的演进目标是把 skill 从"提示词发现"升级为"能力编排"，让 skill 既能作为知识卡片，也能作为受约束的、可执行的能力单元。因此需要一份明确的加载与编排设计文档。

---

## 2. 设计目标

### 2.1 核心目标

1. **保留现有兼容**：`manifest.yaml` / `SKILL.md` frontmatter 继续可用。
2. **引入资源层**：skill 目录里的 `scripts/`、`references/`、`templates/` 等能被结构化引用。
3. **引入执行层**：支持工具白名单（`allowed-tools`）和可选的脚本/代码片段绑定。
4. **提升路由精度**：补充 embedding 召回 + LLM 重排，改善中文语义匹配。
5. **改善工程化**：去全局单例、支持热加载/失效、明确错误传播。

### 2.2 非目标

- 第一阶段不引入远程 skill registry / marketplace。
- 第一阶段不做沙箱执行引擎（仅做工具白名单声明）。
- 不改动现有 `SkillRegistry` 对外查询接口（内部可重构）。

---

## 3. 现状分析

### 3.1 代码结构

```text
app/skills/
  __init__.py        # 公共导出
  bootstrap.py       # 全局单例初始化，builtin + external 加载
  loader.py          # SkillPackageLoader：扫描、解析 manifest / SKILL.md frontmatter
  manifest.py        # SkillManifest Pydantic 模型：name/description/version/capabilities
  registry.py        # SkillRegistry：内存索引 + select_for_query 词法打分
  skill.py           # Skill 冻结 dataclass：name/description/path/manifest/content_path
```

### 3.2 数据流

```text
磁盘目录 (manifest.yaml / SKILL.md)
  -> SkillPackageLoader.discover()   # 扫描 search_paths 的直接子目录
  -> SkillPackageLoader.load_package() # 解析 manifest 为 SkillManifest
  -> Skill(name, ..., manifest, content_path) # 组装 Skill 对象
  -> bootstrap_registries()          # 去重 + 注册进全局单例 SkillRegistry
  -> SkillRegistry.select_for_query() # 按词法打分返回 Top-3
```

### 3.3 当前问题清单

| 问题 | 位置 | 说明 |
|------|------|------|
| Manifest 字段过薄 | `manifest.py:6` | 缺少 `allowed-tools`、`resources`、`inputs/outputs` schema、tags、author |
| 版本号未使用 | `manifest.py:9` | `version` 仅存储，无校验、无冲突仲裁、无兼容判断 |
| 无结构化资源层 | `skill.py:15` | 仅 `content_path: Path | None`，对目录内其他文件零感知 |
| body 未剥离 frontmatter | `skill.py:20` | 直接返回整个 SKILL.md，元数据会污染 LLM prompt |
| 选择算法幼稚 | `registry.py:25` | 硬编码权重（8/5/1），分词器不支持中文；无 embedding/LLM 路由 |
| 发现仅一层 | `loader.py:56` | 只遍历搜索根的直接子目录，不支持嵌套命名空间或 glob |
| 异常吞掉 | `loader.py:47` | `except Exception` 打 warning 即跳过，调试困难 |
| 全局单例 | `bootstrap.py:20` | `_registries` 模块级单例，测试需 reset，无热加载 |
| 无依赖模型 | 全局 | skill 无法声明对其他 skill / Python 包 / 模型能力的依赖 |
| 路径穿越未防护 | `loader.py:37` | `JARVIS_SKILL_PATH` 目录未做路径校验 |
| 无可信校验 | 全局 | 无签名、无校验和，与 npm/PyPI 生态差距明显 |

---

## 4. 业界最新进展与趋势

### 4.1 Anthropic Agent Skills（SKILL.md 生态）

Anthropic 及其社区在推动的 Agent Skills 模式已成为事实上的参考标准之一：

- **协议约定**：目录即 skill，`SKILL.md` 可含 YAML frontmatter + Markdown body。
- **渐进式披露（Progressive Disclosure）**：
  - L1：仅 name/description + capabilities 进入 context（轻量索引）。
  - L2：Agent 判定命中后，读取 SKILL.md body（知识注入）。
  - L3：执行阶段按需读取 `scripts/`、`references/`、`templates/` 等捆绑资源（行为编排）。
- **工具约束**：skill 通过 `allowed-tools` 字段白名单化可调工具，防止越权。
- **元数据丰富**：包含 author、license、tags、inputs schema、outputs schema，支持 marketplace 检索与合规审查。

### 4.2 Semantic Kernel（Microsoft）

- **Skill = 语义函数 + 原生函数**：
  - `semantic function`：prompt template + JSON schema input/output。
  - `native function`：Python/C# 代码直接作为 skill 的一部分，可被 planner 调用。
- **Planner 编排**：LLM 自动编排多个 skill 的调用链，skill 之间可显式声明依赖。
- **Kernel 作为执行上下文**：skill 不持有全局状态，运行时由 Kernel 注入配置、记忆、工具。

### 4.3 LangChain / LangGraph Tool 生态

- **Tool 与 Skill 的边界在模糊化**：LangChain 的 `@tool` 装饰器允许把任何函数注册为工具，而 skill 包则是"工具+提示词+配置"的集合。
- **结构化 I/O**：通过 Pydantic model 严格定义 tool schema，让 LLM 在调用前知道参数结构。
- **Agent 路由**：使用 LLM 本身做工具选择（function calling），而不是前置的关键词匹配。

### 4.4 MCP（Model Context Protocol）

- **context 可组合**：resources + prompts + tools 三类能力标准化暴露。
- **客户端-服务器模型**：skill（或 capability）可作为独立 MCP Server 注册，跨进程/跨语言复用。
- **增量发现**：client 按需拉取能力列表，而非启动时全量加载，降低 token 开销。

### 4.5 对 Jarvis 的启示

| 趋势 | Jarvis 现状 | 建议优先级 |
|------|-------------|-----------|
| 渐进式资源披露（L1/L2/L3） | 只有 L1/L2，无资源层 | **高** |
| 工具白名单（allowed-tools） | 无 | **高** |
| 语义/向量路由 | 词法匹配，中文差 | **高** |
| 结构化 I/O schema | 无 | 中 |
| 依赖与版本模型 | 无 | 中 |
| 热加载 / 失效机制 | 单例，无热加载 | 中 |
| 签名与完整性校验 | 无 | 低（第一阶段可不做） |
| MCP 桥接 | 无 | 低（远期） |

---

## 5. 改造方案

### 5.1 元数据模型扩展（manifest 层）

#### 5.1.1 新增字段

```yaml
# manifest.yaml 示例（向后兼容）
name: code-reviewer
version: "1.2.0"          # 启用 semver 校验
description: "Code review assistant"
author: "Jarvis Team"
license: "MIT"
tags: ["coding", "review", "python"]

# 工具白名单（最关键新增）
allowed-tools:
  - read_file
  - grep_code
  - run_linter

# 结构化输入/输出（可选，第一阶段可留空）
inputs:
  - name: code_snippet
    type: string
    required: true
outputs:
  - name: review_comment
    type: string

# 资源声明（L3）
resources:
  - type: script
    path: scripts/review_prompt.py
  - type: template
    path: templates/review_format.md
  - type: reference
    path: references/google_style_guide.md
```

#### 5.1.2 Pydantic 模型更新

- `SkillManifest` 新增 `allowed_tools: list[str]`、`resources: list[ResourceRef]`、`inputs`/`outputs`、`tags`、`author`、`license`。
- `version` 字段接入 `packaging.version.parse` 做 semver 基础校验。
- 保留 `metadata:` 包裹兼容（现有 `@model_validator` 逻辑保留）。

### 5.2 资源层与渐进式披露（L3）

新增 `SkillResource` 概念：

```python
@dataclass(frozen=True)
class SkillResource:
    type: Literal["script", "template", "reference", "asset"]
    path: Path
    name: str

@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    manifest: SkillManifest
    content_path: Path | None = None
    resources: dict[str, SkillResource] = field(default_factory=dict)

    def load_body(self) -> str:
        # 若来源是 SKILL.md，应剥离 frontmatter 后再返回
        ...

    def get_resource(self, name: str) -> SkillResource:
        ...
```

- `loader.py` 在 `load_package` 时扫描 manifest 声明的 `resources`，校验文件存在性，组装进 `Skill.resources`。
- `Skill.load_body()` 对 `SKILL.md` 来源做 frontmatter 剥离，只返回 `---` 之后的正文。

### 5.3 执行层：工具白名单绑定

- `Skill` 新增 `allowed_tools: frozenset[str]`，从 `manifest.allowed_tools` 解析。
- Agent 执行 skill 时，将 `allowed_tools` 与全局 tool registry 取交集，作为本次调用的可用工具集。
- 若 skill 未声明 `allowed_tools`，回退到全局默认（保持兼容）。

### 5.4 路由层：语义召回 + 重排

当前 `select_for_query` 保留作为**粗排兜底**，但新增两阶段路由：

```text
select_for_query(text)
  -> 阶段 A：显式命令匹配（/skillname）不变
  -> 阶段 B：向量召回（新增）
       - 启动时为所有 skill 的 name + description + tags + capabilities 生成 embedding
       - 查询时做向量相似度召回 Top-K（默认 K=10）
  -> 阶段 C：LLM 重排（可选，新增）
       - 把 Top-K skill 摘要喂给轻量 LLM / classifier，做相关性打分，最终取 limit
  -> 阶段 D：粗排兜底（保留现有词法匹配）
       - 若向量服务不可用，回退到现有 select_for_query 逻辑
```

- 分词器 `_tokens` 增加 Unicode/中文支持（可用 `jieba` 或按 Unicode 字符边界拆分）。
- embedding 层解耦：接口化 `SkillRetriever`，允许替换为 local embedding / 远程 embedding。

### 5.5 工程化改进

#### 5.5.1 去掉全局单例，改为显式生命周期

```python
class SkillManager:
    def __init__(self, loader: SkillPackageLoader, retriever: SkillRetriever | None = None) -> None:
        self._registry: SkillRegistry | None = None
        self._loader = loader
        self._retriever = retriever

    def load(self) -> SkillRegistry:
        ...

    def reload(self) -> SkillRegistry:
        ...

    def get_registry(self) -> SkillRegistry:
        if self._registry is None:
            self.load()
        return self._registry
```

- `bootstrap.py` 的 `_registries` 单例改为 FastAPI dependency 注入的 `SkillManager` 实例（或应用上下文持有者）。
- 测试无需 `reset_registries_for_tests()`，直接 new manager 即可。

#### 5.5.2 错误传播与可观测性

- `SkillPackageLoader.load()` 不再 `except Exception`，改为捕获特定异常（`ValidationError`、`ValueError`、`OSError`）并封装为 `SkillLoadError`。
- 增加 `load_report`：返回加载成功列表 + 失败列表（含路径与原因），便于启动日志与监控。

#### 5.5.3 搜索发现增强

- `discover()` 支持递归子目录（可选，通过 `max_depth` 控制）。
- 支持 `.jarvisskill` 或类似标记文件，允许在一个目录内聚合多个 skill（类似 Python namespace package）。

#### 5.5.4 路径安全

- `JARVIS_SKILL_PATH` 或外部路径在加载前做 `Path.resolve()`，并校验结果路径不以 `..` 跳出预期根目录。

### 5.6 版本与依赖（第二阶段）

- `SkillManifest` 预留 `dependencies` 字段，可声明对其他 skill 或 Python 包的依赖。
- `SkillRegistry` 在加载阶段做拓扑排序，确保依赖先加载。
- 版本冲突策略：先记录 warning，长期引入 semver 兼容判断。

---

## 6. 迁移计划

### Phase 1：元数据与资源层（1-2 周）

1. 扩展 `SkillManifest` 字段（`allowed_tools`、`resources`、`tags`）。
2. 更新 `Skill` 与 `loader`，支持资源扫描与 frontmatter 剥离。
3. 保持 `bootstrap.py` / `registry.py` 接口不变，新增字段仅透传。

### Phase 2：路由升级（2 周）

1. 引入 `SkillRetriever` 抽象接口与向量召回实现（默认回退现有粗排）。
2. 优化分词器支持中文。
3. 在 Agent 调用链路中接入 skill 选择结果，验证效果。

### Phase 3：执行绑定与工程化（2-3 周）

1. Agent 执行 skill 时读取 `allowed_tools`，与工具集取交集。
2. 重构 `bootstrap.py` 单例为 `SkillManager`，接入依赖注入。
3. 错误报告、路径安全、热加载支持。

### Phase 4：远期（未定）

1. 依赖拓扑与版本仲裁。
2. 签名与完整性校验。
3. MCP Server 桥接：让 Jarvis skill 可被外部 MCP client 消费。

---

## 7. 核心接口草案

```python
# manifest.py
class SkillManifest(BaseModel):
    name: str
    description: str = ""
    version: str | None = None
    author: str | None = None
    license: str | None = None
    tags: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list, alias="allowed-tools")
    resources: list[ResourceSpec] = Field(default_factory=list)
    inputs: list[FieldSpec] = Field(default_factory=list)
    outputs: list[FieldSpec] = Field(default_factory=list)

# skill.py
@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    manifest: SkillManifest
    content_path: Path | None = None
    resources: Mapping[str, SkillResource] = field(default_factory=dict)

    def load_body(self) -> str: ...
    def list_resources(self) -> list[SkillResource]: ...
    def get_allowed_tools(self, fallback: frozenset[str] | None = None) -> frozenset[str]: ...

# registry.py
class SkillRegistry:
    def __init__(self, skills: list[Skill]) -> None: ...
    def get(self, name: str) -> Skill: ...
    def list(self) -> list[Skill]: ...
    def select(self, text: str, *, limit: int = 3) -> list[Skill]: ...  # 整合向量+重排+粗排

# loader.py
class SkillPackageLoader:
    def discover(self, recursive: bool = False) -> list[Path]: ...
    def load(self) -> list[LoadedSkillPackage]: ...
    def load_report(self) -> LoadReport: ...
```

---

## 8. 风险与注意事项

| 风险 | 缓解措施 |
|------|----------|
| 向量召回增加启动/内存开销 | embedding 延迟初始化，允许关闭（fallback 粗排） |
| `allowed-tools` 遗漏导致 skill 失效 | 未声明时回退到全局工具集，不 breaking |
| 资源层引入后目录结构变重 | 所有资源声明可选，零资源 skill 与当前行为一致 |
| 单例重构影响现有测试 | 保留 `get_skill_registry()` 兼容层，内部代理到 SkillManager |
| frontmatter 剥离导致老 skill 输出变化 | 仅对 `SKILL.md` 来源剥离；`manifest.yaml + 独立 body.md` 不受影响 |

---

## 9. 结论

当前 Jarvis skill 系统是一个**最小可用的知识型加载器**，在协议兼容、搜索路径、schema 校验上已达标，但在**资源层、执行约束、语义路由、工程化**四个维度与业界主流差距明显。

建议按**"元数据扩展 → 语义路由 → 执行绑定 → 工程化重构"**的顺序分阶段推进，优先落地 `allowed-tools` 与资源目录结构化引用，这是把 skill 从"静态提示词"升级为"可编排能力单元"的最小有效改动。