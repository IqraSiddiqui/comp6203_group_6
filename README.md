# comp6203_group_6

### To run overall simulation: python main_competition_playground.py

Our agent (`Company6` in `group6.py`) aims to maximise **net tournament income**: revenue from won contracts minus fleet operating costs and non-delivery penalties. The approach combines feasible vessel scheduling, cost-aware bidding, future-trade planning, and opponent modelling. Several techniques are inspired by the AAMAS 2025 maritime shipping competition; our extensions are not exact reproductions of the published algorithms.

## Optimisations implemented

- **Detailed fleet cost estimation (Adeeba).** Estimate standalone trade costs, total vessel-schedule costs, and incremental insertion costs using ballast and laden fuel, loading/unloading, and waiting/idle consumption. Use incremental costs when comparing candidate schedules.
- **Multi-start, feasibility-checked scheduling (Iqra).** Explore vessel assignments and pickup/delivery insertion positions, validate route feasibility, and choose low-cost schedules. After auctions, prioritise fulfilling awarded contracts and compare candidate plans using payments, incremental costs, and conservative non-delivery penalty estimates.
- **Post-auction local search (optimised).** Try bounded trade reassignments and route insertions; accept only feasible improvements that preserve planned awards and reduce estimated additional cost. If no improvement is found, retain the existing schedule.
- **AAMAS-inspired pre-auction bidding (Kumail).** Construct multiple candidate schedules, use shared-route cost allocation, and blend standalone and allocated cost **50/50** to calculate a base bid. Apply a trade-selection threshold (`P_SELECT = 0.60`) and restrict selected bids to trades represented together in a feasible candidate plan.
- **Diversified pre-auction planning.** (optimised) Vary trade insertion order, vessel choices, and pickup/delivery positions to obtain distinct feasible plans rather than repeatedly finding the same schedule. This improves the information available to trade-frequency selection.
- **Ballast-aware plan ranking.** (optimised) Rank candidate plans by route-consolidation efficiency with a modest penalty for *additional* empty-vessel repositioning. The coefficient (`BALLAST_PENALTY_WEIGHT = 0.25`) is an experimental choice, not a value from the paper.
- **Quality-weighted plan selection.** (optimised) Give stronger feasible plans more influence over trade appearance frequencies and shared-cost allocations, while preserving diversity. The bounded weighting uses `PLAN_QUALITY_STRENGTH = 1.0` and keeps the existing selection threshold and 50/50 base-bid formula.
- **Future-opportunity planning (Serov).** Use announced future trades, vessel-specific travel costs, competitor locations, and estimated competition to identify valuable future positioning. Apply a bounded, conservative future-opportunity discount to bids; future planning remains enabled.
- **Opponent modelling (optimised).** Record auction outcomes and compare predicted market prices with realised second-price payments. Use robust, shrunk calibration and log prediction errors. **Price calibration is diagnostic-only** and does not automatically change bids or exclude trades.
- **Operational diagnostics.** Log pre-auction plan diversity, rankings and weights; bid and trade margins; estimated fuel components; local-search attempts; and post-auction scheduling. These help diagnose performance but do not constitute actual realised MABLE costs.

## Run the agent

Use **PowerShell** from the project directory, with your Python virtual environment active and the course's MABLE dependencies installed. Ensure the latest `group6.py` is present and your playground registers `Company6`.

Run a competition simulation and save the console output:

```powershell
cmd /c "python main_competition_playground.py > simulation.log 2>&1"
```

Check completion, errors, and optimiser diagnostics:

```powershell
Select-String -Path simulation.log -Pattern "ERROR|Traceback|PRE_PLAN_DIVERSITY|PRE_PLAN_RANK|PRE_PLAN_WEIGHTS|PRE_PLAN_SELECTION|LOCAL_SEARCH|POST_AUCTION|Run Finished"
```

Inspect cost and opponent-model diagnostics (optional):

```powershell
Select-String -Path simulation.log -Pattern "FUEL_AUDIT_TOTAL|TRADE_AUDIT|BID_AUDIT|P4_PRICE_AUDIT|P4_CALIBRATION"
```

Calculate **net income** from the latest competition metrics file (the command assumes Group 6 is company ID `0`):

```powershell
$data = Get-Content (Get-ChildItem "metrics_competition_*.json" | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName -Raw | ConvertFrom-Json; $c = @($data.global_metrics.auction_outcomes | ForEach-Object { $_.'0' } | Where-Object { $null -ne $_ }); $revenue = ($c | Measure-Object -Property payment -Sum).Sum; $fuel = [double]$data.company_metrics.'0'.fuel_cost; $penalty = [double]$data.global_metrics.penalty.'0'; Write-Host "Contracts won:" $c.Count "| Revenue:" $revenue "| Fuel:" $fuel "| Penalties:" $penalty "| NET INCOME:" ($revenue - $fuel - $penalty)
```

For the latest quality-weighted version, the observed **single-scenario** result was 9 contracts won, revenue **9,899.18**, fuel cost **14,241.34**, penalties **0**, and net income **-4,342.16**. This is not a guarantee of performance across the coursework tournament; compare net income across multiple scenarios when evaluating alternatives.
