import unittest

from twbacktest.data import Bar, DataError, parse_csv_text
from twbacktest.engine import BacktestConfig, BacktestError, run_backtest


def make_bars(closes, opens=None):
    opens = opens or closes
    return [
        Bar(
            date=f"2024-01-{index + 1:02d}",
            open=float(opens[index]),
            high=float(max(opens[index], close) + 1),
            low=float(min(opens[index], close) - 1),
            close=float(close),
            volume=1_000_000,
        )
        for index, close in enumerate(closes)
    ]


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.free = BacktestConfig(
            initial_cash=1_000,
            commission_rate=0,
            commission_discount=0,
            min_commission=0,
            tax_rate=0,
            slippage_bps=0,
            lot_size=1,
        )

    def test_signal_is_executed_on_next_open(self):
        bars = make_bars([10, 9, 11, 12, 14, 15], [10, 9, 11, 12, 13, 15])
        result = run_backtest(bars, "sma_cross", {"fast": 2, "slow": 3}, self.free)
        self.assertEqual(result["executions"][0]["side"], "BUY")
        self.assertEqual(result["executions"][0]["date"], "2024-01-05")
        self.assertEqual(result["executions"][0]["price"], 13)

    def test_force_liquidation_completes_trade(self):
        bars = make_bars([10, 9, 11, 12, 14, 15])
        result = run_backtest(bars, "sma_cross", {"fast": 2, "slow": 3}, self.free)
        self.assertEqual(result["summary"]["trade_count"], 1)
        self.assertEqual(result["trades"][0]["exit_reason"], "期末平倉")
        self.assertEqual(result["summary"]["final_equity"], 1071)

    def test_invalid_sma_parameters(self):
        with self.assertRaises(BacktestError):
            run_backtest(make_bars([10, 11]), "sma_cross", {"fast": 5, "slow": 2}, self.free)


class CsvTests(unittest.TestCase):
    def test_english_csv_and_sorting(self):
        text = "date,open,high,low,close,volume\n2024-01-02,11,12,10,11.5,2000\n2024-01-01,10,11,9,10.5,1000\n"
        bars = parse_csv_text(text)
        self.assertEqual([bar.date for bar in bars], ["2024-01-01", "2024-01-02"])
        self.assertEqual(bars[1].volume, 2000)

    def test_missing_csv_column(self):
        with self.assertRaises(DataError):
            parse_csv_text("date,open,close\n2024-01-01,10,11\n")


if __name__ == "__main__":
    unittest.main()
