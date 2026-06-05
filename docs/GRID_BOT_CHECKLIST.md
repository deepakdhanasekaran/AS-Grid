# Grid Bot Checklist

Use this as the working checklist for the grid strategy. It is a planning note, not a test spec.

## Core Trading

- [x] Maker limit grid entries only
- [x] Reduce-only take-profit after entry fills
- [x] Fee-aware minimum grid spacing
- [x] Simple market filter for trends / bad conditions
- [x] Lockdown mode for risky inventory
- [x] Emergency reduction and daily circuit-breaker behavior

## Risk Controls

- [ ] Hard `max_open_orders` cap
- [ ] Hard `max_position_size` / max notional cap
- [ ] Daily loss limit based on realized + unrealized PnL
- [ ] Max leverage validation at startup and runtime
- [ ] Stronger duplicate-order protection when open-order fetch fails

## Grid Management

- [x] Sideways-market scanner and ranking layer
- [ ] Explicit grid re-center / rebuild policy when price drifts too far
- [ ] Clear drift threshold for stale grid range
- [ ] Safer order replacement when market guard toggles on/off
- [ ] Smarter entry cooldown handling after order fetch failures

## Observability

- [x] Log fee-aware guard state
- [x] Log lockdown state persistence
- [x] Basic runtime summary notifications
- [ ] PnL / drawdown summary in logs or notifications
- [ ] Open-order count and risk-state snapshot in runtime summary

## Notes

- Current repo has the core grid loop in place, but the stronger production risk layer is still incomplete.
- Prioritize the hard risk limits first, then the grid rebuild logic, then observability.
