# User Stories

This document is the product contract for the HomeWizard monitor. It is not
meant to be agile paperwork. It exists so future changes can be checked against
the ways the app is actually used.

Use these stories when planning changes, reviewing code, or deciding what to
test. A change that affects a story should either preserve its acceptance
criteria or update this document intentionally.

## Personas

- Home energy owner: wants to understand whole-house electricity use, costs,
  and unusual spikes without babysitting the system.
- Local server operator: wants collection to keep running for weeks or months
  on a small server after reboots, network outages, and dashboard restarts.
- Energy investigator: wants to correlate usage with heat pumps, smart plugs,
  spot prices, invoices, and manually marked events.
- Maintainer or coding agent: wants a clear contract for what must stay true
  when changing collectors, UI, database schema, or deployment scripts.

## Product Principles

- Data continuity matters more than dashboard uptime.
- The dashboard must tell the truth about collector health.
- Running the UI must not accidentally create duplicate data collection.
- Historical views should be backed by durable storage, not process memory.
- Config should have one source of truth per setting.
- Server setup should be reproducible from the repo.

## Core Stories

### P0: Always-On Collection

As a local server operator, I want the data collector to run without the
dashboard, so that energy history is preserved even when nobody has the UI open.

Acceptance criteria:

- The collector can run in `--collector-only` mode.
- The collector writes P1 samples to SQLite and the CSV mirror on its normal
  cadence.
- Kasa, Tuya, and MELCloud collectors can run alongside P1 collection without
  stopping the P1 collector when one integration fails.
- A service restart does not require browser interaction to resume collection.
- Health checks can prove that new rows are still arriving.

Important failure modes:

- The dashboard process is stopped or restarted.
- The server reboots.
- One integration API is offline or credentials are missing.
- SQLite is temporarily busy.

### P0: Split Dashboard Stays Useful

As a home energy owner, I want the dashboard to show fresh data even when it is
not the process collecting data, so that the recommended server setup still
feels live.

Acceptance criteria:

- In `--web-only` mode, `/api/latest` returns the latest durable P1 sample from
  SQLite when no in-process `latest_data` exists.
- The dashboard periodically refreshes current P1 values from durable storage.
- The dashboard status text distinguishes "websocket connected" from
  "collector healthy".
- Historical charts continue to work from SQLite.
- Full-screen charts refresh recent ranges without requiring in-process
  Socket.IO events.

Important failure modes:

- Collector-only and web-only run as separate systemd services.
- Socket.IO events do not cross process boundaries.
- The browser opens after the collector has already been running for days.

### P0: No Accidental Duplicate Collectors

As a server operator, I want the UI controls and API routes to avoid starting a
second collector in split mode, so that statistics are not skewed by duplicate
rows.

Acceptance criteria:

- `--web-only` mode cannot start embedded collectors through dashboard buttons
  or unauthenticated GET requests.
- Start/stop routes are either disabled, protected, or scoped clearly to
  all-in-one mode.
- Concurrent start requests cannot create multiple supervisors in one process.
- P1 sample storage has a strategy for avoiding or detecting accidental
  duplicates.

Important failure modes:

- A browser tab or script calls `/start_monitoring` in web-only mode.
- Two start requests arrive at nearly the same time.
- The all-in-one app and collector service are both enabled.

### P0: Truthful Health

As a server operator, I want health checks to report whether the system is
actually collecting data, so that alerts match reality.

Acceptance criteria:

- Health status includes latest P1 row time, row age, stale threshold, DB path,
  and DB errors.
- In split mode, "collector healthy" can be inferred from fresh durable rows
  even when the web process has no in-memory collector supervisor.
- Process-local thread status is shown separately from durable data freshness.
- `status.py` and `/api/monitoring/status` use the same definitions where
  practical.

Important failure modes:

- The dashboard is healthy but the collector is down.
- The collector is healthy but the dashboard is down.
- The collector is healthy but the web process is running in `--web-only`.

### P1: Historical Energy Review

As an energy investigator, I want to inspect power history across useful time
ranges, so that I can find patterns, peaks, and changes in household behavior.

Acceptance criteria:

- Dashboard charts can show common ranges such as 1 hour, 24 hours, 1 week, and
  1 month.
- Full-screen charts support custom start/end ranges.
- Recent data refreshes without disrupting a manually selected historical view.
- Phase power, total power, and derived costs stay consistent across dashboard,
  full-screen chart, and summary endpoints.

### P1: Appliance and Heat Pump Breakdown

As an energy investigator, I want smart plugs and heat pumps shown alongside
whole-house power, so that I can separate known loads from unexplained usage.

Acceptance criteria:

- Kasa plug metadata and latest readings are visible from the dashboard.
- Tuya/Nedis devices can be shown before setup is complete, with clear status
  when local keys are missing.
- MELCloud devices show latest state and recent derived power where available.
- Device data is treated as context under the P1 whole-house total, not as a
  replacement for it.

### P1: Event Marking

As an energy investigator, I want to mark events on charts, so that I can later
connect power changes to actions such as turning equipment on or off.

Acceptance criteria:

- Markers can be added from chart interactions.
- Markers are stored durably in SQLite.
- Markers appear in historical and full-screen chart views for their time
  range.
- Marker deletes do not affect power samples.

### P1: Cost and Invoice Context

As a home energy owner, I want costs to reflect spot prices and invoices where
possible, so that estimates are useful rather than decorative.

Acceptance criteria:

- Current spot price is shown when available.
- Cost summaries use spot prices or invoice-derived estimates where available.
- Fallback prices are clearly marked as fallback values.
- Imported invoices are stored durably and can be deleted without corrupting
  power history.

### P2: AI-Assisted Analysis

As a home energy owner, I want to ask questions about my power data, so that I
can get quick explanations without writing SQL or reading raw charts.

Acceptance criteria:

- Chat responses use recent power context and relevant historical summaries.
- Missing API credentials fall back to a local/template response rather than
  breaking the dashboard.
- Chat must not be the only way to access important operational status.

### P2: CLI Analysis

As a maintainer, I want command-line analysis to use the same collected data as
the server collector, so that scripts remain useful after moving to always-on
collection.

Acceptance criteria:

- Analyzer input source is explicit.
- Server-collected data can be analyzed without manually copying CSV files.
- If both CLI and web CSVs exist, docs explain which one is authoritative.

## Deployment Stories

### Local All-In-One

As a developer or casual user, I want one command to run the dashboard and
collectors together, so that local testing is easy.

Acceptance criteria:

- `./start_web_monitor.sh` starts collectors and the dashboard.
- The dashboard URL and port are printed.
- Stopping the process stops embedded collectors.

### Local Server

As a server operator, I want collection and dashboard services to be managed by
systemd, so that collection survives normal server operations.

Acceptance criteria:

- The collector service starts on boot and restarts on failure.
- The dashboard service can be restarted independently.
- Service files document which values must be changed for the target server.
- Logs are available through `journalctl`.

## Maintenance Checklist

Use this checklist before merging changes that touch collectors, storage,
dashboard status, or deployment.

- Does `collector-only` still collect without the UI?
- Does `web-only` still show fresh data from durable storage?
- Can any UI action start a duplicate collector accidentally?
- Do health checks distinguish DB freshness from in-process thread state?
- Are SQLite writes safe under multiple threads or processes?
- Are meter URL, DB path, and subnet settings configured in one clear place?
- Do docs and service files match the actual startup behavior?
- Is there a focused test or manual verification note for the changed story?

## Current Known Gaps

These are intentionally listed here because they are user-story failures, not
just implementation details.

- Split dashboard freshness: web-only mode still depends partly on process-local
  `latest_data` and Socket.IO events.
- Duplicate collection risk: dashboard start/stop routes can start embedded
  collectors even when a standalone collector service is intended.
- Health semantics: process-local collector state and durable data freshness are
  not cleanly separated everywhere.
- SQLite multi-process hardening: WAL mode and busy timeouts are not yet applied
  consistently.
- Config consistency: status checks expose more env configuration than the
  collector itself currently honors.
- Reproducible install: the repo does not yet include a dependency manifest for
  building `p1_env` from scratch.
