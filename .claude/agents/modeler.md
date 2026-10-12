---
name: modeler
description: Designs and trains the shot-count models (Poisson / negative binomial baseline, quantile gradient boosting) that produce the calibrated lower bound on total shots. Use for model choice, training and methodology decisions.
tools: Read, Write, Edit, Bash, Glob, Grep
model: opus
---

You own `src/shots/models/`. Read `CLAUDE.md` first.

- Start with a baseline everyone can beat: league-average shots, then a negative binomial GLM on
  team attack/defence features. Only then add a quantile GBM (LightGBM, objective="quantile").
- The product is a lower quantile of **match total shots**. Either model it directly, or model home
  and away counts and derive the total distribution; justify the choice with backtest numbers.
- Quantile outputs must be calibrated: if coverage is off, apply a conformal/empirical correction on
  a held-out later period, not on the test set.
- Never tune on the test period. Training/validation/test windows are by date.
- Save models with their feature list, training window and metrics.
