# Wiki Evidence Role Evaluation

- Dataset ID: `all`
- Query count: `263`
- Retrieval mode: `rrf_v2`
- Top K: `5`
- Candidate: BM25 `20` + Vector `20`
- Avg latency: `166 ms`
- P95 latency: `193 ms`

| Evidence role | Eligible | Recall@K | Recall@K eligible | Chunk hit | Doc hit | Precision@K | MRR | nDCG |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `legacy_chunk` | 263/263 | 0.8783 | 0.8783 | 0.8669 | 0.8973 | 0.1757 | 0.7464 | 0.7796 |
| `answer` | 254/263 | 0.8365 | 0.8661 | 0.8669 | 0.8669 | 0.1673 | 0.7012 | 0.7353 |
| `any` | 263/263 | 0.8783 | 0.8783 | 0.8669 | 0.8973 | 0.1757 | 0.7464 | 0.7796 |
