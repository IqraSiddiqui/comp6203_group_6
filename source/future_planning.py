#person 4 add your code here
"""
Person 4 - Future Planning + Opponent Modelling   (COMP6203, Lab 3 Ex 1-3)
===========================================================================

Paste this class into groupn.py ABOVE Companyn and inherit from it FIRST:

    class Companyn(FuturePlanningMixin, TradingCompany):
        ...

What it gives the rest of the team
----------------------------------
For Person 3 (bidding / inform):
    self.snapshot_competitors()                    call once at the start of inform()
    self.future_trade_bonus(vessel, trade, cost, schedule=None)
          -> £ amount the trade is worth EXTRA because of where it leaves the
             vessel for next month's cargoes. Subtract it from the cost before bidding.
    self.estimated_competitor_bid(trade)
          -> (predicted lowest competitor bid, company name) or (None, None)
    self.expected_profit(trade, our_cost)
          -> predicted payment - our cost. Useful for ranking trades when a
             vessel can only take some of them.
    self.record_our_bid(trade, cost, bid)          call for every bid you submit

For Person 2 (receive):
    self.update_opponent_models(auction_ledger)    call at the start of receive()

Report / analysis:
    self.opponent_summary()                        dict with the learned factors

Auction note for the team
-------------------------
The market uses a SECOND-price reverse auction. If we win, we are paid the
second-lowest bid, not our own. So:
  * Raising our bid does not raise what we are paid. It only raises the
    chance of losing. Bidding close to our true (effective) cost is the
    baseline strategy.
  * The `payment` shown in auction_ledger is the RUNNER-UP's bid, not the
    winner's. The factors learned below therefore describe "the price the
    market clears at". That is exactly what we need to predict what we
    would be paid.
Our "true cost" for a trade = marginal schedule cost (P1/P2) minus
future_trade_bonus (P4). That is where P4 plugs into the bid.

Only public information is used: competitor fleets, their vessels' current
locations and capacities (as in Lab 3 Ex 2), and the auction ledger (Lab 3 Ex 3).
Nothing reads competitors' schedules or bids.
"""

import statistics
import math


class FuturePlanningMixin:

    # ---- tuning knobs (good things to vary in the report's experiments) ----
    FUTURE_WEIGHT = 0.6         # how much of the estimated saving we trust
    FUTURE_CAP_FRACTION = 0.30  # bonus never exceeds 30% of the trade's cost
    FUTURE_TOP_K = 2            # count the best K follow-on trades (decaying)
    FUTURE_DECAY = 0.5          # weight of the 2nd-best, 3rd-best, ... trade
    DEFAULT_MARKET_FACTOR = 1.2 # assumed competitor markup before we have data
    MIN_SAMPLES = 3             # ledger samples needed before trusting a factor
    FACTOR_CLIP = (0.5, 5.0)    # discard silly ratios (bad cost guesses)

    # ------------------------------------------------------------------ state
    def _p4_init(self):
        if getattr(self, "_p4_ready", False):
            return
        self._p4_ready = True
        self._future_trades = []          # trades of NEXT month's auction
        self._future_auction_time = None
        self._p4_dist_cache = {}
        self._p4_comp_snapshot = {}       # company name -> [(vessel, location)]
        self._p4_ratios = {}              # company name -> [payment / est. cost]
        self._p4_market_ratios = []       # all companies pooled
        self._p4_our_bids = {}            # trade key -> (our cost, our bid)
        self._p4_lost = []                # (payment / our cost) on trades we lost
        self._p4_won = []                 # (payment / our cost) on trades we won

    # ======================================================== pre_inform
    def pre_inform(self, trades, time):
        """Called by the market before inform(): `trades` will be auctioned at `time`."""
        self._p4_init()
        self._future_trades = list(trades) if trades else []
        self._future_auction_time = time

    # ============================================================ helpers
    @staticmethod
    def _p4_key(trade):
        """Stable key for a trade, so dicts don't depend on object identity."""
        return (str(trade.origin_port), str(trade.destination_port),
                round(float(trade.amount), 3),
                getattr(trade, "earliest_pickup", None),
                getattr(trade, "latest_drop_off", None))

    def _p4_distance(self, a, b):
        key = (str(a), str(b))
        if key not in self._p4_dist_cache:
            try:
                d = float(self.headquarters.get_network_distance(a, b))
                if not math.isfinite(d) or d < 0:
                    d = float("inf")
            except (TypeError, ValueError, AttributeError):
                d = float("inf")   # unreachable / unknown pair
            self._p4_dist_cache[key] = d
        return self._p4_dist_cache[key]

    @staticmethod
    def _p4_ballast_cost(vessel, distance):
        """Fuel cost of sailing `distance` NM empty."""
        if distance == float("inf"):
            return float("inf")
        if distance <= 0:
            return 0.0
        t = vessel.get_travel_time(distance)
        return vessel.get_cost(vessel.get_ballast_consumption(t, vessel.speed))

    def _p4_standalone_cost(self, vessel, trade, start_location=None):
        """Lab 3 Ex 3 style cost: reach origin + load + laden trip + unload.
        Used to cost a COMPETITOR's vessel. P1's functions cost our own vessels."""
        start = start_location if start_location is not None else vessel.location
        ballast = self._p4_ballast_cost(vessel, self._p4_distance(start, trade.origin_port))
        load_t = vessel.get_loading_time(trade.cargo_type, trade.amount)
        load = vessel.get_cost(vessel.get_loading_consumption(load_t))
        unload = vessel.get_cost(vessel.get_unloading_consumption(load_t))
        laden_d = self._p4_distance(trade.origin_port, trade.destination_port)
        laden_t = vessel.get_travel_time(laden_d)
        laden = vessel.get_cost(vessel.get_laden_consumption(laden_t, vessel.speed))
        return ballast + load + laden + unload

    def _p4_end_location(self, vessel):
        """Where `vessel` will be after its current schedule finishes."""
        try:
            simple = vessel.schedule.get_simple_schedule()
            if simple:
                action, trade = simple[-1]
                return (trade.destination_port if action == "DROP_OFF"
                        else trade.origin_port)
        except Exception:
            pass
        return vessel.location

    @staticmethod
    def _p4_can_carry(vessel, trade):
        """Support MABLE capacity APIs; do not assume an unknown vessel can carry."""
        for attr in ("get_max_capacity", "capacity"):
            method = getattr(vessel, attr, None)
            if callable(method):
                try:
                    return float(method(trade.cargo_type)) >= float(trade.amount)
                except (TypeError, ValueError, AttributeError):
                    continue
        fallback = getattr(vessel, "hold_capacity", None)
        if fallback is not None:
            return float(fallback) >= float(trade.amount)
        return False

    # ===================================== Lab 3 Ex 1: distance to next cargo
    def closest_future_trade(self, trade):
        """(future trade, distance NM) nearest to where `trade` drops off."""
        self._p4_init()
        best, best_d = None, float("inf")
        for f in self._future_trades:
            d = self._p4_distance(trade.destination_port, f.origin_port)
            if d < best_d:
                best, best_d = f, d
        return best, best_d

    # ========================= Lab 3 Ex 2: competitors' closest vessels
    def snapshot_competitors(self):
        """Record competitor vessel positions BEFORE the auction. Call in inform().
        receive() runs after vessels may have moved, so we cost from this snapshot."""
        self._p4_init()
        snap = {}
        for company in self.headquarters.get_companies():
            if company.name == self.name:
                continue
            snap[company.name] = [(v, v.location) for v in company.fleet]
        self._p4_comp_snapshot = snap

    def find_closest_competitor_vessels(self, trade):
        """{company name: (vessel, distance NM to trade origin)} for every competitor."""
        self._p4_init()
        if not self._p4_comp_snapshot:
            self.snapshot_competitors()
        result = {}
        for name, vessels in self._p4_comp_snapshot.items():
            best = None
            for v, loc in vessels:
                if not self._p4_can_carry(v, trade):
                    continue
                d = self._p4_distance(loc, trade.origin_port)
                if best is None or d < best[1]:
                    best = (v, d)
            if best is not None:
                result[name] = best
        return result

    def competitor_costs(self, trade):
        """{company name: estimated cheapest standalone cost for that company}."""
        self._p4_init()
        if not self._p4_comp_snapshot:
            self.snapshot_competitors()
        out = {}
        for name, vessels in self._p4_comp_snapshot.items():
            costs = [self._p4_standalone_cost(v, trade, loc)
                     for v, loc in vessels if self._p4_can_carry(v, trade)]
            costs = [c for c in costs if c != float("inf")]
            if costs:
                out[name] = min(costs)
        return out

    # ================================================ future-opportunity score
    def future_trade_bonus(self, vessel, trade, cost=None, schedule=None):
        """£ value of `vessel` ending at trade.destination_port, because of next
        month's cargoes.

        For each future trade f:
          baseline = cheapest ballast from ANY of our vessels' current end points to f
                     (if another ship of ours is already close, repositioning this
                     one adds little)
          new      = ballast from trade.destination_port to f, by this vessel
          saving   = max(0, baseline - new)
          p_win    = rough chance we win f, from how close the nearest competitor is
        Bonus = FUTURE_WEIGHT * sum of the top-K (p_win * saving), with decay, capped
        at FUTURE_CAP_FRACTION * cost.
        """
        self._p4_init()
        if not self._future_trades:
            return 0.0  # last month, or pre_inform not called yet

        end_time = None
        if schedule is not None:
            try:
                end_time = schedule.completion_time()
            except Exception:
                end_time = None

        values = []
        for f in self._future_trades:
            if not self._p4_can_carry(vessel, f):
                continue
            new_d = self._p4_distance(trade.destination_port, f.origin_port)
            if new_d == float("inf"):
                continue

            # Rough time check: can we reach f's pickup in time?
            if end_time is not None and f.latest_pickup is not None:
                if end_time + vessel.get_travel_time(new_d) > f.latest_pickup:
                    continue

            base_d = min(self._p4_distance(self._p4_end_location(v), f.origin_port)
                         for v in self.fleet)
            if base_d == float("inf"):
                base_d = new_d * 2  # nothing of ours can currently reach it
            saving = (self._p4_ballast_cost(vessel, base_d)
                      - self._p4_ballast_cost(vessel, new_d))
            if saving <= 0:
                continue

            comp = self.find_closest_competitor_vessels(f)
            comp_d = min((d for _, d in comp.values()), default=float("inf"))
            if comp_d == float("inf"):
                p_win = 0.9
            else:
                p_win = comp_d / (comp_d + new_d + 1e-9)  # closer than them -> >0.5
                p_win = min(0.9, max(0.1, p_win))
            values.append(p_win * saving)

        if not values:
            return 0.0
        values.sort(reverse=True)
        total = sum(v * (self.FUTURE_DECAY ** i)
                    for i, v in enumerate(values[: self.FUTURE_TOP_K]))
        bonus = self.FUTURE_WEIGHT * total
        if cost is not None and cost > 0:
            bonus = min(bonus, self.FUTURE_CAP_FRACTION * cost)
        return float(bonus)

    # =================================== Lab 3 Ex 3: auction-outcome analysis
    def record_our_bid(self, trade, cost, bid):
        """Person 3: call for every bid submitted, so we can learn from outcomes."""
        self._p4_init()
        self._p4_our_bids[self._p4_key(trade)] = (cost, bid)

    def update_opponent_models(self, auction_ledger):
        """Person 2: call at the start of receive(contracts, auction_ledger=None, ...).

        auction_ledger = {company name: [Contract(trade, payment), ...]}
        """
        self._p4_init()
        if not auction_ledger:
            return
        for name, contracts in auction_ledger.items():
            for c in contracts or []:
                trade, payment = c.trade, c.payment
                key = self._p4_key(trade)
                ours = self._p4_our_bids.get(key)

                if name == self.name:
                    if ours and ours[0] > 0:
                        self._p4_won.append(payment / ours[0])
                    continue

                # A competitor won. Estimate their cost from the pre-auction snapshot.
                vessels = self._p4_comp_snapshot.get(name)
                if vessels:
                    costs = [self._p4_standalone_cost(v, trade, loc)
                             for v, loc in vessels if self._p4_can_carry(v, trade)]
                    costs = [x for x in costs if 0 < x < float("inf")]
                    if costs:
                        r = payment / min(costs)
                        if self.FACTOR_CLIP[0] <= r <= self.FACTOR_CLIP[1]:
                            self._p4_ratios.setdefault(name, []).append(r)
                            self._p4_market_ratios.append(r)

                if ours and ours[0] > 0:
                    self._p4_lost.append(payment / ours[0])
        self._p4_our_bids = {}

    def competitor_factor(self, name):
        """Learned payment/cost factor for one competitor (median, robust to outliers)."""
        self._p4_init()
        r = self._p4_ratios.get(name, [])
        if len(r) >= self.MIN_SAMPLES:
            return statistics.median(r)
        return self.market_factor()

    def market_factor(self):
        self._p4_init()
        if len(self._p4_market_ratios) >= self.MIN_SAMPLES:
            return statistics.median(self._p4_market_ratios)
        return self.DEFAULT_MARKET_FACTOR

    def estimated_competitor_bid(self, trade):
        """(lowest predicted competitor bid, company name). This approximates what we
        would be paid if we win, because the auction is second-price."""
        best, who = None, None
        for name, c in self.competitor_costs(trade).items():
            b = c * self.competitor_factor(name)
            if best is None or b < best:
                best, who = b, name
        return best, who

    def expected_profit(self, trade, our_cost):
        """Predicted payment minus our cost (None if no competitor can carry it)."""
        bid, _ = self.estimated_competitor_bid(trade)
        return None if bid is None else bid - our_cost

    def lost_margin_hint(self):
        """Median of (payment / our cost) on trades we LOST. Below ~1.0 means
        competitors clear below our cost: our cost model or markup is too high."""
        self._p4_init()
        return statistics.median(self._p4_lost) if self._p4_lost else None

    def opponent_summary(self):
        """Learned figures for logging and for the report."""
        self._p4_init()
        return {
            "per_company_factor": {n: round(statistics.median(r), 3)
                                   for n, r in self._p4_ratios.items() if r},
            "samples": {n: len(r) for n, r in self._p4_ratios.items()},
            "market_factor": round(self.market_factor(), 3),
            "won_payment_over_cost": (round(statistics.median(self._p4_won), 3)
                                      if self._p4_won else None),
            "lost_payment_over_cost": (round(self.lost_margin_hint(), 3)
                                       if self._p4_lost else None),
        }
