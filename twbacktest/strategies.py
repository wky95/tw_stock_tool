"""Strategy plug-in registry and built-in daily strategies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .domain import Bar


class StrategyParameterError(ValueError):
    pass


@dataclass(frozen=True)
class StrategyOutput:
    targets: list[int]
    indicators: dict[str, list[float | None]]


class Strategy(Protocol):
    key: str
    label: str

    def generate(self, bars: list[Bar], params: dict[str, Any]) -> StrategyOutput: ...


def _sma(values: list[float], length: int, index: int) -> float | None:
    if length < 1 or index + 1 < length:
        return None
    return sum(values[index - length + 1 : index + 1]) / length


def _rsi(values: list[float], length: int, index: int) -> float | None:
    if length < 1 or index < length:
        return None
    changes = [values[i] - values[i - 1] for i in range(index - length + 1, index + 1)]
    gains = sum(max(change, 0) for change in changes) / length
    losses = sum(max(-change, 0) for change in changes) / length
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


class SmaCrossStrategy:
    key = "sma_cross"
    label = "雙均線趨勢"

    def generate(self, bars: list[Bar], params: dict[str, Any]) -> StrategyOutput:
        fast = int(params.get("fast", 20))
        slow = int(params.get("slow", 60))
        if fast < 1 or slow <= fast:
            raise StrategyParameterError("均線參數需符合 1 ≤ 短均線 < 長均線")
        closes = [bar.close for bar in bars]
        fast_values = [_sma(closes, fast, i) for i in range(len(bars))]
        slow_values = [_sma(closes, slow, i) for i in range(len(bars))]
        targets = [int(a is not None and b is not None and a > b) for a, b in zip(fast_values, slow_values)]
        return StrategyOutput(targets, {f"SMA {fast}": fast_values, f"SMA {slow}": slow_values})


class RsiReversalStrategy:
    key = "rsi"
    label = "RSI 反轉"

    def generate(self, bars: list[Bar], params: dict[str, Any]) -> StrategyOutput:
        length = int(params.get("length", 14))
        lower = float(params.get("lower", 30))
        upper = float(params.get("upper", 70))
        if length < 2 or not 0 <= lower < upper <= 100:
            raise StrategyParameterError("RSI 參數需符合週期 ≥ 2 且 0 ≤ 買進線 < 賣出線 ≤ 100")
        closes = [bar.close for bar in bars]
        values = [_rsi(closes, length, i) for i in range(len(bars))]
        targets, holding = [], 0
        for value in values:
            if value is not None and value <= lower:
                holding = 1
            elif value is not None and value >= upper:
                holding = 0
            targets.append(holding)
        return StrategyOutput(targets, {f"RSI {length}": values})


class BreakoutStrategy:
    key = "breakout"
    label = "區間突破"

    def generate(self, bars: list[Bar], params: dict[str, Any]) -> StrategyOutput:
        entry = int(params.get("entry", 20))
        exit_length = int(params.get("exit", 10))
        if entry < 2 or exit_length < 2:
            raise StrategyParameterError("突破與出場週期必須至少為 2")
        closes = [bar.close for bar in bars]
        targets, entry_line, exit_line, holding = [], [], [], 0
        for i, close in enumerate(closes):
            high_line = max(closes[max(0, i - entry):i], default=None)
            low_line = min(closes[max(0, i - exit_length):i], default=None)
            entry_line.append(high_line if i >= entry else None)
            exit_line.append(low_line if i >= exit_length else None)
            if i >= entry and high_line is not None and close > high_line:
                holding = 1
            elif i >= exit_length and low_line is not None and close < low_line:
                holding = 0
            targets.append(holding)
        return StrategyOutput(targets, {f"突破 {entry}": entry_line, f"出場 {exit_length}": exit_line})


class StrategyRegistry:
    def __init__(self):
        self._strategies: dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        if strategy.key in self._strategies:
            raise ValueError(f"策略已註冊：{strategy.key}")
        self._strategies[strategy.key] = strategy

    def get(self, key: str) -> Strategy:
        try:
            return self._strategies[key]
        except KeyError as exc:
            raise StrategyParameterError(f"不支援的策略：{key}") from exc

    def describe(self) -> list[dict[str, str]]:
        return [{"key": item.key, "label": item.label} for item in self._strategies.values()]


strategy_registry = StrategyRegistry()
strategy_registry.register(SmaCrossStrategy())
strategy_registry.register(BreakoutStrategy())
strategy_registry.register(RsiReversalStrategy())
