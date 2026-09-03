"""
SpeedTrader AI — bring your own strategy

--------------------------------------------------------------------------------
WHAT THIS IS FOR
--------------------------------------------------------------------------------
S07 is a worked example, not the product. The product is everything BELOW it: 22
deterministic risk checks, options sizing against an exactly known maximum loss,
a single-use execution licence, reconciliation that refuses an unsafe retry, a
reproducible decision journal, and an AI layer that can only subtract.

All of that is strategy-agnostic. So a trader with their own edge should be able
to drop one file into `strategies/`, run the system, and inherit the entire
safety apparatus without touching — or being able to touch — any of it.

    python scripts/strategy_tool.py new my_edge     # scaffold a template
    python scripts/strategy_tool.py check           # validate what you wrote
    python scripts/run_live_paper.py --scan --strategies strategies/

--------------------------------------------------------------------------------
WHAT A STRATEGY CAN AND CANNOT DO
--------------------------------------------------------------------------------
A strategy returns a StrategyResult: a direction, an entry, a stop, a target and
a base score. That is the entire vocabulary. It has no reference to the broker,
the risk engine, the authorization registry or the account, and it is handed a
frozen snapshot rather than a data client.

So the honest statement of the boundary is:

    A strategy PROPOSES a trade. It cannot size one, authorize one, or place one.
    Every number it returns is an input to the deterministic layer's judgement,
    never a substitute for it.

A strategy that returns a score of 999 does not get a bigger position — the score
feeds ranking and expected value, and position size comes from the risk budget
divided by the actual cost of a contract. A strategy that returns an absurd
target does not get a trade: the EV gate, the spread gate and the sizing model
all evaluate it independently.

--------------------------------------------------------------------------------
WHAT THIS IS NOT
--------------------------------------------------------------------------------
Loading a strategy file EXECUTES IT. Python has no meaningful in-process sandbox,
and this module does not pretend to be one. Validation here enforces the
CONTRACT, not safety from hostile code: a strategy file you did not write can do
anything your user account can do, exactly like any other dependency you install.

Treat a third-party strategy the way you would treat a pip package — read it.

What IS guaranteed, and what actually matters for this system's claim, is
narrower and true: no strategy, however written, can execute a trade, enlarge a
position, weaken a risk limit, or bypass the licence. Those paths do not exist
from here.

--------------------------------------------------------------------------------
WHY VALIDATION IS BEHAVIOURAL, NOT JUST STRUCTURAL
--------------------------------------------------------------------------------
Checking that an object has an `evaluate` method proves almost nothing. The
failures that actually hurt are semantic, and all of them are silent:

  * a stop on the WRONG SIDE of entry — risk/reward inverts, and the EV gate
    then reasons from a reward that is really a loss;
  * a NON-DETERMINISTIC strategy — replay is the central claim of this project,
    and a strategy that consults the clock or a random number breaks it for
    every decision it produces;
  * a strategy that MUTATES the snapshot — the snapshot is the single source of
    truth for every layer in one decision, so a strategy that edits it changes
    what the risk engine and the AI reviewer see.

Each is checked by running the strategy, not by inspecting it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from ...data.schemas import (
    Bar, DataSourceMeta, Direction, MarketSnapshot, TechnicalFeatures,
)
from ...common.clock import Freshness
from .base import Strategy, StrategyOutput, StrategyResult

#: How many times a strategy is evaluated on identical input when checking
#: determinism. Two would catch a coin flip only half the time.
DETERMINISM_TRIALS = 3


class StrategyContractError(RuntimeError):
    """A strategy violates the contract and is refused.

    Loud and at LOAD time on purpose. A strategy admitted with a wrong-sided
    stop does not fail — it quietly produces decisions whose expected value was
    computed from a reward that is actually a loss.
    """


@dataclass(frozen=True)
class LoadedStrategy:
    strategy: Any
    path: Path
    #: SHA-256 of the source file, recorded on every decision this strategy
    #: produces. A backtest is only meaningful against a known version of the
    #: logic, and "which code produced this trade" must survive an edit.
    digest: str

    @property
    def id(self) -> str:
        return str(self.strategy.id)


# ==========================================================================
# A probe snapshot: enough structure to exercise a strategy, no market view
# ==========================================================================

def probe_snapshot(
    *, symbol: str = "PROBE", bars: int = 300, seed_price: float = 100.0,
) -> MarketSnapshot:
    """A synthetic but STRUCTURALLY VALID snapshot for contract checking.

    Deliberately boring: a gentle deterministic drift with real highs and lows.
    It is not a market view and no conclusion about a strategy's edge is drawn
    from it — it exists so `evaluate` can be CALLED, which is the only way to
    observe determinism, snapshot mutation, and stop/target geometry.
    """
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    series: list[Bar] = []
    price = seed_price
    for i in range(bars):
        # A fixed triangle wave: no RNG, so two validation runs see identical
        # input and a determinism failure is the strategy's, never the probe's.
        price += 0.10 if (i // 25) % 2 == 0 else -0.10
        high = price + 0.35
        low = price - 0.35
        series.append(Bar(t=start + timedelta(hours=i), o=price - 0.05,
                          h=high, l=low, c=price, v=1_000.0 + i))
    last = series[-1].c
    return MarketSnapshot(
        snapshot_id="probe-snapshot",
        # Pinned, not utcnow(): the determinism check compares two runs, so the
        # probe itself must not be a source of variation.
        timestamp=start + timedelta(hours=bars),
        symbol=symbol,
        price=last,
        bid=last - 0.01,
        ask=last + 0.01,
        spread=0.02,
        spread_pct=0.02 / last * 100.0,
        volume=1_000_000.0,
        bars=series,
        features=TechnicalFeatures(),
        # "cache" rather than "alpaca": this never came from a vendor, and the
        # note says so. The schema's vendor list is not widened for a fixture.
        source=DataSourceMeta(
            vendor="cache",
            fetched_at=start + timedelta(hours=bars),
            bars_available=len(series),
            freshness=Freshness.FRESH,
            notes="synthetic contract-validation probe — not market data",
        ),
        market_open=True,
    )


def _snapshot_digest(snapshot: MarketSnapshot) -> str:
    """Identity of everything a strategy is allowed to read."""
    return hashlib.sha256(
        snapshot.model_dump_json().encode()
    ).hexdigest()


def _output_identity(output: StrategyOutput | None) -> tuple:
    """The fields that must not vary between runs on identical input.

    `breakdown` and `inputs` are excluded: they are human-facing trace, and a
    strategy that formats a float differently on two runs is untidy, not unsafe.
    """
    if output is None:
        return ("none",)
    return (output.strategy_id, output.direction, round(output.entry, 10),
            round(output.stop_loss, 10), round(output.take_profit, 10),
            round(output.base_score, 10))


# ==========================================================================
# Validation
# ==========================================================================

def validate_structure(strategy: Any) -> list[str]:
    """Attributes and signature. Cheap, and catches the honest mistakes."""
    problems: list[str] = []

    for attr in ("id", "source_reference", "min_bars"):
        if not hasattr(strategy, attr):
            problems.append(f"missing required attribute '{attr}'")

    if hasattr(strategy, "id"):
        sid = getattr(strategy, "id")
        # Explicitly including None: `id = None` satisfies hasattr and would
        # otherwise reach the journal as a null key.
        if not isinstance(sid, str) or not sid.strip():
            problems.append(f"id must be a non-empty string, got {sid!r}")

    min_bars = getattr(strategy, "min_bars", None)
    if min_bars is not None and (not isinstance(min_bars, int)
                                 or isinstance(min_bars, bool) or min_bars < 1):
        problems.append(f"min_bars must be an int >= 1, got {min_bars!r}")

    evaluate = getattr(strategy, "evaluate", None)
    if not callable(evaluate):
        problems.append("missing a callable 'evaluate'")
        return problems

    try:
        params = [p for name, p in inspect.signature(evaluate).parameters.items()
                  if name != "self"]
    except (TypeError, ValueError):        # builtins and C callables
        return problems
    required = [p for p in params
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if len(required) != 1:
        problems.append(
            f"evaluate must take exactly one required argument (the snapshot), "
            f"got {len(required)}"
        )
    return problems


def validate_behaviour(strategy: Any, snapshot: MarketSnapshot) -> list[str]:
    """Run it. Everything that matters is only observable by running it."""
    problems: list[str] = []

    before = _snapshot_digest(snapshot)
    results: list[StrategyResult] = []
    for trial in range(DETERMINISM_TRIALS):
        try:
            results.append(strategy.evaluate(snapshot))
        except Exception as e:
            problems.append(
                f"evaluate raised {type(e).__name__} on a structurally valid "
                f"snapshot: {e}. Declining to trade is a StrategyResult with a "
                f"reason, never an exception."
            )
            return problems
    after = _snapshot_digest(snapshot)

    if before != after:
        problems.append(
            "evaluate MUTATED the snapshot. Every layer in one decision reads "
            "the same snapshot, so editing it changes what the risk engine and "
            "the reviewer see. Treat it as read-only."
        )

    for result in results:
        if not isinstance(result, StrategyResult):
            problems.append(
                f"evaluate must return a StrategyResult, got "
                f"{type(result).__name__}"
            )
            return problems

    identities = {_output_identity(r.output) for r in results}
    if len(identities) > 1:
        problems.append(
            f"evaluate is NOT DETERMINISTIC: {DETERMINISM_TRIALS} runs on an "
            f"identical snapshot produced {len(identities)} different outputs. "
            "Replay re-derives every stored decision from its snapshot alone, "
            "so a strategy that consults the clock, a random number or mutable "
            "state cannot be replayed and is refused."
        )

    first = results[0]
    if first.ok and first.output is None:
        problems.append("returned ok=True with no output")
    if first.output is not None:
        problems.extend(validate_output(first.output, strategy))
    return problems


def validate_output(output: StrategyOutput, strategy: Any = None) -> list[str]:
    """Geometry. A wrong-sided stop is silent and corrupts expected value.

    Checked here rather than left to the risk engine because the engine would
    reject the trade with a reason that describes the SYMPTOM (negative EV)
    rather than the cause (the strategy has its stop and target the wrong way
    round), and the author would go looking in the wrong place.
    """
    problems: list[str] = []

    for name in ("entry", "stop_loss", "take_profit", "base_score"):
        value = getattr(output, name, None)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            problems.append(f"{name} must be a number, got {value!r}")
            return problems
        if value != value or value in (float("inf"), float("-inf")):
            problems.append(f"{name} must be finite, got {value!r}")
            return problems

    if output.entry <= 0:
        problems.append(f"entry must be positive, got {output.entry}")
    if output.stop_loss <= 0 or output.take_profit <= 0:
        problems.append("stop_loss and take_profit must be positive")
    if problems:
        return problems

    if output.direction is Direction.BUY:
        if output.stop_loss >= output.entry:
            problems.append(
                f"BUY stop {output.stop_loss} is at or above entry "
                f"{output.entry} — a stop above entry is a target"
            )
        if output.take_profit <= output.entry:
            problems.append(
                f"BUY target {output.take_profit} is at or below entry "
                f"{output.entry}"
            )
    elif output.direction is Direction.SELL:
        if output.stop_loss <= output.entry:
            problems.append(
                f"SELL stop {output.stop_loss} is at or below entry "
                f"{output.entry} — a stop below entry is a target"
            )
        if output.take_profit >= output.entry:
            problems.append(
                f"SELL target {output.take_profit} is at or above entry "
                f"{output.entry}"
            )
    else:
        problems.append(f"direction must be a Direction, got {output.direction!r}")

    if strategy is not None and output.strategy_id != getattr(strategy, "id", None):
        problems.append(
            f"output.strategy_id {output.strategy_id!r} does not match the "
            f"strategy's id {getattr(strategy, 'id', None)!r}; the decision "
            f"journal keys off it, so they must agree"
        )
    return problems


def validate_strategy(strategy: Any, *, snapshot: MarketSnapshot | None = None) -> list[str]:
    """Every problem, not just the first. Fixing them one round-trip at a time
    is the difference between a pleasant contract and an annoying one."""
    problems = validate_structure(strategy)
    if problems:
        return problems
    return validate_behaviour(strategy, snapshot or probe_snapshot())


# ==========================================================================
# Discovery
# ==========================================================================

def _instantiate(obj: Any) -> Any | None:
    """A module may export a class or a ready instance. Accept either."""
    if not inspect.isclass(obj):
        return obj
    try:
        params = [p for name, p in inspect.signature(obj).parameters.items()
                  if name != "self" and p.default is inspect.Parameter.empty
                  and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        return None
    if params:
        # Constructor arguments we would have to invent. Refuse rather than
        # guess: a strategy built with made-up parameters is not the strategy
        # its author tested.
        return None
    try:
        return obj()
    except Exception:
        return None


def discover_in_module(module: Any) -> list[Any]:
    """Strategy-shaped objects a module exports, in declaration order."""
    found: list[Any] = []
    seen: set[int] = set()
    for name, obj in vars(module).items():
        if name.startswith("_"):
            continue
        # Defined elsewhere and merely imported — importing the base contract or
        # S07 for reference must not silently enrol them.
        origin = getattr(obj, "__module__", None)
        if inspect.isclass(obj) and origin != module.__name__:
            continue
        candidate = _instantiate(obj)
        if candidate is None or id(candidate) in seen:
            continue
        if isinstance(candidate, type):
            continue
        if all(hasattr(candidate, a) for a in ("id", "evaluate", "min_bars")):
            seen.add(id(candidate))
            found.append(candidate)
    return found


def load_file(path: Path, *, validate: bool = True) -> list[LoadedStrategy]:
    """Import one .py file and return the strategies it exports.

    The module is given a namespaced name so two strategy files can both define
    `Strategy` without colliding, and is NOT left in sys.modules under a name a
    later import could pick up by accident.
    """
    path = Path(path).resolve()
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    module_name = f"speedtrader_user_strategy_{digest[:12]}"

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise StrategyContractError(f"{path.name}: not an importable Python file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        del sys.modules[module_name]
        raise StrategyContractError(
            f"{path.name}: failed to import: {type(e).__name__}: {e}"
        ) from e

    found = discover_in_module(module)
    if not found:
        raise StrategyContractError(
            f"{path.name}: no strategy found. Export a class with `id`, "
            f"`min_bars` and `evaluate(snapshot)`, or an instance of one."
        )

    loaded: list[LoadedStrategy] = []
    for strategy in found:
        if validate:
            problems = validate_strategy(strategy)
            if problems:
                raise StrategyContractError(
                    f"{path.name}: strategy "
                    f"{getattr(strategy, 'id', '<no id>')!r} violates the "
                    f"contract:\n  - " + "\n  - ".join(problems)
                )
        loaded.append(LoadedStrategy(strategy=strategy, path=path, digest=digest))
    return loaded


def load_directory(
    directory: str | Path, *, validate: bool = True,
) -> list[LoadedStrategy]:
    """Every strategy in a directory, sorted by filename for a stable order.

    Stable order matters: ranking breaks ties by iteration order, so a
    filesystem-dependent order would make the same market state resolve
    differently on two machines and break replay across them.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise StrategyContractError(f"{directory} is not a directory")

    loaded: list[LoadedStrategy] = []
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        loaded.extend(load_file(path, validate=validate))

    ids: dict[str, Path] = {}
    for item in loaded:
        if item.id in ids:
            raise StrategyContractError(
                f"duplicate strategy id {item.id!r} in {ids[item.id].name} and "
                f"{item.path.name}. The decision journal and per-strategy stats "
                f"key off the id, so two strategies sharing one would have their "
                f"histories merged."
            )
        ids[item.id] = item.path
    return loaded


def strategies_of(loaded: Iterable[LoadedStrategy]) -> list[Strategy]:
    """Just the strategy objects, for handing to QuantCore."""
    return [item.strategy for item in loaded]


def provenance(loaded: Sequence[LoadedStrategy]) -> dict[str, dict[str, str]]:
    """What produced these decisions, for the journal.

    A backtest or a replay is only meaningful against a known version of the
    logic, so the file digest travels with the run.
    """
    return {
        item.id: {"file": item.path.name, "sha256": item.digest,
                  "source_reference": str(getattr(item.strategy,
                                                  "source_reference", "") or "")}
        for item in loaded
    }
