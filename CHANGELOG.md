# Changelog — Energy Optimizer

All notable changes to the strategy layer (`energy_optimizer.py`) and the
tactical layer (`energy_optimizer_tactical.yaml`). The two are version-locked
and must be deployed as a matched pair. Versions tagged *(tactical: bump only)*
change nothing in the control contract (mode IDs / helper entities); the
tactical file only needs its version string updated to match.

## 2026.06.10j

Runs one strategic cycle on HA start / pyscript reload (@time_trigger("startup")) so the forecast sensor — a state.set entity that does not survive a restart — is republished within seconds instead of being absent on the dashboard until the next 15-min cron. A settle delay + SOC poll let inputs populate first. No tactical or contract changes.

_Tactical: version bump only — no control-contract change._

## 2026.06.10i

Two ALWAYS-visible INFO lines per run, independent of VERBOSE / HA log level: a "cycle start" line and a "cycle complete" line that reports the resulting strategy and whether the outlook markdown was written (✓/✗, threaded back from _log_24h_outlook — it states the real write result, not just that code finished). These bypass _dbg() deliberately. No tactical changes.

_Tactical: version bump only — no control-contract change._

## 2026.06.10h

Fixes "Unclosed client session" aiohttp errors. The session was created OUTSIDE the try and closed in the finally with a bare await; when task.unique cancelled an overlapping cycle, the CancelledError could abort that await and strand the open session (GC → error). Now both entry points use `async with ClientSession()` which closes deterministically even under cancellation, and task.unique runs BEFORE the session exists. No tactical/contract changes.

_Tactical: version bump only — no control-contract change._

## 2026.06.10g

Forecast attribute payload shrunk to stay under the HA recorder 16 KB cap even if the exclude rule is missing: only FUTURE rows are published (the card draws only those; executed history lives in the md/CSV/Influx), compact short keys (t/f/s/c/p/g/b/pr/so), and an optional FORECAST_SENSOR_STRIDE to thin slots. Still recommended to exclude the sensor from the recorder. CARD CHANGE: forecast filters now read short keys — redeploy energy_overview_card.yaml. No tactical change.

_Tactical: version bump only — no control-contract change._

## 2026.06.10f

Self-test service pyscript.energy_optimizer_self_test: probes every link of the chain (version/triggers/input sensors/file writes/state.set/InfluxDB), runs one real cycle and reports whether the forecast sensor exists afterwards — as a persistent notification + one INFO log block. If the service is not even listed under Developer Tools → Actions, the file is NOT LOADED: search the log for "Exception in </config/pyscript/ energy_optimizer" (a compile/load error, e.g. pyscript older than 1.4 lacking @pyscript_compile/task.executor → update pyscript via HACS). No tactical changes.

_Tactical: version bump only — no control-contract change._

## 2026.06.10e

Load banner: exactly ONE info line on (re)load with the running version — the deployment beacon. Per-slot price dumps and the in-log outlook table cannot be produced by ≥10c at default flags (the table code was removed in 10c); if they appear, an OLD copy is running. Remember: pyscript does NOT auto-reload on file changes (call the pyscript.reload service), and it loads EVERY .py in /config/pyscript — a stale backup copy runs IN PARALLEL and fights this script for the helpers. No tactical or contract changes.

_Tactical: version bump only — no control-contract change._

## 2026.06.10d

Truly quiet logging, independent of HA logger config: v-c only demoted messages to DEBUG, which still floods installs where `logger:` raises pyscript (or default) verbosity. All diagnostics now pass through _dbg() and are not emitted at all unless VERBOSE=True. The cycle summary logs at INFO only when the STRATEGY CHANGES. Warnings are rate-limited per source (_warn, one per WARN_COOLDOWN_MIN per key) so a persistent failure (e.g. InfluxDB down: 12+ warnings PER CYCLE before) warns once per window; errors stay unlimited. The EPEX state-trigger is debounced 3 min — the sensor STATE rolls every price slot and fired a full extra cycle right next to each cron one. No tactical changes.

_Tactical: version bump only — no control-contract change._

## 2026.06.10c

Quiet logging: routine per-cycle messages demoted to DEBUG (re-enable via logger: custom_components.pyscript .file.energy_optimizer: debug). Only the one-line cycle summary, rare events (emergency/recovery/manual run/ daily finalize) and warnings/errors reach the HA log; LOG_DEBUG defaults to False; the duplicated console outlook table was removed (the md file IS the record). Daily markdown rotation: per-day file renamed to energy-optimizer-YYYY-MM-DD.md, refreshed intraday and FINALIZED shortly after midnight as an executed-only record of the full day; the live outlook restarts fresh at the 00:00 cycle. Optional pruning via HISTORY_RETENTION_DAYS. No tactical/contract changes.

_Tactical: version bump only — no control-contract change._

## 2026.06.10b

Publishes sensor.energy_optimizer_forecast every cycle: the combined executed-history + forecast slot list as a `data` attribute (time/strategy/cons_w/pv_w/grid_w/batt_w/price_ct/ soc_pct/forecast-flag), for dashboard cards (plotly-graph reads it like the EPEX attribute data). Exclude this sensor from the recorder — its attributes are ~15 kB every 15 min. No tactical/contract changes.

_Tactical: version bump only — no control-contract change._

## 2026.06.10

LP rebuilt on 4 variable groups: PV-surplus charging is now an LP DECISION (y_pv) instead of a fixed cum_pv term — fixes PV surplus being double-counted in the SOC bounds (the LP could plan more late discharge than the battery would hold). Grid charging split off as y_g, with a linear SOC-ceiling surrogate (GRID_CHARGE_SOC_CEILING_PCT is now enforced, not just documented). Date-aware fallback prices (an EPEX outage no longer collapses the horizon to 1 h). Outlook + history simulate HOLD as the hardware executes it (full-load import, ALL PV→battery) instead of "PV offsets load". Per-slot anti-curtailment exception via a passive-SOC envelope. Weekday-correct consumption profile, cached per day, with a configurable quantile. Outlook keeps the last good table under a STALE banner instead of wiping it on a bad cycle. SOC emergency is edge-triggered with hysteresis and replans automatically on recovery. All file I/O moved off the event loop (task.executor + @pyscript_compile). Influx forecast SCHEMA CHANGE: `strategy` and `minutes_ahead` are FIELDS now and the only tag is `phase` — update Grafana queries. Fixed the mode-ID table in the config comment below (it documented an old, contradictory 4-mode scheme). Removed unused OPPORTUNITY_COST_WEIGHT / GRID_CHARGE_SOC_CHEAP_PCT.

## 2026.06.08

3-strategy model (FOLLOW_GRID/HOLD/GRID_CHARGE); data-driven horizon; pure cost LP; PV cost 4.5ct; terminal value = PV floor (fixes peak-charging); battery throughput cost; PV-surplus → FOLLOW_GRID (fixes high-price morning import); executed-history outlook + Why column + per-day archive.
