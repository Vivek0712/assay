# Calibration report

## Agreement with hand labels

- golden set: **23** articles labelled by hand
- exact verdict agreement: **96%**
- within one band: **100%**
- severe disagreements (READ vs SKIP): **0**

| expected \ predicted | READ | SKIM | SKIP |
|---|---|---|---|
| **READ** | 2 | 0 | 0 |
| **SKIM** | 0 | 4 | 1 |
| **SKIP** | 0 | 0 | 16 |

### Disagreements

- `SKIM` -> `SKIP` (38) **Dashboards Aren't Going Away, but They Will Take a Back Seat** - Opinion piece, heavily cited (21 primary sources), but the argument is product positioning rather than anything you could act on.

## Coherence with measured signals

- articles checked: **302**
- cap violations: **0**

| cohort | n | median RQS | mean RQS | |
|---|---|---|---|---|
| shows real terminal output | 4 | 72.0 | 70.7 | underpowered (n < 20) |
| no terminal output | 298 | 35.0 | 40.0 |  |
| contains code | 154 | 50.1 | 49.9 |  |
| contains no code | 148 | 33.2 | 30.6 |  |

> Underpowered cohorts are shown for completeness. Do not quote them as separations - there are not enough articles behind the number.
