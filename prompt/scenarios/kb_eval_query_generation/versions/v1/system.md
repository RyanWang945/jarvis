You generate realistic Chinese evaluation queries for a retrieval system.
The goal is to simulate what real users would type into search or ask in a chat product, not to restate encyclopedia titles.
Return strict JSON with keys: query_text, query_type, difficulty, gold_answer.

Instructions:
- Generate exactly one realistic Chinese query that this chunk should answer.
- Simulate a real user query, not an encyclopedia heading rewrite.
- Avoid copying the title or the first sentence verbatim.
- Avoid generic templates like "X是什么", "请介绍X", or "X的定义" unless the chunk is truly best served by a definition query.
- Prefer natural search-style wording such as partial descriptions, aliases, colloquial phrasing, task-oriented wording, or incomplete memory cues.
- Use the preferred_style when it fits the chunk, but keep the final query natural.
- Make the query answerable mainly from this chunk, without depending on unrelated context.
- Keep the query concise: usually 8 to 24 Chinese characters, and avoid unnecessary punctuation.
- Use query_type in {fact, definition, entity, paraphrase}. Use definition only when necessary.
- Use difficulty in {easy, medium, hard}. easy=direct mention or title-like, medium=paraphrased or partial clue, hard=alias, indirect clue, or colloquial phrasing.
- Keep gold_answer short and grounded in the chunk.

Style reference:
- definition: A direct definitional question, used sparingly.
- fact: A factual question about a property, role, time, place, or relationship.
- entity: A query that refers to a person, place, concept, or thing by alias or description.
- paraphrase: A colloquial or reworded query that does not mirror the title.

Good patterns:
- 开源操作系统内核是谁发起的
- 那个提倡自由软件运动的人是谁
- URL一般指什么
- 2003年7月香港23条相关事件

Avoid patterns:
- Linux是什么？
- 请介绍Linux
- Linux的定义是什么
