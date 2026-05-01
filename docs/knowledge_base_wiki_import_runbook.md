# Wiki 知识库续导固定操作说明

## 适用场景

当需要继续向现有知识库 source 导入更多 Wikipedia 文档时，使用项目现有导入逻辑进行续跑。

当前项目中已验证的 source：

- `source_id`: `wikipedia_zh_real_300`
- 数据文件：`E:\pythonProject\jarvis\data\wikipedia\wikipedia_20231101_zh_simp.jsonl`
- 语言：`zh`
- 分块策略：`medium_overlap_v1`

## 关键原则

必须继续使用现有导入方法，不要手工改库。

项目已有导入链路：

- [KnowledgeBaseService.ingest_wikipedia](E:/pythonProject/jarvis/app/knowledge_base/service.py)
- [WikipediaIngestService.ingest](E:/pythonProject/jarvis/app/knowledge_base/ingest.py)

现有去重/续跑逻辑：

- 按 `source_id + external_id` 判定是否已存在
- 已存在且 `text_hash` 未变化：`skip`
- 不存在：插入新文档并重新切 chunk

因此补导时应继续使用同一个 `source_id`，并把 `limit_n` 扩大到新的目标范围。

## 当前已验证的续导方式

对于已经导入前 `300` 篇的 `wikipedia_zh_real_300`，继续补导后续 `300` 篇时，直接将 `limit_n` 提升到 `600`：

- 前 `300` 条：自动跳过
- 后 `300` 条：自动新增

本次已验证结果：

- `documents_seen=600`
- `documents_inserted=300`
- `documents_skipped=300`
- `documents_updated=0`
- `chunks_created=1703`

补导完成后，`wikipedia_zh_real_300` 文档总数变为 `600`。

## 为什么需要提权执行

导入方法本身没有变化，仍然是项目现有方法。

之所以需要提权，是因为当前 Codex sandbox 环境下，SQLite 写事务会不稳定地报：

- `sqlite3.OperationalError: disk I/O error`

这个问题不只影响知识库导入，也影响最小 SQLite 写事务探针，因此根因不是导入代码，而是当前执行环境下的 SQLite 提交行为。

在提权执行后，同一条已有导入逻辑可以正常提交事务，因此导入成功。

## 固定执行命令

后续如果继续扩容这套 wiki source，优先复用下面这条命令，只调整 `limit_n`：

```powershell
@'
from app.config import get_settings
from app.knowledge_base.service import KnowledgeBaseService

settings = get_settings()
service = KnowledgeBaseService(settings)
result = service.ingest_wikipedia(
    file_path=r'E:\pythonProject\jarvis\data\wikipedia\wikipedia_20231101_zh_simp.jsonl',
    source_id='wikipedia_zh_real_300',
    language='zh',
    limit_n=600,
    chunk_profile_id='medium_overlap_v1',
)
print(result)
'@ | python -
```

执行要求：

- 在 Codex 中用提权方式执行
- 不要改 `source_id`
- 只改 `limit_n`

## 扩容示例

如果当前已经导入 `600` 篇，想继续补到 `900` 篇：

- 保持 `source_id='wikipedia_zh_real_300'`
- 将 `limit_n` 改为 `900`

预期行为：

- 前 `600` 条自动跳过
- 新增后续 `300` 条

## 导入后检查

建议至少检查：

1. 文档总数
2. 新增文档 ID 范围
3. 是否残留异常 `knowledge.db-journal`

可用只读检查示例：

```powershell
@'
import sqlite3
uri = 'file:E:/pythonProject/jarvis/data/knowledge.db?mode=ro&immutable=1'
conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
conn.row_factory = sqlite3.Row
row = conn.execute(
    "select count(*) as docs, min(cast(external_id as int)) as min_id, max(cast(external_id as int)) as max_id "
    "from kb_documents where source_id='wikipedia_zh_real_300'"
).fetchone()
print(dict(row))
conn.close()
'@ | python -
```

## 后续步骤

导入更多文档后，通常还需要继续执行：

1. 对新增文档补索引
2. 在更大语料规模上重跑评测
