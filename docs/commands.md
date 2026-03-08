# Command Reference

The VPN Monitor tool follows a command-based structure. This document provides an exhaustive reference for every command and flag available in the CLI.

## Common Features

### 📡 Unified Server Filtering (`--servers`)
Almost all commands (`test`, `monitor`, `stats`, `graph`, `export`) support the `--servers` argument. You can mix different types of identifiers in a single comma-separated string:

- **Major ID**: Numeric database ID (e.g., `1, 4`).
- **Remark/Host**: Case-insensitive partial matches for the server name or hostname (e.g., `Italy, Google`).
- **Subscription URL**: Any string starting with `http`. All servers belonging to that subscription will be included.

**Example**: `python3 run.py test --servers "1,Russia,https://mysub.site/link"`

### ⏱️ Unified Time Filtering
Data commands (`stats`, `graph`, `cleanup`, `export`) use a standardized time range logic with the following precedence:

1.  **`--timespan "start - end"`**: Literal range. Supports separators like ` - `, `..`, `/`, ` to `. Timestamps should be ISO-like (`YYYY-MM-DD HH:MM:SS`).
    - *Example*: `--timespan "2026-03-01 12:00..2026-03-01 13:00"`
    - *Single value*: If only one timestamp is provided, it's treated as the start time, and the end time defaults to "now".
2.  **`--days X`**: Look back X days from now.
3.  **`--hours X`**: Look back X hours from now (Default: 24h for stats/graph).

---

## 🛠️ Commands

### `fetch <url> [<url2> ...]`
Downloads and parses subscription links.
- **Input**: Supports standard base64-encoded subscription blocks or lists of direct proxy links.
- **Merging**: Uses a unique `uri_key` (SHA256 of the URI) to identify servers. If a server already exists, its `remark` and `raw_uri` are updated. New servers are inserted.
- **Output**: Summary of new and updated nodes.

### `list`
Displays a compact table of all servers in the database, grouped by their subscription source.
- **Columns**: ID, Protocol, Transport, Remark, Host, Port.

### `test`
Runs a one-shot measurement round.
- **`--tasks`**: Comma-separated list of:
    - `tcp-ping`: Rapid L4 connection check.
    - `xray-ping`: Full SOCKS5 proxy check (requires `xray`).
    - `speed`: Bandwidth measurement.
- **`--batch X`**: Only test a random sample of X servers. Use `-1` for all.
- **`--workers X`**: Number of parallel threads for TCP pings.

### `monitor`
Enters an infinite loop for longitudinal monitoring.
- **`--tasks <name>:<interval>`**: Define tasks and their frequencies.
    - Intervals: `s` (seconds), `m` (minutes), `h` (hours).
    - *Example*: `--tasks "xray-ping:30s,tcp-ping:1m,speed:2h"`
- **Lifecycle**: For `xray-ping` and `speed`, it groups servers into rounds to minimize overhead.

### `stats`
Generates a comprehensive statistical report.
- **`--sort <column>`**: Sort results by any displayed column (e.g., `score`, `mean`, `OK%`).
- **`--cols <list>`**: Customize table columns. Supports specific metrics like `xray-ping:p95` or `tcp-ping:mean`.
- **Persistence**: Automatically saves the plain-text table to `last_general_stats.md` (stripping ANSI colors).

### `graph [<identifier>]`
Generates a multi-plot PNG report for a specific server (requires `matplotlib` and `numpy`).
- **`--plots <type>:<style>`**: Comma-separated plots.
    - Types: `xray-ping`, `tcp-ping`, `xray-jit`, `tcp-jit`, `speed`.
    - Styles:
        - `percentile`: Standard growth curve with P50/P90/P95/P99 markers.
        - `percentile-log`: Logarithmic scale growth curve (best for latency).
        - `dynamic`: Chronological bar chart of measurements.
- **`--fixed-scale`**: Forces a standard Y-axis range (e.g., up to 5000ms for latency) for easier comparison between different servers.

### `cleanup`
Deletes old historical records to keep the database size manageable.
- **Precedence**: Follows the Unified Time Filtering rules (uses `since` as the cutoff).
- **Action**: Runs `VACUUM` on the SQLite database after deletion to reclaim disk space.

### `export`
Dumps raw measurement data joined with server metadata.
- **Format**: CSV.
- **Output**: remark, protocol, transport, host, port, timestamp, method, latency_ms, error.

---

## 🛠️ Advanced Flag Syntax

Many flags in the VPN Monitor use a **Colon Separated Syntax** (`domain:metric` or `metric:style`) to provide deep control over what is tested, displayed, or plotted.

### 1. `stats` and `export` Flags (`--cols`, `--sort`)
Format: `[provider:]metric`

| Provider (Optional) | Metric | Description |
| :--- | :--- | :--- |
| `xray-ping` | `mean`, `score1`, `score2`, `score3` | Average, or Stability Score. |
| `xray-jit`  | `mean`, `p50`, `p90`, `p95` | Average jitter or percentile variations. |
| `tcp-ping` | `p50`, `p90`, `p95` | Latency percentiles. |
| (none) | `OK%`, `N`, `σ` | Success rate, Sample count, or StdDev. |

- **Examples**: 
    - `--cols "Server,xray-ping:p95,speed"` (Shows Server name, Xray P95, and Speed).
    - `--sort "xray-jit:mean"` (Sorts the table by Xray jitter).

### 2. `monitor` Flags (`--tasks`)
Format: `metric:interval`

| Metric | Frequency Suffix | Example |
| :--- | :--- | :--- |
| `xray-ping` | `s` (seconds) | `xray-ping:10s` |
| `tcp-ping` | `m` (minutes) | `tcp-ping:5m` |
| `speed` | `h` (hours) | `speed:1h` |

- **Example**: `monitor --tasks "xray-ping:15s,speed:30m"`

### 3. `graph` Flags (`--plots`)
Format: `metric:style`

| Metric | Plot Style | Visual Output |
| :--- | :--- | :--- |
| `xray-ping` | `percentile` | Linear growth curve with P-markers. |
| `tcp-ping` | `percentile-log` | Log-scale curve (best for latency). |
| `xray-jit` | `dynamic` | Chronological bar chart of values. |
| `speed` | | |

- **Example**: `graph "MyNode" --plots "xray-ping:percentile-log,speed:dynamic"`
