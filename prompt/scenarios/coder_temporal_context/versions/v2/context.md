{{#has_temporal}}
时间上下文：
{{#current_date}}
- 当前日期：{{ current_date }}
{{/current_date}}
{{#current_time}}
- 当前时间：{{ current_time }}
{{/current_time}}
{{#timezone}}
- 时区：{{ timezone }}
{{/timezone}}
- 解释“今天、当前、最新、最近、today、current、latest、recent”等相对时间时，必须以这里的时间上下文为准。
{{/has_temporal}}
{{^has_temporal}}
时间上下文：不可用；不要根据模型记忆推断当前日期。
{{/has_temporal}}
