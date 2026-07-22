"""Fee math tests.

The NATO regression case at the bottom is the reason this module exists.
"""
import math

import pytest

from edge_engine.ingest.models import Venue
from edge_engine.sizing.fees import (
    cheaper_venue,
    kalshi_fee,
    min_viable_gross_edge,
    polymarket_fee,
    polymarket_rate,
    total_fees,
)


class TestKalshiFee:
    def test_peaks_at_fifty_cents(self):
        # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> ceil to cent -> 0.02
        assert kalshi_fee(0.50, 1) == pytest.approx(0.02)

    def test_symmetric_around_fifty(self):
        assert kalshi_fee(0.30, 100) == pytest.approx(kalshi_fee(0.70, 100))

    def test_cheap_at_extremes(self):
        # The favorite-longshot edge lives at the extremes, where fees are cheapest.
        assert kalshi_fee(0.90, 100) < kalshi_fee(0.50, 100) / 2

    def test_ceiling_is_a_small_size_penalty(self):
        """One contract pays a full cent; 100 contracts pay the true rate."""
        one = kalshi_fee(0.10, 1)
        hundred_per_contract = kalshi_fee(0.10, 100) / 100
        assert one == pytest.approx(0.01)
        assert hundred_per_contract < one
        # true rate: 0.07 * 0.10 * 0.90 = 0.0063
        assert hundred_per_contract == pytest.approx(0.0063, abs=1e-4)

    def test_hundred_contracts_at_fifty(self):
        # 0.07 * 100 * 0.25 = 1.75, already whole cents
        assert kalshi_fee(0.50, 100) == pytest.approx(1.75)

    @pytest.mark.parametrize("price", [0.0, 1.0, -0.1, 1.5])
    def test_degenerate_prices_are_free(self, price):
        assert kalshi_fee(price, 100) == 0.0

    def test_zero_contracts(self):
        assert kalshi_fee(0.5, 0) == 0.0


class TestPolymarketFee:
    def test_geopolitics_is_free(self):
        assert polymarket_rate("geopolitics") == 0.0
        assert polymarket_fee(0.50, 1000, category="geopolitics") == 0.0

    def test_politics_cheaper_than_kalshi(self):
        poly = polymarket_fee(0.65, 100, category="politics")
        kalshi = kalshi_fee(0.65, 100)
        assert poly < kalshi
        # 0.04 vs 0.07 -> roughly 43% cheaper
        assert poly / kalshi == pytest.approx(4 / 7, abs=0.02)

    def test_crypto_matches_kalshi_rate(self):
        assert polymarket_rate("crypto") == 0.07

    def test_makers_and_sells_are_free(self):
        assert polymarket_fee(0.5, 100, category="sports", maker=True) == 0.0
        assert polymarket_fee(0.5, 100, category="sports", is_sell=True) == 0.0

    def test_per_market_override_beats_category_table(self):
        """The API's takerBaseFee is authoritative; the table is only a fallback."""
        assert polymarket_fee(0.5, 100, category="crypto", rate_override=0.0) == 0.0

    def test_unknown_category_falls_back(self):
        assert polymarket_rate("something-new") == 0.05

    def test_takerbasefee_unit_is_rejected_loudly(self):
        """Regression: `takerBaseFee` is 1000, not a rate.

        Passing it through unguarded inflated fees ~1000x and silently rejected
        every candidate in the universe while the scanner appeared healthy.
        """
        with pytest.raises(ValueError, match="implausible"):
            polymarket_rate("politics", override=1000)

    def test_valid_override_still_accepted(self):
        assert polymarket_rate("politics", override=0.05) == 0.05
        assert polymarket_rate("politics", override=0.0) == 0.0

    def test_no_ceiling_penalty(self):
        """Unlike Kalshi, one contract pays the true rate, not a forced cent."""
        assert polymarket_fee(0.10, 1, category="politics") == pytest.approx(
            0.04 * 0.10 * 0.90, abs=1e-6
        )


class TestVenueRouting:
    def test_routes_politics_to_polymarket(self):
        venue, saving, _ = cheaper_venue(0.65, 100, "politics")
        assert venue is Venue.POLYMARKET
        assert saving > 0

    def test_routes_geopolitics_to_polymarket_for_free(self):
        venue, saving, pct = cheaper_venue(0.50, 100, "geopolitics")
        assert venue is Venue.POLYMARKET
        assert saving == pytest.approx(kalshi_fee(0.50, 100))
        assert pct > 0.03  # >3% of notional saved on a 50c contract

    def test_crypto_is_a_wash_or_favors_kalshi_ceiling_aside(self):
        venue, saving, _ = cheaper_venue(0.50, 100, "crypto")
        assert saving == pytest.approx(0.0, abs=0.01)


class TestMultiLegArbEconomics:
    """The general result that decides where combinatorial arb is worth running."""

    def test_fee_burden_converges_to_full_rate_as_legs_grow(self):
        for n in (4, 10, 50):
            prices = [1.0 / n] * n
            burden = min_viable_gross_edge(prices, Venue.KALSHI)
            assert burden == pytest.approx(0.07 * (1 - 1.0 / n), abs=1e-9)
        # 50 legs -> needs ~6.9% gross edge just to break even
        assert min_viable_gross_edge([1 / 50] * 50, Venue.KALSHI) > 0.068

    def test_geopolitics_needs_only_positive_edge(self):
        prices = [0.2] * 5
        assert min_viable_gross_edge(
            prices, Venue.POLYMARKET, category="geopolitics"
        ) == 0.0

    def test_polymarket_politics_burden_is_lower_than_kalshi(self):
        prices = [0.125] * 8
        assert min_viable_gross_edge(prices, Venue.POLYMARKET, category="politics") < \
               min_viable_gross_edge(prices, Venue.KALSHI)


class TestNatoRegression:
    """Live Kalshi event KXNEXTNATOSECGEN-99, captured 2026-07-21.

    Sum of YES asks was $0.98 across 8 candidates: a naive scanner reports
    2.04% risk-free profit. With real fees it is a LOSS. This is the single
    most important test in the suite - it is the exact false positive that
    every published arb scanner emits.
    """

    PRICES = [0.14, 0.10, 0.10, 0.10, 0.10, 0.12, 0.17, 0.15]

    def test_gross_looks_profitable(self):
        assert sum(self.PRICES) == pytest.approx(0.98)
        assert 1.0 - sum(self.PRICES) == pytest.approx(0.02)

    def test_one_contract_each_loses_money(self):
        gross = 1.0 - sum(self.PRICES)
        fees = total_fees([(p, 1) for p in self.PRICES], Venue.KALSHI)
        assert fees == pytest.approx(0.08)  # 8 legs x forced 1c
        assert gross - fees < 0
        assert gross - fees == pytest.approx(-0.06, abs=1e-9)

    def test_still_loses_at_scale_so_it_is_not_just_the_ceiling(self):
        """Scaling up removes the ceiling penalty and it STILL loses.

        The real problem is that fees are paid on every leg, and
        sum(p*(1-p)) across 8 cheap legs is ~0.855 -> ~6% of stake, against a
        2% gross edge.
        """
        qty = 100
        gross = (1.0 - sum(self.PRICES)) * qty
        fees = total_fees([(p, qty) for p in self.PRICES], Venue.KALSHI)
        assert gross == pytest.approx(2.00)
        assert fees > gross
        assert gross - fees < -3.0

    def test_burden_formula_predicts_the_loss(self):
        burden = min_viable_gross_edge(self.PRICES, Venue.KALSHI)
        gross_edge = 1.0 - sum(self.PRICES)
        assert burden == pytest.approx(0.07 * 0.8546, abs=1e-3)
        assert gross_edge < burden  # required edge not met -> correctly rejected

    def test_same_book_would_clear_on_polymarket_geopolitics(self):
        """Identical prices, zero-fee venue: the 2% is real."""
        burden = min_viable_gross_edge(
            self.PRICES, Venue.POLYMARKET, category="geopolitics"
        )
        assert burden == 0.0
        assert (1.0 - sum(self.PRICES)) > burden
