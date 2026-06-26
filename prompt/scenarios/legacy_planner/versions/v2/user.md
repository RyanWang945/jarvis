指令：
{{ instruction }}

不使用 function calls 时，返回符合此 schema 的 JSON：{"confidence": 0.0-1.0, "needs_clarification": boolean, "clarification_question": string|null, "tasks": [{"tool_name": string, "tool_args": object, "title": string, "description": string, "dod": string}]}
