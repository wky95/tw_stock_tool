import math
import unittest
from datetime import date, timedelta

from twbacktest.domain import Bar
from twbacktest.factors import discover_factors, factor_registry, synthesize_factors


def synthetic_panel(asset_count=8, days=180):
    panel = {}
    start = date(2025, 1, 1)
    for asset in range(asset_count):
        price = 80 + asset * 4
        bars = []
        for day in range(days):
            drift = (asset - (asset_count - 1) / 2) * 0.0007
            cycle = math.sin(day / 9 + asset * 0.4) * 0.002
            open_price = price * (1 + cycle * 0.2)
            price *= 1 + drift + cycle
            high = max(open_price, price) * 1.006
            low = min(open_price, price) * 0.994
            bars.append(Bar(
                date=(start + timedelta(days=day)).isoformat(),
                open=open_price, high=high, low=low, close=price,
                volume=1_000_000 + asset * 100_000 + day * 500,
            ))
        panel[f"tse:{2300 + asset}"] = bars
    return panel


class FactorResearchTests(unittest.TestCase):
    def test_catalog_has_multiple_factor_families(self):
        categories = {item["category"] for item in factor_registry.describe()}
        self.assertGreaterEqual(len(factor_registry.describe()), 15)
        self.assertTrue({"動能", "反轉", "風險", "量能"}.issubset(categories))

    def test_discovery_uses_train_and_test_samples(self):
        result = discover_factors(synthetic_panel(), horizon=5, train_ratio=0.7, top_k=5)
        self.assertTrue(result["selected_keys"])
        candidate = result["candidates"][0]
        self.assertGreater(candidate["train"]["dates"], 20)
        self.assertGreater(candidate["test"]["dates"], 5)
        self.assertGreaterEqual(candidate["train"]["q_value"], 0)
        self.assertLessEqual(candidate["train"]["q_value"], 1)
        self.assertIn(candidate["status"], {"stable", "weak", "sign_flip", "insufficient"})

    def test_synthesis_returns_weights_and_latest_ranking(self):
        panel = synthetic_panel()
        result = synthesize_factors(panel, ["momentum_5", "momentum_20", "volume_ratio_5"], horizon=5)
        self.assertAlmostEqual(sum(abs(item["weight"]) for item in result["weights"]), 1.0)
        self.assertEqual(len(result["latest_ranking"]), len(panel))
        self.assertEqual(result["latest_ranking"][0]["rank"], 1)
        self.assertGreaterEqual(result["average_rank_turnover_pct"], 0)


if __name__ == "__main__":
    unittest.main()
