"""
Provider error translation and output parsing.

The network path needs credentials and is not exercised in CI, but the logic
that decides HOW a failure is classified, and what counts as a usable reply, is
pure and entirely testable. It is also the part that matters: every one of these
outcomes must end as an abstention rather than as a decision.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest  # noqa: E402

from speedtrader.llm.providers.anthropic_provider import (  # noqa: E402
    DEFAULT_MODEL, SUPPORTED_MODELS, AnthropicProvider, _text_of, _translate,
    parse_json_object,
)
from speedtrader.llm.providers.base import (  # noqa: E402
    LLMMalformedOutput, LLMProvider, LLMRequest, LLMTimeout, LLMUnavailable,
)
from speedtrader.options.risk import OptionsSizingPolicy, size_option_position  # noqa: E402


# ============================================ model ids

def test_model_ids_carry_no_date_suffix():
    """A date-suffixed variant recalled from training data is not a valid id."""
    for model in SUPPORTED_MODELS:
        tail = model.rsplit("-", 1)[-1]
        assert not (tail.isdigit() and len(tail) == 8), f"{model} looks date-suffixed"


def test_the_default_model_is_supported():
    assert DEFAULT_MODEL in SUPPORTED_MODELS


def test_the_provider_satisfies_the_neutral_protocol():
    assert isinstance(AnthropicProvider(client=object()), LLMProvider)


# ============================================ error translation

@pytest.mark.parametrize("name", ["APITimeoutError", "TimeoutError"])
def test_timeouts_are_classified_as_timeouts(name):
    exc = type(name, (Exception,), {})("slow")
    assert isinstance(_translate(exc), LLMTimeout)


@pytest.mark.parametrize("name", [
    "RateLimitError", "APIConnectionError", "InternalServerError",
    "APIStatusError", "AuthenticationError", "PermissionDeniedError",
    "NotFoundError", "BadRequestError",
])
def test_known_sdk_failures_become_unavailable(name):
    exc = type(name, (Exception,), {})("nope")
    assert isinstance(_translate(exc), LLMUnavailable)


def test_an_unrecognised_failure_still_translates_rather_than_escaping():
    """An untranslated exception would propagate past the abstention handler."""
    assert isinstance(_translate(ValueError("surprise")), LLMUnavailable)


def test_translation_never_returns_none():
    for exc in (Exception("x"), KeyError("k"), OSError("io")):
        assert _translate(exc) is not None


# ============================================ output parsing

def test_a_plain_json_object_parses():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_a_fenced_block_is_unwrapped():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('```\n{"a": 1}\n```') == {"a": 1}


@pytest.mark.parametrize("text", ["", "   ", "not json", "[1,2]", '"str"', "42",
                                  "null", "true"])
def test_anything_that_is_not_a_json_object_raises(text):
    """Guessing at half-formed output is how a malformed reply becomes a decision."""
    with pytest.raises(LLMMalformedOutput):
        parse_json_object(text)


# ============================================ response text extraction

class _Block:
    def __init__(self, type_, text=None):
        self.type, self.text = type_, text


class _Resp:
    def __init__(self, content):
        self.content = content


def test_only_text_blocks_contribute():
    r = _Resp([_Block("thinking", "hidden"), _Block("text", "visible"),
               _Block("tool_use")])
    assert _text_of(r) == "visible"


def test_multiple_text_blocks_are_joined():
    assert _text_of(_Resp([_Block("text", "a"), _Block("text", "b")])) == "a\nb"


def test_no_content_yields_empty_string():
    assert _text_of(_Resp([])) == ""
    assert _text_of(_Resp(None)) == ""


# ============================================ a refusal is not an answer

class _RefusingClient:
    def with_options(self, **kw):
        return self

    class messages:
        @staticmethod
        def create(**kw):
            r = _Resp([_Block("text", "I cannot help with that")])
            r.stop_reason = "refusal"
            return r


def test_a_refusal_is_malformed_output_not_a_parsed_answer():
    provider = AnthropicProvider(client=_RefusingClient())
    with pytest.raises(LLMMalformedOutput, match="declined"):
        provider.complete(LLMRequest(system="s", prompt="p"))


class _EmptyClient:
    def with_options(self, **kw):
        return self

    class messages:
        @staticmethod
        def create(**kw):
            r = _Resp([_Block("text", "   ")])
            r.stop_reason = "end_turn"
            return r


def test_an_empty_reply_is_refused():
    with pytest.raises(LLMMalformedOutput):
        AnthropicProvider(client=_EmptyClient()).complete(
            LLMRequest(system="s", prompt="p"))


def test_the_timeout_is_passed_to_the_sdk_in_seconds():
    """Python SDK timeouts are seconds; sending milliseconds would be a 1000x
    error in the wrong direction."""
    seen = {}

    class Client:
        def with_options(self, **kw):
            seen.update(kw)
            return self

        class messages:
            @staticmethod
            def create(**kw):
                r = _Resp([_Block("text", '{"ok":1}')])
                r.stop_reason = "end_turn"
                r.usage = None
                r.model = "m"
                return r

    AnthropicProvider(client=Client()).complete(
        LLMRequest(system="s", prompt="p", timeout_seconds=12.5))
    assert seen["timeout"] == 12.5


# ============================================ sizing policy validation

@pytest.mark.parametrize("policy", [
    OptionsSizingPolicy(risk_per_trade_pct=0),
    OptionsSizingPolicy(risk_per_trade_pct=-1),
    OptionsSizingPolicy(risk_per_trade_pct=101),
    OptionsSizingPolicy(max_contracts=0),
    OptionsSizingPolicy(max_premium_pct_of_balance=0),
    OptionsSizingPolicy(max_premium_pct_of_balance=101),
])
def test_an_incoherent_sizing_policy_is_refused(policy):
    """A nonsensical policy must fail loudly, not size a position from it."""
    from speedtrader.options.contracts import (
        ContractType, OptionContract, OptionQuote,
    )
    from datetime import date
    contract = OptionContract(
        symbol="X", underlying="T", type=ContractType.CALL, strike=100.0,
        expiration=date(2026, 10, 1), multiplier=100,
        quote=OptionQuote(bid=3.0, ask=3.2))
    with pytest.raises(ValueError):
        size_option_position(contract=contract, account_balance=100_000.0,
                             policy=policy)
