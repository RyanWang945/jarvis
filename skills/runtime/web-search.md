# Runtime Skill: Web Search

Use this skill when a React node needs external web information.

## Core Principle

Do not blindly search the user's exact words.

Before searching, infer the user's information need:
- What is the user probably trying to know?
- Is the answer stable, recent, or fast-changing?
- What source type would be reliable?
- What source type would be misleading?
- What evidence must be present before the result can be used?

## Freshness Reasoning

Some information is fast-changing even when the user does not explicitly say "today", "latest", "current", or "now".

Examples:
- market prices
- gold price, oil price, stock price, crypto price
- exchange rates
- weather
- sports scores
- current events
- laws, policies, regulations
- product prices, availability, inventory
- software versions
- schedules, events, transport, visa rules

For fast-changing information:
- Use query terms like "live", "current", "today", "latest", or the concrete current date.
- Prefer source/data pages over old articles.
- Verify timestamp, trading date, publication date, update time, or other update context.
- Prefer results that contain concrete values, units, and timestamps.
- If freshness cannot be verified, return this as an uncertainty.

## Query Construction

For short or ambiguous user queries:
- Generate multiple query candidates.
- Use different phrasings.
- Prefer English queries for global market, finance, and technical information when likely to retrieve better sources.
- Prefer Chinese queries for China-specific or local information.
- Do not rely on one raw query.

For example, for "看看金价":
- "XAU USD live gold price today"
- "spot gold price live USD per ounce"
- "COMEX gold futures price today"
- "上海黄金交易所 Au99.99 今日价格"

## Source Evaluation

Prefer:
- official or primary source pages
- exchange pages
- finance quote pages
- market data pages
- pages with explicit update time or trading date

Be careful with:
- old news articles
- SEO pages
- pages without timestamp
- summaries without source
- brand retail pages unless the user asks for retail price
- pages that mix different units or asset scopes

## Evidence Contract

Before returning, check:
- Did I identify the asset or topic scope?
- Did I collect a concrete value if the user asked for a value?
- Did I include unit?
- Did I include timestamp, trading date, publication date, or update context?
- Did I include source?
- Did I preserve ambiguity and uncertainty?

Return uncertainty explicitly instead of hiding it.
