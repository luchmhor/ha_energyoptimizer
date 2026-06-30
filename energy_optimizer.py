# pyscript/energy_optimizer.py
"""
Energy Optimizer — pyscript (HACS) — Strategic planning layer only.

╔══════════════════════════════════════════════════════════════════════════╗
║  VERSION: 2026.06.10m (strategy layer)                                     ║
║  Must be paired with tactical YAML of the SAME version. The two files form ║
║  one system: the strategy writes mode_id 0/1/2, the tactical YAML reads it.║
║  If the versions differ, redeploy BOTH and reload.                         ║
║  (2026.06.10a–m change nothing in the tactical contract — mode IDs and     ║
║  helper entities are unchanged — so the tactical YAML only needs its       ║
║  version string bumped to match.)                                          ║
║                                                                            ║
║  Requires pyscript ≥ 1.4 (task.executor + @pyscript_compile).              ║
║                                                                            ║
║  Changelog: see CHANGELOG.md (moved out of this header 2026.06.10j).       ║
╚══════════════════════════════════════════════════════════════════════════╝

Runs every 15 minutes (and on EPEX price update events).
Writes mode and setpoint to input_number helpers for the HA tactical automation.

Requires in configuration.yaml:
pyscript:
  allow_all_imports: true

input_number:
  energy_optimizer_setpoint:
    name: Energy Optimizer Setpoint
    min: -1200
    max: 1200
    step: 10
    unit_of_measurement: W
    icon: mdi:lightning-bolt
  energy_optimizer_mode_id:
    name: Energy Optimizer Mode ID
    min: 0
    max: 2
    step: 1
    icon: mdi:state-machine
    # 0=FOLLOW_GRID  1=HOLD  2=GRID_CHARGE      ← must match MODE_IDS below.
    # FIX 2026.06.10: this comment previously documented an OLD 4-mode scheme
    # (0=BALANCE 1=GRID_CHARGE 2=DISCHARGE 3=TRICKLE) that CONTRADICTED the
    # code. If your tactical YAML was ever (re)built from that table, HOLD
    # became grid-charge and GRID_CHARGE became discharge — rebuild it from
    # the table above.

input_text:
  energy_optimizer_mode:
    name: Energy Optimizer Mode
    max: 32
    icon: mdi:battery-charging
  energy_optimizer_reason:
    name: Energy Optimizer Reason
    max: 255
    icon: mdi:information-outline

command_line:
  - sensor:
      name: energy_optimizer_outlook
      command: "python3 -c \"import json; f=open('/config/www/energy_outlook.md'); print(json.dumps({'content': f.read()}))\""
      scan_interval: 1800
      value_template: "OK"
      json_attributes:
        - content

Only file needed:
  /config/pyscript/energy_optimizer.py

─── LP VARIABLE LAYOUT (4×N variables) ────────────────────────────────────
  x[t]     = discharge power (W)            domain [0, OUTPUT_MAX_W]
  y_pv[t]  = charge power from PV surplus   domain [0, pv_surplus[t]]
  y_g[t]   = charge power from the grid     domain [0, |OUTPUT_MIN_W|] (0 if
                                            ALLOW_GRID_CHARGE is False)
  g[t]     = grid-import slack (W)          domain [0, ∞)

  Net battery setpoint sent to inverter = x[t] - y_pv[t] - y_g[t]
  Positive sign → discharge (export to home bus)
  Negative sign → charge  (import from grid / PV)

  Energy balance per slot (Δt = 0.25 h):
    E[k+1] = E[k]
             - x[k] * Δt / η_dis                  (discharge depletes storage)
             + (y_pv[k] + y_g[k]) * Δt * η_chg    (charging fills storage)

  ALL charging — including passive PV absorption — is carried by y_pv/y_g.

  *** FIX 2026.06.10 (PV double-count) **********************************
  The previous formulation injected PV surplus into the SOC bounds as a
  fixed `cum_pv` term while ALSO letting the single charge variable y
  absorb the same surplus at zero grid cost (the slack constraint nets PV
  against load). With the terminal credit exceeding the wear cost, the
  solver always did so — the modeled battery gained the surplus twice and
  the lower SOC bound was too loose, so the plan could schedule more late
  discharge than the real battery would hold (it hit the floor early).
  Now y_pv is the ONLY path for PV surplus into the battery: bounded
  per-slot by the surplus, free at the point of use, and counted exactly
  once. Because the terminal credit (PV floor × η) exceeds the wear cost,
  the LP voluntarily absorbs surplus whenever headroom exists — matching
  the hardware's passive behaviour without a forced lower bound (which
  could go infeasible near full).
  **********************************************************************

  Objective: minimise net grid cost over the horizon, as a true cost optimizer.
    Σ_t  price[t]·Δt/1000·g[t]                            (real grid cost)
       + wear·Δt·(x[t] + y_pv[t] + y_g[t])                (per-throughput cost)
       − tv_price·(Σ (y_pv+y_g)·η_c − Σ x/η_d)·Δt/1000    (terminal value)
  where g linearises grid import via g ≥ load − pv + y_pv + y_g − x, g ≥ 0,
  and tv_price values energy still stored at the horizon end. Charge-cheap /
  discharge-expensive is DERIVED from these terms; there are no
  price-percentile bans on charging or discharging. Bounds carry only physical
  limits (hardware rate, grid-charge SoC ceiling) and no-export.

  *** FIX (energy-balance coupling, kept from 2026.06.08) ***************
  The cumulative SOC constraints include BOTH the charge and the discharge
  terms in each bound. An earlier version put only x in the lower bound and
  only y in the upper bound, which made the two cumulative balances
  independent. That silently forbade "charge cheap now → discharge expensive
  later" arbitrage within the horizon, so on a low starting SOC the solver
  could only ever discharge its initial reserve. With the coupled form the
  solver can buy in cheap slots and sell into peaks, which is the entire
  purpose here.
  **********************************************************************

  KNOWN APPROXIMATION (mode realisation): when the LP plans "idle" in a
  daylight slot with a PV DEFICIT (0 < pv < load), it costs that slot as
  "PV offsets load, battery flat". The hardware has no mode that realises
  this: HOLD imports the FULL load and routes ALL PV to the battery, and
  FOLLOW_GRID would discharge the deficit the LP wanted to keep. _decide_mode
  picks HOLD (battery preservation is what the LP asked for), so in such
  slots the real grid import is higher by pv·price and the battery gains
  pv·η_c the LP did not plan. The OUTLOOK and the EXECUTED HISTORY simulate
  the true HOLD behaviour (fix 2026.06.10), so the reporting is honest even
  though the LP's internal cost for these slots is slightly optimistic.
  Modelling it exactly would require per-mode binaries (MILP) — out of scope.
─────────────────────────────────────────────────────────────────────────────
"""

import aiohttp
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from scipy.optimize import linprog

# ════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════

# Version of this strategy layer. Must match the tactical YAML's version.
# Logged on every strategic run so the running version is visible in the log.
VERSION = "2026.06.10m"

USE_LP_OPTIMIZER = True

BATTERY_SIZE_WH     = 2760
OUTPUT_MIN_W        = -1200   # max charge  (negative convention: grid→battery)
OUTPUT_MAX_W        =  1200   # max discharge
BATTERY_FULL_PCT    =   98
BATTERY_TRICKLE_PCT =   96    # NOT used by this layer (historic trickle mode
BATTERY_TRICKLE_W   =   10    # was removed in the 3-strategy model); kept only
                              # so the constant block documents the hardware.
BATTERY_EMPTY_PCT   =   15
GRID_DEADZONE_W     =   10

BATTERY_CHARGE_EFF    = 0.95   # η_chg : energy stored per Wh drawn
BATTERY_DISCHARGE_EFF = 0.95   # η_dis : energy delivered per Wh taken from storage
# Round-trip = 0.95 × 0.95 = 0.9025 (~10 % total system loss)

NETWORK_FEE_CT_PER_KWH = 10.5

# ── Horizon ───────────────────────────────────────────────────────────────
# The LP plans over a DATA-DRIVEN horizon: as many 15-min slots as we have
# consistent data for ALL streams (grid price AND PV forecast; consumption is
# always available from the historical profile). Capped for tractability.
MAX_HORIZON_SLOTS = 192        # 48 h hard cap (192 × 15 min)
MIN_HORIZON_SLOTS = 4          # need at least 1 h of data to bother

# ── COST-OPTIMIZER PARAMETERS (LP) ────────────────────────────────────────
# The LP is a pure cost minimiser over the horizon. It needs only physical
# constraints + the price signal + ONE economic input below. It derives
# charge-cheap / discharge-expensive / hold-for-peak entirely from the math —
# there are no price-percentile bans, spread gates or separate "hold"
# heuristics. The three strategies are read directly off its plan.
#
# Refill / terminal-floor value of stored energy (€/kWh). NOTE on semantics
# (comment fixed 2026.06.10): PV is FREE at the point of use inside the LP —
# the slack constraint nets it against the load at zero cost, and y_pv absorbs
# surplus without a price. This constant does NOT price PV generation; it is
# the FLOOR value of energy in (or left in) the battery: leftover energy at
# the horizon end is worth at most what PV could refill it for. Effects:
#   • terminal floor → the LP never grid-charges above ~this merely to end
#     the horizon full (see TERMINAL_VALUE_REFERENCE below);
#   • combined with wear it sets the price below which grid charging is
#     worthwhile even without an upcoming peak (≈ 3.8 ct with the defaults).
PV_COST_CT = 4.5               # ct/kWh

# Battery throughput cost (ct/kWh of energy moved INTO or OUT OF the battery).
# This is a real cost (cell degradation per cycle) AND a necessary regulariser:
# with zero throughput cost the LP is indifferent to economically-pointless
# cycling and will, e.g., discharge at 17 ct then grid-charge back at 15 ct
# minutes later (a wash in the model, but a ~10% round-trip loss in reality and
# what looked like "charging at the morning peak"). A small cost makes the LP
# only charge/discharge when the price spread genuinely exceeds the wear +
# round-trip loss. Applied to all three legs (x, y_pv, y_g) — wear is
# source-agnostic, PV-charging cycles the cells too.
# Rough basis: replacement_cost / (cycles × usable_kWh × 2 legs). Keep it small
# but non-zero; ~0.3–0.6 ct/kWh per leg is typical for LFP.
BATTERY_THROUGHPUT_COST_CT = 0.5   # ct/kWh per leg (charge and discharge)

# Terminal value: what energy still stored at the END of the finite horizon is
# worth. This MUST be conservative. If it is set to a high reference (e.g. the
# horizon's p75), the LP treats "end the horizon full" as worth ~p75/kWh and
# will pay almost any sub-p75 price to charge — including buying a full battery
# during the EVENING PEAK or an expensive overnight tail, with no planned use
# for that energy. That is exactly the failure we must avoid.
#
# Valuing leftover energy at the PV-cost floor (4.5 ct) fixes it: stored energy
# is worth only what PV could refill it for, so the LP NEVER grid-charges above
# ~4.5 ct merely to end full. It still charges when there is a genuine
# within-horizon arbitrage (buy now, discharge into a real upcoming peak),
# because that value comes from the future grid price in the LP, not from this
# terminal term. The terminal term's only job is to stop gratuitous end-of-
# horizon dumping — nothing more.
#   "pv_cost" → value leftover energy at PV_COST_CT (4.5 ct).  [recommended]
#   "p25"     → slightly higher; mild hoarding bias.
#   "p75"/"p90"/"avg" → DO NOT USE without understanding the peak-charging risk.
TERMINAL_VALUE_REFERENCE = "pv_cost"

# Allow the LP to pay the grid to charge when cost-optimal. PV charging is
# always allowed (via y_pv) regardless.
ALLOW_GRID_CHARGE = True

ALLOW_EXPORT = False

PV_NAMEPLATE_WP = 1200   # nameplate peak power in Wp (informational only —
                         # not used by the strategy layer)

# Per-slot quantile of the 4 same-weekday historical samples used as the load
# forecast. 0.75 is a deliberate conservative high-side estimate — note that it
# systematically inflates planned load and therefore the value of stored
# energy, so the optimizer over-banks slightly. Set 0.5 (median) to A/B the
# bias against realised cost.
CONSUMPTION_QUANTILE = 0.75

# Smoothing of the per-slot consumption profile (2026.06.10k). The profile is a
# per-15-min quantile over only 4 same-weekday samples, so it is NOISY — a slot
# that happened to see a high-load event in 2 of 4 past weeks spikes while its
# neighbours stay low, producing a sawtooth (e.g. 130→743→99→1176 W in
# consecutive slots). That noise makes the chosen strategy flip-flop
# (FOLLOW_GRID/HOLD) between adjacent slots as the jagged load crosses the PV
# line, even though the house's real load does not swing that fast. A centered
# moving average over CONSUMPTION_SMOOTH_SLOTS quarter-hours (must be odd; 1 =
# off) calms this without biasing the daily total. Smoothing runs ALONG TIME
# within each weekday and wraps across hour/day boundaries. It does not change
# any optimizer logic — only the input load series.
CONSUMPTION_SMOOTH_SLOTS = 3   # 3 = ±1 slot (45-min window); 5 = ±2; 1 = off

# ── Temperature-dependent AC adjustment (2026.06.10l) ──────────────────────
# On hot days the apartment draws extra power for cooling. The historical
# consumption profile ALREADY contains the AC load of whatever the temperature
# was on the past sample days, so we must NOT simply "add AC because it is hot":
# that double-counts. Instead we adjust by the *cooling-degree difference*
# between the forecast day and the historical sample days, per hour:
#
#   CDD(T)   = max(0, T - AC_BASE_TEMP_C)              (cooling degrees)
#   ΔCDD(H)  = CDD(T_forecast(H)) - mean_k CDD(T_hist_k(H))
#   extra_W  = clamp(AC_GAIN_W_PER_CDD * ΔCDD(H), -AC_MAX_BONUS_W, AC_MAX_BONUS_W)
#   load'(slot in hour H) = max(0, load(slot) + extra_W)
#
# Properties: a forecast matching the historical average → no change; hotter
# than history → load increased; cooler → load DECREASED (correctly removing AC
# the profile baked in). Cooling-DEGREES (not raw temp) mean mild swings below
# the base add nothing. Per-HOUR alignment puts the afternoon heat on afternoon
# slots, not the cool morning. Forecast hourly temps come from Met.no via the
# weather.get_forecasts service (its hourly forecast is no longer in entity
# attributes since HA 2024.3); historical temps from InfluxDB (same shape as
# the consumption query). Applied AFTER smoothing so the AC term is not itself
# smeared. Any missing data (no forecast / no history) → adjustment skipped for
# that cycle, base profile used, one rate-limited warning. Tune AC_GAIN to your
# unit: roughly (AC electrical kW) / (indoor-outdoor ΔT at which it runs flat
# out) × 1000, but the honest way is to measure — compare a hot day's realised
# load against a mild day's at the same hours.
AC_TEMP_ENABLE     = True
AC_BASE_TEMP_C     = 30.0     # balance point; cooling demand accrues above this
                              # (this apartment needs cooling only above ~30°C)
AC_GAIN_W_PER_CDD  = 60.0     # extra watts per °C of cooling-degree DIFFERENCE
AC_MAX_BONUS_W     = 1500.0   # clamp on |adjustment| so a bad forecast can't explode load
E_WEATHER          = "weather.forecast_home"   # Met.no entity (hourly forecast)
INFLUX_TEMP_ENTITY = "outdoor_temperature"     # entity_id tag in InfluxDB
INFLUX_TEMP_UNIT   = "°C"                       # InfluxDB measurement (unit-named)

# ── Heat-soak / consecutive-day accumulation (2026.06.10m) ─────────────────
# A single hot day barely warms the building; on the 2nd/3rd consecutive hot
# day the structure is heat-soaked and the AC works harder for the SAME outdoor
# temperature. The driver is how well it cools OVERNIGHT: a cool night sheds the
# stored heat and resets, a warm night carries it forward. We build an
# accumulation index A from the recent overnight MINIMA (actual nights from
# InfluxDB) extended by tonight's forecast low, then amplify today's forecast
# cooling-degrees by (1 + SOAK_GAIN·A). Per the design choice, the heat-soak
# multiplier rides on the FORECAST term only (it raises today's projected
# load); the historical baseline is left unamplified. Minor, bounded
# consequence: a past sample day that was itself heat-soaked already has that
# AC in the profile, so on such days we slightly over-add — acceptable since
# heatwaves are the minority and the effect is small.
#
#   H(d)      = max(0, night_min(d) - NIGHT_RESET_C)     # nightly heat input
#   A         = Σ over nights, decayed:  A = A·SOAK_DECAY + H(d)
#   soak_mult = 1 + SOAK_GAIN · A                        # ≥ 1
#   extra_W(h)= clamp( AC_GAIN_W_PER_CDD ·
#                      (soak_mult·CDD(T_fc(h)) - CDD(T_hist(h))), ±AC_MAX_BONUS_W )
#
# Day 1 of heat (cool prior nights) → A≈0, soak_mult≈1 (mild, short cooling
# just above base). Day 3 with warm nights → A grows, soak_mult >1 (same
# afternoon temp draws more). A cool night decays A → building resets.
AC_HEAT_SOAK_ENABLE = True
NIGHT_RESET_C       = 20.0   # overnight min at/below this lets the building reset
SOAK_DECAY          = 0.5    # fraction of yesterday's accumulation carried over
SOAK_GAIN           = 0.04   # amplification per unit of accumulated heat index.
                              # CALIBRATION NOTE: with SOAK_DECAY 0.5 the steady
                              # accumulator for nights ΔT above reset saturates at
                              # A_max≈2·ΔT, so soak_mult_max≈1+SOAK_GAIN·2·ΔT. At
                              # 0.04 and warm 24°C nights (ΔT=4) that is ≈1.32 on a
                              # sustained heatwave, ≈1.16 after one warm night — a
                              # moderate, realistic boost. 0.15 would DOUBLE the AC
                              # term on a heatwave; tune to your building, measure
                              # if you can (compare day-3 vs day-1 afternoon load
                              # at equal outdoor temperature).
SOAK_LOOKBACK_DAYS  = 3      # recent ACTUAL nights pulled from InfluxDB
# Night window (local hours) over which the overnight minimum is taken.
NIGHT_HOUR_START    = 22     # 22:00 …
NIGHT_HOUR_END      = 7      # … 07:00 next morning

# SOC safety ceiling for GRID charging (not PV). Physical protection only — the
# economics are handled by the objective. ENFORCED since 2026.06.10 as a linear
# surrogate in the LP (see constraint (6) in _solve_optimal_schedule): the
# PV-less SOC trajectory E_now + Σ(grid charge − discharge) must stay ≤ this
# ceiling, so grid charging can never be the thing that pushes SOC into the top
# band. PV may still fill to BATTERY_FULL_PCT. Set equal to BATTERY_FULL_PCT to
# disable.
GRID_CHARGE_SOC_CEILING_PCT = 95

# ── Emergency SOC floor (edge-triggered, with hysteresis) ─────────────────
SOC_CRITICAL_PCT = 12.0   # force HOLD when SOC crosses BELOW this
SOC_RECOVER_PCT  = 15.0   # clear the emergency (and replan) at/above this

# ── Startup cycle timing (2026.06.10j) ────────────────────────────────────
# After HA (re)start the input entities are briefly 'unknown'; give them time
# to populate before the first post-boot strategic cycle so it uses real data
# instead of skipping. These only affect the one-shot startup run.
STARTUP_SETTLE_S = 30.0   # fixed delay after start before doing anything
SOC_WAIT_MAX_S   = 90.0   # then poll up to this long for a valid SOC…
SOC_WAIT_POLL_S  = 5.0    # …at this interval

# ── Legacy constant (heuristic fallback only; the LP ignores it) ───────────
# (2026.06.10: OPPORTUNITY_COST_WEIGHT and GRID_CHARGE_SOC_CHEAP_PCT were
#  removed — nothing referenced them — and the duplicate definitions of this
#  block were collapsed to one.)
GRID_CHARGE_SOC_BLOCK_PCT = 70

INFLUX_URL       = "http://localhost:8086/query"
INFLUX_DB        = "homeassistant"
INFLUX_USER      = "homeassistant"
INFLUX_PASS      = "hainflux!"
INFLUX_ENTITY    = "total_consumption"
INFLUX_ENTITY_PV = "ezhi_photovoltaic_power"
INFLUX_UNIT      = "W"

SOLAR_BLEND_HOURS = 2   # hours over which actuals scale-factor is blended out

E_BATTERY_SOC    = "sensor.ezhi_battery_state_of_charge"
E_BATTERY_POWER  = "sensor.ezhi_battery_power"
E_PRICE_DATA     = "sensor.epex_spot_data_total_price"
E_SOLAR_HOUR     = "sensor.solcast_pv_forecast_forecast_next_hour"
E_SOLAR_TODAY    = "sensor.solcast_pv_forecast_forecast_today"
E_SOLAR_TOMORROW = "sensor.solcast_pv_forecast_forecast_tomorrow"

E_MODE_ID  = "input_number.energy_optimizer_mode_id"
E_SETPOINT = "input_number.energy_optimizer_setpoint"

MODE_IDS = {
    "FOLLOW_GRID": 0,   # cover real load from battery (track to zero grid flow)
    "HOLD":        1,    # battery idle, import from grid, preserve charge
    "GRID_CHARGE": 2,    # actively charge the battery from the grid
}

E_STATUS_MODE   = "input_text.energy_optimizer_mode"
E_STATUS_REASON = "input_text.energy_optimizer_reason"

OUTLOOK_FILE      = "/config/www/energy_outlook.md"
FORECAST_CSV_FILE = "/config/www/energy_forecast.csv"
# Per-day execution history. Each strategic run records the strategy actually
# applied for the current 15-min slot here, so the outlook can show the realised
# part of the day (00:00 → now) alongside the forecast, and a full-day archive
# can be written as <HISTORY_DIR>/YYYY-MM-DD.md.
HISTORY_DIR       = "/config/www/energy_history"

# Marker prepended to the outlook file when a cycle fails: the last GOOD table
# is preserved underneath instead of being wiped (fix 2026.06.10).
OUTLOOK_STALE_PREFIX = "> ⚠️ **STALE**"

# Sensor created by this script (2026.06.10b): carries the combined executed
# history + forecast as a `data` attribute for dashboard cards (plotly-graph
# reads it the same way it reads the EPEX sensor's attribute data).
# RECOMMENDED in configuration.yaml — the attributes are ~15 kB and refresh
# every 15 min, which would bloat the recorder database:
#   recorder:
#     exclude:
#       entities:
#         - sensor.energy_optimizer_forecast
E_FORECAST_SENSOR = "sensor.energy_optimizer_forecast"
# Publish every Nth future slot in the sensor attribute (1 = every slot). The
# attribute must stay < 16 KB or the recorder refuses to store it (a warning,
# not a failure — the live state the card reads is unaffected). Future-only +
# compact keys keep a 48 h horizon well under that; raise to 2 only if you run
# a very long horizon AND cannot exclude the sensor from the recorder. The
# CSV / InfluxDB / markdown outputs always keep full resolution.
FORECAST_SENSOR_STRIDE = 1

def _history_json(day) -> str:
    return f"{HISTORY_DIR}/{day.strftime('%Y-%m-%d')}.json"

# 0 = keep daily files forever; N > 0 = delete daily .md/.json older than N
# days during the nightly finalize run.
HISTORY_RETENTION_DAYS = 0

def _history_md(day) -> str:
    # Rotated daily markdown (2026.06.10c): energy-optimizer-YYYY-MM-DD.md.
    # Refreshed on every strategic run during its day; overwritten once by
    # midnight_finalize() the night after with the executed-only record.
    return f"{HISTORY_DIR}/energy-optimizer-{day.strftime('%Y-%m-%d')}.md"

# ── Logging policy (2026.06.10d) ──────────────────────────────────────────
# The script SELF-GATES its diagnostics: every routine message goes through
# _dbg() and is NOT EMITTED AT ALL unless VERBOSE is True. This is deliberate:
# v2026.06.10c only demoted messages to DEBUG level, which still floods the
# main log on installs where `logger:` raises custom_components.pyscript (or
# the default) to debug/info — a common leftover from developing pyscript
# apps. Gating inside the script makes quietness independent of HA's logger
# configuration. What still reaches the HA log, at ANY logger setting:
#   * one INFO line when the STRATEGY CHANGES (not every cycle),
#   * rare events: SOC emergency + recovery, manual run, daily finalize,
#   * warnings — rate-limited via _warn() to one per source per
#     WARN_COOLDOWN_MIN, so a persistent failure (e.g. InfluxDB unreachable,
#     which previously produced 12+ warnings PER CYCLE) warns once per
#     window instead of storming — and all errors (never limited).
# Set VERBOSE = True to restore full diagnostics; they are emitted at INFO
# level, so they are visible WITHOUT touching configuration.yaml. LOG_DEBUG
# additionally gates the very large data dumps (per-slot prices, per-hour
# PV) and requires VERBOSE as well.
VERBOSE   = False
LOG_DEBUG = False
WARN_COOLDOWN_MIN = 30   # minutes between repeated warnings of the same kind

# Timezone. Constructed LAZILY (not at module load) to avoid a blocking
# zoneinfo file read inside the event loop during script load — Home Assistant
# flags that as a blocking I/O violation (pyscript issue #687). `_ensure_tz()`
# is called at the top of every entry point, so TZ is always set before use.
TIMEZONE_NAME = "Europe/Vienna"
TZ = None

def _ensure_tz():
    global TZ
    if TZ is None:
        TZ = ZoneInfo(TIMEZONE_NAME)
    return TZ

# Shared context (price quantiles, last schedule, caches, flags) persisted
# across cycles.
_ctx: dict = {
    "p25": 0.10,
    "p75": 0.20,
    "last_schedule": [],
    "cons_cache": None,      # {"day": date, "profile": {(wd,h,q): W}}
    "soc_emergency": False,  # set by on_soc_critical, cleared on recovery
}


def _dbg(msg: str):
    """Diagnostic message. Emitted only when VERBOSE is True (then at INFO
    level, so no `logger:` configuration is needed to see it). With VERBOSE
    False nothing is emitted, regardless of HA's logger settings."""
    if VERBOSE:
        log.info(msg)


def _warn(key: str, msg: str):
    """Rate-limited warning: at most one per WARN_COOLDOWN_MIN per key, so a
    persistent failure condition warns once per window instead of storming the
    log every cycle. Suppressed repeats are still visible under VERBOSE.
    log.error sites are intentionally NOT limited."""
    now_ts = datetime.now(timezone.utc).timestamp()
    stamps = _ctx.setdefault("warn_ts", {})
    if now_ts - stamps.get(key, 0.0) >= WARN_COOLDOWN_MIN * 60:
        stamps[key] = now_ts
        log.warning(msg)
    else:
        _dbg(f"(warning suppressed [{key}]) {msg}")

# ════════════════════════════════════════════════════════════════════════════
# BLOCKING FILE I/O PRIMITIVES — compiled, run via task.executor
# ════════════════════════════════════════════════════════════════════════════
# FIX 2026.06.10: the old _write_text_file ran os.open/write/fsync directly in
# the event loop (the same class of violation the lazy-TZ workaround above
# exists for). These @pyscript_compile helpers are true CPython functions, so
# task.executor can run them on a worker thread. They must not use any
# pyscript builtins (log, state, …) — they return/raise and the async wrappers
# handle logging.

@pyscript_compile
def _read_text_file_blocking(path: str) -> str:
    fh = open(path, "r", encoding="utf-8")
    try:
        return fh.read()
    finally:
        fh.close()


@pyscript_compile
def _read_json_file_blocking(path: str):
    import json
    fh = open(path, "r", encoding="utf-8")
    try:
        return json.load(fh)
    finally:
        fh.close()


@pyscript_compile
def _write_text_file_blocking(path: str, content: str):
    import os
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


async def _write_text_file(path: str, content: str) -> bool:
    """Non-blocking text write (executor thread). Returns True on success."""
    try:
        await task.executor(_write_text_file_blocking, path, content)
        return True
    except Exception as exc:
        _warn(f"write:{path}", f"Could not write {path}: {exc}")
        return False


# ════════════════════════════════════════════════════════════════════════════
# INFLUXDB HELPERS
# ════════════════════════════════════════════════════════════════════════════

async def _influx_query(q: str, session: aiohttp.ClientSession) -> dict:
    async with session.get(
        INFLUX_URL,
        params={"db": INFLUX_DB, "u": INFLUX_USER, "p": INFLUX_PASS, "q": q},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


async def _fetch_historical_hourly_temp(session) -> dict:
    """Return {(weekday, hour): mean_outdoor_C} averaged over the same past
    same-weekday days the consumption profile uses (4 weeks back, today + 2).
    Mirrors the consumption query shape. Empty dict on any failure."""
    now   = datetime.now(TZ)
    today = now.date()
    accum: dict = {}
    weekdays_done = []
    for day_offset in range(0, 3):
        target = today + timedelta(days=day_offset)
        wd     = target.weekday()
        if wd in weekdays_done:
            continue
        weekdays_done.append(wd)
        for week_back in range(1, 5):
            sample_day = target - timedelta(weeks=week_back)
            day_start  = datetime(sample_day.year, sample_day.month,
                                  sample_day.day, 0, 0, 0, tzinfo=TZ)
            day_end    = day_start + timedelta(days=1)
            s_utc = day_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            e_utc = day_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            q = (
                f'SELECT mean("value") FROM "{INFLUX_TEMP_UNIT}" '
                f"WHERE \"entity_id\" = '{INFLUX_TEMP_ENTITY}' "
                f"AND time >= '{s_utc}' AND time < '{e_utc}' "
                f"GROUP BY time(1h) fill(previous)"
            )
            try:
                data   = await _influx_query(q, session)
                series = data.get("results", [{}])[0].get("series", [])
                if not series:
                    continue
                cols  = series[0]["columns"]
                t_idx = cols.index("time"); m_idx = cols.index("mean")
                for row in series[0].get("values", []):
                    if row[m_idx] is None:
                        continue
                    t_local = datetime.fromisoformat(
                        row[t_idx].replace("Z", "+00:00")).astimezone(TZ)
                    accum.setdefault((wd, t_local.hour), []).append(float(row[m_idx]))
            except Exception as exc:
                _warn("temp_hist", f"Temp history query error (wd {wd}, -{week_back}w): {exc}")
    return {k: sum(v) / len(v) for k, v in accum.items() if v}


async def _fetch_forecast_hourly_temp() -> dict:
    """Return {datetime(hour-truncated, TZ): forecast_C} from Met.no via the
    weather.get_forecasts service (hourly). Since HA 2024.3 the hourly forecast
    is only available through this service, not entity attributes. Empty dict on
    any failure."""
    out: dict = {}
    try:
        # pyscript exposes services as callable functions; weather.get_forecasts
        # returns response data when called with return_response=True. (Since HA
        # 2024.3 the hourly forecast is only available via this service, not via
        # entity attributes.)
        resp = await weather.get_forecasts(
            entity_id=E_WEATHER, type="hourly",
            blocking=True, return_response=True,
        )
        # resp is keyed by entity_id → {"forecast": [ {datetime, temperature, …}, … ]}
        entry = (resp or {}).get(E_WEATHER, {})
        for fc in entry.get("forecast", []):
            t_raw = fc.get("datetime")
            temp  = fc.get("temperature")
            if t_raw is None or temp is None:
                continue
            t = datetime.fromisoformat(str(t_raw))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            t = t.astimezone(TZ).replace(minute=0, second=0, microsecond=0)
            out[t] = float(temp)
    except Exception as exc:
        _warn("temp_forecast", f"Met.no forecast fetch failed: {exc}")
    return out


async def _fetch_recent_night_minima(session) -> dict:
    """Return {date: overnight_min_C} for the last SOAK_LOOKBACK_DAYS nights from
    InfluxDB actuals. A 'night' spanning NIGHT_HOUR_START→NIGHT_HOUR_END (e.g.
    22:00→07:00) is attributed to the date it ENDS on (the morning). Empty on
    failure."""
    now   = datetime.now(TZ)
    out: dict = {}
    for back in range(1, SOAK_LOOKBACK_DAYS + 1):
        morning = (now - timedelta(days=back - 1)).date()   # night ending this morning
        # window = previous day NIGHT_HOUR_START → this morning NIGHT_HOUR_END
        start_local = datetime(morning.year, morning.month, morning.day,
                               0, 0, tzinfo=TZ) - timedelta(days=1) \
                      + timedelta(hours=NIGHT_HOUR_START)
        end_local   = datetime(morning.year, morning.month, morning.day,
                               NIGHT_HOUR_END, 0, tzinfo=TZ)
        s_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        e_utc = end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        q = (
            f'SELECT min("value") FROM "{INFLUX_TEMP_UNIT}" '
            f"WHERE \"entity_id\" = '{INFLUX_TEMP_ENTITY}' "
            f"AND time >= '{s_utc}' AND time < '{e_utc}'"
        )
        try:
            data   = await _influx_query(q, session)
            series = data.get("results", [{}])[0].get("series", [])
            if not series:
                continue
            cols = series[0]["columns"]; m_idx = cols.index("min")
            vals = series[0].get("values", [])
            if vals and vals[0][m_idx] is not None:
                out[morning] = float(vals[0][m_idx])
        except Exception as exc:
            _warn("night_min", f"Night-min query error (-{back}d): {exc}")
    return out


def _forecast_tonight_min(fc_temp: dict) -> float:
    """Overnight minimum of TONIGHT from the hourly forecast (NIGHT_HOUR_START
    tonight → NIGHT_HOUR_END tomorrow). Returns None if not covered."""
    now = datetime.now(TZ)
    start = now.replace(hour=NIGHT_HOUR_START, minute=0, second=0, microsecond=0)
    if now.hour < NIGHT_HOUR_END:
        # we are already in the small hours: tonight's window started yesterday
        start -= timedelta(days=1)
    end = (start + timedelta(days=1)).replace(hour=NIGHT_HOUR_END)
    mins = [t_c for dt, t_c in fc_temp.items() if start <= dt < end]
    return min(mins) if mins else None


def _heat_soak_multiplier(night_minima: dict, tonight_min) -> float:
    """Accumulate the heat-soak index over recent actual nights (oldest→newest)
    plus tonight's forecast, then return soak_mult = 1 + SOAK_GAIN·A. With no
    data, returns 1.0 (no amplification)."""
    if not AC_HEAT_SOAK_ENABLE:
        return 1.0
    def _h(tmin):
        return max(0.0, tmin - NIGHT_RESET_C)
    # ordered list of (date, min) oldest→newest from actuals, then tonight
    nights = sorted(night_minima.items())          # [(date, min), …] oldest first
    seq = [v for _, v in nights]
    if tonight_min is not None:
        seq.append(tonight_min)
    if not seq:
        return 1.0
    A = 0.0
    for tmin in seq:
        A = A * SOAK_DECAY + _h(tmin)
    mult = 1.0 + SOAK_GAIN * A
    _dbg(f"Heat-soak: {len(seq)} nights, A={A:.1f}, soak_mult={mult:.2f}")
    return mult


def _apply_ac_adjustment(profile: dict, hist_temp: dict, fc_temp: dict,
                         soak_mult: float = 1.0) -> dict:
    """Adjust the (weekday, hour, quarter) consumption profile by the per-hour
    cooling-degree DIFFERENCE between the forecast and the historical sample
    days. See the constant block for the rationale (avoids double-counting the
    AC already baked into the historical profile). Returns a NEW dict. If the
    needed temperature data is absent the profile is returned unchanged."""
    if not AC_TEMP_ENABLE or not profile or not fc_temp or not hist_temp:
        return profile

    def _cdd(t):
        return max(0.0, t - AC_BASE_TEMP_C)

    # Map forecast datetimes onto (weekday, hour) so they align with the profile.
    fc_by_wh = {}
    for dt, t in fc_temp.items():
        fc_by_wh[(dt.weekday(), dt.hour)] = t

    out = dict(profile)
    n_adj = 0
    for (wd, h, q), base in profile.items():
        tf = fc_by_wh.get((wd, h))
        th = hist_temp.get((wd, h))
        if tf is None or th is None:
            continue
        # Heat-soak amplifies the FORECAST cooling-degrees only (raises today's
        # projected load); the historical baseline term is left unamplified.
        d_cdd  = soak_mult * _cdd(tf) - _cdd(th)
        extra  = AC_GAIN_W_PER_CDD * d_cdd
        extra  = max(-AC_MAX_BONUS_W, min(AC_MAX_BONUS_W, extra))
        out[(wd, h, q)] = max(0.0, base + extra)
        if abs(extra) > 1.0:
            n_adj += 1
    _dbg(f"AC adjustment applied to {n_adj} slots "
         f"(base {AC_BASE_TEMP_C:.0f}°C, gain {AC_GAIN_W_PER_CDD:.0f} W/°C, "
         f"soak_mult {soak_mult:.2f})")
    return out


def _smooth_consumption_profile(profile: dict) -> dict:
    """Centered moving average of the (weekday, hour, quarter)-keyed consumption
    profile along the time axis, within each weekday, wrapping across hour and
    day-of-week boundaries (slot (wd,h,3) is followed by (wd,h+1,0); the last
    quarter of a weekday wraps to the first quarter of the same weekday — a
    cyclic 24 h day, which is what the planner sees). Window =
    CONSUMPTION_SMOOTH_SLOTS (odd). Missing neighbours are skipped, so the
    average is over whatever real slots exist. Returns a NEW dict; the daily
    total per weekday is preserved up to edge effects of skipped slots."""
    w = CONSUMPTION_SMOOTH_SLOTS
    if w <= 1 or not profile:
        return profile
    half = w // 2
    # Index each weekday's 96 quarter-of-day slots for O(1) neighbour lookup.
    # qod = hour*4 + quarter, 0..95, cyclic within the weekday.
    by_wd = {}
    for (wd, h, q), v in profile.items():
        by_wd.setdefault(wd, {})[h * 4 + q] = v
    out = {}
    for (wd, h, q), _ in profile.items():
        qod = h * 4 + q
        acc, n = 0.0, 0
        for d in range(-half, half + 1):
            nb = (qod + d) % 96
            val = by_wd[wd].get(nb)
            if val is not None:
                acc += val
                n += 1
        out[(wd, h, q)] = acc / n if n else profile[(wd, h, q)]
    return out


async def _fetch_historical_consumption(session: aiohttp.ClientSession) -> dict:
    """
    Return {(weekday, hour, quarter): watts} for every weekday the planning
    horizon can touch (today + 2 days → up to 3 distinct weekdays), each built
    from that weekday's past 4 samples at the per-slot CONSUMPTION_QUANTILE.

    FIX 2026.06.10 (weekday-correct profile): the profile used to be keyed by
    (hour, quarter) from TODAY's weekday only, so any horizon crossing midnight
    planned tomorrow's morning with today's weekday pattern (e.g. a Monday
    morning from a Sunday profile). Each slot now gets its own weekday.

    The result is cached for the rest of the calendar day (the source data —
    whole past weeks — cannot change intraday), so the up-to-12 InfluxDB
    queries run once per day instead of every 15 minutes.
    """
    now   = datetime.now(TZ)
    today = now.date()

    cache = _ctx.get("cons_cache") or {}
    if cache.get("day") == today and cache.get("profile"):
        return cache["profile"]

    accum: dict = {}
    weekdays_done = []
    for day_offset in range(0, 3):
        target = today + timedelta(days=day_offset)
        wd     = target.weekday()
        if wd in weekdays_done:
            continue
        weekdays_done.append(wd)
        for week_back in range(1, 5):
            sample_day = target - timedelta(weeks=week_back)
            day_start  = datetime(sample_day.year, sample_day.month,
                                  sample_day.day, 0, 0, 0, tzinfo=TZ)
            day_end    = day_start + timedelta(days=1)
            s_utc = day_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            e_utc = day_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            q = (
                f'SELECT mean("value") FROM "{INFLUX_UNIT}" '
                f"WHERE \"entity_id\" = '{INFLUX_ENTITY}' "
                f"AND time >= '{s_utc}' AND time < '{e_utc}' "
                f"GROUP BY time(15m) fill(previous)"
            )
            try:
                data   = await _influx_query(q, session)
                series = data.get("results", [{}])[0].get("series", [])
                if not series:
                    continue
                cols     = series[0]["columns"]
                t_idx    = cols.index("time")
                mean_idx = cols.index("mean")
                for row in series[0].get("values", []):
                    if row[mean_idx] is None:
                        continue
                    t_local = datetime.fromisoformat(
                        row[t_idx].replace("Z", "+00:00")
                    ).astimezone(TZ)
                    key = (wd, t_local.hour, t_local.minute // 15)
                    accum.setdefault(key, []).append(row[mean_idx])
            except Exception as exc:
                _warn("influx_consumption", f"InfluxDB query error (wd {wd}, week -{week_back}): {exc}")

    if not accum:
        _warn("influx_consumption", "No InfluxDB data — using fallback consumption profile")
        return _fallback_consumption()

    result: dict = {}
    for k, values in accum.items():
        if not values:
            continue
        sorted_vals = sorted(values)
        idx = max(0, min(len(sorted_vals) - 1,
                         int(CONSUMPTION_QUANTILE * (len(sorted_vals) - 1))))
        result[k] = sorted_vals[idx]

    # Temperature-dependent AC adjustment (2026.06.10l), BEFORE smoothing so the
    # per-hour cooling-degree steps get softened into realistic ramps. Needs
    # both historical (InfluxDB) and forecast (Met.no) hourly temps; if either
    # is unavailable the profile is returned unchanged.
    if AC_TEMP_ENABLE:
        try:
            hist_temp = await _fetch_historical_hourly_temp(session)
            fc_temp   = await _fetch_forecast_hourly_temp()
            if hist_temp and fc_temp:
                # Heat-soak multiplier from recent actual nights + tonight forecast.
                soak_mult = 1.0
                if AC_HEAT_SOAK_ENABLE:
                    night_min   = await _fetch_recent_night_minima(session)
                    tonight_min = _forecast_tonight_min(fc_temp)
                    soak_mult   = _heat_soak_multiplier(night_min, tonight_min)
                result = _apply_ac_adjustment(result, hist_temp, fc_temp, soak_mult)
            else:
                _warn("ac_temp_data",
                      "AC adjustment skipped: missing temp history or forecast")
        except Exception as exc:
            _warn("ac_temp", f"AC adjustment error: {exc}")

    if CONSUMPTION_SMOOTH_SLOTS > 1:
        result = _smooth_consumption_profile(result)

    _ctx["cons_cache"] = {"day": today, "profile": result}
    _dbg(
        f"Consumption profile rebuilt: weekdays {weekdays_done}, "
        f"{len(result)} slots, q{int(CONSUMPTION_QUANTILE * 100)}, "
        f"smooth={CONSUMPTION_SMOOTH_SLOTS}, ac={AC_TEMP_ENABLE} (cached for today)"
    )
    return result


def _fallback_consumption() -> dict:
    hourly = [
        150, 150, 150, 150, 150, 150,
        600, 600, 600,
        350, 350, 350, 350, 350,
        350, 350, 350, 350,
        700, 700, 700, 700, 700,
        300, 300,
    ]
    out: dict = {}
    for wd in range(7):
        for h in range(24):
            for q in range(4):
                out[(wd, h, q)] = hourly[h]
    return out


# ════════════════════════════════════════════════════════════════════════════
# PV ACTUALS (InfluxDB)
# ════════════════════════════════════════════════════════════════════════════

async def _get_solar_actuals(session: aiohttp.ClientSession) -> dict:
    """Return {hour: watts} for hours that have already passed today."""
    now       = datetime.now(TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    s_utc = day_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    e_utc = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = (
        f'SELECT mean("value") FROM "{INFLUX_UNIT}" '
        f"WHERE \"entity_id\" = '{INFLUX_ENTITY_PV}' "
        f"AND time >= '{s_utc}' AND time < '{e_utc}' "
        f"GROUP BY time(1h) fill(previous)"
    )
    actuals: dict = {}
    try:
        data   = await _influx_query(q, session)
        series = data.get("results", [{}])[0].get("series", [])
        if not series:
            _warn("pv_actuals", "No actual PV data from InfluxDB")
            return actuals
        cols     = series[0]["columns"]
        t_idx    = cols.index("time")
        mean_idx = cols.index("mean")
        for row in series[0].get("values", []):
            if row[mean_idx] is None:
                continue
            t_local = datetime.fromisoformat(
                row[t_idx].replace("Z", "+00:00")
            ).astimezone(TZ)
            actuals[t_local.hour] = float(row[mean_idx])
        _dbg(f"PV actuals loaded: {len(actuals)} hours")
        if LOG_DEBUG:
            for h in sorted(actuals):
                _dbg(f"  pv actual {h:02d}:00 = {actuals[h]:.0f}W")
    except Exception as exc:
        _warn("pv_actuals", f"PV actuals fetch error: {exc}")
    return actuals


# ════════════════════════════════════════════════════════════════════════════
# SOLAR FORECAST (Solcast + actuals blend)
# ════════════════════════════════════════════════════════════════════════════

def _get_solar_forecast(actuals: dict) -> dict:
    """
    Build a per-hour solar estimate blending actuals with the Solcast forecast.

    *** FIX (today/tomorrow collision) ***********************************
    The forecast is now keyed by an absolute hour-truncated datetime instead of
    by hour-of-day. Previously both `forecast` and `solar` were keyed by
    hour-of-day (0–23), so the "tomorrow" pass overwrote "today" for every hour
    they share. For any horizon that runs past midnight (i.e. almost always),
    the remaining hours of *today* were served tomorrow's forecast, and the
    PV bias scale-factor compared today's actuals against tomorrow's forecast.
    Keying by absolute datetime keeps the two days distinct.
    **********************************************************************

    Returns {datetime(hour-truncated, TZ): watts}.
    Current hour        → 50 % actual + 50 % scaled forecast
    Next BLEND_HOURS    → linear ramp scaled → pure forecast
    Beyond blend window → pure Solcast forecast
    """
    now      = datetime.now(TZ)
    today    = now.date()
    tomorrow = (now + timedelta(days=1)).date()

    def _parse_hourly(fl, filter_date):
        result: dict = {}
        for entry in fl:
            t_raw = entry.get("period_start")
            pv_kw = float(entry.get("pv_estimate") or 0)
            if t_raw is None:
                continue
            if isinstance(t_raw, str):
                t = datetime.fromisoformat(t_raw)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                t = t.astimezone(TZ)
            else:
                try:
                    t = t_raw.astimezone(TZ)
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc).astimezone(TZ)
                except Exception:
                    t = datetime(*t_raw.timetuple()[:6], tzinfo=timezone.utc).astimezone(TZ)
            if t.date() != filter_date:
                continue
            result[t.hour] = pv_kw * 1000.0
        return result

    # forecast keyed by absolute (date, hour) so today and tomorrow don't collide
    forecast: dict = {}
    try:
        attrs = state.getattr(E_SOLAR_TODAY) or {}
        fl    = attrs.get("detailedHourly") or []
        for h, w in _parse_hourly(fl, today).items():
            forecast[(today, h)] = w
        # NB: pyscript's interpreter has no generator expressions — use a list comp.
        n_today = len([k for k in forecast if k[0] == today])
        _dbg(f"Solcast today: {n_today} hours loaded")
    except Exception as exc:
        _warn("solcast", f"Solar today error: {exc}")

    try:
        attrs  = state.getattr(E_SOLAR_TOMORROW) or {}
        fl     = attrs.get("detailedHourly") or []
        parsed = _parse_hourly(fl, tomorrow)
        for h, w in parsed.items():
            forecast[(tomorrow, h)] = w
        _dbg(f"Solcast tomorrow: {len(parsed)} hours added")
    except Exception as exc:
        _warn("solcast", f"Solar tomorrow error: {exc}")

    # Derive a scale factor from the last 2 completed hours of TODAY where
    # Solcast predicted meaningfully (>50 W) so we can correct systematic bias.
    scale = 1.0
    comparison_hours = [
        actuals[h] / forecast[(today, h)]
        for h in range(max(0, now.hour - 2), now.hour)
        if h in actuals and (today, h) in forecast and forecast[(today, h)] > 50
    ]
    if comparison_hours:
        raw_scale = sum(comparison_hours) / len(comparison_hours)
        scale     = max(0.3, min(2.0, raw_scale))
        _dbg(f"PV scale factor: {scale:.2f} (raw {raw_scale:.2f}, {len(comparison_hours)} hours)")
    else:
        _dbg("PV scale factor: no comparison hours, using 1.0")

    # Build solar keyed by absolute hour-truncated datetime across the horizon
    solar: dict = {}
    if forecast:
        base       = now.replace(minute=0, second=0, microsecond=0)
        span_hours = (MAX_HORIZON_SLOTS * 15) // 60 + 2   # cover full horizon + margin
        for hours_ahead in range(0, span_hours):
            slot_dt = base + timedelta(hours=hours_ahead)
            key     = (slot_dt.date(), slot_dt.hour)
            fc      = forecast.get(key, 0.0)

            if hours_ahead == 0:
                scaled     = fc * scale
                actual_val = actuals.get(slot_dt.hour) if slot_dt.date() == today else None
                solar[slot_dt] = (0.5 * actual_val + 0.5 * scaled) if actual_val is not None else scaled
            elif hours_ahead <= SOLAR_BLEND_HOURS:
                blend_w        = hours_ahead / SOLAR_BLEND_HOURS
                solar[slot_dt] = (1.0 - blend_w) * (fc * scale) + blend_w * fc
            else:
                solar[slot_dt] = fc

        if solar:
            peak = max(solar, key=solar.get)
            _dbg(f"Solar blended: {len(solar)} hours, peak {solar[peak]:.0f}W at {peak:%H:%M}")
            if LOG_DEBUG:
                for k in sorted(solar):
                    if solar[k] > 0:
                        a_str = f" actual={actuals[k.hour]:.0f}W" if (k.date() == today and k.hour in actuals) else ""
                        f_str = f" fc={forecast.get((k.date(), k.hour), 0):.0f}W"
                        _dbg(f"  solar {k:%Y-%m-%d %H:%M} = {solar[k]:.0f}W{a_str}{f_str}")
        return solar

    # Scalar last-resort fallback
    try:
        val = float(state.get(E_SOLAR_HOUR) or 0)
        solar[now.replace(minute=0, second=0, microsecond=0)] = val
        _warn("solcast_fallback", f"Solcast fallback scalar: {val}W for current hour only")
    except Exception as exc:
        _warn("solcast_fallback", f"Solar scalar fallback error: {exc}")
    return solar


# ════════════════════════════════════════════════════════════════════════════
# SPOT PRICES
# ════════════════════════════════════════════════════════════════════════════

def _fallback_prices() -> dict:
    """
    FIX 2026.06.10: keyed by (date, hour, quarter) for the next 3 calendar days,
    matching the real EPEX keys. The old fallback used (hour, quarter) keys,
    which _compute_horizon never matched — an EPEX outage therefore collapsed
    the horizon to MIN_HORIZON_SLOTS (1 h) and the carefully shaped 24 h curve
    below was never actually planned over.
    """
    hourly_ct = {
        0: 8.0,  1: 7.5,  2: 7.0,  3: 6.5,  4: 6.5,  5: 7.0,
        6: 18.0, 7: 22.0, 8: 20.0, 9: 15.0, 10: 11.0, 11: 8.0,
        12: 6.0, 13: 6.0, 14: 7.0, 15: 9.0,  16: 14.0, 17: 22.0,
        18: 26.0, 19: 28.0, 20: 24.0, 21: 18.0, 22: 13.0, 23: 10.0,
    }
    now = datetime.now(TZ)
    out: dict = {}
    for day_offset in range(0, 3):     # today + 2 days covers the 48 h cap
        d = (now + timedelta(days=day_offset)).date()
        for h, ct in hourly_ct.items():
            for q in range(4):
                out[(d, h, q)] = (ct + NETWORK_FEE_CT_PER_KWH) / 100.0
    return out


def _get_spot_prices() -> dict:
    prices: dict = {}
    try:
        raw_attrs = state.getattr(E_PRICE_DATA) or {}
        data      = raw_attrs.get("data", [])
        _dbg(f"EPEX data slots found: {len(data)}")
        if data:
            _dbg(f"EPEX first entry: {data[0]}")
        for entry in data:
            t    = datetime.fromisoformat(entry["start_time"]).astimezone(TZ)
            epex = float(entry["price_per_kwh"])
            # FIX: key by (date, hour, quarter), NOT (hour, quarter). EPEX
            # publishes today AND tomorrow; an hour-of-day key let tomorrow's
            # price overwrite today's for the same clock hour, collapsing the
            # day's price spread the LP needs (e.g. an 8.5 ct midday trough and
            # a 23 ct evening peak both read as tomorrow's value → no charge).
            prices[(t.date(), t.hour, t.minute // 15)] = epex + NETWORK_FEE_CT_PER_KWH / 100.0
    except Exception as exc:
        log.error(f"Spot price parse error: {exc}")

    if not prices:
        _warn("epex_fallback", "EPEX data unavailable — using fallback price curve")
        return _fallback_prices()
    return prices


# ════════════════════════════════════════════════════════════════════════════
# SCHEDULE BUILDER
# ════════════════════════════════════════════════════════════════════════════

def _compute_horizon(now, solar: dict, prices: dict) -> int:
    """
    Plan ahead only as far as ALL data streams are consistently available:
    a real grid PRICE and a real PV FORECAST entry for each slot. (Consumption
    comes from the historical profile and is always available, so it never
    limits the horizon.) Returns the number of consecutive 15-min slots from
    `now` for which both exist, clamped to [MIN_HORIZON_SLOTS, MAX_HORIZON_SLOTS].
    Price keys are (date, hour, quarter) — both EPEX and fallback (2026.06.10).
    """
    n = 0
    for i in range(MAX_HORIZON_SLOTS):
        t          = now + timedelta(minutes=15 * i)
        slot_hour  = t.replace(minute=0, second=0, microsecond=0)
        has_price  = (t.date(), t.hour, t.minute // 15) in prices
        has_solar  = slot_hour in solar          # night = explicit 0.0 entry, still "present"
        if not (has_price and has_solar):
            break
        n += 1
    return max(MIN_HORIZON_SLOTS, min(MAX_HORIZON_SLOTS, n))


def _build_schedule(consumption: dict, solar: dict, prices: dict) -> list:
    now    = datetime.now(TZ)
    minute = (now.minute // 15) * 15
    now    = now.replace(minute=minute, second=0, microsecond=0)

    horizon = _compute_horizon(now, solar, prices)
    _dbg(
        f"Planning horizon: {horizon} slots ({horizon*15/60:.1f}h) — "
        f"limited by the shortest of price/PV-forecast coverage."
    )

    out = []
    for i in range(horizon):
        t          = now + timedelta(minutes=15 * i)
        # Consumption is weekday-aware (2026.06.10): each slot uses ITS day's
        # weekday profile, not today's.
        c          = consumption.get((t.weekday(), t.hour, t.minute // 15), 300.0)
        slot_hour  = t.replace(minute=0, second=0, microsecond=0)
        s          = solar.get(slot_hour, 0.0)
        p          = prices.get((t.date(), t.hour, t.minute // 15), 0.15)
        out.append({"i": i, "time": t, "cons": c, "solar": s, "price": p, "net": c - s})
    return out


# ════════════════════════════════════════════════════════════════════════════
# LP OPTIMIZER  — split variable formulation (4 groups)
# ════════════════════════════════════════════════════════════════════════════
#
#  (Banner rewritten 2026.06.10 — the previous one described an objective with
#   opportunity costs, discharge penalties, cheap-charging bonuses and
#   price-percentile charge/discharge blocks that no longer existed in the
#   code. What follows is the ACTUAL formulation.)
#
#  Decision variables  (4 × N):
#    x[t]    ∈ [0, P_dis_max]        discharge power (W) — positive → home bus
#    y_pv[t] ∈ [0, pv_surplus[t]]    charge power from PV surplus (W)
#    y_g[t]  ∈ [0, P_chg_max]        charge power from the grid (W)
#                                    (0 when ALLOW_GRID_CHARGE is False)
#    g[t]    ∈ [0, ∞)                grid-import slack (W) — linearises max()
#
#  Setpoint sent to inverter = x[t] − y_pv[t] − y_g[t]  (discharge positive)
#
#  Objective (minimise):
#    Σ_t  price[t] · Δt/1000 · g[t]                       (grid cost, dominant)
#       + wear · Δt · (x[t] + y_pv[t] + y_g[t])           (throughput cost)
#       − tv_price · Δt/1000 · ((y_pv+y_g)·η_c − x/η_d)   (terminal value,
#                                                          folded per slot)
#
#  Constraints  (A_ub·z ≤ b_ub):
#    (1)  g[t] ≥ load[t] − pv[t] + y_pv[t] + y_g[t] − x[t]    ∀t
#    (2)  y_pv[t] + y_g[t] ≤ P_chg_max                        per slot, only
#         emitted when the individual bounds could jointly exceed the
#         hardware charge rate
#    (3)  No-export discharge cap (when ALLOW_EXPORT is False):
#         x[t] ≤ net_load[t] when net_load[t] > 0; with PV surplus, x[t] may
#         re-route that surplus to the home bus (anti-curtailment) ONLY in
#         slots where the passive-SOC envelope says the battery is already
#         full ENTERING the slot (2026.06.10: per-slot envelope instead of
#         freezing the solve-time SOC into every future slot). This cap is a
#         permission, not an incentive — the objective never rewards it, the
#         hardware realisation happens in _decide_mode.
#    (4)  SOC lower bound  ∀k:  E[k] ≥ E_min_eff
#    (5)  SOC upper bound  ∀k:  E[k] ≤ E_max
#         with E[k] = E_now + Σ_{t≤k} ((y_pv+y_g)·η_c − x/η_d)·Δt.
#         BOTH charge and discharge appear in BOTH bounds (coupled — see the
#         FIX note in the module docstring). There is NO separate cum_pv term:
#         all charging, PV included, flows through y (fix 2026.06.10).
#         E_min_eff = min(E_min, E_now): if SOC starts below the configured
#         floor the floor is clamped to keep the LP feasible — it then simply
#         cannot plan any NET discharge until the battery is back above it.
#    (6)  Grid-charge SOC ceiling (linear surrogate, 2026.06.10):
#           E_now + Σ_{t≤k} (y_g·η_c − x/η_d)·Δt ≤ E_ceil      ∀k
#         i.e. the PV-LESS SOC trajectory may never exceed the ceiling, so
#         grid charging alone can never push the battery into the top band.
#         (Exact per-slot semantics — "no grid charging while ACTUAL SOC is
#         above the ceiling" — is a complementarity condition and not LP-
#         representable; PV can still lift the true SOC above the ceiling, as
#         intended, capped by E_max.)
#
# ════════════════════════════════════════════════════════════════════════════

def _compute_price_quantiles(prices: list) -> dict:
    """Return p10, p20, p25, p50, p75, p90 from a list of prices."""
    s = sorted(prices)
    n = len(s)
    if n == 0:
        return {q: 0.15 for q in ("p10", "p20", "p25", "p50", "p75", "p90")}
    def _q(frac):
        idx = max(0, min(n - 1, int(frac * n)))
        return s[idx]
    return {
        "p10": _q(0.10),
        "p20": _q(0.20),
        "p25": _q(0.25),
        "p50": _q(0.50),
        "p75": _q(0.75),
        "p90": _q(0.90),
    }


def _solve_optimal_schedule(soc: float, schedule: list) -> list:
    N  = len(schedule)
    DT = 0.25   # hours per slot

    if N == 0:
        return []

    E_now = soc / 100.0 * BATTERY_SIZE_WH
    E_min = BATTERY_EMPTY_PCT / 100.0 * BATTERY_SIZE_WH
    E_max = BATTERY_FULL_PCT  / 100.0 * BATTERY_SIZE_WH
    # Clamp the floor if SOC is already below it (emergency drain): keeps the
    # LP feasible; it then cannot plan net discharge until back above floor.
    E_min_eff   = min(E_min, E_now)
    E_ceil_grid = GRID_CHARGE_SOC_CEILING_PCT / 100.0 * BATTERY_SIZE_WH

    loads  = [s["cons"]  for s in schedule]
    solars = [s["solar"] for s in schedule]
    prices = [s["price"] for s in schedule]

    pq = _compute_price_quantiles(prices)
    _ctx["p25"] = pq["p25"]
    _ctx["p75"] = pq["p75"]
    price_avg = sum(prices) / N

    chg_cap = abs(OUTPUT_MIN_W)

    # PV surplus available for charging each slot (cap: hardware charge rate).
    pv_surplus = [
        min(max(0.0, solars[t] - loads[t]), chg_cap)
        for t in range(N)
    ]

    # Passive-SOC envelope: the SOC trajectory if the battery did nothing but
    # absorb PV surplus. full_at[t] is True when the battery is (approximately)
    # full ENTERING slot t on this envelope — used per-slot for the
    # anti-curtailment exception in constraint (3). FIX 2026.06.10: the old
    # code froze the SOLVE-TIME SOC into every future slot, so future slots in
    # which the battery would be full could not re-route surplus, and if it was
    # full NOW, future drained slots wrongly got the exception. Where this
    # envelope is too loose (the LP discharges first and the battery is not
    # actually full), the relaxation is economically inert: re-routing costs
    # wear + terminal value and saves nothing, so the LP never uses it anyway.
    full_at = []
    e_env   = E_now
    for t in range(N):
        full_at.append(e_env >= E_max - 1.0)
        e_env = min(E_max, e_env + pv_surplus[t] * DT * BATTERY_CHARGE_EFF)

    # ── Terminal value ───────────────────────────────────────────────────
    # Energy still stored at the END of the finite horizon, never valued below
    # the PV cost floor — leftover energy is worth at least (and with the
    # recommended setting: exactly) what PV could refill it for. This is a
    # structural part of finite-horizon optimisation, not a heuristic gate.
    pv_floor = PV_COST_CT / 100.0
    if TERMINAL_VALUE_REFERENCE == "p90":
        tv_price = pq["p90"]
    elif TERMINAL_VALUE_REFERENCE == "p75":
        tv_price = pq["p75"]
    elif TERMINAL_VALUE_REFERENCE == "p25":
        tv_price = pq["p25"]
    elif TERMINAL_VALUE_REFERENCE == "avg":
        tv_price = price_avg
    else:   # "pv_cost" (recommended) — value leftover energy only at PV floor
        tv_price = pv_floor
    tv_price = max(tv_price, pv_floor)

    _dbg(
        f"LP cost-optimizer: N={N} slots ({N*15/60:.1f}h)  "
        f"terminal={tv_price*100:.2f} ct/kWh (≥PV {PV_COST_CT:.1f}ct)  "
        f"prices p25={pq['p25']*100:.1f} p75={pq['p75']*100:.1f} ct  "
        f"grid_charge={'on' if ALLOW_GRID_CHARGE else 'off'} "
        f"(ceiling {GRID_CHARGE_SOC_CEILING_PCT}%)"
    )

    # ── Objective  [x, y_pv, y_g, g] ──────────────────────────────────────
    # Pure cost minimiser. The terminal term, folded per-slot, rewards charging
    # (raises end SoC) and penalises discharge (lowers it). The LP derives
    # charge-cheap / discharge-dear / hold-for-peak from this alone. Wear
    # applies to all three battery legs — PV charging cycles the cells too.
    NV   = 4 * N
    wear = BATTERY_THROUGHPUT_COST_CT / 100.0 * DT / 1000.0   # € per W-leg-slot
    c_obj = []
    for t in range(N):                      # x[t] discharge
        c_obj.append(tv_price * DT / 1000.0 / BATTERY_DISCHARGE_EFF + wear)
    for t in range(N):                      # y_pv[t] charge from PV surplus
        c_obj.append(-tv_price * DT / 1000.0 * BATTERY_CHARGE_EFF + wear)
    for t in range(N):                      # y_g[t] charge from grid
        c_obj.append(-tv_price * DT / 1000.0 * BATTERY_CHARGE_EFF + wear)
    for t in range(N):                      # g[t] grid import
        c_obj.append(prices[t] * DT / 1000.0)

    # ── Bounds — PHYSICS ONLY ─────────────────────────────────────────────
    grid_charge_cap = float(chg_cap) if ALLOW_GRID_CHARGE else 0.0
    bounds = []
    for t in range(N):                      # x[t] discharge rate
        bounds.append((0.0, float(OUTPUT_MAX_W)))
    for t in range(N):                      # y_pv[t] ≤ this slot's PV surplus
        bounds.append((0.0, float(pv_surplus[t])))
    for t in range(N):                      # y_g[t] grid charge rate
        bounds.append((0.0, grid_charge_cap))
    for t in range(N):                      # g[t] grid import
        bounds.append((0.0, None))

    # ── Constraints  A_ub·z ≤ b_ub ────────────────────────────────────────
    A_ub, b_ub = [], []

    # (1) Grid slack:  g[t] ≥ load[t] − pv[t] + y_pv[t] + y_g[t] − x[t]
    #     ↔  −x[t] + y_pv[t] + y_g[t] − g[t] ≤ −(load[t] − pv[t])
    #     (y_pv ≤ pv_surplus can never create positive grid demand by itself,
    #      so including it here is exact: PV-surplus charging is grid-free.)
    for t in range(N):
        row = [0.0] * NV
        row[t]         = -1.0
        row[N + t]     =  1.0
        row[2 * N + t] =  1.0
        row[3 * N + t] = -1.0
        A_ub.append(row)
        b_ub.append(-(loads[t] - solars[t]))

    # (2) Per-slot hardware charge-rate cap, only where the individual bounds
    #     could jointly exceed it.
    for t in range(N):
        if pv_surplus[t] + grid_charge_cap > chg_cap + 1e-9:
            row = [0.0] * NV
            row[N + t]     = 1.0
            row[2 * N + t] = 1.0
            A_ub.append(row)
            b_ub.append(float(chg_cap))

    # (3) No-export cap on discharge: cover load only; the one exception is a
    #     full battery with PV surplus, which may re-route that surplus to the
    #     home bus (anti-curtailment) rather than export battery energy.
    #     "Full" is judged per slot via the passive-SOC envelope (see above).
    if not ALLOW_EXPORT:
        for t in range(N):
            net_load = loads[t] - solars[t]
            if net_load > 0:
                cap = net_load
            elif full_at[t]:
                cap = max(0.0, -net_load)
            else:
                cap = 0.0
            row    = [0.0] * NV
            row[t] = 1.0
            A_ub.append(row)
            b_ub.append(cap)

    # (4+5) Coupled cumulative SOC bounds. ALL charging flows through y_pv/y_g
    #       — there is no separate cum_pv term (fix 2026.06.10, see banner).
    for k in range(N):
        row = [0.0] * NV                     # lower:  E[k] ≥ E_min_eff
        for t in range(k + 1):
            row[t]         =  DT / BATTERY_DISCHARGE_EFF
            row[N + t]     = -DT * BATTERY_CHARGE_EFF
            row[2 * N + t] = -DT * BATTERY_CHARGE_EFF
        A_ub.append(row)
        b_ub.append(E_now - E_min_eff)

        row = [0.0] * NV                     # upper:  E[k] ≤ E_max
        for t in range(k + 1):
            row[N + t]     =  DT * BATTERY_CHARGE_EFF
            row[2 * N + t] =  DT * BATTERY_CHARGE_EFF
            row[t]         = -DT / BATTERY_DISCHARGE_EFF
        A_ub.append(row)
        b_ub.append(max(0.0, E_max - E_now))

    # (6) Grid-charge SOC ceiling — linear surrogate: the PV-less trajectory
    #     E_now + Σ(y_g·η_c − x/η_d)·Δt must stay ≤ E_ceil for every k, so grid
    #     charging can never be the thing that pushes SOC into the top band.
    #     RHS clamped at 0: if PV already filled past the ceiling, further grid
    #     charge requires equivalent prior discharge (never forces discharge).
    if ALLOW_GRID_CHARGE and GRID_CHARGE_SOC_CEILING_PCT < BATTERY_FULL_PCT:
        for k in range(N):
            row = [0.0] * NV
            for t in range(k + 1):
                row[2 * N + t] =  DT * BATTERY_CHARGE_EFF
                row[t]         = -DT / BATTERY_DISCHARGE_EFF
            A_ub.append(row)
            b_ub.append(max(0.0, E_ceil_grid - E_now))

    # ── Solve ─────────────────────────────────────────────────────────────
    try:
        result = linprog(
            c_obj, A_ub=A_ub, b_ub=b_ub, bounds=bounds,
            method="highs", options={"disp": False, "time_limit": 30.0},
        )
    except Exception as exc:
        log.error(f"LP solve exception: {exc} — falling back to heuristic")
        return _heuristic_schedule(soc, schedule)

    if result.status != 0:
        _warn("lp_fallback", f"LP status {result.status}: {result.message} — heuristic fallback")
        return _heuristic_schedule(soc, schedule)

    # ── Extract setpoints + simulate SOC ──────────────────────────────────
    # The simulation uses the RAW solution values (not the rounded setpoint)
    # and the same energy balance as the constraints — no extra PV term.
    optimal, e, total_cost = [], E_now, 0.0
    for t in range(N):
        x_t  = result.x[t]
        ypv  = result.x[N + t]
        yg   = result.x[2 * N + t]
        sp   = int(round((x_t - ypv - yg) / 10.0) * 10)
        sp   = max(OUTPUT_MIN_W, min(OUTPUT_MAX_W, sp))
        optimal.append(sp)

        grid_w = loads[t] - solars[t] + ypv + yg - x_t
        if not ALLOW_EXPORT:
            grid_w = max(0.0, grid_w)
        if grid_w > 0:
            total_cost += grid_w * prices[t] * DT / 1000.0

        e += (-x_t / BATTERY_DISCHARGE_EFF + (ypv + yg) * BATTERY_CHARGE_EFF) * DT
        e  = max(E_min_eff, min(E_max, e))

    soc_end = e / BATTERY_SIZE_WH * 100.0
    _dbg(
        f"LP solved ✓  slot-0={optimal[0]:+d}W  horizon_cost={total_cost:.4f}€  "
        f"SOC_end={soc_end:.0f}%"
    )
    return optimal


# ════════════════════════════════════════════════════════════════════════════
# HEURISTIC OPTIMIZER  (fallback when LP is disabled or fails)
# ════════════════════════════════════════════════════════════════════════════

def _assess_future_value(schedule: list, p75: float) -> dict:
    high_demand_wh = 0.0
    slots = 0
    for entry in schedule[1:]:
        if entry["price"] >= p75 and entry["net"] > 0:
            high_demand_wh += entry["net"] * 0.25
            slots += 1
    return {"high_demand_wh": high_demand_wh, "slots": slots}


def _estimate_pv_recharge(schedule: list, p75: float) -> float:
    surplus_wh = 0.0
    for entry in schedule[1:]:
        if entry["net"] < 0:
            surplus_wh += abs(entry["net"]) * 0.25
        elif entry["net"] > 0 and entry["price"] >= p75:
            break
    return surplus_wh


def _heuristic_schedule(soc: float, schedule: list) -> list:
    if not schedule:
        return []

    prices_list = [s["price"] for s in schedule]
    pq          = _compute_price_quantiles(prices_list)

    p20 = pq["p20"]
    p25 = pq["p25"]
    p75 = pq["p75"]

    _ctx["p25"] = p25
    _ctx["p75"] = p75

    available_wh   = max(0.0, (soc - BATTERY_EMPTY_PCT) / 100.0 * BATTERY_SIZE_WH)
    future_value   = _assess_future_value(schedule, p75)
    pv_recharge_wh = _estimate_pv_recharge(schedule, p75)

    _dbg(
        f"Heuristic: P25={p25 * 100:.1f} P75={p75 * 100:.1f} ct/kWh | "
        f"Future demand={future_value['high_demand_wh']:.0f}Wh | "
        f"PV recharge={pv_recharge_wh:.0f}Wh | "
        f"Avail={available_wh:.0f}Wh"
    )

    result = []
    for s in schedule:
        price    = s["price"]
        net      = s["net"]
        net_load = max(0, int(net))

        if price <= p20 and net > 0:
            # Cheapest slots: never discharge
            sp = 0
        elif soc <= BATTERY_EMPTY_PCT:
            sp = OUTPUT_MIN_W if price <= p25 else 0
        elif price >= p75:
            sp = min(OUTPUT_MAX_W, net_load)
        elif price <= p25:
            sp = OUTPUT_MIN_W if soc < GRID_CHARGE_SOC_BLOCK_PCT else 0
        else:
            # Mid-price: hold if future value warrants it
            if future_value["high_demand_wh"] > 0:
                if available_wh >= future_value["high_demand_wh"]:
                    sp = (
                        min(int(net), net_load)
                        if pv_recharge_wh >= future_value["high_demand_wh"]
                        else max(0, int(net - available_wh))
                    )
                else:
                    sp = min(int(net), net_load)
            else:
                sp = min(int(net), net_load)
        result.append(sp)

    return result


# ════════════════════════════════════════════════════════════════════════════
# SCHEDULE DISPATCHER
# ════════════════════════════════════════════════════════════════════════════

def _get_schedule(soc: float, schedule: list) -> list:
    if USE_LP_OPTIMIZER:
        _dbg("Using LP optimizer")
        return _solve_optimal_schedule(soc, schedule)
    _dbg("Using heuristic optimizer")
    return _heuristic_schedule(soc, schedule)


def _decide_mode(sp: int, pv: float, load: float) -> tuple:
    """
    Read ONE of three strategies off the LP's per-slot plan, accounting for how
    this hardware ACTUALLY behaves in each mode:

      FOLLOW_GRID — inverter tracks grid toward zero: PV covers the AC load
                    first, the battery covers any deficit (or, with PV surplus,
                    sits at ~0 output while the surplus charges it via DC). No
                    needless grid import.
      HOLD        — inverter AC output = 0. On THIS hardware that means PV is
                    routed entirely to the battery (DC) and the apartment load
                    is imported from the GRID. So HOLD is effectively partial
                    grid-charging — only worthwhile when grid is cheap and we
                    deliberately want to import (ration) rather than discharge.
      GRID_CHARGE — actively import to charge harder than PV alone.

    Key fix: HOLD must NOT be used whenever PV can cover the load, because there
    it would needlessly import the load at the current price while banking all
    PV. Whenever there is PV surplus (PV ≥ load), FOLLOW_GRID is correct — PV
    covers the load for free and the surplus still charges the battery. HOLD is
    reserved for the genuine ration case: the LP wants the battery idle AND
    there is a load DEFICIT (PV < load) that we choose to import rather than
    discharge the battery for (saving it for a pricier slot).

    NOTE (model/hardware mismatch in the deficit-idle case): when the LP plans
    idle with 0 < pv < load, no mode realises "PV offsets load, battery flat"
    exactly. HOLD is chosen because it preserves the battery as the LP asked,
    at the cost of importing the PV-covered share of the load too (and banking
    that PV). The outlook and the executed history simulate this true HOLD
    behaviour; see the KNOWN APPROXIMATION note in the module docstring.
    """
    pv_surplus = max(0.0, pv - load)        # free PV beyond the load

    if sp < -GRID_DEADZONE_W:               # LP plans to charge
        grid_to_batt = (-sp) - pv_surplus
        if grid_to_batt > GRID_DEADZONE_W:
            return ("GRID_CHARGE", sp)      # charging beyond PV → pull from grid
        # Charging only from PV surplus: FOLLOW_GRID realises this correctly —
        # PV covers the load AND the surplus charges the battery, no import.
        return ("FOLLOW_GRID", OUTPUT_MAX_W)

    if sp > GRID_DEADZONE_W:                # LP plans to discharge to cover load
        return ("FOLLOW_GRID", OUTPUT_MAX_W)

    # Idle plan (sp ≈ 0):
    if pv_surplus > GRID_DEADZONE_W:
        # PV exceeds load → let PV cover the load (no import); surplus charges
        # the battery. Using HOLD here would import the load at the current
        # price while banking all PV — wrong, especially at high prices.
        return ("FOLLOW_GRID", OUTPUT_MAX_W)

    # Idle with a deficit (PV ≤ load): the LP chose not to discharge, so import
    # the deficit and preserve the battery. This is the only case HOLD's
    # import-load behaviour is intended (cheap-grid rationing for a peak ahead).
    return ("HOLD", 0)


# ════════════════════════════════════════════════════════════════════════════
# PER-MODE REALISTIC POWER FLOWS  (shared by outlook simulation + history)
# ════════════════════════════════════════════════════════════════════════════

def _mode_power_flows(mode: str, sp: int, cons: float, pv: float) -> tuple:
    """
    Return (batt_w, grid_w) as the HARDWARE will actually realise them in the
    given mode — NOT as the LP modeled the slot.

    FIX 2026.06.10: HOLD used to be simulated as "battery flat, import the NET
    load (load − pv)". On this hardware HOLD imports the FULL load and routes
    ALL PV into the battery — the old simulation understated both the grid
    import and the charging whenever the sun was up, so the outlook's planned
    cost, the SoC trajectory AND the recorded history were wrong in daylight
    HOLD slots. Used identically by the outlook and by _record_executed_slot,
    so plan and history can never disagree on mode semantics again.

    batt_w sign convention: positive = discharge, negative = charge.
    """
    chg_cap  = abs(OUTPUT_MIN_W)
    net_load = max(0.0, cons - pv)
    surplus  = max(0.0, pv - cons)

    if mode == "GRID_CHARGE":
        batt_w = sp                                    # negative: charge command
        grid_w = max(0.0, cons - pv - batt_w)          # load + charge draw − PV
    elif mode == "HOLD":
        batt_w = -int(min(pv, chg_cap))                # ALL PV → battery (DC)
        grid_w = cons                                  # FULL load imported
    else:  # FOLLOW_GRID
        if net_load > 0:
            batt_w = int(min(net_load, OUTPUT_MAX_W))  # battery covers deficit
            grid_w = max(0.0, net_load - batt_w)
        else:
            batt_w = -int(min(surplus, chg_cap))       # surplus charges battery
            grid_w = 0.0
    return batt_w, grid_w


# ════════════════════════════════════════════════════════════════════════════
# HA OUTPUT HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _write_outputs(mode: str, sp: int):
    mode_id = MODE_IDS.get(mode, 0)
    input_number.set_value(entity_id=E_MODE_ID,  value=mode_id)
    input_number.set_value(entity_id=E_SETPOINT, value=sp)
    _dbg(f"Output → mode_id={mode_id} ({mode}) setpoint={sp:+d}W")


def _update_status(mode: str, reason: str):
    mode_icons = {
        "FOLLOW_GRID": "🔋 FOLLOW GRID (cover load)",
        "HOLD":        "⏸️ HOLD (grid consumption)",
        "GRID_CHARGE": "⚡ GRID CHARGE",
    }
    label = mode_icons.get(mode, mode)
    input_text.set_value(entity_id=E_STATUS_MODE,   value=label)
    input_text.set_value(entity_id=E_STATUS_REASON, value=reason)
    _dbg(f"Status: {label} | {reason}")


# ════════════════════════════════════════════════════════════════════════════
# 24H OUTLOOK  (file + InfluxDB write)
# ════════════════════════════════════════════════════════════════════════════

def _strategy_reason(mode: str, price: float, pv: float, load: float,
                     soc: float, peak_ahead: float) -> str:
    """A short, human 'why' for a chosen strategy, derived from context."""
    ct = price * 100.0
    if mode == "GRID_CHARGE":
        return f"Grid cheap ({ct:.1f} ct) — banking energy for later"
    if mode == "HOLD":
        # On this hardware HOLD imports the full load and banks all PV.
        pv_note = "; PV→battery" if pv > GRID_DEADZONE_W else ""
        if soc <= BATTERY_EMPTY_PCT:
            return f"Battery at floor — load on grid ({ct:.1f} ct){pv_note}"
        if peak_ahead * 100.0 - ct >= 1.0:
            return f"Holding charge for pricier slot ahead ({peak_ahead*100:.1f} ct){pv_note}"
        return f"Battery idle — importing load from grid ({ct:.1f} ct){pv_note}"
    # FOLLOW_GRID
    if pv - load > GRID_DEADZONE_W:
        return "PV covers load; surplus charges battery"
    p75 = _ctx.get("p75", 0.20)
    if price >= p75:
        return f"Covering load from battery at peak price ({ct:.1f} ct)"
    return f"Covering load from battery ({ct:.1f} ct)"


async def _read_history(day) -> dict:
    """Load the per-day executed-slot history: {quarter_of_day: {row...}}.

    JSON parsing happens inside a @pyscript_compile helper on an executor
    thread (non-blocking, 2026.06.10), which also sidesteps the old pyscript
    quirk where a `return` from inside a `with` nested in `try` was lost."""
    data = {}
    try:
        data = await task.executor(_read_json_file_blocking, _history_json(day))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data


async def _record_executed_slot(now, mode: str, reason: str, price: float,
                                cons: float, pv: float, grid: float,
                                batt: int, soc: float):
    """Append/overwrite the CURRENT 15-min slot in today's history file. Called
    once per strategic run, capturing the strategy actually applied now."""
    try:
        import json
        day  = now.date()
        # Quarter-of-day 0..95. NOTE (DST): Europe/Vienna has 92 or 100
        # wall-clock quarters on the two transition days; this key collides for
        # the repeated autumn hour and leaves a gap in spring. Accepted — the
        # affected rows are cosmetic history, never control inputs.
        qod  = now.hour * 4 + now.minute // 15
        hist = await _read_history(now)
        if not isinstance(hist, dict):
            hist = {}
        hist[str(qod)] = {
            "time":   now.strftime("%H:%M"),
            "mode":   mode,
            "reason": reason,
            "price":  round(price, 5),
            "cons":   round(cons, 0),
            "pv":     round(pv, 0),
            "grid":   round(grid, 0),
            "batt":   int(batt),
            "soc":    round(soc, 1),
        }
        await _write_text_file(_history_json(day), json.dumps(hist))
    except Exception as exc:
        _warn("history_record", f"Could not record executed slot: {exc}")


async def _mark_outlook_stale(msg: str):
    """On a cycle that produced no (or a failed) outlook, do NOT wipe the last
    good table (fix 2026.06.10 — the old placeholder overwrote a perfectly
    valid outlook on every transient blip). Instead, prepend/refresh a STALE
    banner on the existing file; only when there is no previous content fall
    back to a bare placeholder. The original 'since' timestamp survives
    repeated stale cycles; only the 'last attempt' time advances."""
    now_str = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    old = ""
    try:
        old = await task.executor(_read_text_file_blocking, OUTLOOK_FILE)
    except Exception:
        old = ""

    since = now_str
    if old.startswith(OUTLOOK_STALE_PREFIX):
        # Refresh the existing banner: keep its original 'since', drop the
        # banner line itself so banners never stack.
        first_nl = old.find("\n")
        banner   = old[:first_nl] if first_nl >= 0 else old
        try:
            after = banner.split(" since ", 1)[1]
            since = after.split(" — ", 1)[0].strip()
        except Exception:
            since = now_str
        old = old[first_nl + 1:] if first_nl >= 0 else ""
        old = old.lstrip("\n")

    if old.strip():
        content = (
            f"{OUTLOOK_STALE_PREFIX} since {since} — {msg} "
            f"(last attempt {now_str}, v{VERSION}). Last good outlook below.\n\n"
            f"{old}"
        )
    else:
        content = (
            f"## ⚡ Energy Optimizer — 24h Outlook\n\n"
            f"_Updated {now_str} (v{VERSION})_\n\n"
            f"> {msg}\n"
        )
    if await _write_text_file(OUTLOOK_FILE, content):
        _dbg(f"Outlook marked stale: {msg}")


def _slots_from_history(hist: dict, day_dt, qod_from: int, qod_to: int) -> list:
    """Turn per-day history records (quarter-of-day keyed) into outlook slot
    dicts, marked as executed (forecast=False). Shared by the live outlook
    (today, 00:00 → now) and midnight_finalize (yesterday, full day)."""
    out = []
    for q in range(qod_from, qod_to):
        rec = hist.get(str(q))
        if not rec:
            continue
        hh, mm = divmod(q * 15, 60)
        out.append({
            "time":   day_dt.replace(hour=hh, minute=mm, second=0, microsecond=0),
            "mode":   rec["mode"],
            "reason": rec.get("reason", ""),
            "forecast": False,
            "price":  rec["price"],
            "cons_w": rec["cons"],
            "pv_w":   rec["pv"],
            "batt_w": rec["batt"],
            "grid_w": rec["grid"],
            "soc_start_pct": rec["soc"],
            "soc_pct": rec["soc"],
        })
    return out


def _render_md_table(slots: list, header: str, now_hhmm) -> str:
    """Render slot dicts as the strategy markdown table: contiguous windows
    sharing (mode, forecast, reason), ✓-tagged executed rows, and — when
    now_hhmm is given — a "now / forecast below" boundary before the first
    forecast window. Used by the live outlook, the intraday daily file and
    the finalized daily record (2026.06.10c refactor; the duplicated console
    log table was dropped — the markdown file IS the record)."""

    def _new_window(slot):
        return {
            "mode":     slot["mode"],
            "reason":   slot["reason"],
            "forecast": slot["forecast"],
            "start":    slot["time"],
            "prices":   [slot["price"]],
            "cons_w":   [slot["cons_w"]],
            "pv_w":     [slot["pv_w"]],
            "batt_w":   [slot["batt_w"]],
            "grid_w":   [slot["grid_w"]],
            "soc_pct":  [slot["soc_pct"]],
            "n_slots":   1,
        }

    def _same(slot, win):
        return (slot["mode"] == win["mode"]
                and slot["forecast"] == win["forecast"]
                and slot["reason"] == win["reason"])

    # Splitting on reason as well as mode keeps a long "HOLD" run from hiding a
    # change in *why* (e.g. "PV covers load" → "holding for peak").
    windows = []
    cur = _new_window(slots[0])
    for slot in slots[1:]:
        if _same(slot, cur):
            cur["prices"].append(slot["price"])
            cur["cons_w"].append(slot["cons_w"])
            cur["pv_w"].append(slot["pv_w"])
            cur["batt_w"].append(slot["batt_w"])
            cur["grid_w"].append(slot["grid_w"])
            cur["soc_pct"].append(slot["soc_pct"])
            cur["n_slots"] += 1
        else:
            cur["end"] = slot["time"]
            windows.append(cur)
            cur = _new_window(slot)
    cur["end"] = cur["start"] + timedelta(minutes=15 * cur["n_slots"])
    windows.append(cur)

    def _avg(lst):
        return sum(lst) / len(lst)

    strategy_text = {
        "FOLLOW_GRID": "🔋 FOLLOW_GRID",
        "HOLD":        "⏸️ HOLD",
        "GRID_CHARGE": "⚡ GRID_CHARGE",
    }

    md_lines = [header, ""]
    md_lines.append("| Time | Strategy | Why | Price | Consumption | PV | Grid import | Batt setpoint | SOC end |")
    md_lines.append("|------|----------|-----|-------|-------------|----|-------------|---------------|---------|")

    boundary_drawn = False
    for w in windows:
        start_str = w["start"].strftime("%H:%M")
        end_str   = w["end"].strftime("%H:%M")
        duration  = w["n_slots"] * 15
        strat     = strategy_text.get(w["mode"], w["mode"])
        why       = w["reason"]
        avg_price = _avg(w["prices"])
        min_price = min(w["prices"]); max_price = max(w["prices"])
        avg_cons  = _avg(w["cons_w"]); avg_pv = _avg(w["pv_w"])
        avg_grid  = _avg(w["grid_w"]); avg_batt = _avg(w["batt_w"])
        soc_end   = w["soc_pct"][-1]
        avg_ct, min_ct, max_ct = avg_price*100, min_price*100, max_price*100
        price_str = (
            f"{avg_ct:.1f} ct" if abs(min_ct - max_ct) < 0.05
            else f"{avg_ct:.1f} ct ({min_ct:.1f}–{max_ct:.1f})"
        )
        if now_hhmm and w["forecast"] and not boundary_drawn:
            md_lines.append(f"| **— now ({now_hhmm}) · forecast below —** | | | | | | | | |")
            boundary_drawn = True

        tag = "" if w["forecast"] else "✓ "   # ✓ = actually executed
        md_lines.append(
            f"| {tag}`{start_str}–{end_str}` ({duration}min) "
            f"| {strat} | {why} | {price_str} "
            f"| {avg_cons:.0f} W | {avg_pv:.0f} W "
            f"| {avg_grid:+.0f} W | {avg_batt:+.0f} W | {soc_end:.0f}% |"
        )
    return "\n".join(md_lines)


async def _log_24h_outlook(schedule: list, optimal_schedule: list, soc: float, session: aiohttp.ClientSession) -> bool:
    """Returns True iff the main outlook markdown (OUTLOOK_FILE) was written
    this cycle — used by the always-visible "cycle complete" INFO line."""
    if not schedule or not optimal_schedule:
        _dbg("Outlook: no schedule available")
        await _mark_outlook_stale("No schedule available (missing price or PV data).")
        return False

    DT    = 0.25
    E_now = soc / 100.0 * BATTERY_SIZE_WH
    E_min = BATTERY_EMPTY_PCT / 100.0 * BATTERY_SIZE_WH
    E_max = BATTERY_FULL_PCT  / 100.0 * BATTERY_SIZE_WH
    E_min_sim = min(E_min, E_now)   # same floor clamp as the LP

    slots = []
    e     = E_now
    for i in range(min(len(schedule), len(optimal_schedule))):
        s       = schedule[i]
        raw_sp  = optimal_schedule[i]
        p       = s["price"]

        soc_start = e / BATTERY_SIZE_WH * 100.0

        # Read the strategy straight off the LP plan for this slot — same
        # function the live path uses, with this slot's PV/load context.
        eff_mode, sp = _decide_mode(raw_sp, s["solar"], s["cons"])

        # Realistic delivered power per mode — shared helper, identical to what
        # the executed-history recorder writes (fix 2026.06.10: HOLD is
        # simulated as the hardware executes it: full-load import, ALL
        # PV→battery — no separate passive-PV term any more).
        sim_sp, grid_w = _mode_power_flows(eff_mode, sp, s["cons"], s["solar"])

        # Simulate SOC change — same sign convention and balance as the LP.
        if sim_sp > 0:
            e_after = e - sim_sp * DT / BATTERY_DISCHARGE_EFF
        elif sim_sp < 0:
            e_after = e + abs(sim_sp) * DT * BATTERY_CHARGE_EFF
        else:
            e_after = e
        e_after   = max(E_min_sim, min(E_max, e_after))
        soc_after = e_after / BATTERY_SIZE_WH * 100.0

        # Peak price ahead of this slot (for the 'why' text).
        tail = [schedule[j]["price"] for j in range(i + 1, len(schedule))]
        peak_ahead = max(tail) if tail else p
        reason = _strategy_reason(eff_mode, p, s["solar"], s["cons"], soc_start, peak_ahead)

        slots.append({
            "time":          s["time"],
            "mode":          eff_mode,     # one of the 3 strategies only
            "reason":        reason,
            "forecast":      True,         # future (planned) row
            "price":         p,
            "cons_w":        s["cons"],
            "pv_w":          s["solar"],
            "batt_w":        sim_sp,   # realistic delivered power, not the
                                       # full-authority command, so the column
                                       # matches the SoC trajectory and reality
            "grid_w":        grid_w,
            "soc_start_pct": round(soc_start, 1),
            "soc_pct":       soc_after,
        })
        e = e_after

    if not slots:
        await _mark_outlook_stale("No forecast rows produced this cycle.")
        return False

    # ── Prepend today's EXECUTED history (00:00 → now) ────────────────────
    # These rows are what was actually applied earlier today, read back from the
    # per-day history file, so the table shows the realised part of the day in
    # the same 3-strategy vocabulary, followed by the forecast from now on.
    now_dt    = datetime.now(TZ)
    first_qod = now_dt.hour * 4 + now_dt.minute // 15
    hist      = await _read_history(now_dt)
    if not isinstance(hist, dict):
        hist = {}
    past_slots = _slots_from_history(hist, now_dt, 0, first_qod)
    slots = past_slots + slots

    # ── Publish the forecast as sensor attributes (2026.06.10b/g) ─────────
    # Dashboard cards (plotly-graph) read this live from hass.states. To stay
    # under the recorder's 16 KB attribute cap (2026.06.10g) the payload is:
    #   • FUTURE rows only — the card's dotted "planned" traces draw only
    #     these; the executed history is in the md / CSV / InfluxDB outputs;
    #   • compact short keys (see SCHEMA below) — the field semantics are
    #     unchanged, only the JSON is smaller;
    #   • optionally thinned by FORECAST_SENSOR_STRIDE.
    # SCHEMA (per row): t=ISO time, s=strategy, c=cons_w, p=pv_w, g=grid_w,
    #   b=batt_w, pr=price_ct, so=soc_pct. (No per-row forecast flag: every
    #   published row is a forecast.) State value = row count.
    try:
        fut = [s for s in slots if s.get("forecast", True)]
        # Auto-thin to stay under the 16 KB recorder cap on very long horizons.
        # ~120 B/row compact → ~125 rows fit; beyond that, take every Nth slot
        # (the near-term plan keeps full 15-min detail, only the far tail
        # coarsens). The explicit FORECAST_SENSOR_STRIDE is honoured as a floor.
        stride = max(1, FORECAST_SENSOR_STRIDE)
        if len(fut) // stride > 120:
            stride = max(stride, -(-len(fut) // 120))   # ceil division
        if stride > 1:
            fut = fut[::stride]
        attr_rows = [{
            "t":  s["time"].isoformat(),
            "s":  s.get("mode", ""),
            "c":  round(s["cons_w"], 0),
            "p":  round(s["pv_w"], 0),
            "g":  round(s["grid_w"], 0),
            "b":  int(s["batt_w"]),
            "pr": round(s["price"] * 100, 2),
            "so": round(s["soc_pct"], 1),
        } for s in fut]
        state.set(
            E_FORECAST_SENSOR,
            value=len(attr_rows),
            new_attributes={
                "friendly_name": "Energy Optimizer Forecast",
                "icon": "mdi:chart-timeline-variant",
                "updated": now_dt.isoformat(),
                "version": VERSION,
                "schema": "t,s,c,p,g,b,pr,so;future-only;v2026.06.10g",
                "data": attr_rows,
            },
        )
        # Guard rail: log the serialized size so an oversized payload is
        # diagnosable from the log rather than only from the recorder warning.
        try:
            import json as _json
            sz = len(_json.dumps(attr_rows))
            if sz > 15000:
                _warn("forecast_size",
                      f"Forecast attribute is {sz} B (recorder cap 16384) — "
                      f"raise FORECAST_SENSOR_STRIDE or exclude the sensor.")
            _dbg(f"Forecast sensor published ({len(attr_rows)} future rows, {sz} B)")
        except Exception:
            _dbg(f"Forecast sensor published ({len(attr_rows)} future rows)")
    except Exception as exc:
        _warn("forecast_sensor", f"Could not publish forecast sensor: {exc}")

    # ── Render + write the live outlook ───────────────────────────────────
    # Same now_dt as the history/forecast split above, so the header timestamp
    # and the "— now —" boundary row can never disagree.
    now_str = now_dt.strftime("%d.%m.%Y %H:%M")
    header = (
        f"**{'LP' if USE_LP_OPTIMIZER else 'Heuristic'} optimizer** | "
        f"SOC **{soc:.0f}%** | "
        f"P25 {_ctx.get('p25', 0) * 100:.1f} · "
        f"P75 {_ctx.get('p75', 0) * 100:.1f} ct/kWh "
        f"_(updated {now_str}, v{VERSION})_"
    )
    content = _render_md_table(slots, header, now_dt.strftime("%H:%M"))
    outlook_written = await _write_text_file(OUTLOOK_FILE, content)
    if outlook_written:
        _dbg(f"Outlook written to {OUTLOOK_FILE}")

    # ── Refresh today's rotated daily markdown (intraday view) ─────────────
    # energy-optimizer-YYYY-MM-DD.md mirrors the live outlook during its day
    # (executed history so far + remaining forecast). A NEW file starts with
    # the first strategic run after 00:00; shortly after midnight,
    # midnight_finalize() overwrites YESTERDAY's file once more with the
    # executed-only record of the complete day, freezing it as the archive.
    try:
        if await _write_text_file(_history_md(now_dt), content):
            _dbg(f"Daily markdown refreshed: {_history_md(now_dt)}")
    except Exception as exc:
        _warn("daily_md", f"Could not write daily markdown: {exc}")

    # ── Write CSV forecast file ────────────────────────────────────────────
    try:
        import csv, io as _io
        buf    = _io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=[
            "time", "strategy", "reason", "forecast", "price_ct", "cons_w", "pv_w",
            "batt_w", "grid_w", "soc_start_pct", "soc_end_pct",
        ])
        writer.writeheader()
        for slot in slots:
            writer.writerow({
                "time":          slot["time"].strftime("%Y-%m-%dT%H:%M"),
                "strategy":      slot.get("mode", ""),
                "reason":        slot.get("reason", ""),
                "forecast":      slot.get("forecast", True),
                "price_ct":      round(slot["price"] * 100, 3),
                "cons_w":        round(slot["cons_w"], 1),
                "pv_w":          round(slot["pv_w"], 1),
                "batt_w":        slot["batt_w"],
                "grid_w":        round(slot["grid_w"], 1),
                "soc_start_pct": slot.get("soc_start_pct", slot.get("soc_pct")),
                "soc_end_pct":   round(slot["soc_pct"], 1),
            })
        if await _write_text_file(FORECAST_CSV_FILE, buf.getvalue()):
            _dbg(f"Forecast CSV written to {FORECAST_CSV_FILE}")
    except Exception as exc:
        _warn("forecast_csv", f"Could not write forecast CSV: {exc}")

    # ── Write forecast to InfluxDB ─────────────────────────────────────────
    # SCHEMA CHANGE 2026.06.10: `strategy` and `minutes_ahead` are now FIELDS,
    # not tags. As tags they spawned a brand-new series per re-plan at the same
    # timestamp (strategy flips between runs) and the executed-history rows got
    # nonsense minutes_ahead values (the old code numbered the COMBINED
    # history+forecast list from 0). The only tag is now `phase`
    # (executed|forecast), so each slot carries at most two points: its final
    # pre-execution forecast and the executed truth — which makes plan-vs-actual
    # comparison in Grafana trivial. UPDATE GRAFANA QUERIES accordingly.
    try:
        lines = []
        for slot in slots:
            ts_ns  = int(slot["time"].timestamp()) * 1_000_000_000
            soc_start = slot.get("soc_start_pct", slot.get("soc_pct", 0.0))
            minutes_ahead = int(round((slot["time"] - now_dt).total_seconds() / 60.0))
            phase  = "forecast" if slot.get("forecast", True) else "executed"
            tags   = f"phase={phase}"
            fields = (
                f"consumption_w={slot['cons_w']:.1f},"
                f"pv_w={slot['pv_w']:.1f},"
                f"price_ct={slot['price'] * 100:.3f},"
                f"setpoint_w={slot['batt_w']}i,"
                f"grid_import_w={slot['grid_w']:.1f},"
                f"soc_start_pct={soc_start:.1f},"
                f"soc_end_pct={slot['soc_pct']:.1f},"
                f"minutes_ahead={minutes_ahead}i,"
                f"strategy=\"{slot.get('mode', '')}\""
            )
            lines.append(f"energy_optimizer_forecast,{tags} {fields} {ts_ns}")

        body   = "\n".join(lines).encode("utf-8")
        params = f"db={INFLUX_DB}&u={INFLUX_USER}&p={INFLUX_PASS}"
        url    = f"http://localhost:8086/write?{params}"
        async with session.post(
            url,
            data=body,
            headers={"Content-Type": "application/octet-stream"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status in (200, 204):
                _dbg(f"Forecast written to InfluxDB ({len(lines)} points)")
            else:
                text = await resp.text()
                _warn("influx_forecast", f"InfluxDB write failed: {resp.status} {text}")
    except Exception as exc:
        _warn("influx_forecast", f"Could not write forecast to InfluxDB: {exc}")

    return outlook_written


# ════════════════════════════════════════════════════════════════════════════
# STRATEGIC LAYER — runs every 15 minutes
# ════════════════════════════════════════════════════════════════════════════

@time_trigger("cron(0,15,30,45 * * * *)")
async def strategic_optimize():
    _ensure_tz()
    # FIX: prevent overlapping runs. cron, the EPEX state-trigger, and the
    # manual service can all fire close together; task.unique cancels any
    # previously-started instance so they don't race on _ctx / file writes.
    # (A cancelled instance aborts its pending outlook write — the surviving
    # newer run produces the fresh one, which is exactly what we want.)
    # task.unique cancels any previously-started instance so cron, the EPEX
    # state-trigger and the manual service don't race on _ctx / file writes.
    # It MUST run before the session is created: cancelling an overlapping
    # instance raises CancelledError at its current await, and if that happened
    # with a manually-managed session open, the close could be skipped and the
    # session leaked ("Unclosed client session"). With task.unique first and
    # the session in an `async with`, closure is deterministic even on cancel.
    task.unique("energy_optimizer_strategic")
    _ctx["last_cycle_ts"] = datetime.now(timezone.utc).timestamp()

    # Always-visible run markers (2026.06.10i): these two log.info lines are
    # emitted regardless of VERBOSE or HA log level, so the log always shows
    # that the cycle started and how it ended (strategy + whether the outlook
    # markdown was written). They intentionally bypass _dbg().
    log.info(f"Strategic cycle START (v{VERSION})")

    _dbg(f"── Strategic cycle v{VERSION} ({'LP' if USE_LP_OPTIMIZER else 'Heuristic'}) ──")
    # Defined up-front so the finally-block outlook write is always safe, even if
    # the cycle raises before they are assigned.
    schedule = []
    optimal_schedule = []
    soc = 0.0
    skip_reason = ""
    outlook_ok = False
    final_mode = "—"
    async with aiohttp.ClientSession() as session:
      try:
          soc_raw = state.get(E_BATTERY_SOC)
          if soc_raw in (None, "unavailable", "unknown"):
              skip_reason = "Battery SOC unavailable — cycle skipped."
              _warn("soc_unavailable", "Battery SOC unavailable — skipping cycle")
              return
          soc = float(soc_raw)

          # Clear a stale emergency flag if the recovery state-trigger was missed
          # (HA restart, sensor gap, …).
          if _ctx.get("soc_emergency") and soc >= SOC_RECOVER_PCT:
              _ctx["soc_emergency"] = False
              persistent_notification.dismiss(notification_id="energy_optimizer_critical")
              log.info(f"SOC emergency cleared by strategic cycle ({soc:.0f}% ≥ {SOC_RECOVER_PCT:.0f}%)")
          # NOTE: while the emergency IS active (SOC between critical and recover)
          # this cycle still runs — that is safe by construction: the LP clamps
          # its floor to the current SOC (E_min_eff), so it cannot plan any net
          # discharge; the strategy it produces can only be HOLD or GRID_CHARGE.

          consumption = await _fetch_historical_consumption(session)
          actuals     = await _get_solar_actuals(session)
          solar       = _get_solar_forecast(actuals)
          prices      = _get_spot_prices()

          if LOG_DEBUG and prices:
              _dbg("── EPEX prices (incl. network fee) ──")
              # Keys are uniformly (date, hour, quarter) since 2026.06.10 — the
              # fallback curve is date-keyed too.
              for k, p in sorted(prices.items(), key=lambda kv: kv[0]):
                  d, h, q = k
                  _dbg(f"  {d} {h:02d}:{q * 15:02d}  {p * 100:.3f} ct/kWh")
              _dbg("─────────────────────────────────────")

          if not prices:
              skip_reason = "No price data available — cycle skipped."
              _warn("epex_missing", "No EPEX price data — mode unchanged")
              return

          schedule         = _build_schedule(consumption, solar, prices)
          optimal_schedule = _get_schedule(soc, schedule)
          _ctx["last_schedule"] = optimal_schedule

          raw_sp = optimal_schedule[0] if optimal_schedule else 0
          price  = schedule[0]["price"] if schedule else 0.15
          pv0    = schedule[0]["solar"] if schedule else 0.0
          load0  = schedule[0]["cons"]  if schedule else 0.0

          # The strategy IS the sign of the LP's slot-0 plan — no extra heuristics.
          mode, sp = _decide_mode(raw_sp, pv0, load0)
          final_mode = mode
          _write_outputs(mode, sp)

          p75 = _ctx.get("p75", 0.20)

          if mode == "HOLD":
              pv_note = " PV is charging the battery meanwhile." if pv0 > GRID_DEADZONE_W else ""
              if soc <= BATTERY_EMPTY_PCT:
                  reason = (
                      f"Battery at floor ({soc:.0f}%). Idle, importing load from grid; "
                      f"no discharge. Price: {price * 100:.1f} ct/kWh.{pv_note}"
                  )
              else:
                  reason = (
                      f"Holding: limited battery (SOC {soc:.0f}%) and a pricier peak "
                      f"ahead. Importing from grid now ({price * 100:.1f} ct/kWh) to "
                      f"save charge for the peak.{pv_note}"
                  )
          elif mode == "GRID_CHARGE":
              reason = (
                  f"Charging from grid at {price * 100:.1f} ct/kWh (cheap relative to "
                  f"the upcoming peak). SOC: {soc:.0f}%."
              )
          else:  # FOLLOW_GRID
              if soc >= BATTERY_FULL_PCT:
                  reason = (
                      f"Battery full ({soc:.0f}%). Covering load from battery / routing "
                      f"PV to the home bus. Price: {price * 100:.1f} ct/kWh."
                  )
              elif price >= p75:
                  reason = (
                      f"Covering load from battery at peak price "
                      f"({price * 100:.1f} ct ≥ P75 {p75 * 100:.1f} ct). SOC: {soc:.0f}%."
                  )
              else:
                  reason = (
                      f"Covering load from battery (self-consumption) at "
                      f"{price * 100:.1f} ct/kWh. SOC: {soc:.0f}%."
                  )

          _update_status(mode, reason)

          # Record this slot as ACTUALLY EXECUTED so the outlook can show the
          # realised part of the day (00:00 → now). Uses the SAME per-mode power
          # model as the outlook (_mode_power_flows), so history and plan agree
          # on what each mode does — including HOLD's full-load import.
          now_slot   = datetime.now(TZ)
          peak_ahead = max([s["price"] for s in schedule[1:]] or [price]) if schedule else price
          short_reason = _strategy_reason(mode, price, pv0, load0, soc, peak_ahead)
          hist_batt, hist_grid = _mode_power_flows(mode, sp, load0, pv0)
          await _record_executed_slot(now_slot, mode, short_reason, price,
                                      load0, pv0, hist_grid, hist_batt, soc)

          # One INFO line per STRATEGY CHANGE (a handful per day); unchanged-
          # strategy cycles log the same summary only as a diagnostic.
          summary = (
              f"Mode={mode} | SOC={soc:.0f}% | "
              f"Price={price * 100:.1f} ct | "
              f"LP={raw_sp:+d}W → Applied={sp:+d}W"
          )
          if mode != _ctx.get("last_logged_mode"):
              _ctx["last_logged_mode"] = mode
              log.info(summary)
          else:
              _dbg(summary)

      except Exception as exc:
          import traceback
          skip_reason = f"Strategic error: {exc}"
          log.error(f"Strategic error: {exc}\n{traceback.format_exc()}")
      finally:
          # Always leave the outlook file in a known state — but never wipe a good
          # table on a bad cycle (fix 2026.06.10): a full schedule rewrites it,
          # anything less marks the existing one STALE with the reason.
          try:
              if schedule and optimal_schedule:
                  outlook_ok = await _log_24h_outlook(schedule, optimal_schedule, soc, session)
              else:
                  await _mark_outlook_stale(skip_reason or "No schedule produced this cycle.")
                  outlook_ok = False
          except Exception as exc:
              import traceback
              _warn("outlook_write", f"Outlook write failed: {exc}\n{traceback.format_exc()}")
              outlook_ok = False
              try:
                  await _mark_outlook_stale(f"Outlook generation failed: {exc}")
              except Exception:
                  pass
          # Always-visible completion line (2026.06.10i): bypasses _dbg() so it
          # appears at any log level. Reports the resulting strategy and the
          # real outlook-write result, or the skip/error reason.
          if skip_reason:
              log.info(f"Strategic cycle COMPLETE — {skip_reason} "
                       f"(outlook md: {'written' if outlook_ok else 'stale/not written'})")
          else:
              log.info(f"Strategic cycle COMPLETE — strategy={final_mode}, "
                       f"outlook md: {'written ✓' if outlook_ok else 'NOT written ✗'}")


# ════════════════════════════════════════════════════════════════════════════
# EVENT TRIGGERS
# ════════════════════════════════════════════════════════════════════════════

@pyscript_compile
def _prune_history_blocking(directory: str, keep_days: int, today_ord: int) -> list:
    """Delete daily history files (md + json, current and legacy naming) older
    than keep_days. Compiled + run via task.executor (filesystem I/O)."""
    import os, re, datetime
    removed = []
    pat = re.compile(r"(?:energy-optimizer-)?(\d{4})-(\d{2})-(\d{2})\.(?:md|json)$")
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return removed
    for n in names:
        m = pat.fullmatch(n)
        if not m:
            continue
        try:
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if today_ord - d.toordinal() > keep_days:
            try:
                os.remove(os.path.join(directory, n))
                removed.append(n)
            except OSError:
                pass
    return removed


@time_trigger("cron(1 0 * * *)")
async def midnight_finalize():
    """Daily markdown rotation (2026.06.10c), runs at 00:01.

    The 00:00 strategic cycle already starts the NEW day's outlook and its new
    energy-optimizer-<today>.md. This job then rewrites YESTERDAY's file one
    last time from yesterday's executed-slot JSON: a pure record of what was
    actually done 00:00–24:00 — all rows ✓, no leftover forecast tail and no
    missing final slot (the intraday refresh could never show the 23:45 slot
    as executed, because the next run after it already belongs to today).
    Optionally prunes daily files older than HISTORY_RETENTION_DAYS."""
    _ensure_tz()
    now  = datetime.now(TZ)
    yday = (now - timedelta(days=1)).date()
    try:
        hist = await _read_history(yday)
        if hist:
            y_dt  = datetime(yday.year, yday.month, yday.day, tzinfo=TZ)
            slots = _slots_from_history(hist, y_dt, 0, 96)
            if slots:
                header = (
                    f"**Daily record {yday.strftime('%d.%m.%Y')}** — "
                    f"executed strategies "
                    f"_(finalized {now.strftime('%d.%m.%Y %H:%M')}, v{VERSION})_"
                )
                content = _render_md_table(slots, header, None)
                if await _write_text_file(_history_md(yday), content):
                    log.info(f"Daily markdown finalized: {_history_md(yday)}")
        else:
            _dbg(f"No history for {yday} — nothing to finalize")
    except Exception as exc:
        log.warning(f"Midnight finalize failed: {exc}")

    if HISTORY_RETENTION_DAYS > 0:
        try:
            removed = await task.executor(
                _prune_history_blocking, HISTORY_DIR,
                HISTORY_RETENTION_DAYS, now.date().toordinal(),
            )
            if removed:
                _dbg(f"History pruned: {len(removed)} files removed")
        except Exception as exc:
            log.warning(f"History pruning failed: {exc}")


@time_trigger("startup")
async def on_ha_start():
    """Run one strategic cycle shortly after Home Assistant has started, so the
    dashboard forecast sensor (sensor.energy_optimizer_forecast) — which is
    created by state.set and does NOT survive a restart — is republished
    immediately instead of being absent until the next 15-min cron tick.

    Two guards make this robust on a cold boot:
      • a short settle delay, because right after start the input entities
        (SOC, EPEX, Solcast) are often still 'unknown' and a cycle would just
        skip with "Battery SOC unavailable";
      • a brief poll for a valid SOC (up to ~SOC_WAIT_MAX_S) before running, so
        the first post-boot plan uses real data rather than skipping.
    If SOC never becomes valid in time we still call the cycle once: it will
    skip cleanly and the normal cron will retry, exactly as before.

    `time_trigger("startup")` fires once per (re)load — both on an HA restart
    and on a pyscript reload — which is precisely when the sensor was wiped.
    """
    _ensure_tz()
    import asyncio
    await asyncio.sleep(STARTUP_SETTLE_S)

    waited = 0.0
    while waited < SOC_WAIT_MAX_S:
        soc_raw = state.get(E_BATTERY_SOC)
        if soc_raw not in (None, "unavailable", "unknown"):
            break
        await asyncio.sleep(SOC_WAIT_POLL_S)
        waited += SOC_WAIT_POLL_S

    log.info(f"HA start detected — running initial strategic cycle "
             f"(waited {waited:.0f}s for inputs)")
    await strategic_optimize()


@state_trigger(E_PRICE_DATA)
async def on_price_update(**kwargs):
    """Replan when EPEX data changes — DEBOUNCED (2026.06.10d). The sensor's
    STATE is the *current* price and rolls on every price slot, so this
    trigger used to fire a full extra strategic cycle (with all its logging
    and I/O) seconds next to each cron cycle. Skipping when a cycle started
    within the last 3 minutes removes the duplicates while genuinely new
    day-ahead data (published mid-hour) still accelerates replanning."""
    now_ts = datetime.now(timezone.utc).timestamp()
    if now_ts - _ctx.get("last_cycle_ts", 0.0) < 180:
        _dbg("EPEX state change debounced (recent strategic cycle)")
        return
    _dbg("EPEX price data updated — triggering strategic cycle")
    await strategic_optimize()


@state_trigger(E_BATTERY_SOC)
async def on_soc_critical(value=None, old_value=None, **kwargs):
    """Emergency SOC floor — edge-triggered with hysteresis (fix 2026.06.10).

    The old version re-fired on EVERY SOC tick below 12% (rewriting the helpers
    and spamming a warning each time) and had no recovery path: the forced HOLD
    lingered until the next cron run even after the battery had recharged. Now:
      • the emergency activates ONCE, on the downward crossing of
        SOC_CRITICAL_PCT (guarded by a flag, so unavailable→11% also triggers);
      • it stays latched until SOC reaches SOC_RECOVER_PCT (hysteresis band,
        so a value bouncing around 12% can't flap the mode);
      • on recovery the notification is dismissed and a strategic cycle is
        kicked off immediately so the optimizer takes over again.
    While latched, intervening cron cycles remain safe: the LP clamps its floor
    to the current SOC and therefore cannot plan net discharge (see
    strategic_optimize)."""
    _ensure_tz()
    try:
        soc_new = float(value)
    except (TypeError, ValueError):
        return

    if not _ctx.get("soc_emergency") and soc_new < SOC_CRITICAL_PCT:
        _ctx["soc_emergency"] = True
        input_number.set_value(entity_id=E_MODE_ID,  value=MODE_IDS["HOLD"])
        input_number.set_value(entity_id=E_SETPOINT, value=0)
        _update_status(
            "HOLD",
            f"⚠️ Emergency: SOC critically low ({soc_new:.0f}%). Battery idle, load on grid.",
        )
        persistent_notification.create(
            title="⚠️ Battery Critical",
            message=f"SOC is {soc_new:.0f}% — battery held idle, load served from grid.",
            notification_id="energy_optimizer_critical",
        )
        log.warning(f"Battery critical ({soc_new:.0f}%) — forced HOLD, setpoint 0W")
    elif _ctx.get("soc_emergency") and soc_new >= SOC_RECOVER_PCT:
        _ctx["soc_emergency"] = False
        persistent_notification.dismiss(notification_id="energy_optimizer_critical")
        log.info(
            f"Battery recovered ({soc_new:.0f}% ≥ {SOC_RECOVER_PCT:.0f}%) — "
            f"emergency cleared, re-planning now"
        )
        await strategic_optimize()


# ════════════════════════════════════════════════════════════════════════════
# MANUAL SERVICE CALL
# ════════════════════════════════════════════════════════════════════════════

@service
async def energy_optimizer_self_test():
    """End-to-end diagnostic for 'the forecast sensor never appears' problems.
    Call via Developer Tools → Actions → pyscript.energy_optimizer_self_test.
    Results are posted as a persistent notification AND logged once at INFO.

    If this service is NOT listed in Developer Tools at all, the file is not
    loaded: search the HA log for "Exception in </config/pyscript/
    energy_optimizer" (load/compile error — e.g. a pyscript version older
    than 1.4, which lacks @pyscript_compile/task.executor; update pyscript
    via HACS), verify the file is at /config/pyscript/energy_optimizer.py,
    remove stale .py copies, and call the pyscript.reload service."""
    _ensure_tz()
    now = datetime.now(TZ)

    def _safe_get(ent):
        try:
            return state.get(ent)
        except Exception:
            return None

    lines = [f"version loaded: v{VERSION}"]

    ts = _ctx.get("last_cycle_ts")
    if ts:
        age_min = (datetime.now(timezone.utc).timestamp() - ts) / 60.0
        lines.append(f"last strategic cycle: ✓ {age_min:.1f} min ago")
    else:
        lines.append("last strategic cycle: ✗ NEVER since load — cron not firing?")

    for label, ent in (
        ("battery SOC",   E_BATTERY_SOC),
        ("EPEX price",    E_PRICE_DATA),
        ("Solcast today", E_SOLAR_TODAY),
        ("mode helper",   E_MODE_ID),
    ):
        v = _safe_get(ent)
        if v in (None, "unavailable", "unknown"):
            lines.append(f"{label}: ✗ {v} ({ent})")
        else:
            lines.append(f"{label}: ✓ {str(v)[:40]}")

    try:
        epex_n = len((state.getattr(E_PRICE_DATA) or {}).get("data", []))
        lines.append(f"EPEX attribute slots: {'✓' if epex_n else '✗'} {epex_n}")
    except Exception as exc:
        lines.append(f"EPEX attribute slots: ✗ ERROR {exc}")

    ok = await _write_text_file(f"{HISTORY_DIR}/selftest.txt",
                                f"self-test {now.isoformat()}\n")
    lines.append(f"file write ({HISTORY_DIR}): {'✓' if ok else '✗ FAILED'}")

    try:
        state.set(
            "sensor.energy_optimizer_selftest",
            value=now.strftime("%H:%M:%S"),
            new_attributes={"friendly_name": "Energy Optimizer Self-Test"},
        )
        chk = _safe_get("sensor.energy_optimizer_selftest")
        lines.append(f"state.set → sensor: {'✓ readback ' + str(chk) if chk else '✗ readback empty'}")
    except Exception as exc:
        lines.append(f"state.set → sensor: ✗ ERROR {exc}")

    async with aiohttp.ClientSession() as session:
        try:
            await _influx_query("SHOW DATABASES", session)
            lines.append("InfluxDB: ✓ reachable")
        except Exception as exc:
            lines.append(f"InfluxDB: ✗ {exc}")

    pre = _safe_get(E_FORECAST_SENSOR)
    pre_ok = pre not in (None, "unknown", "unavailable")
    lines.append(f"forecast sensor before: {('✓ ' + str(pre) + ' rows') if pre_ok else '✗ missing'}")

    lines.append("— running one strategic cycle —")
    await strategic_optimize()

    post = _safe_get(E_FORECAST_SENSOR)
    if post not in (None, "unknown", "unavailable"):
        lines.append(f"forecast sensor after: ✓ {post} rows — the card's dotted"
                     f" lines should appear within one refresh")
    else:
        lines.append("forecast sensor after: ✗ STILL MISSING")
        lines.append(f"last status reason: {_safe_get(E_STATUS_REASON)}")
        lines.append("→ search the FULL log for 'Strategic error' and"
                     " 'Could not publish forecast sensor'")

    report = "\n".join(["• " + l for l in lines])
    persistent_notification.create(
        title=f"Energy Optimizer self-test (v{VERSION})",
        message=report,
        notification_id="energy_optimizer_selftest",
    )
    log.info(f"Self-test result:\n{report}")


@service
async def energy_optimizer_force_run():
    """Callable via Developer Tools → Actions → pyscript.energy_optimizer_force_run"""
    log.info("Manual trigger — running strategic cycle now")
    await strategic_optimize()


# ════════════════════════════════════════════════════════════════════════════
# LOAD BANNER — deployment beacon
# ════════════════════════════════════════════════════════════════════════════
# Exactly ONE info line at (re)load, deliberately kept visible even in quiet
# mode. If the HA log shows old-style chatter (per-slot price dumps, the
# outlook table mirrored to the log), the FIRST check is what version this
# banner reports — and whether a new banner appeared at all after deploying
# (pyscript does not auto-reload; call the pyscript.reload service). If TWO
# banners with different versions appear at one reload, a stale backup copy
# of this script is also loaded from /config/pyscript (pyscript loads every
# .py file there) and must be renamed to .py.bak or removed — it would run
# in parallel and fight this script for the mode helpers.
log.info(
    f"energy_optimizer v{VERSION} loaded "
    f"(quiet logging: VERBOSE={VERBOSE}, LOG_DEBUG={LOG_DEBUG})"
)
