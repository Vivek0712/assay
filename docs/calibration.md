# Calibration report

## Agreement with hand labels

- golden set: **23** articles labelled by hand
- exact verdict agreement: **96%**
- within one band: **100%**
- severe disagreements (READ vs SKIP): **0**

| expected \ predicted | READ | SKIM | SKIP |
|---|---|---|---|
| **READ** | 2 | 0 | 0 |
| **SKIM** | 0 | 5 | 0 |
| **SKIP** | 0 | 1 | 15 |

### Disagreements

- `SKIP` -> `SKIM` (44) **From Root Cause Analysis to AWS-Aligned Solutions with Kiro** - Case study framing with a single five-line snippet and nothing reproducible.

## Coherence with measured signals

- articles checked: **307**
- cap violations: **0**

| cohort | n | median RQS | mean RQS | |
|---|---|---|---|---|
| shows real terminal output | 4 | 62.9 | 59.3 | underpowered (n < 20) |
| no terminal output | 303 | 35.0 | 39.9 |  |
| contains code | 157 | 49.2 | 48.8 |  |
| contains no code | 150 | 33.8 | 31.1 |  |

> Underpowered cohorts are shown for completeness. Do not quote them as separations - there are not enough articles behind the number.
