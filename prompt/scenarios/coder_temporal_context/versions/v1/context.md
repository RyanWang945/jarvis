{{#has_temporal}}
Temporal context:
{{#current_date}}
- Current date: {{ current_date }}
{{/current_date}}
{{#current_time}}
- Current time: {{ current_time }}
{{/current_time}}
{{#timezone}}
- Timezone: {{ timezone }}
{{/timezone}}
- Interpret today/current/latest/recent and 今天/当前/最新/最近 relative to this context.
{{/has_temporal}}
{{^has_temporal}}
Temporal context: unavailable; do not infer current dates from model memory.
{{/has_temporal}}
