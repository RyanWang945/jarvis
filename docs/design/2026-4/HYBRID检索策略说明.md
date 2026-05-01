# Hybrid 检索策略说明

本文档说明当前 Wiki 知识库的 Hybrid 检索实现、最新评测现状，以及下一步的优化方向。

## 1. 背景

当前知识库检索支持三种模式：

- `bm25`
- `vector`
- `hybrid`

其中：

- `bm25` 负责词面匹配
- `vector` 负责语义匹配
- `hybrid` 负责把两路结果融合，期望同时获得更高召回和更好的排序质量

当前实现代码主要在：

- [app/knowledge_base/search.py](E:/pythonProject/jarvis/app/knowledge_base/search.py)
- [app/knowledge_base/indexing.py](E:/pythonProject/jarvis/app/knowledge_base/indexing.py)
- [app/knowledge_base/eval.py](E:/pythonProject/jarvis/app/knowledge_base/eval.py)

## 2. 当前实现

### 2.1 BM25 检索

当前 BM25 查询由以下几部分组成：

- `multi_match`：字段 `title^2`, `content`
- `term`：`title.raw` 精确匹配，`boost=8`
- `wildcard`：`title.raw` 模糊包含，`boost=6`
- `wildcard`：`content.raw` 模糊包含，`boost=3`

这意味着 BM25 既在做常规全文匹配，也在做较强的标题精确匹配和模糊包含匹配。

### 2.2 Vector 检索

当前向量检索直接对 `embedding` 字段做 KNN 查询：

- 使用同一 embedding 模型：`text-embedding-v4`
- 查询时返回 `top_k`

### 2.3 Hybrid 融合

当前 Hybrid 的融合逻辑是“最大值归一化 + 线性加权”。

实现方式如下：

1. 分别拿到 BM25 和 Vector 的结果
2. 对两路结果按 `chunk_id` 做并集
3. 计算：
   - `bm25_norm = bm25_score / bm25_max`
   - `vector_norm = vector_score / vector_max`
4. 最终分数：
   - `final_score = 0.45 * bm25_norm + 0.55 * vector_norm`
5. 按 `final_score` 排序，返回最终 `top_k`

当前默认权重：

- `alpha = 0.45`
- `beta = 0.55`

代码位置：

- [app/knowledge_base/search.py](E:/pythonProject/jarvis/app/knowledge_base/search.py:237)

## 3. 当前评测现状

### 3.1 评测数据

本轮评测使用：

- `source_id`: `wikipedia_zh_real_300`
- 文档规模：`3000`
- 索引：`kb_wikipedia_zh_medium_overlap_v1`
- query 数据集：`kb_eval_dataset_7523e419-b827-4db9-baf7-fdc9c2242866`
- query 数量：`200`
- query 生成模型：`deepseek-v4-flash`

这批 query 相比之前更接近真实用户表达，包含：

- 别名
- 简称
- 口语化表达
- 部分线索式问法
- 任务式问法

### 3.2 评测结果

`top_k = 5`

| 模式 | Recall@5 | Precision@5 | MRR | nDCG | Chunk Hit Rate | Boundary Spill Rate | Avg Latency | P95 Latency |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `bm25` | 0.515 | 0.103 | 0.4538 | 0.4693 | 0.515 | 0.005 | 144 ms | 151 ms |
| `vector` | 0.855 | 0.171 | 0.7283 | 0.7603 | 0.855 | 0.005 | 12 ms | 13 ms |
| `hybrid` | 0.855 | 0.171 | 0.6988 | 0.7384 | 0.855 | 0.005 | 149 ms | 153 ms |

### 3.3 结果解读

当前结果说明：

- `vector` 明显优于 `bm25`
- `hybrid` 在召回上没有超过 `vector`
- `hybrid` 在 `MRR` 和 `nDCG` 上反而低于 `vector`

也就是说，当前 Hybrid 没有带来额外召回收益，反而把原本 vector 路线已经排得比较好的结果拉差了。

## 4. 当前 Hybrid 效果不理想的原因

### 4.1 融合公式过于粗糙

当前使用的是“按本次结果最大值归一化后线性加权”。

这个方法的问题是：

- BM25 分数和向量分数不是同一量纲
- 只按本次 `top_k` 的最大值归一化，稳定性较差
- 少量异常高分的 BM25 结果会对最终排序产生过大影响

结果就是：

- 即使 Vector 已经找到了正确 chunk
- 只要 BM25 给了几个不太相关但词面分高的结果
- Hybrid 就可能把正确结果往后压

### 4.2 BM25 在真实 query 上偏弱

这批 200 条 query 更接近真实用户表达，包含大量：

- 非标题式表达
- 同义改写
- 口语化表达
- 部分记忆线索

这类 query 更偏语义匹配，对 BM25 不友好。

从结果看：

- `bm25 Recall@5 = 0.515`
- `vector Recall@5 = 0.855`

说明在当前 query 分布下，BM25 更像一个噪声源，而不是一个强补充源。

### 4.3 当前 BM25 查询容易引入噪声

当前 BM25 中的这两个条件风险较大：

- `wildcard title.raw`
- `wildcard content.raw`

问题在于：

- 对短 query、别名 query、口语 query，很容易命中表面包含但语义不准的文本
- 这些噪声结果一旦进入 Hybrid 融合，会干扰 vector 的好排序

### 4.4 当前 Hybrid 召回池太小

当前实现是：

- BM25 取 `top_k`
- Vector 取 `top_k`
- 然后直接融合

这意味着：

- 两边候选池都太浅
- Hybrid 的“补召回”能力非常有限
- 更像是在对两个很小候选集合重新洗牌

所以它更容易伤排序，而不是带来真正的增益。

## 5. 当前策略总结

当前 Hybrid 的实际行为可以概括为：

- 有 Hybrid 形式
- 但没有真正发挥“多路召回 + 稳定融合”的作用

它目前更接近：

- `vector` 结果为主
- `bm25` 结果做简单打扰

而不是：

- `bm25` 与 `vector` 各自提供独立价值
- 再通过可靠融合得到更优排序

## 6. 优化策略

### 6.1 第一优先级：改融合算法

建议优先从“分数融合”切到“排序融合”。

首选方案：

- `RRF`（Reciprocal Rank Fusion）

原因：

- 不依赖不同分数体系的绝对值可比性
- 对 BM25 / Vector 两路信号更稳
- 通常比“max-normalize + weighted sum”更适合异构召回融合

建议做法：

1. BM25 和 Vector 各自独立召回
2. 按排名而不是分数做融合
3. 最终输出统一排序

### 6.2 第二优先级：减弱 BM25 噪声

建议逐步收紧 BM25 查询：

- 降低或移除 `content.raw` 的 `wildcard`
- 评估是否保留 `title.raw` 的 `wildcard`
- 保留更可信的 `multi_match`
- 继续保留标题精确匹配，但避免过强放大噪声

目标不是让 BM25 更激进，而是让 BM25 成为更可信的补充信号。

### 6.3 第三优先级：提高 Vector 权重

如果暂时不改融合算法，至少应先调低 BM25 权重。

可优先尝试：

- `alpha=0.20, beta=0.80`
- `alpha=0.10, beta=0.90`

当前 query 分布已经证明：

- 语义匹配能力明显更强
- 因此 Hybrid 不应让 BM25 贡献过高权重

### 6.4 第四优先级：扩大候选池

建议把 Hybrid 改成“两路大召回，小结果融合”：

- BM25 先召回 `20-50`
- Vector 先召回 `20-50`
- 融合后再截断到最终 `top_k=5`

这样才能让 Hybrid 真正有机会：

- 用 BM25 补 Vector 没召回到的结果
- 用 Vector 修正 BM25 的词面偏差

### 6.5 第五优先级：分 query 类型评测

后续不应只看总分，还要按 query 类型拆开看：

- 标题/近标题 query
- 别名 query
- 口语化 query
- 描述型 query
- 事实型 query

因为很可能出现这种情况：

- BM25 在标题型 query 上有价值
- Vector 在口语化和描述型 query 上更强

这类分桶评测会直接决定：

- Hybrid 是否真的值得保留
- 以及应该怎样融合

## 7. 建议的落地顺序

建议按以下顺序推进：

1. 保持当前数据集不变，先复现实验基线
2. 把 Hybrid 融合改成 `RRF`
3. 用同一批 200 条 query 复跑
4. 再做一版“弱化 wildcard 的 BM25”
5. 再复跑对比
6. 最后再看是否还需要保留线性权重融合版本

## 8. 当前结论

当前结论可以明确写成：

- 当前 `hybrid` 实现没有优于 `vector`
- 问题不在于“Hybrid 思路本身错误”
- 主要在于当前融合实现过于粗糙，BM25 支路噪声偏高
- 下一步应优先优化融合算法，而不是继续增加固定权重的分数混合复杂度

在当前这批更贴近真实世界的 200 条 query 上：

- `vector` 是当前最强基线
- `hybrid` 仍有优化价值
- 但必须先升级融合策略，才有机会真正超过 `vector`
