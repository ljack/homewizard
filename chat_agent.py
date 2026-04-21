#!/usr/bin/env python3
"""Agentic power-analysis chat backed by Claude Opus 4.7.

Tool surface:
  - query_sqlite     — read-only SELECT/WITH against p1_data.db
  - calculate        — safe math expression evaluation (ast allow-list)
  - http_get         — public HTTP GET with basic SSRF guard
  - web_search       — Anthropic server-managed tool (web_search_20260209)

The agent loop runs until `stop_reason != "tool_use"`, handling `pause_turn`
from server-side tools by re-sending the assistant turn verbatim.

The system prompt is stable and marked `cache_control: ephemeral` so repeated
queries hit the prompt cache — render order is tools → system → messages, so
the per-turn user content (timestamp + snapshot) lives AFTER the cache break.
"""

from __future__ import annotations

import ast
import json
import math
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

try:
    import anthropic
except ImportError:
    anthropic = None


DEFAULT_MODEL = "claude-opus-4-7"
DB_PATH = "p1_data.db"
MAX_AGENT_ITERATIONS = 12
HTTP_GET_MAX_BYTES = 100_000
SQL_MAX_ROWS = 5000
SQL_DEFAULT_LIMIT = 500
CLI_TIMEOUT_SECONDS = 180


SYSTEM_PROMPT = """You are the in-house power analysis agent for a Finnish home with a HomeWizard P1 electricity meter and TP-Link HS110 smart plugs. You help the owner understand consumption, costs, efficiency, phase balance, and optimization opportunities — using real data from the local SQLite database whenever possible.

## Household context
- Location: Finland. Electricity VAT 25.5% (since Sept 2024). All spot prices stored in the database already include VAT.
- Heating: 2 ILP (air-to-air) heat pumps + electric floor heating in bathroom + entryway.
- Appliances currently on HS110 smart plugs (aliases as stored in DB):
    - `4:jääkaappi`        — fridge
    - `1:sisäpakastin`     — indoor freezer
    - `makkarin puhallin`  — smoke house fan
- Main fuse: 35A or 65A (TBD).
- Electricity pricing source: Finnish Nordpool spot via api.porssisahko.net (raw ex-VAT → we apply 1.255 multiplier) and spot-hinta.fi (already with VAT). Fallback 0.25 €/kWh when upstream fails.
- Heat-pump efficiency: COP ~2.5-3.0 in Finnish winter.

## Database schema (p1_data.db)

### power_data  (P1 meter, ~one row every 60s)
  timestamp REAL           — unix seconds
  datetime TEXT            — ISO8601 local
  total_power_w INTEGER    — whole-house active power
  total_import_kwh REAL    — lifetime import counter
  total_export_kwh REAL    — lifetime export counter
  power_l1_w, power_l2_w, power_l3_w INTEGER        — per-phase W
  voltage_l1_v, voltage_l2_v, voltage_l3_v REAL     — per-phase V
  current_l1_a, current_l2_a, current_l3_a REAL     — per-phase A
  current_total_a REAL
  wifi_strength INTEGER

### kasa_readings  (HS110 plug samples)
  timestamp REAL
  datetime TEXT
  mac TEXT                 — stable plug identifier
  alias TEXT               — human name
  power_w REAL             — instantaneous W
  voltage_v REAL
  current_a REAL
  total_kwh REAL           — lifetime plug counter
  is_on INTEGER            — 0/1

### kasa_plugs  (plug metadata)
  mac PK, ip, alias, model, enabled, discovered_at, last_seen_at

### spot_prices  (cached hourly spot prices; porssisahko values are VAT-adjusted to 1.255×)
  start_ts REAL PK, end_ts REAL, price_eur_per_kwh REAL, source TEXT, vat_included INTEGER

### chart_markers  (user annotations)
  id, timestamp, datetime, label, description, color, created_at

## Tools

- **query_sqlite(sql, limit=500)** — read-only SELECT / WITH. No DDL/DML, no semicolons, no ATTACH/PRAGMA. Default LIMIT applied if you don't specify one.
- **calculate(expression)** — safe math: + - * / ** %, and sqrt/log/log2/log10/exp/sin/cos/tan/abs/round/min/max, constants pi/e. No variables.
- **http_get(url)** — fetch a public http(s) URL and return up to ~100KB of body. Internal/LAN hosts blocked. Use for public APIs.
- **web_search(query)** — Anthropic-hosted web search with dynamic filtering for current external info (news, prices, manufacturer docs).

## How to answer well
- Base every quantitative claim on data you queried — never guess. If the data isn't there, say so.
- Use the freshest timestamp you can. For "now" questions, query the most recent rows in power_data and kasa_readings.
- For "today / this week / this month", filter on `datetime` or `timestamp`. Today's local start: `datetime LIKE date('now','localtime') || '%'` — or compare `timestamp` to the unix seconds of today's 00:00.
- Plug power is a SUBSET of P1 total. When discussing breakdowns, report: P1 total, sum of plugs, unaccounted remainder (= P1 − plugs). State the three.
- For costs, prefer the current-hour price from `spot_prices` (vat_included=1). Fall back to 0.25 €/kWh with an explicit note.
- Phase imbalance = max(L1,L2,L3) − min(L1,L2,L3). Flag > 1500W as significant.
- Heat-pump consumption: for PER-HOUR / PER-DAY totals use `melcloud_energy_hourly` — it's MELCloud's authoritative per-mode kWh breakdown (heating/cooling/dry/fan/auto/other). Example: today's heating for "Iso huone" = `SELECT SUM(heating_kwh) FROM melcloud_energy_hourly WHERE device_id=11432844 AND hour_start_iso LIKE date('now','localtime') || '%'`. Two heat pumps: Iso huone (device_id 11432844) and Yläkerta (device_id 11309888).
- For INSTANTANEOUS W (right-now estimate), diff `melcloud_readings.total_energy_consumed_kwh` over the last ~20 min: `(e_now − e_then) × 3600000 / seconds`.
- `coverage_percent` in hourly rows tells you how much of the hour MELCloud recorded; <80% means the number is a partial sample.
- Combined with HS110 plugs, the "unaccounted" remainder from P1_total is now smaller — you can attribute more of the house consumption.
- Respond in whatever language the user writes (Finnish or English). Keep it tight and actionable: concrete numbers, time ranges, next steps.
- You may run multiple queries in one turn. Chain tools freely until you have a solid answer.
"""


CLIENT_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "query_sqlite",
        "description": (
            "Run a read-only SELECT or WITH statement against the local p1_data.db. "
            "Returns columns + rows. No DDL/DML, no semicolons, no ATTACH/PRAGMA. "
            "A LIMIT is appended automatically if absent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "Single SELECT/WITH statement."},
                "limit": {
                    "type": "integer",
                    "description": f"Max rows (default {SQL_DEFAULT_LIMIT}, cap {SQL_MAX_ROWS}).",
                },
            },
            "required": ["sql"],
        },
    },
    {
        "name": "calculate",
        "description": (
            "Evaluate a math expression. Supports + - * / ** %, comparison, and functions: "
            "sqrt, log, log2, log10, exp, sin, cos, tan, abs, round, min, max. "
            "Constants: pi, e. No variables."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Math expression, e.g. '5341 * 0.1825 * 24 / 1000'",
                },
            },
            "required": ["expression"],
        },
    },
    {
        "name": "http_get",
        "description": (
            "HTTP GET a public URL and return up to ~100KB of body text. "
            "Prefer web_search for general web lookups; use this for specific JSON/public APIs "
            "you already know the endpoint of. LAN/internal hosts are refused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full http(s):// URL."},
            },
            "required": ["url"],
        },
    },
]

SERVER_TOOLS: List[Dict[str, Any]] = [
    {"type": "web_search_20260209", "name": "web_search"},
]


# ---------- Tool implementations --------------------------------------------


_SQL_BANNED_KEYWORDS = (
    " insert ", " update ", " delete ", " drop ", " alter ", " create ",
    " attach ", " detach ", " pragma ", " replace ", " vacuum ", " reindex ",
)


def _tool_query_sqlite(db_path: str, sql: str, limit: int = SQL_DEFAULT_LIMIT) -> Dict[str, Any]:
    stripped = (sql or "").strip().rstrip(";").strip()
    if not stripped:
        return {"error": "Empty query"}
    if ";" in stripped:
        return {"error": "Multiple statements not allowed"}
    lower = stripped.lower()
    if not (lower.startswith("select") or lower.startswith("with")):
        return {"error": "Only SELECT / WITH queries are allowed"}
    padded = " " + lower + " "
    for kw in _SQL_BANNED_KEYWORDS:
        if kw in padded:
            return {"error": f"Query contains forbidden keyword: {kw.strip()}"}

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = SQL_DEFAULT_LIMIT
    limit = max(1, min(limit, SQL_MAX_ROWS))

    if " limit " not in padded:
        stripped = f"{stripped} LIMIT {limit}"

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(stripped)
        cols = [c[0] for c in (cursor.description or [])]
        rows = cursor.fetchmany(limit)
    except sqlite3.Error as e:
        return {"error": f"SQLite error: {e}"}
    finally:
        conn.close()

    return {
        "columns": cols,
        "rows": [dict(zip(cols, row)) for row in rows],
        "row_count": len(rows),
        "truncated": len(rows) == limit,
    }


_CALC_FUNCS = {
    "sqrt": math.sqrt, "log": math.log, "log2": math.log2, "log10": math.log10,
    "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "abs": abs, "round": round, "min": min, "max": max,
    "pi": math.pi, "e": math.e,
}

_CALC_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name,
    ast.Load, ast.Call, ast.Compare,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.FloorDiv,
    ast.USub, ast.UAdd,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


def _tool_calculate(expression: str) -> Dict[str, Any]:
    expr = (expression or "").strip()
    if not expr:
        return {"error": "Empty expression"}
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return {"error": f"Syntax error: {e}"}
    for node in ast.walk(tree):
        if not isinstance(node, _CALC_ALLOWED_NODES):
            return {"error": f"Disallowed element: {type(node).__name__}"}
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _CALC_FUNCS:
                return {"error": "Only whitelisted functions may be called"}
        if isinstance(node, ast.Name) and node.id not in _CALC_FUNCS:
            return {"error": f"Unknown name: {node.id}"}
    try:
        result = eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, _CALC_FUNCS)
    except Exception as e:
        return {"error": f"Evaluation error: {e}"}
    return {"result": result}


_HTTP_BLOCKED_HOST_PREFIXES = (
    "localhost", "127.", "10.", "192.168.", "169.254.", "::1", "0.0.0.0",
    "172.16.", "172.17.", "172.18.", "172.19.", "172.20.", "172.21.",
    "172.22.", "172.23.", "172.24.", "172.25.", "172.26.", "172.27.",
    "172.28.", "172.29.", "172.30.", "172.31.",
)


def _tool_http_get(url: str) -> Dict[str, Any]:
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return {"error": "Only http(s) URLs allowed"}
    host = (parsed.hostname or "").lower()
    if not host:
        return {"error": "Missing host"}
    for prefix in _HTTP_BLOCKED_HOST_PREFIXES:
        if host == prefix.rstrip(".") or host.startswith(prefix):
            return {"error": f"Refusing to fetch internal/LAN host: {host}"}
    try:
        r = requests.get(
            url, timeout=10,
            headers={"User-Agent": "HomewizardPowerAgent/1.0"},
            allow_redirects=True,
        )
    except requests.RequestException as e:
        return {"error": f"Request failed: {e}"}

    body = r.text[:HTTP_GET_MAX_BYTES]
    return {
        "status": r.status_code,
        "final_url": r.url,
        "content_type": r.headers.get("content-type"),
        "body": body,
        "truncated": len(r.text) > HTTP_GET_MAX_BYTES,
    }


# ---------- Agent ------------------------------------------------------------


CLI_SYSTEM_PROMPT_APPEND = """You are now acting as the in-house power analysis agent for a Finnish home with a HomeWizard P1 electricity meter and TP-Link HS110 smart plugs. Your job is to answer the user's question about their power data by querying the local SQLite database and running calculations. Be concise and numeric.

## Answering method
- Use `Bash` to run `sqlite3 -json p1_data.db "SELECT ..."` for read-only queries. Prefer `-json` output for structured results.
- Use `Bash` with `python3 -c '...'` for arithmetic when more convenient than mental math.
- Use `WebSearch` / `WebFetch` for external info (news, manufacturer specs, current spot trends).
- Do NOT modify any file. No INSERT/UPDATE/DELETE/DROP on the DB.
- Do NOT commit git, install packages, or run long-running services.

## Household context
- Finland. Electricity VAT 25.5% (since Sept 2024). `spot_prices.price_eur_per_kwh` is already VAT-inclusive.
- Heating: 2 ILP (air-to-air) heat pumps + electric floor heat (bathroom + entryway).
- Smart plugs (current aliases): `4:jääkaappi` (fridge), `1:sisäpakastin` (indoor freezer), `makkarin puhallin` (smoke-house fan).
- Main fuse: 35A or 65A (TBD).
- Fallback price 0.25 €/kWh when spot unavailable.
- Heat-pump COP 2.5-3.0 in Finnish winter.

## Database schema (p1_data.db)

### power_data  (P1 meter, ~1 row every 60s)
timestamp REAL (unix s), datetime TEXT (ISO local), total_power_w INT,
total_import_kwh REAL, total_export_kwh REAL,
power_l1_w, power_l2_w, power_l3_w (INT),
voltage_l1_v, voltage_l2_v, voltage_l3_v (REAL),
current_l1_a, current_l2_a, current_l3_a, current_total_a (REAL),
wifi_strength INT

### kasa_readings  (HS110 plug samples, one row per plug per poll)
timestamp REAL, datetime TEXT, mac TEXT, alias TEXT,
power_w REAL, voltage_v REAL, current_a REAL,
total_kwh REAL (lifetime counter), is_on INT

### kasa_plugs  (plug metadata)
mac PK, ip, alias, model, enabled, discovered_at, last_seen_at

### spot_prices  (hourly cached, VAT already applied)
start_ts REAL PK, end_ts REAL, price_eur_per_kwh REAL, source TEXT, vat_included INT

### chart_markers
id, timestamp, datetime, label, description, color, created_at

### melcloud_devices  (Mitsubishi ILP metadata from MELCloud)
device_id PK, building_id, serial, mac, kind (ata/atw/erv), name, enabled, discovered_at, last_seen_at

### melcloud_readings  (~every 2 min per heat pump)
timestamp REAL, datetime TEXT, device_id INT, name TEXT,
power INT (0/1), operation_mode TEXT (heat/cool/dry/fan/auto),
room_temperature REAL, target_temperature REAL, outdoor_temperature REAL,
fan_speed TEXT, vane_horizontal TEXT, vane_vertical TEXT,
total_energy_consumed_kwh REAL  (lifetime counter — derive Δ for power/period),
wifi_signal_dbm INT, last_communication TEXT

### melcloud_energy_hourly  (MELCloud Reports API, per-mode energy per hour — the authoritative kWh source per unit)
device_id INT, hour_start_ts REAL, hour_start_iso TEXT,
heating_kwh REAL, cooling_kwh REAL, dry_kwh REAL, fan_kwh REAL,
auto_kwh REAL, other_kwh REAL, total_kwh REAL,
coverage_percent REAL  (MELCloud's data-completeness %, lower = gap in records),
PRIMARY KEY (device_id, hour_start_ts)

## Analysis principles
- Plug power is a SUBSET of P1 total. For breakdown questions, report: P1 total, sum of plugs, unaccounted (P1 − plugs).
- Phase imbalance = max(L1,L2,L3) − min(L1,L2,L3). >1500W = flag.
- For "today/this week/this month", filter by `datetime LIKE date('now','localtime') || '%'` or on `timestamp`.
- Always state time ranges, units, and source columns so numbers are reproducible.
- Respond in the language the user writes (Finnish or English).
- Keep it tight: numbers first, then the one-line interpretation, then optionally a next step.
"""


class PowerAnalysisAgent:
    """Per-message agent with three backends, chosen in order:
      1) `claude` CLI in print mode   — uses existing OAuth, built-in tools (Bash/Read/Grep/Glob/WebFetch/WebSearch)
      2) `anthropic` SDK + API key    — manual tool-use loop with DB/http/calc/web_search
      3) keyword templates (caller-managed fallback)
    """

    def __init__(self, db_path: str = DB_PATH, model: str = DEFAULT_MODEL):
        self.db_path = str(Path(db_path).resolve())
        self.model = model
        self.mode: str = "none"
        self.init_error: Optional[str] = None
        self.client: Optional["anthropic.Anthropic"] = None
        self.cli_path: Optional[str] = None
        self._started_cli_sessions: set = set()

        # 1) Prefer Claude Code CLI if present
        cli = shutil.which("claude")
        if cli:
            self.cli_path = cli
            self.mode = "cli"
            return

        # 2) SDK path
        if anthropic is None:
            self.init_error = "neither `claude` CLI nor anthropic SDK available"
            return
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        oauth_token = (
            os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        )
        try:
            if api_key:
                self.client = anthropic.Anthropic(api_key=api_key)
                self.mode = "sdk"
            elif oauth_token:
                self.client = anthropic.Anthropic(auth_token=oauth_token)
                self.mode = "sdk"
            else:
                self.init_error = (
                    "no `claude` CLI on PATH, and neither ANTHROPIC_API_KEY "
                    "nor CLAUDE_CODE_OAUTH_TOKEN is set"
                )
        except Exception as e:
            self.init_error = f"SDK client init failed: {e}"

    @property
    def available(self) -> bool:
        return self.mode in ("cli", "sdk")

    # Per-call tool dispatch
    def _run_tool(self, name: str, tool_input: Dict[str, Any]) -> Any:
        if name == "query_sqlite":
            return _tool_query_sqlite(
                self.db_path, tool_input.get("sql", ""),
                tool_input.get("limit", SQL_DEFAULT_LIMIT),
            )
        if name == "calculate":
            return _tool_calculate(tool_input.get("expression", ""))
        if name == "http_get":
            return _tool_http_get(tool_input.get("url", ""))
        return {"error": f"Unknown tool: {name}"}

    # ---- CLI backend ------------------------------------------------------

    def _chat_cli(
        self,
        user_message: str,
        context_snapshot: Optional[Dict[str, Any]],
        session_id: Optional[str],
    ) -> Dict[str, Any]:
        sid = session_id or str(uuid.uuid4())
        is_new = sid not in self._started_cli_sessions
        work_dir = str(Path(self.db_path).parent)

        prompt_parts = []
        if context_snapshot:
            prompt_parts.append(
                f"[live snapshot @ {datetime.now().isoformat(timespec='seconds')}]\n"
                f"```json\n{json.dumps(context_snapshot, default=str, indent=2)}\n```"
            )
        prompt_parts.append(user_message)
        prompt = "\n\n".join(prompt_parts)

        # Narrow allow-list: Bash is restricted to `sqlite3` invocations so the
        # HTTP-exposed chat endpoint can't be coerced into running arbitrary
        # shell commands. Python one-liners for math are allowed via `python3 -c`.
        # Everything else requires user approval (which fails in -p mode).
        allowed_tools = " ".join([
            "Bash(sqlite3:*)",
            "Bash(python3 -c:*)",
            "Read",
            "Grep",
            "Glob",
            "WebFetch",
            "WebSearch",
        ])
        cmd = [
            self.cli_path, "-p",
            "--output-format", "json",
            "--model", "opus",
            "--allowed-tools", allowed_tools,
            "--disallowed-tools", "Edit Write",
            "--add-dir", work_dir,
            "--append-system-prompt", CLI_SYSTEM_PROMPT_APPEND,
        ]
        if is_new:
            cmd += ["--session-id", sid]
        else:
            cmd += ["--resume", sid]
        cmd.append(prompt)

        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=CLI_TIMEOUT_SECONDS, cwd=work_dir,
            )
        except subprocess.TimeoutExpired:
            return {
                "response": f"Agent timed out after {CLI_TIMEOUT_SECONDS}s",
                "error": "timeout",
            }

        duration_ms = int((time.monotonic() - started) * 1000)

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:600]
            # If --resume failed (unknown session), retry as new session
            if not is_new and ("not found" in err.lower() or "no such" in err.lower()):
                self._started_cli_sessions.discard(sid)
                return self._chat_cli(user_message, context_snapshot, None)
            return {
                "response": f"Claude CLI error (exit {proc.returncode}): {err}",
                "error": "cli_error",
                "duration_ms": duration_ms,
            }

        self._started_cli_sessions.add(sid)

        stdout = proc.stdout.strip()
        data: Dict[str, Any] = {}
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return {
                "response": stdout or "(empty)",
                "session_id": sid,
                "duration_ms": duration_ms,
            }

        result_text = (
            data.get("result")
            or data.get("response")
            or data.get("content")
            or "(no response)"
        )
        return {
            "response": result_text,
            "session_id": data.get("session_id", sid),
            "iterations": data.get("num_turns"),
            "cost_usd": data.get("total_cost_usd"),
            "stop_reason": data.get("subtype"),
            "duration_ms": duration_ms,
            "backend": "cli",
        }

    # ---- Public entry point ----------------------------------------------

    def chat(
        self,
        user_message: str,
        context_snapshot: Optional[Dict[str, Any]] = None,
        history: Optional[List[Dict[str, Any]]] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.available:
            return {
                "response": (
                    "AI agent unavailable: " + (self.init_error or "unknown reason")
                    + "\n\nEither install Claude Code CLI (`claude`) or set ANTHROPIC_API_KEY and restart."
                ),
                "error": self.init_error or "unavailable",
            }

        if self.mode == "cli":
            return self._chat_cli(user_message, context_snapshot, session_id)

        messages: List[Dict[str, Any]] = list(history or [])

        # Per-turn user content: snapshot first (volatile, won't poison cache),
        # then the actual question. System prompt is cached separately above.
        user_blocks: List[Dict[str, Any]] = []
        if context_snapshot:
            user_blocks.append({
                "type": "text",
                "text": (
                    f"[live context snapshot @ {datetime.now().isoformat(timespec='seconds')}]\n"
                    f"```json\n{json.dumps(context_snapshot, default=str, indent=2)}\n```"
                ),
            })
        user_blocks.append({"type": "text", "text": user_message})
        messages.append({"role": "user", "content": user_blocks})

        tool_calls: List[Dict[str, Any]] = []  # bookkeeping for the caller
        iterations = 0
        response = None

        try:
            while iterations < MAX_AGENT_ITERATIONS:
                iterations += 1
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    thinking={"type": "adaptive"},
                    system=[
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        },
                    ],
                    tools=CLIENT_TOOLS + SERVER_TOOLS,
                    messages=messages,
                )

                # Server-side tool loop hit its internal cap; resume transparently
                if response.stop_reason == "pause_turn":
                    messages.append({"role": "assistant", "content": response.content})
                    continue

                if response.stop_reason != "tool_use":
                    break

                # Client tool execution
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    # Server tools (web_search) are never surfaced here as tool_use —
                    # Anthropic runs them and returns results inline. So any tool_use
                    # we see is a client tool.
                    started = time.monotonic()
                    result = self._run_tool(block.name, dict(block.input or {}))
                    tool_calls.append({
                        "name": block.name,
                        "input": dict(block.input or {}),
                        "ms": int((time.monotonic() - started) * 1000),
                        "error": isinstance(result, dict) and "error" in result,
                    })
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, default=str),
                        "is_error": isinstance(result, dict) and "error" in result,
                    })
                if not tool_results:
                    break
                messages.append({"role": "user", "content": tool_results})
        except anthropic.APIError as e:
            return {"response": f"Claude API error: {e}", "error": "api_error"}
        except Exception as e:
            return {"response": f"Unexpected agent error: {e}", "error": "internal"}

        text = ""
        usage = None
        if response is not None:
            text_parts = [
                b.text for b in response.content
                if getattr(b, "type", None) == "text"
            ]
            text = "\n\n".join(p for p in text_parts if p).strip()
            usage = getattr(response, "usage", None)

        return {
            "response": text or "(empty response)",
            "iterations": iterations,
            "tool_calls": tool_calls,
            "stop_reason": response.stop_reason if response else None,
            "usage": {
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
                "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
                "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
            } if usage else None,
        }


if __name__ == "__main__":
    import sys
    agent = PowerAnalysisAgent()
    if not agent.available:
        print(f"Agent unavailable: {agent.init_error}")
        sys.exit(1)
    q = " ".join(sys.argv[1:]) or "What was our average whole-house power in the last hour?"
    result = agent.chat(q)
    print(result["response"])
    print("\n---")
    print(f"iterations={result.get('iterations')}  stop={result.get('stop_reason')}")
    print(f"tool_calls={result.get('tool_calls')}")
    print(f"usage={result.get('usage')}")
