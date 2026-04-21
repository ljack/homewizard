# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Python monitor + analyzer for a HomeWizard P1 electricity meter on the LAN. Two entry points share one data model:

- **CLI monitor** (`p1_monitor.py`) — terminal display, writes `p1_data.csv`.
- **Web dashboard** (`web_monitor.py`) — Flask + SocketIO on port 5001, writes both `p1_web_data.csv` and SQLite `p1_data.db`.

Target meter defaults to `http://192.168.11.35`. Change by editing `P1Monitor.__init__` in `p1_monitor.py`.

## Common commands

All scripts activate `p1_env/` virtualenv. Run from repo root.

```bash
./start_web_monitor.sh        # Flask dashboard → http://localhost:5001 (auto-starts background collection)
./run_monitor.sh              # Terminal monitor, 60s poll
./run_analyze.sh              # CSV summary stats
./run_analyze.sh plot [hours] # Matplotlib plots, default 24h
python3 status.py             # Check meter + dashboard + DB
python3 test_markers.py       # Marker DB tests
python3 init_markers_db.py    # (Re)create markers table
```

No test framework / linter configured. `test_markers.py` is a standalone script.

## Architecture

### Data flow

```
P1 meter /api/v1/data        →  P1Monitor.fetch_data()           → power_data + CSV
HS110 plugs (TCP 9999, LAN)  →  kasa_monitor.poll_plugs()        → kasa_readings
porssisahko.net / spot-hinta →  spot_price.get_fi_spot_price*    → spot_prices

All writers share p1_data.db. socketio broadcasts new_data (P1) and
kasa_data (plugs) on each poll. Plugs are treated as a subset of the P1
total: UI derives "Unaccounted" = P1_total - sum(plug_power).
```

`WebP1Monitor(P1Monitor)` subclasses CLI monitor and adds SQLite persistence. Both CSV files are kept in sync by the web path — do not remove CSV mirror without also updating `p1_analyze.py` which reads CSV.

### Key field-name divergence (important)

The live HomeWizard API uses `active_power_w`, `active_power_l1_w`, `total_power_import_kwh`, etc. The SQLite schema renames these to `total_power_w`, `power_l1_w`, `total_import_kwh`. Code that reads from **both** sources (e.g. `PiAgentChat.prepare_context`) must accept either key — see `_get_value(latest, 'active_power_w', 'total_power_w')` pattern in `pi_agent_integration.py`. When adding new consumers, use the same fallback helper, not one name.

### SQLite schema (`p1_data.db`)

- `power_data` — P1 readings, one row per minute. Columns match web CSV header.
- `chart_markers` — user annotations, `{id, timestamp, datetime, label, description, color, created_at}`.
- `spot_prices` — cached hourly FI spot prices, `{start_ts PK, end_ts, price_eur_per_kwh, source, vat_included}`. Populated by `spot_price._refresh_series_cache()`.
- `kasa_plugs` — discovered HS110s, `{mac PK, ip, alias, model, enabled, discovered_at, last_seen_at}`.
- `kasa_readings` — plug samples, `{id, timestamp, datetime, mac, alias, power_w, voltage_v, current_a, total_kwh, is_on}`. Indexed on `(mac, timestamp)`.

Schema is auto-created in `WebP1Monitor.setup_database()` + `ensure_spot_price_table()` + `kasa_monitor.ensure_kasa_tables()`. `init_markers_db.py` creates markers table with a slightly different name (`markers` vs `chart_markers`) — web code uses `chart_markers`; treat `init_markers_db.py` as legacy.

### Pricing

`spot_price.py` → `get_fi_spot_price()` fetches Finnish spot price with 5-minute in-memory cache, VAT-inclusive preferred. Tries spot-hinta.fi then porssisahko.net, falls back to stale cache, then to hard-coded €0.25/kWh (`DEFAULT_ELECTRICITY_PRICE_EUR_PER_KWH` in `web_monitor.py`). All cost math in `/api/stats` and chat context uses `get_pricing_context()`.

### Chat integration

`pi_agent_integration.py` shells out to `pi chat --file <tmpfile>` if a `pi` CLI is on PATH; otherwise returns templated Finnish-household-specific fallback responses keyed on message substrings (cost/phase/heatpump/efficiency). Context blurb is hard-coded to a Finnish home with 2 heat pumps, electric floor heating, 35A/65A main fuse — update both branches if the setup changes.

### Frontend

Jinja templates in `templates/dashboard.html`, `templates/chart_full.html`. Static JS/CSS in `static/`. Real-time updates via `socketio.emit('new_data', ...)` and `socketio.emit('kasa_data', ...)` every 60s. REST endpoints:

- P1 / totals: `/api/latest`, `/api/history/<hours>`, `/api/history/range`, `/api/stats/<hours>`
- Markers: `/api/markers` (GET/POST/DELETE)
- Chat: `/api/chat`
- Pricing: `/api/spot-price`, `/api/spot-price/series?start=&end=`
- Kasa plugs: `/api/kasa/plugs`, `/api/kasa/discover` (POST), `/api/kasa/history/<hours>`, `/api/kasa/history/range`, `/api/kasa/breakdown/<hours>`, `/api/kasa/breakdown/range` (aligned per-plug + unaccounted series for chart stacking)

### Agentic chat (Claude Opus 4.7)

`chat_agent.py` exposes `PowerAnalysisAgent`, a manual tool-use loop against Claude Opus 4.7 via the `anthropic` SDK. Tools:

- **query_sqlite** — read-only SELECT/WITH against `p1_data.db`. Blocks DDL/DML/PRAGMA/semicolons; auto-appends LIMIT.
- **calculate** — AST-validated math expression evaluator (allow-list of functions).
- **http_get** — public URL fetch with LAN/loopback SSRF guard, ~100KB cap.
- **web_search** — server-managed `web_search_20260209` (dynamic filtering enabled on Opus 4.7).

System prompt contains the household context + full DB schema and is marked `cache_control: ephemeral` — repeated turns hit prompt cache. Volatile content (`now` + live snapshot) goes in the user message, after the cache break. Loop runs until `stop_reason != "tool_use"`, re-sends verbatim on `pause_turn`. Conversation history is kept in `chat_histories` dict in `web_monitor.py`, keyed by a client-generated `session_id` from localStorage, capped to 12 turns.

Requires `ANTHROPIC_API_KEY` env var. Without it, `/api/chat` falls back to the old keyword-template responder in `pi_agent_integration.py` and returns `agent: "fallback_templates"` in the JSON.

### Mitsubishi MELCloud integration

`melcloud_monitor.py` pulls heat-pump state from Mitsubishi's cloud via `pymelcloud` (reverse-engineered classic API — MELCloud has no public OAuth/partner API for individuals). Credentials are read from `MELCLOUD_USER` / `MELCLOUD_PASS` in `.env` (gitignored, perms 600). Token (`ContextKey`) cached in module-level variable, re-login on 401.

Tables `melcloud_devices` (device_id PK, mac, kind ata/atw/erv, name) and `melcloud_readings` (~every 2 min). The ILP doesn't expose instantaneous W — we persist `total_energy_consumed_kwh` (lifetime counter, from `Device.CurrentEnergyConsumed` / 1000) and derive rolling-window W via `estimate_current_power_w(device_id, window_seconds=1200)`.

Poll cadence 120s (MELCloud rate-limits aggressive polling); consecutive failures back off to 10 min.

U6 / U6 Lite WiFi adapters only expose Shift_JIS setup-UI on port 80 (basic-auth protected, config-only — not state). No local ECHONET Lite. If `pymelcloud` ever breaks for the U6 generation, fallback is the newer `melcloud-home` HA custom component (AWS Cognito backend).

### Kasa HS110 integration

`kasa_monitor.py` handles discovery and polling via `python-kasa` (IOT protocol, TCP 9999). Subnets to scan: `DEFAULT_SUBNETS = ["192.168.11.0/24"]` — same subnet as P1 meter, not the Mac's local LAN. Discovery uses direct `Discover.discover_single(ip)` probes over the whole /24 with asyncio concurrency 50, because UDP broadcast doesn't traverse the subnet boundary from this machine.

Aliases set on the device via `dev.set_alias(name)` persist across reboots and show up in the Kasa mobile app — preferred over storing display-name overrides locally. Example: `4:jääkaappi`, `1:sisäpakastin`, `makkarin puhallin`.

Deprecation note: `emeter_realtime` is deprecated in kasa 0.10 in favor of `device.modules[Module.Energy]`; the old accessor still works and is kept for brevity until upstream removes it.

## Gotchas

- `web_monitor.py` auto-starts background collection at import-time of `__main__`. The `/start_monitoring` and `/stop_monitoring` endpoints also toggle the same `monitoring_active` global. Both P1 and Kasa polling threads check this flag.
- Collection interval hard-coded to 60s in `background_data_collection` and `background_kasa_collection`.
- `p1_analyze.py` reads `p1_data.csv` (CLI file), not the DB — running only the web dashboard leaves the analyzer empty unless you point it at `p1_web_data.csv`.
- Meter IP is duplicated in `p1_monitor.py` and `status.py`.
- Kasa scan runs on every `/api/kasa/discover` call: ~10-15s for a /24, so avoid hammering it.
- The Mac this repo was developed on is on `192.168.5.x`; P1 + Kasa plugs live on `192.168.11.x` routed through the gateway. `kasa discover` (UDP broadcast) returns 0 in that setup; discovery relies on unicast probes.
