"""
Bring-your-own-strategy: the loader and its contract.

The point of this layer is that a stranger's file inherits the whole safety
apparatus without being able to touch it. Two things therefore need proving:

  1. the contract CATCHES the silent failures — a wrong-sided stop, a
     non-deterministic evaluate, a strategy that edits the snapshot;
  2. the boundary HOLDS — a hostile strategy still cannot size, authorize or
     place a trade.

Structural checks ("has an evaluate method") are almost worthless here, so most
of these tests write a real strategy file and load it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import pytest  # noqa: E402

from speedtrader.data.schemas import Direction  # noqa: E402
from speedtrader.quant.strategies.base import (  # noqa: E402
    Code, StrategyOutput, StrategyResult,
)
from speedtrader.quant.strategies.plugins import (  # noqa: E402
    DETERMINISM_TRIALS, StrategyContractError, discover_in_module, load_directory,
    load_file, probe_snapshot, provenance, strategies_of, validate_output,
    validate_strategy, validate_structure,
)
from speedtrader.quant.strategies.s07 import S07MomentumBreakout  # noqa: E402

HEADER = dedent("""
    from speedtrader.data.schemas import Direction, MarketSnapshot
    from speedtrader.quant.strategies.base import Code, StrategyOutput, StrategyResult
""")


def write(tmp_path: Path, name: str, *bodies: str) -> Path:
    """Each fragment is dedented separately: joining before dedenting leaves the
    common prefix of the SHORTEST fragment, which silently breaks indentation."""
    path = tmp_path / f"{name}.py"
    path.write_text(HEADER + "\n".join(dedent(b) for b in bodies))
    return path


GOOD = """
    class Good:
        id = "good"
        source_reference = "test"
        min_bars = 10
        def evaluate(self, snapshot):
            p = snapshot.price
            return StrategyResult(ok=True, code=Code.SIGNAL, reason="ok",
                output=StrategyOutput(strategy_id=self.id, direction=Direction.BUY,
                    entry=p, stop_loss=p - 1.0, take_profit=p + 2.0, base_score=50.0))
"""


# ============================================ the probe

def test_the_probe_snapshot_is_structurally_usable():
    s = probe_snapshot()
    assert len(s.bars) == 300
    assert s.price and s.price > 0
    assert all(b.h >= b.c >= b.l for b in s.bars)


def test_the_probe_is_identical_every_time():
    """It is the control in a determinism experiment, so it must not vary or a
    deterministic strategy would be accused of being random."""
    assert probe_snapshot().model_dump_json() == probe_snapshot().model_dump_json()


def test_the_probe_is_not_labelled_as_vendor_data():
    """It never came from Alpaca and the record must not imply that it did."""
    s = probe_snapshot()
    assert s.source.vendor != "alpaca"
    assert "probe" in (s.source.notes or "").lower()


# ============================================ the system's own strategy passes

def test_s07_satisfies_the_contract_it_defines():
    assert validate_strategy(S07MomentumBreakout()) == []


# ============================================ the silent failures

def test_a_buy_with_the_stop_above_entry_is_refused():
    """The failure this whole layer exists for: it does not raise, it inverts
    risk/reward so EV is computed from a reward that is really a loss."""
    problems = validate_output(StrategyOutput(
        strategy_id="x", direction=Direction.BUY, entry=100.0,
        stop_loss=101.0, take_profit=99.0, base_score=50.0))
    assert any("stop" in p for p in problems)
    assert any("target" in p for p in problems)


def test_a_sell_with_the_stop_below_entry_is_refused():
    problems = validate_output(StrategyOutput(
        strategy_id="x", direction=Direction.SELL, entry=100.0,
        stop_loss=99.0, take_profit=101.0, base_score=50.0))
    assert problems


@pytest.mark.parametrize("direction,stop,target", [
    (Direction.BUY, 99.0, 101.0),
    (Direction.SELL, 101.0, 99.0),
])
def test_correctly_oriented_geometry_is_accepted(direction, stop, target):
    assert validate_output(StrategyOutput(
        strategy_id="x", direction=direction, entry=100.0,
        stop_loss=stop, take_profit=target, base_score=50.0)) == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_price_is_refused(bad):
    assert validate_output(StrategyOutput(
        strategy_id="x", direction=Direction.BUY, entry=bad,
        stop_loss=99.0, take_profit=101.0, base_score=50.0))


def test_an_output_whose_id_disagrees_with_the_strategy_is_refused():
    """The journal and per-strategy statistics key off the id."""
    class S:
        id = "declared"
    problems = validate_output(StrategyOutput(
        strategy_id="something_else", direction=Direction.BUY, entry=100.0,
        stop_loss=99.0, take_profit=101.0, base_score=50.0), S())
    assert any("strategy_id" in p for p in problems)


def test_a_non_deterministic_strategy_is_refused(tmp_path):
    """Replay re-derives every decision from its snapshot alone."""
    path = write(tmp_path, "rand", """
        import random
        class Rand:
            id = "rand"
            source_reference = "t"
            min_bars = 10
            def evaluate(self, snapshot):
                p = snapshot.price * random.uniform(0.9, 1.1)
                return StrategyResult(ok=True, code=Code.SIGNAL, reason="r",
                    output=StrategyOutput(strategy_id=self.id, direction=Direction.BUY,
                        entry=p, stop_loss=p - 1, take_profit=p + 2, base_score=50.0))
    """)
    with pytest.raises(StrategyContractError, match="DETERMINISTIC"):
        load_file(path)


def test_determinism_is_checked_more_than_twice():
    """Two trials would miss a coin flip half the time."""
    assert DETERMINISM_TRIALS >= 3


def test_a_strategy_that_edits_the_snapshot_is_refused(tmp_path):
    path = write(tmp_path, "mut", """
        class Mut:
            id = "mut"
            source_reference = "t"
            min_bars = 10
            def evaluate(self, snapshot):
                snapshot.bars.pop()
                return StrategyResult(ok=False, reason="no", code=Code.NO_SIGNAL)
    """)
    with pytest.raises(StrategyContractError, match="MUTATED"):
        load_file(path)


def test_a_strategy_that_raises_is_refused(tmp_path):
    """Declining to trade is a result with a reason, not an exception."""
    path = write(tmp_path, "boom", """
        class Boom:
            id = "boom"
            source_reference = "t"
            min_bars = 10
            def evaluate(self, snapshot):
                raise ValueError("nope")
    """)
    with pytest.raises(StrategyContractError, match="raised ValueError"):
        load_file(path)


def test_a_strategy_returning_the_wrong_type_is_refused(tmp_path):
    path = write(tmp_path, "wrong", """
        class Wrong:
            id = "wrong"
            source_reference = "t"
            min_bars = 10
            def evaluate(self, snapshot):
                return {"verdict": "BUY", "quantity": 99999}
    """)
    with pytest.raises(StrategyContractError, match="StrategyResult"):
        load_file(path)


def test_every_problem_is_reported_not_just_the_first():
    """Fixing a contract one round-trip at a time is what makes a plugin API
    unpleasant to write against."""
    problems = validate_output(StrategyOutput(
        strategy_id="x", direction=Direction.BUY, entry=100.0,
        stop_loss=101.0, take_profit=99.0, base_score=50.0))
    assert len(problems) >= 2


# ============================================ structure

@pytest.mark.parametrize("missing", ["id", "source_reference", "min_bars"])
def test_a_missing_required_attribute_is_reported(missing):
    attrs = {"id": "s", "source_reference": "t", "min_bars": 10,
             "evaluate": lambda self, snapshot: None}
    attrs.pop(missing)
    assert any(missing in p for p in validate_structure(type("S", (), attrs)()))


@pytest.mark.parametrize("bad", [0, -1, "50", 10.5, True])
def test_a_nonsense_min_bars_is_reported(bad):
    class S:
        id = "s"
        source_reference = "t"
        min_bars = bad
        def evaluate(self, snapshot): ...
    assert any("min_bars" in p for p in validate_structure(S()))


@pytest.mark.parametrize("bad", ["", "   ", 7, None])
def test_a_nonsense_id_is_reported(bad):
    class S:
        id = bad
        source_reference = "t"
        min_bars = 10
        def evaluate(self, snapshot): ...
    assert validate_structure(S())


def test_an_evaluate_taking_the_wrong_number_of_arguments_is_reported():
    class S:
        id = "s"
        source_reference = "t"
        min_bars = 10
        def evaluate(self, snapshot, extra): ...
    assert any("one required argument" in p for p in validate_structure(S()))


# ============================================ discovery

def test_a_good_strategy_loads(tmp_path):
    loaded = load_file(write(tmp_path, "good", GOOD))
    assert len(loaded) == 1 and loaded[0].id == "good"


def test_the_file_digest_is_recorded(tmp_path):
    """A backtest is only meaningful against a known version of the logic."""
    path = write(tmp_path, "good", GOOD)
    first = load_file(path)[0].digest
    assert len(first) == 64
    path.write_text(path.read_text() + "\n# an edit\n")
    assert load_file(path)[0].digest != first


def test_provenance_carries_the_digest_and_the_file(tmp_path):
    loaded = load_file(write(tmp_path, "good", GOOD))
    record = provenance(loaded)["good"]
    assert record["file"] == "good.py"
    assert record["sha256"] == loaded[0].digest


def test_an_imported_class_is_not_silently_enrolled(tmp_path):
    """Importing S07 for reference must not make it one of YOUR strategies."""
    path = write(tmp_path, "imports", """
        from speedtrader.quant.strategies.s07 import S07MomentumBreakout
    """, GOOD)
    assert [item.id for item in load_file(path)] == ["good"]


def test_a_class_needing_constructor_arguments_is_skipped(tmp_path):
    """Inventing its arguments would produce a strategy its author never tested."""
    path = write(tmp_path, "needsargs", """
        class NeedsArgs:
            id = "needsargs"
            source_reference = "t"
            min_bars = 10
            def __init__(self, period):
                self.period = period
            def evaluate(self, snapshot):
                return StrategyResult(ok=False, reason="n", code=Code.NO_SIGNAL)
    """, GOOD)
    assert [item.id for item in load_file(path)] == ["good"]


def test_a_ready_made_instance_is_accepted_as_well_as_a_class(tmp_path):
    path = write(tmp_path, "inst", GOOD, "instance = Good()\n")
    assert load_file(path)


def test_a_file_with_no_strategy_is_refused_with_advice(tmp_path):
    path = tmp_path / "empty.py"
    path.write_text("x = 1\n")
    with pytest.raises(StrategyContractError, match="no strategy found"):
        load_file(path)


def test_an_unimportable_file_is_refused_not_skipped(tmp_path):
    path = tmp_path / "broken.py"
    path.write_text("this is not python(((\n")
    with pytest.raises(StrategyContractError, match="failed to import"):
        load_file(path)


def test_a_module_exporting_nothing_strategy_shaped_finds_nothing():
    import types
    m = types.ModuleType("m")
    m.x, m.f = 1, (lambda: None)
    assert discover_in_module(m) == []


# ============================================ directories

def test_a_directory_loads_in_a_stable_order(tmp_path):
    """Ranking breaks ties by iteration order, so a filesystem-dependent order
    would make one market state resolve differently on two machines."""
    for name in ("charlie", "alpha", "bravo"):
        write(tmp_path, name, GOOD.replace('"good"', f'"{name}"')
              .replace("class Good", f"class {name.capitalize()}"))
    assert [i.id for i in load_directory(tmp_path)] == ["alpha", "bravo", "charlie"]


def test_two_strategies_sharing_an_id_are_refused(tmp_path):
    """Their journals and statistics would silently merge."""
    write(tmp_path, "one", GOOD)
    write(tmp_path, "two", GOOD.replace("class Good", "class Good2"))
    with pytest.raises(StrategyContractError, match="duplicate strategy id"):
        load_directory(tmp_path)


def test_underscore_prefixed_files_are_helpers_not_strategies(tmp_path):
    write(tmp_path, "_helpers", GOOD)
    write(tmp_path, "real", GOOD.replace('"good"', '"real"'))
    assert [i.id for i in load_directory(tmp_path)] == ["real"]


def test_a_missing_directory_is_refused_rather_than_read_as_empty(tmp_path):
    """Empty would look like 'no strategies configured' and run nothing."""
    with pytest.raises(StrategyContractError, match="not a directory"):
        load_directory(tmp_path / "nope")


def test_one_bad_strategy_stops_the_whole_directory(tmp_path):
    """Trading with fewer strategies than you asked for is worse than not
    starting: you would never notice."""
    write(tmp_path, "good", GOOD)
    write(tmp_path, "bad", """
        class Bad:
            id = "bad"
            source_reference = "t"
            min_bars = 10
            def evaluate(self, snapshot):
                p = snapshot.price
                return StrategyResult(ok=True, code=Code.SIGNAL, reason="r",
                    output=StrategyOutput(strategy_id=self.id, direction=Direction.BUY,
                        entry=p, stop_loss=p + 1, take_profit=p - 1, base_score=50.0))
    """)
    with pytest.raises(StrategyContractError):
        load_directory(tmp_path)


def test_strategies_of_returns_objects_quantcore_can_use(tmp_path):
    loaded = load_directory(tmp_path) if False else load_file(write(tmp_path, "g", GOOD))
    objects = strategies_of(loaded)
    assert all(hasattr(o, "evaluate") and hasattr(o, "id") for o in objects)


# ============================================ the boundary itself

def test_a_loaded_strategy_holds_no_broker_risk_or_account_reference(tmp_path):
    """The claim is structural: propose, never execute. A strategy is handed a
    frozen snapshot and returns a dataclass — there is no reachable path from
    one to an order."""
    strategy = load_file(write(tmp_path, "good", GOOD))[0].strategy
    forbidden = ("broker", "adapter", "registry", "risk", "account", "submit",
                 "authorize", "execute", "portfolio")
    for name in dir(strategy):
        assert not any(f in name.lower() for f in forbidden), name


def test_an_absurd_score_does_not_travel_as_a_position_size(tmp_path):
    """A strategy returning 10^9 gets no larger position: the score feeds
    ranking and EV, and size comes from the risk budget and the contract cost."""
    path = write(tmp_path, "loud", GOOD.replace("base_score=50.0",
                                                "base_score=1e9")
                 .replace('"good"', '"loud"').replace("class Good", "class Loud"))
    output = load_file(path)[0].strategy.evaluate(probe_snapshot()).output
    assert output.base_score == 1e9
    # It is carried, unmodified and unhonoured: the sizing model never reads it.
    import inspect

    from speedtrader.options import risk as options_risk
    assert "base_score" not in inspect.getsource(options_risk)
