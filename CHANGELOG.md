# Changelog — Energy Optimizer

All notable changes to the Energy Optimizer system:
the strategy layer (`energy_optimizer.py`), the tactical layer
(`energy_optimizer_tactical.yaml`) and the dashboard card
(`energy_overview_card.yaml`). Strategy and tactical are version-locked and
must be deployed as a matched pair; versions tagged *(tactical: bump only)*
change nothing in the control contract (mode IDs / helper entities). The
dashboard card has its own revision track (it reads the strategy layer's
output but has no control role); the **Dashboard card** section at the end
records its changes.

## 2026.06.10m

Refines the AC adjustment (v…l) for this apartment's behaviour: cooling base temperature raised to 30 °C (cooling only needed above ~30 °C), and a consecutive-day **heat-soak** factor added. A single hot day barely warms the building; on the 2nd/3rd hot day it is heat-soaked and the AC works harder for the same outdoor temperature. The driver is the overnight low: a cool night sheds stored heat and resets, a warm night carries it forward. An accumulation index `A` is built from recent actual overnight minima (InfluxDB, `SOAK_LOOKBACK_DAYS` nights) extended by tonight's forecast low, decayed each night by `SOAK_DECAY`; `soak_mult = 1 + SOAK_GAIN·A` amplifies the FORECAST cooling-degrees only (raises today's projected load; the historical baseline is left unamplified, a deliberate, bounded simplification). Constants: `NIGHT_RESET_C` 20 °C, `SOAK_DECAY` 0.5, `SOAK_GAIN` 0.04 (≈×1.16 after one warm night, ≈×1.30 on a sustained heatwave — tune/measure to your building), night window 22:00–07:00. Disable via `AC_HEAT_SOAK_ENABLE`. No tactical or contract changes.

_Tactical: version bump only — no control-contract change._

## 2026.06.10l

Optional temperature-dependent AC adjustment of the consumption forecast (`AC_TEMP_ENABLE`, default on). On hot days the apartment needs cooling, but the historical profile already contains the AC load of the past sample days' weather — so the adjustment is by the per-hour *cooling-degree difference* between the Met.no forecast (`weather.forecast_home`, fetched via `weather.get_forecasts`) and the historical outdoor temperature (InfluxDB `outdoor_temperature`), not by raw temperature. `CDD(T)=max(0,T-AC_BASE_TEMP_C)`; `extra_W = clamp(AC_GAIN_W_PER_CDD · (CDD_forecast - CDD_hist), ±AC_MAX_BONUS_W)`. A forecast matching history changes nothing; hotter raises the load, cooler lowers it (removing baked-in AC); below the base temperature nothing accrues. Applied per hour before smoothing; any missing temperature data skips the adjustment for that cycle. Constants: `AC_BASE_TEMP_C` 22, `AC_GAIN_W_PER_CDD` 60 W/°C (tune/measure to your unit), `AC_MAX_BONUS_W` 1500. No tactical or contract changes.

_Tactical: version bump only — no control-contract change._

## 2026.06.10k

Smooths the historical consumption profile with a centered moving average (`CONSUMPTION_SMOOTH_SLOTS`, default 3 = ±1 slot / 45-min window) before it feeds the planner. The profile is a per-15-min quantile over only 4 same-weekday samples and was therefore noisy — a slot that saw a high-load event in 2 of 4 past weeks spiked while its neighbours stayed low (e.g. 130→743→99→1176 W in consecutive slots). That sawtooth made the chosen strategy flip-flop between FOLLOW_GRID and HOLD as the jagged load crossed the PV line, even though real house load does not swing that fast. Smoothing runs along time within each weekday, wraps across hour/day boundaries, and preserves the daily total; it changes only the input load series, no optimizer logic. Set `CONSUMPTION_SMOOTH_SLOTS = 1` to disable. No tactical or contract changes.

_Tactical: version bump only — no control-contract change._

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

---

## Dashboard card — `energy_overview_card.yaml`

The Plotly overview card. Its revisions are independent of the strategy/tactical
versions; each notes the minimum strategy version it requires.

### rev 4 — compact sensor (requires strategy ≥ v2026.06.10g)
Forecast traces updated for the slimmed `sensor.energy_optimizer_forecast`
payload: read compact keys (`r.t`, `r.c`, `r.p`, `r.so`) and no longer filter on
a per-row `forecast` flag (the sensor now publishes future-only rows, so every
row is a forecast). Without this change the dotted planned-consumption/PV/SoC
lines silently render nothing against a g+ sensor.

### rev 3 — price history
Price drawn as two joined traces sharing one legend entry (`legendgroup`): the
past from the recorder's state history (`extend_to_present`), the future from the
EPEX attribute data sliced to "now onward" so the two don't double-draw. Past
days no longer vanish from the plot. Hover unit overridden to `ct/kWh` (the
filter converts €→ct); planned traces given `W` / `%` hover units.

### rev 2 — hardened forecast filters
Forecast filters read the sensor's live attributes via `hass.states[...]` instead
of `meta`, with full guards, so a missing sensor renders empty instead of throwing
(and the read is independent of recorder exclusion).

### rev 1 — initial overview + planned traces
First version with dotted planned consumption, PV and SoC from
`sensor.energy_optimizer_forecast`. Fixes over the original card: 6h range button
(was 12 h), the now-line moved to the fixed invisible y3 axis (full height, dark-
theme visible, hover disabled), removed the non-option `default: null`, EPEX price
drawn stepped (`shape: hv`) with the +10.5 ct network fee, removed no-op identity
filters, `show_value` added so the power-trace labels render, and `period: auto`
on the statistics traces so the wide range buttons don't fetch a month of
5-minute buckets.
