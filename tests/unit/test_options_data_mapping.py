"""
Alpaca options payload mapping.

Alpaca returns several risk-critical numbers as STRINGS. A silent str/float
confusion would not raise — it would produce a contract whose multiplier or open
interest is wrong, and the position would then be sized against a maximum loss
that does not exist. These tests pin that every such field is converted
explicitly and that anything unconvertible drops the contract rather than
defaulting it.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest  # noqa: E402

from speedtrader.alpaca.options_data import (  # noqa: E402
    AlpacaOptionsData, ChainRequest, OptionsDataUnavailable, build_chain,
    map_contract, map_quote,
)
from speedtrader.options.contracts import ContractType  # noqa: E402

RAW = {
    "symbol": "AAPL260930C00230000",
    "underlying_symbol": "AAPL",
    "type": "call",
    "strike_price": "230.0",          # string, as Alpaca returns it
    "expiration_date": "2026-09-30",
    "size": "100",                    # string
    "open_interest": "1543",          # string
    "status": "active",
    "tradable": True,
}
QUOTE = {"bid_price": 3.00, "ask_price": 3.20}


def test_string_numerics_are_converted_not_carried_through():
    c = map_contract(RAW, QUOTE)
    assert c is not None
    assert c.strike == 230.0 and isinstance(c.strike, float)
    assert c.multiplier == 100 and isinstance(c.multiplier, int)
    assert c.open_interest == 1543 and isinstance(c.open_interest, int)
    assert c.expiration == date(2026, 9, 30)
    assert c.type is ContractType.CALL


def test_quote_maps_to_a_two_sided_market():
    q = map_quote(QUOTE)
    assert q.bid == 3.00 and q.ask == 3.20
    assert q.mid == pytest.approx(3.10)


@pytest.mark.parametrize("quote", [
    None, {}, {"bid_price": 3.0}, {"ask_price": 3.2},
    {"bid_price": 0.0, "ask_price": 3.2}, {"bid_price": 3.0, "ask_price": 0.0},
    {"bid_price": "x", "ask_price": "y"},
])
def test_a_one_sided_or_unreadable_book_is_not_a_quote(quote):
    """Without two sides there is no bounded price to buy at."""
    assert map_quote(quote) is None


@pytest.mark.parametrize("field", [
    "symbol", "underlying_symbol", "type", "strike_price", "expiration_date",
])
def test_a_missing_risk_critical_field_drops_the_contract(field):
    raw = {**RAW}
    raw[field] = None
    assert map_contract(raw, QUOTE) is None


@pytest.mark.parametrize("bad", ["not-a-number", "", "NaN"])
def test_an_unconvertible_strike_drops_the_contract(bad):
    assert map_contract({**RAW, "strike_price": bad}, QUOTE) is None


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-100"])
def test_an_unreadable_size_drops_the_contract_rather_than_assuming_100(bad):
    """Assuming 100 for an adjusted contract would corrupt max-loss sizing."""
    assert map_contract({**RAW, "size": bad}, QUOTE) is None


def test_an_adjusted_contract_is_mapped_with_its_real_multiplier():
    """Mapping preserves the truth; selection is what rejects it."""
    c = map_contract({**RAW, "size": "137"}, QUOTE)
    assert c is not None and c.multiplier == 137


@pytest.mark.parametrize("bad_date", ["", "not-a-date", "2026-13-45"])
def test_an_unparseable_expiry_drops_the_contract(bad_date):
    assert map_contract({**RAW, "expiration_date": bad_date}, QUOTE) is None


@pytest.mark.parametrize("bad_type", ["straddle", "", None, "CALLS"])
def test_an_unknown_contract_type_drops_the_contract(bad_type):
    assert map_contract({**RAW, "type": bad_type}, QUOTE) is None


@pytest.mark.parametrize("strike", ["0", "-5"])
def test_a_non_positive_strike_drops_the_contract(strike):
    assert map_contract({**RAW, "strike_price": strike}, QUOTE) is None


def test_inactive_or_untradable_contracts_are_marked_not_tradable():
    assert map_contract({**RAW, "status": "inactive"}, QUOTE).tradable is False
    assert map_contract({**RAW, "tradable": False}, QUOTE).tradable is False


def test_a_missing_open_interest_is_none_not_zero():
    """None means unknown; zero would read as a real, illiquid contract and be
    rejected for the wrong reason."""
    c = map_contract({**RAW, "open_interest": None}, QUOTE)
    assert c.open_interest is None


def test_object_style_payloads_work_as_well_as_dicts():
    class Obj:
        pass
    o = Obj()
    for k, v in RAW.items():
        setattr(o, k, v)
    c = map_contract(o, QUOTE)
    assert c is not None and c.symbol == RAW["symbol"]


def test_build_chain_drops_bad_elements_without_raising():
    chain = build_chain(
        [RAW, {**RAW, "symbol": "X2", "strike_price": "bad"}, {**RAW, "symbol": "X3"}],
        {RAW["symbol"]: QUOTE, "X3": QUOTE},
    )
    assert [c.symbol for c in chain] == [RAW["symbol"], "X3"]


def test_build_chain_attaches_the_matching_quote_only():
    chain = build_chain([RAW], {"SOME_OTHER_SYMBOL": QUOTE})
    assert chain[0].quote is None


# ============================================ failures are outages, not silence

class Boom:
    def get_option_contracts(self, *a, **k):
        raise ConnectionError("network down")


def test_a_contract_listing_failure_raises_rather_than_returning_empty():
    """An empty list would be indistinguishable from 'no contract qualified',
    which reads as a legitimate no-trade rather than a data outage."""
    d = AlpacaOptionsData(Boom(), None)
    with pytest.raises(OptionsDataUnavailable):
        d.fetch_chain(ChainRequest("AAPL"), spot=230.0, asof=date(2026, 9, 2))


def test_a_quote_failure_raises_rather_than_pricing_blind():
    class Contracts:
        option_contracts = [type("C", (), RAW)()]

        def get_option_contracts(self, *a, **k):
            return self
    class BadQuotes:
        def get_option_latest_quote(self, *a, **k):
            raise TimeoutError("quotes unavailable")
    d = AlpacaOptionsData(Contracts(), BadQuotes())
    with pytest.raises(OptionsDataUnavailable):
        d.fetch_chain(ChainRequest("AAPL"), spot=230.0, asof=date(2026, 9, 2))


@pytest.mark.parametrize("spot", [0.0, -1.0])
def test_an_invalid_spot_is_refused(spot):
    d = AlpacaOptionsData(None, None)
    with pytest.raises(OptionsDataUnavailable):
        d.fetch_chain(ChainRequest("AAPL"), spot=spot, asof=date(2026, 9, 2))


# ============================================ quote batching
# Alpaca caps the latest-quote endpoint at 100 symbols. Found by calling the
# live API with a 354-contract AAPL chain, which returned
# APIError: {"message":"symbol limit is 100"}. The adapter failed CLOSED (no
# chain rather than an empty one) — correct, but it meant no contract could
# ever be priced against the real broker.

from speedtrader.alpaca.options_data import QUOTE_BATCH_LIMIT, _batched  # noqa: E402


def test_the_batch_limit_matches_alpacas_documented_cap():
    assert QUOTE_BATCH_LIMIT == 100


@pytest.mark.parametrize("n,expected", [
    (0, 0), (1, 1), (100, 1), (101, 2), (354, 4), (500, 5),
])
def test_symbols_are_split_into_request_sized_batches(n, expected):
    assert len(_batched(list(range(n)), 100)) == expected


def test_no_batch_exceeds_the_limit_and_none_is_empty():
    batches = _batched(list(range(354)), QUOTE_BATCH_LIMIT)
    assert all(0 < len(b) <= QUOTE_BATCH_LIMIT for b in batches)
    assert sum(len(b) for b in batches) == 354


def test_batching_preserves_every_symbol_exactly_once():
    symbols = [f"S{i}" for i in range(250)]
    flat = [s for batch in _batched(symbols, QUOTE_BATCH_LIMIT) for s in batch]
    assert flat == symbols


def test_a_zero_batch_size_is_refused_rather_than_looping_forever():
    with pytest.raises(ValueError):
        _batched([1, 2, 3], 0)


class _Chain:
    """A chain large enough to require several quote requests."""
    def __init__(self, n):
        self.option_contracts = [
            type("C", (), {**RAW, "symbol": f"SYM{i}"})() for i in range(n)]

    def get_option_contracts(self, *a, **k):
        return self


class _CountingQuotes:
    def __init__(self, fail_on=None):
        self.batch_sizes, self.fail_on = [], fail_on

    def get_option_latest_quote(self, request):
        symbols = request.symbol_or_symbols
        self.batch_sizes.append(len(symbols))
        if self.fail_on is not None and len(self.batch_sizes) == self.fail_on:
            raise RuntimeError("upstream hiccup")
        return {s: {"bid_price": 1.0, "ask_price": 1.2} for s in symbols}


def test_a_large_chain_is_fetched_in_several_capped_requests():
    quotes = _CountingQuotes()
    chain = AlpacaOptionsData(_Chain(354), quotes).fetch_chain(
        ChainRequest("AAPL"), spot=230.0, asof=date(2026, 9, 2))
    assert len(quotes.batch_sizes) == 4
    assert max(quotes.batch_sizes) <= QUOTE_BATCH_LIMIT
    assert len(chain) == 354
    assert all(c.quote is not None for c in chain)


def test_one_failed_batch_fails_the_whole_fetch():
    """A partially-priced chain would silently narrow selection to whichever
    contracts happened to load — a quieter and worse failure than no chain."""
    quotes = _CountingQuotes(fail_on=2)
    with pytest.raises(OptionsDataUnavailable, match="batch"):
        AlpacaOptionsData(_Chain(354), quotes).fetch_chain(
            ChainRequest("AAPL"), spot=230.0, asof=date(2026, 9, 2))
