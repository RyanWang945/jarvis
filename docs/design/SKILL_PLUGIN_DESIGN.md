# Skill 插件系统设计文档

**状态：** 已部分落地，继续硬化
**作者：** Claude（架构评审）
**日期：** 2026-04-20
**范围：** 外部 Skill 的发现、加载、注册与生命周期管理

---

## 0. 当前进展（2026-04-20）

已落地：

- `ToolSpec` 已增加 `worker_type`，`strategize` 直接使用 `tool.worker_type`，不再依赖 `_tool_to_worker_type()` 硬编码映射。
- 新增 `bootstrap_registries()`，统一加载内置 Skill/ToolSpec 与外部 Skill 包。
- `get_default_skill_registry()` / `get_default_tool_registry()` 仍保留为兼容入口，但已代理到 bootstrap 后的全局注册表。
- 新增外部 Skill loader，支持扫描：
  - `data/skills/`
  - `~/.jarvis/skills/`
  - `JARVIS_SKILL_PATH`
- 外部 Skill 包支持：
  - `manifest.yaml`
  - `SKILL.md` YAML frontmatter + `metadata.jarvis`
- 损坏的 Skill 包会 warning 后跳过，不影响内置能力和其他包。
- 已接入外部 Tavily search 包：`data/skills/openclaw-tavily-search-0.1.0/`，对 LLM 暴露 `tavily_search`。
- 内置 `web_search` 已移除，搜索能力统一由外部 `tavily_search` 提供。
- 已验证 DeepSeek planner 能从工具列表中选择 `tavily_search`，并完成真实 Tavily API 搜索。

与原草案的差异：

- 外部包不仅支持 `manifest.yaml`，也支持 Agent Skills 风格的 `SKILL.md`。
- 当前未引入热重载，新增/删除外部 Skill 仍需重启或强制重新 bootstrap。
- 当前外部 Skill 是本地 Python 动态 import，等价于信任本地代码执行，尚未实现沙箱。

当前暴露的问题：

- 外部 Skill/Tool 与内置名称冲突覆盖已修复：bootstrap 默认跳过重复注册包，注册表也拒绝重复名称。
- `verification_cmd` 风险绕过已修复：高危 verification command 已纳入 WorkOrder 风险计算并触发审批。
- 搜索结果进入 final answer synthesis 前已有基础结构化压缩和 prompt injection 防护。
- 搜索类 fallback 已具备 URL 抽取和摘要片段输出，但仍需要更稳定的摘要策略。

---

## 1. 背景与动机

Jarvis 目前内置了四个 Skill：`echo`、`shell`、`coder`、`web_search`。Skill 的注册链完全静态，并在四个层级中硬编码：

1. `app/skills/<name>.py` —— Skill 实现
2. `app/skills/registry.py` —— `SkillRegistry` 的实例化列表
3. `app/tools/registry.py` —— `ToolRegistry` 的实例化列表（`ToolSpec`）
4. `app/agent/nodes.py` —— `_tool_to_worker_type()` 硬编码映射函数

这意味着**如果不修改核心源码，就无法加载任何外部或用户自定义的 Skill**。当用户从外部仓库（如 clawhub）下载 Skill，或希望编写私有/内部 Skill 时，必须 Fork 代码库并维护自己的补丁。

本文档提出一种**插件式 Skill 系统**，以支持：
- 第三方 Skill 的即插即用安装
- 零核心代码改动的定制化能力
- `ToolSpec -> Skill` 绑定的单一事实来源

---

## 2. 用例

### UC-1：从外部仓库安装 Skill（例如 clawhub）
> Alice 从 clawhub 下载了一个 `database_migration` Skill 包，放到 `data/skills/database_migration/` 目录下。她重启 Jarvis 后，该 Skill 被自动发现，其工具自动暴露给 LLM Planner，并可以立即被调度执行。

**验收标准：**
- 无需修改 `app/**/*.py` 中的任何文件
- Skill 的工具自动出现在 LLM 系统提示词中
- Skill 遵循 Jarvis 的风险检查与审批工作流

---

### UC-2：在不 Fork 核心代码的前提下编写私有 Skill
> Bob 的团队需要一个能调用内部 REST API 来预配沙箱环境的 Skill。他编写了一个实现 `Skill` 协议的 Python 类，将其放在 `~/.jarvis/skills/sandbox_provisioner/` 下，然后重启 Jarvis。

**验收标准：**
- Skill 包中包含一个 Manifest，声明其名称、入口点和工具元数据
- Jarvis 在启动时加载该包
- Skill 在运行时接收与内置 Skill 相同的 `SkillRequest` / `SkillResult` 契约

---

### UC-3：在不引发停机问题的前提下移除或升级 Skill
> Carol 想移除一个有缺陷的第三方 Skill。她删除 `data/skills/` 下对应的文件夹，然后重启 Jarvis。该工具不再出现在 Planner 提示词中，且旧引用被优雅处理。

**验收标准：**
- 重启后注册表中不存在孤立条目
- 历史运行记录中引用该 Skill 的数据仍保持可读（只读审计）

---

### UC-4：Skill 权限沙箱
> Dave 安装了一个社区 Skill，该 Skill 需要文件系统和网络访问权限。Jarvis 在安装时发出警告，并可选地在具有受限能力的子进程中运行该 Skill。

**验收标准：**
- Manifest 声明所需的能力（`fs`、`net`、`shell`、`git`）
- 高风险能力在调度前触发管理员审批
- 可选的沙箱模式（未来阶段）隔离 Skill 进程

---

## 3. 设计目标

| # | 目标 | 优先级 |
|---|---|---|
| 1 | **插件发现** —— 启动时扫描指定目录，加载所有合法的 Skill 包 | 必须 |
| 2 | **自描述** —— 每个 Skill 自带 Manifest（名称、入口点、工具规格、权限） | 必须 |
| 3 | **统一注册** —— 单次启动流程同时注册 `Skill` 实例及其 `ToolSpec` | 必须 |
| 4 | **消除硬编码映射** —— 删除 `_tool_to_worker_type()`；`ToolSpec` 直接声明与 `Skill.name` 对齐的 `worker_type` | 必须 |
| 5 | **向后兼容** —— 迁移期间内置 Skill 完全保持原有行为 | 必须 |
| 6 | **隔离加载** —— 损坏的 Skill 包不会导致整个应用崩溃 | 应当 |
| 7 | **热重载** —— 无需重启进程即可增删 Skill（未来阶段） | 可以 |

---

## 4. 术语

| 术语 | 定义 |
|---|---|
| **Skill 包** | 包含 Skill 实现 + `manifest.yaml` 的目录或 Python 模块 |
| **Manifest** | YAML 文件，描述 Skill 的身份、入口点、工具和权限 |
| **内置 Skill** | 随 `app/skills/` 一起发布，通过静态导入加载的 Skill |
| **外部 Skill** | 运行时从 `data/skills/` 或 `~/.jarvis/skills/` 加载的 Skill |
| **能力（Capability）** | Skill 声明的权限令牌（`fs`、`net`、`shell`、`git`、`code_exec`） |

---

## 5. 架构设计

### 5.1 Skill 包结构

合法的外部 Skill 包必须遵循以下布局：

```
<skill_dir>/
├── manifest.yaml          # 必须。自描述文件
├── __init__.py            # 如果入口是包，则必须
└── skill.py               # 实现文件（名称灵活，由 manifest 引用）
```

示例：`data/skills/database_migration/`

```
data/skills/database_migration/
├── manifest.yaml
├── __init__.py
└── migration_skill.py
```

---

### 5.2 Manifest 模式

```yaml
manifest_version: "1.0"

skill:
  name: "database_migration"           # 必须与 Skill.name 一致
  version: "0.1.0"
  description: "针对 PostgreSQL 执行 SQL 迁移脚本。"
  author: "clawhub"
  entry: "migration_skill:MigrationSkill"  # 模块路径:类名

capabilities:
  - fs                                 # 需要文件系统访问
  - net                                # 需要访问数据库的网络连接
  - shell                              # 可能调用 pg_dump / psql

tools:
  - name: "run_migration"
    description: "将待处理的迁移文件应用到目标数据库。"
    args_schema:
      type: object
      properties:
        target:
          type: string
          description: "数据库连接字符串或别名。"
        dry_run:
          type: boolean
          default: true
      required: ["target"]
    action: "migrate"
    risk_level: "high"                  # high -> 触发审批门
    exposed_to_llm: true
```

**约束：**
- `skill.name` 必须在所有内置 + 外部 Skill 中全局唯一
- `tools[].name` 必须在所有已注册工具中全局唯一
- `tools[].action` 在运行时被传入 `SkillRequest.action`
- `tools[].risk_level` 在命令文本不可用做正则扫描时覆盖默认值

---

### 5.3 注册表重构

#### 5.3.1 启动时合并注册表

引入一个统一的 `bootstrap_registries()` 函数，在应用生命周期启动时调用一次。

```python
# app/bootstrap.py  (新文件)
from app.skills.registry import SkillRegistry
from app.tools.registry import ToolRegistry

def bootstrap_registries() -> tuple[SkillRegistry, ToolRegistry]:
    skills: list[Skill] = []
    tools: list[ToolSpec] = []

    # 阶段 1：加载内置 Skill（静态，保持向后兼容）
    skills.extend(_load_builtin_skills())
    tools.extend(_load_builtin_tools())   # ToolSpec 现在自带 worker_type

    # 阶段 2：加载外部包
    for pkg_dir in _discover_skill_packages():
        try:
            skill, tool_specs = _load_external_skill_package(pkg_dir)
        except SkillLoadError as exc:
            logger.warning("加载 Skill 包 %s 失败: %s", pkg_dir, exc)
            continue
        skills.append(skill)
        tools.extend(tool_specs)

    return SkillRegistry(skills), ToolRegistry(tools)
```

**关键变更：** `ToolSpec` 增加显式 `worker_type` 字段，默认与 `skill` 同值。`app/agent/nodes.py` 中的 `_tool_to_worker_type()` 函数被删除。

#### 5.3.2 ToolSpec 更新

```python
# app/tools/specs.py
class ToolSpec(BaseModel):
    name: str
    description: str
    args_schema: dict[str, Any] = Field(default_factory=dict)
    skill: str                      # 哪个 Skill 包拥有此工具
    action: str
    worker_type: str                # 新增: 直接映射到 Skill.name
    risk_level: RiskLevel = "low"
    exposed_to_llm: bool = False
```

在策略化阶段（`nodes.py`）：
```python
# 旧代码（待删除）:
# worker_type = _tool_to_worker_type(tool_name)

# 新代码:
worker_type = tool.worker_type
```

---

### 5.4 外部加载流程

```
发现路径
  ├─ data/skills/          (项目本地)
  ├─ ~/.jarvis/skills/     (用户全局)
  └─ (可选环境变量 JARVIS_SKILL_PATH)
       │
       ▼
对每个目录：
  ├─ 根据模式校验 manifest.yaml
  ├─ 导入 skill.entry 指定的模块
  ├─ 实例化 Skill 类
  ├─ 校验 skill.name == manifest.skill.name
  ├─ 从 manifest.tools 构建 ToolSpec 列表
  └─ 追加到注册表
```

**错误隔离：** `_load_external_skill_package()` 中捕获任何异常，记录日志并跳过。其他 Skill 继续加载。

---

## 6. 核心接口

### 6.1 Skill 协议（现有 —— 无需变更）

```python
class Skill(Protocol):
    name: str
    def run(self, request: SkillRequest) -> SkillResult: ...
```

### 6.2 新增：SkillPackageLoader

```python
from pathlib import Path
from dataclasses import dataclass

@dataclass
class LoadedSkillPackage:
    skill: Skill
    tools: list[ToolSpec]
    capabilities: list[str]

class SkillPackageLoader:
    def __init__(self, search_paths: list[Path]) -> None: ...

    def load_all(self) -> list[LoadedSkillPackage]: ...
```

---

## 7. 数据流

### 7.1 启动时发现

```
┌─────────────────┐
│   Jarvis 启动   │
└────────┬────────┘
         │
         ▼
┌──────────────────────────┐
│ bootstrap_registries()   │
│  ├─ _load_builtin_*()    │
│  └─ _load_external_*()   │
└────────┬─────────────────┘
         │
         ▼
┌──────────────────────────┐     ┌────────────────────┐
│   SkillRegistry          │◄────│ 外部包 + 内置      │
│   (name -> Skill)        │     │                    │
└────────┬─────────────────┘     └────────────────────┘
         │
         ▼
┌──────────────────────────┐
│   ToolRegistry           │
│   (name -> ToolSpec)     │
│   (exposed_to_llm=True)  │────► LLM Planner 提示词
└──────────────────────────┘
```

### 7.2 运行时调度

```
Planner 产出 ToolCallPlan
         │
         ▼
   ToolRegistry.get(tool_name)
         │
         ▼
   tool.worker_type  ──────────────► SkillRegistry.get(worker_type)
         │                                    │
         │                                    ▼
         │                           Skill.run(SkillRequest)
         │                                    │
         │                                    ▼
         │                              WorkResult
         │                                    │
         └────────────────────────────────────┘
```

删除 `_tool_to_worker_type()` 后，LLM 可见的工具名直接映射到 `ToolSpec`，再通过 `worker_type` 直接映射到 `Skill`。

---

## 8. 安全模型

### 8.1 能力声明

每个 Skill（无论内置或外部）在其 Manifest 或内联元数据中声明能力。内置 Skill 将 retrofit 默认能力集以保持对等性。

| 能力 | 含义 | 风险含义 |
|---|---|---|
| `fs` | 读写 `workdir` 之外的文件 | 中 |
| `net` | HTTP/TCP 出站调用 | 中 |
| `shell` | 执行任意 shell 命令 | 高 |
| `git` | Git commit / push / reset | 高 |
| `code_exec` | 运行解释型或编译型代码 | 高 |

### 8.2 调度时强制

在调度阶段（`nodes.py`），`ToolSpec.risk_level` 的用法与现在相同。此外，如果 Skill 声明的能力命中配置的黑名单，工单自动升级为 `critical` 并阻塞，等待管理员审批。

### 8.3 损坏包隔离

- Manifest YAML 格式错误 -> 跳过该包，启动继续
- Skill 模块导入错误 -> 跳过该包，启动继续
- `Skill.run()` 运行时异常 -> 被 `execute_work_order()` 捕获，返回 `SkillResult(ok=False, ...)`

### 8.4 未来：签名验证（阶段 3）

外部包可以在 Manifest 中包含 `signature` 块。加载时，Jarvis 根据可信密钥环验证包哈希/签名后再导入。

---

## 9. 向后兼容与迁移计划

### 阶段 1：重构内置 Skill（暂不引入外部加载）

1. 向 `ToolSpec` 添加 `worker_type` 字段
2. 更新所有内置 `ToolSpec` 定义，将 `worker_type` 设为与 `skill` 名称相同
3. 从 `app/agent/nodes.py` 中删除 `_tool_to_worker_type()`；改用 `tool.worker_type`
4. 验证所有现有测试通过

**影响：** 零行为变更；纯内部重构。

### 阶段 2：Manifest + 目录扫描

1. 添加 `SkillPackageLoader` 和 Manifest 解析器
2. 将 `data/skills/` 指定为默认外部 Skill 目录
3. 将一个内置 Skill（例如 `echo`）转换为规范 Manifest 示例，以验证模式
4. 添加 `bootstrap_registries()` 并从 `app.main` 的生命周期中调用
5. 提供 CLI 子命令 `jarvis-cli skill install <path>`，将包复制到 `data/skills/`

**影响：** 用户现在可以将 Skill 包放入 `data/skills/` 即插即用。

### 阶段 3：远程安装（可选）

1. 实现 `jarvis-cli skill install <git_url>` 或与 clawhub API 集成
2. 添加签名验证钩子
3. 为不可信 Skill 添加沙箱执行模式

---

## 10. 附录

### 10.1 示例：最小外部 Skill

`data/skills/uuid_generator/manifest.yaml`：
```yaml
manifest_version: "1.0"
skill:
  name: "uuid_generator"
  version: "1.0.0"
  description: "生成 v4 UUID。"
  entry: "uuid_skill:UuidSkill"
capabilities: []
tools:
  - name: "generate_uuid"
    description: "生成一个或多个 UUIDv4 字符串。"
    args_schema:
      type: object
      properties:
        count:
          type: integer
          minimum: 1
          maximum: 100
          default: 1
      required: []
    action: "generate"
    risk_level: "low"
    exposed_to_llm: true
```

`data/skills/uuid_generator/uuid_skill.py`：
```python
import uuid
from app.skills.base import SkillRequest, SkillResult

class UuidSkill:
    name = "uuid_generator"

    def run(self, request: SkillRequest) -> SkillResult:
        count = int(request.args.get("count", 1))
        uuids = [str(uuid.uuid4()) for _ in range(count)]
        text = "\n".join(uuids)
        return SkillResult(ok=True, exit_code=0, stdout=text, summary=f"生成了 {count} 个 UUID。")
```

### 10.2 目录搜索顺序

```python
# app/bootstrap.py
import os
from pathlib import Path

def _default_skill_search_paths() -> list[Path]:
    paths: list[Path] = []
    # 1. 项目本地
    paths.append(Path("data/skills").resolve())
    # 2. 用户全局
    home = Path.home()
    paths.append(home / ".jarvis" / "skills")
    # 3. 环境变量覆盖
    if env_path := os.getenv("JARVIS_SKILL_PATH"):
        paths.extend(Path(p.strip()) for p in env_path.split(os.pathsep) if p.strip())
    return [p for p in paths if p.exists() and p.is_dir()]
```

---

## 11. 待解决问题

1. **热重载 vs 重启：** 阶段 2 接受进程重启是否足够，还是我们需要一个 API 端点在运行时重载 Skill？
2. **依赖管理：** 外部 Skill 可能需要额外的 PyPI 包。Manifest 是否应支持 `dependencies` 列表，且 `jarvis-cli skill install` 自动运行 `pip install`？
3. **单 Skill 多工具：** 单个 Skill 包应暴露多个 `ToolSpec` 条目（如本文所提议）还是强制 1:1？Manifest 模式支持 N:1 以减少样板代码。
4. **版本与升级：** 如果 `data/skills/` 中包含同一 Skill 名称的两个版本，Jarvis 应快速失败还是加载最高 SemVer 版本？
