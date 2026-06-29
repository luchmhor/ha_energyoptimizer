# Energy Optimizer for Home Assistant

Cost-optimal battery control for a small PV + battery system (APsystems EZHI
class hardware) driven by EPEX day-ahead spot prices. A linear-program (LP)
cost optimizer plans 15-minute slots over up to 48 h; a fast tactical
controller executes the plan against real measured grid flow; a Plotly
dashboard card shows history, the live plan and the price curve in one chart.

**Current version: v2026.06.10k** — the strategy file and the tactical
automation are version-locked and must always be deployed as a pair.

---

## 1. Architecture

```
                       ┌──────────────────────────────────────────────┐
  EPEX spot prices ───►│  STRATEGY LAYER (pyscript, every 15 min)     │
  Solcast PV forecast ►│  energy_optimizer.py                         │
  InfluxDB history ───►│  · LP cost optimizer (scipy linprog)         │
  Battery SoC ────────►│  · picks ONE strategy for the current slot   │
                       └───────────────┬──────────────────────────────┘
                                       │ writes mode_id 0/1/2
                                       ▼
                       ┌──────────────────────────────────────────────┐
  Grid meter (SHRDZM) ►│  TACTICAL LAYER (HA automation, every 5 s)   │
  Battery SoC ────────►│  energy_optimizer_tactical.yaml              │
                       │  · proportional grid-following controller    │
                       │  · safety clamps, anti-curtailment, ceiling  │
                       └───────────────┬──────────────────────────────┘
                                       │ number.set_value
                                       ▼
                            EZHI inverter (max_output_power)

  Outputs: live outlook markdown · rotated daily records · forecast CSV ·
  InfluxDB forecast series · sensor.energy_optimizer_forecast (dashboard)
```

The **strategy layer** expresses its intent purely through a mode ID
(0 = FOLLOW_GRID, 1 = HOLD, 2 = GRID_CHARGE). The **tactical layer**
deliberately ignores the numeric setpoint and decides wattage in real time
from measured grid flow — forecast errors therefore never leak power to the
grid. The **dashboard card** reads everything from native HA sources.

### The three strategies (as this hardware executes them)

| Mode | ID | Hardware behaviour |
|------|----|--------------------|
| FOLLOW_GRID | 0 | Track grid flow toward zero: PV covers the load first, battery covers the deficit; with PV surplus the battery charges via DC. Never imports needlessly, never exports. |
| HOLD | 1 | Inverter AC output 0. On this hardware that means the **full load is imported** and **all PV charges the battery**. Used to ration a limited battery for a pricier slot ahead. |
| GRID_CHARGE | 2 | Actively import to charge, up to the hardware rate, capped by a 95 % SOC ceiling enforced in both layers. |

---

## 2. Dependencies

### Integrations / add-ons

| Dependency | Source | Notes |
|---|---|---|
| [pyscript](https://github.com/custom-components/pyscript) **≥ 1.4** | HACS | Needs `allow_all_imports: true`. Versions < 1.4 lack `@pyscript_compile` / `task.executor` — the module will fail to load with a `NameError`. |
| **scipy** | Python env | `scipy.optimize.linprog` is imported at module level — it must be importable by HA's Python (Core/Container: `pip install scipy`; HAOS: requires an image that ships it). Without scipy the file does not load at all. |
| [EPEX Spot](https://github.com/mampfes/ha_epex_spot) | HACS | Provides `sensor.epex_spot_data_total_price` with the `data` attribute (today + tomorrow, `start_time` / `price_per_kwh`). |
| [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar) | HACS | Provides `..._forecast_today` / `..._forecast_tomorrow` with `detailedHourly`. |
| InfluxDB 1.x + HA `influxdb` integration | — | History source for the consumption profile and PV actuals; also receives the plan for Grafana. The HA integration must export the consumption and PV entities (measurement `W`). |
| [plotly-graph-card](https://github.com/dbuezas/lovelace-plotly-graph-card) | HACS | Use a **recent** version — the card's filter functions must receive `hass` (older builds throw `hass is undefined` on the forecast traces). |

### Entities consumed (adapt to your installation)

These IDs are configured at the top of `energy_optimizer.py`, in the
tactical YAML's `variables:` block, and in the card:

| Purpose | Default entity |
|---|---|
| Battery SoC | `sensor.ezhi_battery_state_of_charge` |
| Inverter setpoint (writable, **must accept negative values**) | `number.apsystems_ezhi_max_output_power` |
| Grid meter, signed (positive = import) | `sensor.shrdzm_485519e15aae_16_7_0` |
| PV power (live, for the card) | `sensor.ezhi_photovoltaic_power` |
| Inverter AC output (card) | `sensor.ezhi_on_grid_power` |
| EPEX price | `sensor.epex_spot_data_total_price` |
| Solcast today / tomorrow / next hour | `sensor.solcast_pv_forecast_forecast_*` |
| InfluxDB entity IDs (history) | `total_consumption`, `ezhi_photovoltaic_power` |

---

## 3. Installation

### Step 1 — configuration.yaml

```yaml
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
    # 0=FOLLOW_GRID  1=HOLD  2=GRID_CHARGE — must match MODE_IDS in the script

input_text:
  energy_optimizer_mode:
    name: Energy Optimizer Mode
    max: 32
    icon: mdi:battery-charging
  energy_optimizer_reason:
    name: Energy Optimizer Reason
    max: 255
    icon: mdi:information-outline

# Optional: expose the live outlook markdown to a dashboard markdown card
command_line:
  - sensor:
      name: energy_optimizer_outlook
      command: "python3 -c \"import json; f=open('/config/www/energy_outlook.md'); print(json.dumps({'content': f.read()}))\""
      scan_interval: 1800
      value_template: "OK"
      json_attributes:
        - content

# Strongly recommended: the forecast sensor refreshes ~15 kB of attributes
# every 15 minutes — keep it out of the database.
recorder:
  exclude:
    entities:
      - sensor.energy_optimizer_forecast
```

Restart HA after adding helpers / recorder changes.

### Step 2 — strategy layer

1. Copy `energy_optimizer.py` to `/config/pyscript/`.
2. Open the file and adapt the **CONFIGURATION** block: entity IDs, battery
   size and power limits, network fee, timezone, InfluxDB URL/credentials.
   (Recommended: move `INFLUX_PASS` out of the file via pyscript's config /
   `!secret` mechanism rather than leaving it inline.)
3. Make sure `/config/pyscript/` contains **no other copy** of the script —
   pyscript loads *every* `.py` file in that folder; a stale backup runs in
   parallel and fights for the helpers. Rename backups to `.py.bak`.
4. Call the **`pyscript.reload`** service (pyscript does not auto-reload on
   file changes).
5. Verify the **load banner** in the HA log — exactly one line:

   ```
   energy_optimizer v2026.06.10k loaded (quiet logging: VERBOSE=False, LOG_DEBUG=False)
   ```

   Two banners with different versions = a duplicate copy is loaded.
   No banner = the file failed to load; search the log for
   `Exception in </config/pyscript/energy_optimizer`.

### Step 3 — tactical layer

1. Create a new automation, switch to YAML mode, and paste
   `energy_optimizer_tactical.yaml`.
2. Adapt the entity IDs in `variables:` (inverter number entity, grid meter,
   SoC sensor) and verify on your hardware:
   * the inverter number entity **accepts negative values** (charging) — if
     its minimum is 0, the charge path silently clamps;
   * the grid meter's sign convention is **positive = import**;
   * with output 0 (HOLD), PV charges the battery via DC — the strategy
     layer's HOLD model assumes exactly this.
3. The constants marked `# must match …` (`soc_empty`, `soc_grid_ceiling`,
   `abs_max_charge/dis`) mirror the Python file — change them in **both**
   files or neither.

### Step 4 — dashboard

* **Overview chart**: add a manual card and paste `energy_overview_card.yaml`
  (plotly-graph). It contains the live power traces, SoC, the price as a
  joined past(recorder)+future(attribute) pair, the now-line, and the dotted
  *planned* consumption / PV / SoC traces fed by
  `sensor.energy_optimizer_forecast`.
* **Outlook table** (optional, needs the command_line sensor from step 1):

  ```yaml
  type: markdown
  content: "{{ state_attr('sensor.energy_optimizer_outlook', 'content') }}"
  ```

### Step 5 — verify end to end

Run **`pyscript.energy_optimizer_self_test`** (Developer Tools → Actions).
It probes every link — version, triggers, input sensors, file writes,
`state.set`, InfluxDB — then runs one real cycle and reports whether the
forecast sensor exists, as a persistent notification with ✓/✗ per line.
The first ✗ from the top is the broken link.

Quick manual checks:

```jinja
{{ states('input_text.energy_optimizer_reason') }}
{{ states('sensor.energy_optimizer_forecast') }}   {# row count, ~100–290 #}
{{ (state_attr('sensor.energy_optimizer_forecast','data') or [])
   | selectattr('forecast') | list | count }}      {# future rows > 0 #}
```

The dotted forecast traces appear on the card within one refresh once the
sensor carries future rows. Note the sensor vanishes on every HA restart and
reappears after the first strategic cycle (a pre-restart forecast would be
stale by design).

---

## 4. Key configuration constants (strategy layer)

| Constant | Default | Meaning |
|---|---|---|
| `BATTERY_SIZE_WH` / `OUTPUT_MIN_W` / `OUTPUT_MAX_W` | 2760 / −1200 / 1200 | Battery capacity and hardware charge/discharge limits. |
| `BATTERY_EMPTY_PCT` / `BATTERY_FULL_PCT` | 15 / 98 | Working SOC window of the LP. |
| `NETWORK_FEE_CT_PER_KWH` | 10.5 | Added to every EPEX price; also added in the card's price traces. |
| `PV_COST_CT` | 4.5 | Refill/terminal floor value of stored energy. The LP never grid-charges above ≈ this merely to end the horizon full. |
| `BATTERY_THROUGHPUT_COST_CT` | 0.5 | Wear per charge/discharge leg; suppresses economically pointless cycling. |
| `TERMINAL_VALUE_REFERENCE` | `pv_cost` | Leave at `pv_cost` — higher references reintroduce the "buy a full battery at the evening peak" failure. |
| `GRID_CHARGE_SOC_CEILING_PCT` | 95 | Grid charging may never push SOC above this (enforced in the LP **and** in real time by the tactical layer). PV may still fill to 98 %. |
| `CONSUMPTION_QUANTILE` | 0.75 | Per-slot quantile of the 4 same-weekday history samples. Conservative high-side; set 0.5 to A/B the over-banking bias. |
| `CONSUMPTION_SMOOTH_SLOTS` | 3 | Centered moving-average window (odd) over the consumption profile, calming the few-sample sawtooth that caused strategy flip-flop. 1 = off. |
| `SOC_CRITICAL_PCT` / `SOC_RECOVER_PCT` | 12 / 15 | Emergency HOLD latch with hysteresis; auto-replans on recovery. |
| `HISTORY_RETENTION_DAYS` | 0 | > 0 prunes daily history files older than N days each night. 0 = keep forever. |
| `VERBOSE` / `LOG_DEBUG` | False / False | See *Logging* below. |
| `WARN_COOLDOWN_MIN` | 30 | One warning per failure source per window. |

---

## 5. Outputs

| Output | Where | Notes |
|---|---|---|
| Mode + setpoint | `input_number.energy_optimizer_mode_id` / `_setpoint` | The contract with the tactical layer. Setpoint is informational; the mode is authoritative. |
| Status | `input_text.energy_optimizer_mode` / `_reason` | Human-readable current decision. |
| Live outlook | `/config/www/energy_outlook.md` | Executed history (✓ rows) + forecast, merged into strategy windows with a "Why" column. On a failed cycle the last good table is kept under a ⚠️ STALE banner instead of being wiped. |
| Daily records | `/config/www/energy_history/energy-optimizer-YYYY-MM-DD.md` | Refreshed intraday; finalized at 00:01 the next night as an executed-only record of the full day. |
| Forecast CSV | `/config/www/energy_forecast.csv` | Full 15-min resolution, machine-readable. |
| InfluxDB | measurement `energy_optimizer_forecast` | Tag `phase` (executed/forecast); `strategy` and `minutes_ahead` are **fields**. Each slot carries its final pre-execution forecast and the executed truth — plan-vs-actual in Grafana is trivial. |
| Dashboard sensor | `sensor.energy_optimizer_forecast` | `data` attribute = **future** slots only, compact keys (`t,s,c,p,g,b,pr,so`), auto-thinned to stay under the recorder's 16 KB attribute cap. Created automatically; still best excluded from the recorder. |

---

## 6. Logging

The script self-gates: with `VERBOSE = False` the routine diagnostics are not
emitted **at all**, regardless of any `logger:` configuration. What reaches
the HA log: the one-line load banner, one INFO line whenever the *strategy
changes*, rare events (SOC emergency/recovery, manual run, nightly finalize),
rate-limited warnings, and all errors. Set `VERBOSE = True` (+ reload) for
full diagnostics at INFO; `LOG_DEBUG = True` additionally enables the per-slot
price and per-hour PV dumps.

---

## 7. Operations & troubleshooting

* **Deploying a new version**: copy the file → `pyscript.reload` → check the
  banner. The tactical YAML's version string must match
  (`alias` + description header).
* **Force a cycle**: `pyscript.energy_optimizer_force_run`.
* **Anything broken**: `pyscript.energy_optimizer_self_test` first — it
  pinpoints the failing link in seconds.
* **Outlook shows ⚠️ STALE**: the banner names the reason (missing SOC,
  price or PV data). The previous good table is preserved beneath it.
* **No dotted lines on the card**: the forecast sensor must exist
  (see step 5). It is wiped by an HA restart / pyscript reload and is
  republished automatically by the startup cycle within ~30–120 s (or call
  `pyscript.energy_optimizer_force_run` to restore it immediately).
* **Price history range**: the past price trace covers the recorder's
  `purge_keep_days` (default 10). For longer in-card ranges switch that trace
  to `statistic: mean` + `period: auto` (needs long-term statistics on the
  EPEX sensor); for months-scale analysis use Grafana on InfluxDB.
* **Known limits**: the LP slightly underestimates HOLD's cost in daylight
  deficit slots (documented model/hardware approximation — the outlook and
  history report the true behaviour); daily history keys assume 96
  slots/day, so the two DST transition days have one cosmetic collision/gap.

## 8. Safety behaviour (summary)

Discharge is forbidden below 15 % SOC in both layers independently; below
12 % an edge-triggered emergency forces HOLD and notifies, clearing with
hysteresis at 15 % and replanning automatically. Export is never commanded;
with a full battery, a latched anti-curtailment override covers the load from
the battery so PV stays alive. Grid charging stops at the 95 % ceiling in
real time. If the grid meter or SoC sensor goes unavailable, the tactical
layer ramps the inverter to idle instead of freezing the last command.
