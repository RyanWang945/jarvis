# Pydantic 学习笔记

本项目大量使用了 Pydantic v2，主要包括两个场景：
1. **配置管理**：通过 `pydantic-settings` 的 `BaseSettings` 管理环境变量和 `.env` 配置
2. **接口校验**：通过 `BaseModel` 定义 FastAPI 的请求体（Request）和响应体（Response）

---

## 一、配置管理：BaseSettings

### 1. 基本写法

参考代码：`app/config.py`

```python
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from pathlib import Path

class Settings(BaseSettings):
    app_name: str = "Jarvis"
    port: int = 8000
    log_dir: Path = Field(default=Path("logs"))
    dashscope_api_key: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="JARVIS_",
        extra="ignore",
    )
```

**关键点：**
- 继承 `BaseSettings`，它会自动从环境变量中读取字段值
- `env_prefix="JARVIS_"` 表示环境变量名需要加前缀，例如 `JARVIS_PORT=9000`
- `env_file=".env"` 表示会自动加载项目根目录下的 `.env` 文件
- `extra="ignore"` 表示忽略未在类中定义的环境变量，避免报错
- 字段可以给默认值（如 `app_name: str = "Jarvis"`），也可以不给（此时必须在环境变量中提供）
- 用 `str | None` 表示可选字段，不配置时默认为 `None`

### 2. 使用方式

```python
from functools import lru_cache

@lru_cache
def get_settings() -> Settings:
    return Settings()

# 调用
settings = get_settings()
print(settings.port)  # 8000 或环境变量覆盖的值
```

`@lru_cache` 的作用是让 `Settings` 只实例化一次，避免重复读取 `.env` 文件。

### 3. Field 在配置中的用法

当默认值比较特殊（如 `Path` 对象），或者需要额外描述时，使用 `Field`：

```python
log_dir: Path = Field(default=Path("logs"))
```

注意：在 `BaseSettings` 中，`Field` 的用法和 `BaseModel` 基本一致，但校验逻辑更偏向"读取并转换环境变量值"。

---

## 二、接口校验：BaseModel

### 1. 定义请求体和响应体

参考代码：`app/knowledge_base/api.py`

```python
from pydantic import BaseModel, Field

class KnowledgeBaseIngestRequest(BaseModel):
    file_path: str = Field(min_length=1)
    source_id: str | None = None
    language: str | None = None
    limit_n: int | None = Field(default=None, ge=1)
    chunk_profile_id: str | None = None
```

**关键点：**
- `BaseModel` 是 Pydantic 的核心，用于定义数据结构并自动做类型校验
- `Field(...)` 可以给字段增加约束条件，例如 `min_length=1` 表示字符串不能为空
- `ge=1` 表示大于等于 1（greater than or equal）
- `str | None` 表示该字段可选，不传时默认为 `None`

### 2. 常用 Field 约束

```python
class KnowledgeBaseSearchRequest(BaseModel):
    query: str = Field(min_length=1)           # 必填，且长度 >= 1
    mode: str = Field(pattern="^(bm25|vector|hybrid)$")  # 正则匹配
    top_k: int = Field(default=5, ge=1, le=50) # 默认 5，范围 1~50
```

| 参数 | 含义 | 示例 |
|---|---|---|
| `min_length` | 最小长度 | `Field(min_length=1)` |
| `pattern` | 正则匹配 | `Field(pattern="^(bm25|vector|hybrid)$")` |
| `ge` | >= | `Field(ge=1)` |
| `le` | <= | `Field(le=50)` |
| `gt` | > | `Field(gt=0)` |
| `default` | 默认值 | `Field(default=5)` |

### 3. 嵌套模型

响应体中经常嵌套其他模型：

```python
class KnowledgeBaseSearchHitResponse(BaseModel):
    chunk_id: str
    doc_id: str
    score: float
    source: dict

class KnowledgeBaseSearchResponse(BaseModel):
    hits: list[KnowledgeBaseSearchHitResponse]
```

FastAPI 会自动将嵌套模型转换为正确的 JSON 结构。

---

## 三、类型注解速查

本项目使用的 Python 类型注解风格：

| 写法 | 含义 | 示例字段 |
|---|---|---|
| `str` | 字符串 | `app_name: str` |
| `int` | 整数 | `port: int` |
| `float` | 浮点数 | `llm_timeout_seconds: float` |
| `bool` | 布尔值 | `auto_recover_on_startup: bool` |
| `Path` | 路径对象 | `data_dir: Path` |
| `str \| None` | 可选字符串 | `source_id: str \| None = None` |
| `list[str]` | 字符串列表 | `file_names: list[str] \| None = None` |
| `dict` | 字典 | `source: dict` |

注意：`str | None` 是 Python 3.10+ 的写法，等价于 `Optional[str]`。

---

## 四、配置与接口的对比

| 特性 | BaseSettings（配置） | BaseModel（接口） |
|---|---|---|
| 继承自 | `pydantic_settings.BaseSettings` | `pydantic.BaseModel` |
| 主要用途 | 读取环境变量 / `.env` | 校验 HTTP 请求/响应 |
| 自动读取环境变量 | 是 | 否 |
| 常用额外配置 | `SettingsConfigDict` | `Field` 约束 |
| 典型文件 | `app/config.py` | `app/knowledge_base/api.py` |

---

## 五、一个完整的字段演进示例

以 `source_id` 字段为例，看它在不同地方的写法：

**在配置中（可选，无校验）：**
```python
# app/config.py 中没有 source_id，但在其他地方：
source_id: str | None = None
```

**在请求体中（可选，有校验）：**
```python
# app/knowledge_base/api.py
class KnowledgeBaseIngestRequest(BaseModel):
    source_id: str | None = None   # 可选，无额外约束
```

**在请求体中（必填）：**
```python
class KnowledgeBaseIndexRequest(BaseModel):
    source_id: str = Field(min_length=1)  # 必填，且不能为空字符串
```

---

## 六、注意事项

1. **Pydantic v1 vs v2**：本项目使用的是 Pydantic v2，导入路径是 `from pydantic import BaseModel, Field`，`from pydantic_settings import BaseSettings`。如果是 v1，`BaseSettings` 是在 `pydantic` 包里的，不需要单独安装 `pydantic-settings`。

2. **默认值与 Field**：当需要给字段加约束（如 `ge=1`）时，即使字段可选，也必须包一层 `Field(default=None, ge=1)`，不能直接写 `limit_n: int | None = None, ge=1`。

3. **Path 类型**：`Path` 字段在 `BaseSettings` 中会自动将字符串转换为 `pathlib.Path` 对象，非常方便处理文件路径。
