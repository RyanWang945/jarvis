---
name: weather
description: Get current weather and forecasts (no API key required).
when_to_use: User asks for current weather, forecast, temperature, rain, wind, humidity, or local conditions.
tools:
  - tavily_search
tags:
  - weather
  - forecast
  - current-info
  - 天气
  - 预报
capabilities:
  - weather
  - forecast
  - temperature
  - humidity
  - wind
  - rain
  - 天气
  - 预报
  - 气温
  - 湿度
  - 风力
  - 降雨
homepage: https://wttr.in/:help
metadata: {"clawdbot":{"emoji":"🌤️","requires":{"bins":["curl"]}}}
---

# Weather

Use this skill when the user asks for current weather, forecasts, temperature, rain, wind, humidity, or local outdoor conditions.

Jarvis skills are procedural guidance only. They do not grant new tools or permission to run network shell commands.

## Jarvis workflow

1. If `tavily_search` is available, use it for live weather lookup. Search for the specific location and date/time, for example `Shanghai current weather` or `上海 当前 天气`.
2. Prefer sources that provide current conditions or forecasts directly. Summarize the location, temperature, condition, humidity/wind if available, and the observation or forecast time.
3. If no web/search tool is available, explain that Jarvis cannot verify live weather in this turn instead of guessing from memory.
4. Do not use shell tools for web searches or live factual lookups unless the user explicitly requested a local diagnostic command and runtime policy allows it.

## Manual reference

Two free services, no API keys needed.

## wttr.in (primary)

Quick one-liner:
```bash
curl -s "wttr.in/London?format=3"
# Output: London: ⛅️ +8°C
```

Compact format:
```bash
curl -s "wttr.in/London?format=%l:+%c+%t+%h+%w"
# Output: London: ⛅️ +8°C 71% ↙5km/h
```

Full forecast:
```bash
curl -s "wttr.in/London?T"
```

Format codes: `%c` condition · `%t` temp · `%h` humidity · `%w` wind · `%l` location · `%m` moon

Tips:
- URL-encode spaces: `wttr.in/New+York`
- Airport codes: `wttr.in/JFK`
- Units: `?m` (metric) `?u` (USCS)
- Today only: `?1` · Current only: `?0`
- PNG: `curl -s "wttr.in/Berlin.png" -o /tmp/weather.png`

## Open-Meteo (fallback, JSON)

Free, no key, good for programmatic use:
```bash
curl -s "https://api.open-meteo.com/v1/forecast?latitude=51.5&longitude=-0.12&current_weather=true"
```

Find coordinates for a city, then query. Returns JSON with temp, windspeed, weathercode.

Docs: https://open-meteo.com/en/docs
