# Wiki Evidence Role Evaluation

- Dataset ID: `kb_eval_dataset_7523e419-b827-4db9-baf7-fdc9c2242866`
- Query count: `200`
- Retrieval mode: `rrf_v2`
- Top K: `5`
- Candidate: BM25 `20` + Vector `20`
- Avg latency: `170 ms`
- P95 latency: `199 ms`

| Evidence role | Eligible | Recall@K | Recall@K eligible | Chunk hit | Doc hit | Precision@K | MRR | nDCG |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `legacy_chunk` | 200/200 | 0.8800 | 0.8800 | 0.8650 | 0.8950 | 0.1760 | 0.7436 | 0.7777 |
| `answer` | 194/200 | 0.8350 | 0.8608 | 0.8650 | 0.8650 | 0.1670 | 0.6973 | 0.7319 |
| `any` | 200/200 | 0.8800 | 0.8800 | 0.8650 | 0.8950 | 0.1760 | 0.7436 | 0.7777 |
