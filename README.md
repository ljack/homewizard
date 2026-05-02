# HomeWizard P1 Meter Monitor & Analyzer

A complete web-based system to monitor, store, and analyze your HomeWizard P1 meter data with real-time dashboard and AI-powered chat analysis.

For product-level behavior and acceptance criteria, see
[`docs/USER_STORIES.md`](docs/USER_STORIES.md).

For preserving appliance identities when Kasa plugs are moved, see
[`docs/SMART_PLUG_MOVES.md`](docs/SMART_PLUG_MOVES.md).

## 🚀 Quick Start

### Local All-In-One
```bash
./start_web_monitor.sh
```
Then open: **http://localhost:5001**

On macOS, the dashboard also advertises itself with Bonjour/mDNS as
`HomeWizard P1` on `_http._tcp.local` and `_homewizard-p1._tcp.local` when
`dns-sd` is available. Other devices can usually open it directly at
`http://<mac-hostname>.local:5001`, for example `http://sysi6.local:5001` on
the current Mac.

### Always-On Collector (Recommended For A Server)
```bash
./start_data_collector.sh
```
This runs the data gatherer without the UI, so collection can stay up even if the dashboard is stopped or restarted.

### Dashboard Only
```bash
./start_web_ui.sh
```
Use this when the collector is already running as its own service.

### Command Line Tools
```bash
./run_monitor.sh           # Terminal monitoring
./run_analyze.sh           # Data analysis  
./run_analyze.sh plot      # Generate plots
```

## 🔁 Server Setup

If you want data gathering to survive browser closes, reboots, or app crashes, run the collector as its own service and keep the dashboard separate.

### Modes

```bash
python3 web_monitor.py                 # dashboard + collectors
python3 web_monitor.py --collector-only
python3 web_monitor.py --web-only
python3 web_monitor.py --no-mdns       # dashboard without Bonjour advertising
```

### Local Discovery

Dashboard mode advertises a Bonjour/mDNS service by default when a local
advertiser is available. On macOS this uses the built-in `dns-sd` command, so
there is no extra dependency. You can customize or disable it in `.env`:

```bash
MDNS_ENABLED=1
MDNS_SERVICE_NAME=HomeWizard P1
MDNS_SERVICE_TYPES=_http._tcp,_homewizard-p1._tcp
```

This is service discovery, not a DNS hostname alias. It helps apps and Bonjour
browsers find the dashboard. A literal name like `homewizard.local` requires a
separate host alias, hostname change, or Avahi configuration on the server.

### systemd Example

Service templates live in `deploy/systemd/`:

- `homewizard-collector.service` - always-on data collection
- `homewizard-dashboard.service` - optional web UI process

Typical setup on a Linux server:

```bash
sudo cp deploy/systemd/homewizard-collector.service /etc/systemd/system/
sudo cp deploy/systemd/homewizard-dashboard.service /etc/systemd/system/
sudoedit /etc/systemd/system/homewizard-collector.service
sudoedit /etc/systemd/system/homewizard-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now homewizard-collector.service
sudo systemctl enable --now homewizard-dashboard.service
```

Before enabling them, change these fields in the unit files:

- `User` / `Group`
- `WorkingDirectory`
- `ExecStart`

Health checks:

```bash
./p1_env/bin/python status.py
curl http://localhost:5001/api/monitoring/status
```

### macOS launchd Example

For local Mac use, install the LaunchAgent template:

```bash
cp deploy/launchd/fi.local.homewizard.web-monitor.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/fi.local.homewizard.web-monitor.plist
launchctl enable "gui/$(id -u)/fi.local.homewizard.web-monitor"
launchctl kickstart -k "gui/$(id -u)/fi.local.homewizard.web-monitor"
```

The job uses `KeepAlive` and `RunAtLoad`, so macOS restarts the dashboard plus embedded collectors after failures and starts it again on login. Runtime logs are written to `web_monitor_launchd.log` and `web_monitor_launchd.err.log`.

## 🜐 Web Dashboard Features

- **Real-time monitoring** with automatic updates every minute
- **Interactive charts** showing power consumption trends  
- **Full-screen chart view** with advanced time controls
- **Custom time ranges** - select any start/end period
- **Chart markers** - annotate events like "turned pool heater off"
- **Phase balance visualization** with color-coded bars
- **Live cost calculations** and utilization metrics
- **AI-powered chat** for intelligent data analysis
- **Historical data** with selectable time periods (1H to 1M)
- **Mobile-responsive** design for monitoring on any device
- **Automated alerts** for phase imbalance and high loads
- **Data persistence** - markers and annotations saved to database

## 📊 What It Does

### Monitor (`p1_monitor.py`)
- ✅ Fetches real-time data from P1 meter every 60 seconds
- ✅ Beautiful live display with power consumption, phase balance, costs
- ✅ Stores all data to CSV file for analysis
- ✅ Shows imbalance warnings and utilization

### Analyzer (`p1_analyze.py`) 
- 📈 Statistical analysis of consumption patterns
- 💰 Cost calculations and projections  
- ⚖️ Phase balance analysis over time
- 📊 Trend analysis and recent data views
- 📉 Generates plots (with matplotlib)

## 🗃️ Data Storage

Data is stored in two places depending on which monitor you run:

- **CLI monitor** (`p1_monitor.py`) → `p1_data.csv`
- **Web monitor** (`web_monitor.py`) → `p1_web_data.csv` **and** SQLite database `p1_data.db`
  - `power_data` table: all readings (fast queries for charts)
  - `markers` table: chart annotations (persisted across sessions)

CSV fields:
- Timestamp and datetime
- Total power consumption (W)
- Energy totals (kWh import/export)
- Per-phase power, voltage, current
- WiFi signal strength

## 📈 Monitoring Examples

**Live monitoring will show (example reading):**
```
⚡ POWER CONSUMPTION
   Total: 5,341W (5.34 kW)

📊 PHASE DISTRIBUTION
   Phase 1: 2125W (40.0%) ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓
   Phase 2:  593W (11.2%) ▓▓▓▓▓
   Phase 3: 2597W (48.9%) ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓

💰 COST ESTIMATE
   Current rate: €1.34/hour
   If sustained: €32.0/day
```

**Analysis will show trends:**
- Average consumption over time
- Peak usage hours
- Phase balance evolution
- Energy efficiency metrics

## 🛠️ Technical Details

**Requirements:**
- Python 3.x with virtual environment (`p1_env/`)
- HomeWizard P1 meter on local network (`P1_METER_URL`, default: `http://homewizard.local`)
- Dependencies: `requests`, `pandas`, `matplotlib`, `flask`, `flask-socketio`

**Files:**

*Core monitoring & analysis*
- `p1_monitor.py` — P1 meter client + CLI monitor
- `p1_analyze.py` — Data analysis script
- `web_monitor.py` — Flask + SocketIO web dashboard (port 5001)
- `pi_agent_integration.py` — AI chat backend for the dashboard
- `status.py` — Quick status snapshot

*Scripts*
- `start_web_monitor.sh` — Launch dashboard + embedded collectors
- `start_data_collector.sh` — Launch collector-only mode
- `start_web_ui.sh` — Launch dashboard without collectors
- `run_monitor.sh` / `run_analyze.sh` — CLI convenience wrappers

*Deployment*
- `deploy/launchd/fi.local.homewizard.web-monitor.plist` — macOS LaunchAgent for restart-on-failure local runs
- `deploy/systemd/homewizard-collector.service` — systemd unit for always-on collection
- `deploy/systemd/homewizard-dashboard.service` — systemd unit for dashboard-only mode

*Data & database*
- `p1_data.csv` — CLI monitor data
- `p1_web_data.csv` — Collector/web CSV mirror
- `p1_data.db` — SQLite DB (readings + markers)
- `init_markers_db.py` — Initialize the markers table
- `test_markers.py` — Marker tests

*Frontend*
- `templates/dashboard.html`, `templates/chart_full.html`
- `static/css/`, `static/js/`

*Environment*
- `p1_env/` — Python virtual environment

## 🎯 Next Steps

1. **For a server install**: Prefer `./start_data_collector.sh`
2. **For a one-process local run**: Use `./start_web_monitor.sh`
3. **Let it run for a few hours** to see patterns
4. **Check analysis**: Use `./run_analyze.sh` to see trends
5. **View plots**: Use `./run_analyze.sh plot` for visual analysis
6. **Plan phase rebalancing** based on the data collected

## 📈 Advanced Chart Features

### Full-Screen Chart View
Click the expand button on any chart to open the full-screen view with:
- **Custom time ranges**: Select any date/time period
- **Quick periods**: 1H, 6H, 24H, 1W, 1M buttons
- **Interactive markers**: Click anywhere to add annotations
- **Live updates**: Auto-refresh for recent data
- **Dark theme**: Optimized for extended viewing

### Chart Markers
Add contextual annotations to your power data:
```
📍 "Pool heater turned off" - See immediate power drop
📍 "Heat pump maintenance" - Track efficiency changes  
📍 "Solar panels installed" - Monitor generation
📍 "EV charging" - Identify charging sessions
```

**To add a marker:**
1. Click on any point in the chart
2. Enter a label (e.g., "Pool heater off")
3. Add optional description
4. Choose a color
5. Save - marker persists across sessions

### Usage Examples
- **Energy audits**: Mark appliance changes and track impact
- **Troubleshooting**: Annotate when issues occurred
- **Optimization**: Document efficiency improvements
- **Maintenance**: Track service dates and performance

The system will help you:
- Track energy usage patterns  
- Identify peak consumption times
- Monitor phase balance improvements
- Calculate actual costs vs estimates
- Make data-driven decisions about electrical improvements

---

**💡 Pro Tip:** Run the monitor overnight to capture your heat pump cycling patterns and get realistic average consumption data!
