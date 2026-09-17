"""Cross-sectional daily factor discovery and synthesis.

All selection weights are estimated on the training sample.  Test metrics are
reported afterwards and are never used to choose factors or weights.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Callable

from .domain import Bar


FactorValues = dict[str, list[float | None]]


class FactorError(ValueError):
    pass


@dataclass(frozen=True)
class FactorDefinition:
    key: str
    label: str
    category: str
    minimum_bars: int
    calculate: Callable[[list[Bar]], list[float | None]]

    def describe(self) -> dict:
        return {"key": self.key, "label": self.label, "category": self.category, "minimum_bars": self.minimum_bars}


class FactorRegistry:
    def __init__(self):
        self._items: dict[str, FactorDefinition] = {}

    def register(self, factor: FactorDefinition) -> None:
        if factor.key in self._items:
            raise FactorError(f"因子已註冊：{factor.key}")
        self._items[factor.key] = factor

    def get(self, key: str) -> FactorDefinition:
        try:
            return self._items[key]
        except KeyError as exc:
            raise FactorError(f"未知因子：{key}") from exc

    def all(self) -> list[FactorDefinition]:
        return list(self._items.values())

    def describe(self) -> list[dict]:
        return [item.describe() for item in self._items.values()]


def _rolling_mean(values: list[float], end: int, window: int) -> float | None:
    if end + 1 < window:
        return None
    sample = values[end - window + 1:end + 1]
    return sum(sample) / window


def _momentum(window: int, reverse: bool = False):
    def calculate(bars: list[Bar]) -> list[float | None]:
        result = []
        for i, bar in enumerate(bars):
            value = None if i < window else bar.close / bars[i - window].close - 1
            result.append(-value if reverse and value is not None else value)
        return result
    return calculate


def _volatility(window: int):
    def calculate(bars: list[Bar]) -> list[float | None]:
        returns = [0.0] + [math.log(bars[i].close / bars[i - 1].close) for i in range(1, len(bars))]
        result = []
        for i in range(len(bars)):
            sample = returns[i - window + 1:i + 1] if i >= window else []
            result.append(statistics.stdev(sample) * math.sqrt(252) if len(sample) >= 2 else None)
        return result
    return calculate


def _volume_ratio(window: int):
    def calculate(bars: list[Bar]) -> list[float | None]:
        volumes = [float(bar.volume) for bar in bars]
        result = []
        for i, value in enumerate(volumes):
            average = _rolling_mean(volumes, i, window)
            result.append(value / average if average and average > 0 else None)
        return result
    return calculate


def _ma_gap(fast: int, slow: int):
    def calculate(bars: list[Bar]) -> list[float | None]:
        closes = [bar.close for bar in bars]
        result = []
        for i in range(len(bars)):
            fast_ma, slow_ma = _rolling_mean(closes, i, fast), _rolling_mean(closes, i, slow)
            result.append(fast_ma / slow_ma - 1 if fast_ma is not None and slow_ma else None)
        return result
    return calculate


def _price_position(window: int):
    def calculate(bars: list[Bar]) -> list[float | None]:
        result = []
        for i, bar in enumerate(bars):
            if i + 1 < window:
                result.append(None)
                continue
            sample = bars[i - window + 1:i + 1]
            high, low = max(item.high for item in sample), min(item.low for item in sample)
            result.append((bar.close - low) / (high - low) - 0.5 if high > low else 0.0)
        return result
    return calculate


def _intraday_strength(window: int):
    def calculate(bars: list[Bar]) -> list[float | None]:
        raw = [(bar.close - bar.open) / (bar.high - bar.low) if bar.high > bar.low else 0.0 for bar in bars]
        return [_rolling_mean(raw, i, window) for i in range(len(bars))]
    return calculate


def _range_volatility(window: int):
    def calculate(bars: list[Bar]) -> list[float | None]:
        raw = [(bar.high - bar.low) / bar.close for bar in bars]
        return [_rolling_mean(raw, i, window) for i in range(len(bars))]
    return calculate


def _illiquidity(window: int):
    def calculate(bars: list[Bar]) -> list[float | None]:
        raw = [0.0]
        for i in range(1, len(bars)):
            traded_value = bars[i].close * bars[i].volume
            raw.append(abs(bars[i].close / bars[i - 1].close - 1) / traded_value * 1e9 if traded_value else 0.0)
        return [_rolling_mean(raw, i, window) for i in range(len(bars))]
    return calculate


factor_registry = FactorRegistry()
for _window in (5, 10, 20, 60):
    factor_registry.register(FactorDefinition(f"momentum_{_window}", f"{_window} 日動能", "動能", _window + 1, _momentum(_window)))
for _window in (3, 5, 10):
    factor_registry.register(FactorDefinition(f"reversal_{_window}", f"{_window} 日反轉", "反轉", _window + 1, _momentum(_window, True)))
for _window in (5, 10, 20):
    factor_registry.register(FactorDefinition(f"volatility_{_window}", f"{_window} 日波動率", "風險", _window + 1, _volatility(_window)))
for _window in (5, 20):
    factor_registry.register(FactorDefinition(f"volume_ratio_{_window}", f"{_window} 日量比", "量能", _window, _volume_ratio(_window)))
for _fast, _slow in ((5, 20), (10, 60), (20, 60)):
    factor_registry.register(FactorDefinition(f"ma_gap_{_fast}_{_slow}", f"均線乖離 {_fast}/{_slow}", "趨勢", _slow, _ma_gap(_fast, _slow)))
for _window in (10, 20, 60):
    factor_registry.register(FactorDefinition(f"price_position_{_window}", f"{_window} 日價格位置", "突破", _window, _price_position(_window)))
for _window in (1, 5):
    factor_registry.register(FactorDefinition(f"intraday_strength_{_window}", f"{_window} 日盤中強度", "價量", _window, _intraday_strength(_window)))
for _window in (5, 20):
    factor_registry.register(FactorDefinition(f"range_volatility_{_window}", f"{_window} 日振幅", "風險", _window, _range_volatility(_window)))
for _window in (5, 20):
    factor_registry.register(FactorDefinition(f"illiquidity_{_window}", f"{_window} 日非流動性", "流動性", _window + 1, _illiquidity(_window)))


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right):
        return None
    left_mean, right_mean = _mean(left), _mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_sum = sum((a - left_mean) ** 2 for a in left)
    right_sum = sum((b - right_mean) ** 2 for b in right)
    denominator = math.sqrt(left_sum * right_sum)
    return numerator / denominator if denominator else None


def _ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    result = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        rank = (i + j - 1) / 2 + 1
        for position in range(i, j):
            result[indexed[position][0]] = rank
        i = j
    return result


def _spearman(left: list[float], right: list[float]) -> float | None:
    return _pearson(_ranks(left), _ranks(right))


def _factor_values(panel: dict[str, list[Bar]], factor: FactorDefinition) -> FactorValues:
    return {key: factor.calculate(bars) for key, bars in panel.items()}


def _rank_map(panel: dict[str, list[Bar]], values: FactorValues) -> dict[tuple[str, str], float]:
    by_date: dict[str, list[tuple[str, float]]] = {}
    for symbol, bars in panel.items():
        for bar, value in zip(bars, values[symbol]):
            if value is not None and math.isfinite(value):
                by_date.setdefault(bar.date, []).append((symbol, value))
    result = {}
    for date, items in by_date.items():
        ranks = _ranks([value for _, value in items])
        denominator = max(1, len(items) - 1)
        for (symbol, _), rank in zip(items, ranks):
            result[(date, symbol)] = (rank - 1) / denominator - 0.5
    return result


def _daily_factor_stats(panel: dict[str, list[Bar]], values: FactorValues, horizon: int) -> list[dict]:
    by_date: dict[str, list[tuple[float, float]]] = {}
    for symbol, bars in panel.items():
        factor_values = values[symbol]
        for i in range(len(bars) - horizon):
            value = factor_values[i]
            if value is None or not math.isfinite(value):
                continue
            forward_return = bars[i + horizon].close / bars[i].close - 1
            by_date.setdefault(bars[i].date, []).append((value, forward_return))
    result = []
    for date in sorted(by_date):
        observations = by_date[date]
        if len(observations) < 3:
            continue
        factor_sample = [item[0] for item in observations]
        return_sample = [item[1] for item in observations]
        ic = _spearman(factor_sample, return_sample)
        if ic is None:
            continue
        ordered = sorted(observations, key=lambda item: item[0])
        bucket = max(1, len(ordered) // 3)
        spread = _mean([item[1] for item in ordered[-bucket:]]) - _mean([item[1] for item in ordered[:bucket]])
        result.append({"date": date, "ic": ic, "spread": spread, "count": len(observations)})
    return result


def _summary(rows: list[dict], horizon: int) -> dict:
    ics = [row["ic"] for row in rows]
    spreads = [row["spread"] for row in rows]
    mean_ic = _mean(ics)
    ic_std = statistics.stdev(ics) if len(ics) > 1 else None
    ic_ir = mean_ic / ic_std * math.sqrt(252 / horizon) if mean_ic is not None and ic_std else None
    hac_t_stat, p_value = _hac_significance(ics, horizon)
    return {
        "dates": len(rows),
        "observations": sum(row["count"] for row in rows),
        "mean_ic": mean_ic,
        "ic_std": ic_std,
        "ic_ir": ic_ir,
        "hac_t_stat": hac_t_stat,
        "p_value": p_value,
        "positive_ic_ratio": sum(value > 0 for value in ics) / len(ics) if ics else None,
        "mean_spread_pct": _mean(spreads) * 100 if spreads else None,
    }


def _hac_significance(values: list[float], horizon: int) -> tuple[float | None, float | None]:
    """Newey-West style t-stat with lag matched to overlapping forward returns."""
    count = len(values)
    if count < 3:
        return None, None
    mean = sum(values) / count
    centered = [value - mean for value in values]
    gamma_zero = sum(value * value for value in centered) / count
    long_run_variance = gamma_zero
    max_lag = min(horizon - 1, count - 2)
    for lag in range(1, max_lag + 1):
        covariance = sum(centered[i] * centered[i - lag] for i in range(lag, count)) / count
        weight = 1 - lag / (max_lag + 1)
        long_run_variance += 2 * weight * covariance
    variance_of_mean = max(long_run_variance, 0) / count
    if variance_of_mean <= 0:
        return None, None
    t_stat = mean / math.sqrt(variance_of_mean)
    p_value = math.erfc(abs(t_stat) / math.sqrt(2))
    return t_stat, min(1.0, max(0.0, p_value))


def evaluate_factor(panel: dict[str, list[Bar]], values: FactorValues, horizon: int, train_ratio: float) -> dict:
    rows = _daily_factor_stats(panel, values, horizon)
    if len(rows) < 20:
        raise FactorError("有效橫斷面日期不足 20 日；請增加標的或日期範圍")
    split = min(len(rows) - 5, max(10, int(len(rows) * train_ratio)))
    train_rows, test_rows = rows[:split], rows[split:]
    train, test = _summary(train_rows, horizon), _summary(test_rows, horizon)
    train_ic, test_ic = train["mean_ic"], test["mean_ic"]
    if test_ic is None:
        status = "insufficient"
    elif train_ic * test_ic < 0:
        status = "sign_flip"
    elif abs(test_ic) < 0.01:
        status = "weak"
    else:
        status = "stable"
    return {
        "train": train,
        "test": test,
        "split_date": rows[split]["date"],
        "score": abs(train_ic or 0) * math.sqrt(len(train_rows)),
        "orientation": 1 if (train_ic or 0) >= 0 else -1,
        "status": status,
        "daily_ic": [{"date": row["date"], "ic": row["ic"]} for row in rows],
    }


def _factor_correlation(left: dict[tuple[str, str], float], right: dict[tuple[str, str], float], cutoff: str) -> float:
    keys = sorted(key for key in left.keys() & right.keys() if key[0] < cutoff)
    return _pearson([left[key] for key in keys], [right[key] for key in keys]) or 0.0


def discover_factors(
    panel: dict[str, list[Bar]], horizon: int = 5, train_ratio: float = 0.7,
    top_k: int = 6, correlation_threshold: float = 0.85,
) -> dict:
    if horizon < 1 or horizon > 60:
        raise FactorError("預測週期必須介於 1～60 個交易日")
    if not 0.5 <= train_ratio <= 0.9:
        raise FactorError("訓練比例必須介於 0.5～0.9")
    if top_k < 1 or top_k > 20:
        raise FactorError("保留因子數必須介於 1～20")
    evaluated, value_cache, rank_cache = [], {}, {}
    for factor in factor_registry.all():
        values = _factor_values(panel, factor)
        try:
            metrics = evaluate_factor(panel, values, horizon, train_ratio)
        except FactorError:
            continue
        value_cache[factor.key] = values
        rank_cache[factor.key] = _rank_map(panel, values)
        evaluated.append({**factor.describe(), **metrics})
    if not evaluated:
        raise FactorError("沒有因子具備足夠資料，請增加日期範圍")
    # Benjamini-Hochberg false-discovery-rate correction across all candidates.
    ordered_p = sorted(evaluated, key=lambda item: item["train"]["p_value"] if item["train"]["p_value"] is not None else 1.0)
    running_q = 1.0
    for reverse_index in range(len(ordered_p) - 1, -1, -1):
        item = ordered_p[reverse_index]
        rank = reverse_index + 1
        p_value = item["train"]["p_value"] if item["train"]["p_value"] is not None else 1.0
        running_q = min(running_q, p_value * len(ordered_p) / rank)
        item["train"]["q_value"] = min(1.0, running_q)
    evaluated.sort(key=lambda item: item["score"], reverse=True)
    selected = []
    for candidate in evaluated:
        if all(abs(_factor_correlation(rank_cache[candidate["key"]], rank_cache[item["key"]], candidate["split_date"])) < correlation_threshold for item in selected):
            selected.append(candidate)
        if len(selected) >= top_k:
            break
    return {
        "candidates": evaluated,
        "selected_keys": [item["key"] for item in selected],
        "settings": {"horizon": horizon, "train_ratio": train_ratio, "top_k": top_k, "correlation_threshold": correlation_threshold},
    }


def synthesize_factors(
    panel: dict[str, list[Bar]], factor_keys: list[str], method: str = "ic_weighted",
    horizon: int = 5, train_ratio: float = 0.7,
) -> dict:
    if not 2 <= len(factor_keys) <= 20:
        raise FactorError("因子合成需選擇 2～20 個因子")
    if method not in {"equal", "ic_weighted", "icir_weighted"}:
        raise FactorError("合成方法僅支援 equal、ic_weighted、icir_weighted")
    factors = [factor_registry.get(key) for key in dict.fromkeys(factor_keys)]
    evaluations, rank_maps = {}, {}
    for factor in factors:
        values = _factor_values(panel, factor)
        evaluations[factor.key] = evaluate_factor(panel, values, horizon, train_ratio)
        rank_maps[factor.key] = _rank_map(panel, values)
    raw_weights = {}
    for factor in factors:
        metrics = evaluations[factor.key]["train"]
        mean_ic = metrics["mean_ic"] or 0.0
        if method == "equal":
            raw_weights[factor.key] = 1.0 if mean_ic >= 0 else -1.0
        elif method == "ic_weighted":
            raw_weights[factor.key] = mean_ic
        else:
            raw_weights[factor.key] = mean_ic / max(metrics["ic_std"] or 0.001, 0.001)
    denominator = sum(abs(value) for value in raw_weights.values())
    if not denominator:
        raise FactorError("訓練區間因子權重皆為 0，無法合成")
    weights = {key: value / denominator for key, value in raw_weights.items()}

    composite_lookup: dict[tuple[str, str], float] = {}
    all_keys = set().union(*(mapping.keys() for mapping in rank_maps.values()))
    for observation_key in all_keys:
        available = [(weights[key], rank_maps[key][observation_key]) for key in weights if observation_key in rank_maps[key]]
        available_weight = sum(abs(weight) for weight, _ in available)
        if available_weight:
            composite_lookup[observation_key] = sum(weight * value for weight, value in available) / available_weight
    composite_values: FactorValues = {}
    for symbol, bars in panel.items():
        composite_values[symbol] = [composite_lookup.get((bar.date, symbol)) for bar in bars]
    evaluation = evaluate_factor(panel, composite_values, horizon, train_ratio)

    dates = sorted({date for date, _ in composite_lookup})
    latest_date = dates[-1] if dates else None
    latest = sorted(
        ({"symbol": symbol, "score": composite_lookup[(latest_date, symbol)]} for symbol in panel if (latest_date, symbol) in composite_lookup),
        key=lambda item: item["score"], reverse=True,
    )
    for rank, item in enumerate(latest, start=1):
        item["rank"] = rank
    by_date: dict[str, dict[str, float]] = {}
    for (date, symbol), score in composite_lookup.items():
        by_date.setdefault(date, {})[symbol] = score
    rank_history = []
    for date in sorted(by_date):
        items = list(by_date[date].items())
        ranks = _ranks([score for _, score in items])
        denominator_rank = max(1, len(items) - 1)
        rank_history.append({symbol: (rank - 1) / denominator_rank for (symbol, _), rank in zip(items, ranks)})
    turnovers = []
    for previous, current in zip(rank_history, rank_history[1:]):
        common = previous.keys() & current.keys()
        if common:
            turnovers.append(sum(abs(current[key] - previous[key]) for key in common) / len(common))
    return {
        "method": method,
        "weights": [{"key": factor.key, "label": factor.label, "weight": weights[factor.key]} for factor in factors],
        "performance": evaluation,
        "latest_date": latest_date,
        "latest_ranking": latest,
        "average_rank_turnover_pct": (_mean(turnovers) or 0) * 100,
    }
