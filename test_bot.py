import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock
import sys
import os

from scraper import TelegramScraper
from tradelocker_client import TradeLockerClient
from risk import RiskManager

class TestParser(unittest.TestCase):
    def setUp(self):
        # Initialize scraper with mock callback and configuration
        config = {
            "telegram": {
                "api_id": 123456,
                "api_hash": "mockhash",
                "channel_ids": [123]
            },
            "gemini": {
                "api_key": "your_gemini_api_key_here"
            }
        }
        self.scraper = TelegramScraper(config, lambda x: None)

    def test_heuristic_standard_buy(self):
        text = "XAUUSD BUY NOW\nEntry: 2315.50\nSL: 2305.00\nTP1: 2325.00\nTP2: 2335.00\nTP3: 2345.00"
        result = self.scraper.parse_heuristic(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "Buy")
        self.assertEqual(result["entry_price"], 2315.50)
        self.assertEqual(result["stop_loss"], 2305.00)
        self.assertEqual(result["take_profits"], [2325.00, 2335.00, 2345.00])

    def test_heuristic_sell_limit(self):
        text = "GOLD SELL LIMIT AT 2450.00\nSL: 2465.00\nTarget 1: 2430.00\nTarget 2: 2410.00"
        result = self.scraper.parse_heuristic(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "Sell Limit")
        self.assertEqual(result["entry_price"], 2450.00)
        self.assertEqual(result["stop_loss"], 2465.00)
        self.assertEqual(result["take_profits"], [2410.00, 2430.00])  # Sorted

    def test_heuristic_user_sample_high_price(self):
        text = (
            "✅GOLD SELL 4254/4257\n"
            "✅SL_ 4267\n\n"
            "✅TP¹ 4250\n"
            "✅TP² 4245\n"
            "✅TP³ 4240\n"
            "✅TP⁴ 4235\n"
            "✅TP⁵ 4230\n"
            "✅TP⁶ 4225\n"
            "✅TP⁷ 4220\n"
            "✅TP⁸ 4215"
        )
        result = self.scraper.parse_heuristic(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "Sell")
        self.assertEqual(result["entry_price"], 4254.00)
        self.assertEqual(result["stop_loss"], 4267.00)
        self.assertEqual(result["take_profits"], [4215.00, 4220.00, 4225.00, 4230.00, 4235.00, 4240.00, 4245.00, 4250.00])

    def test_non_gold_signal(self):
        text = "EURUSD BUY NOW at 1.08500 sl 1.08000 tp 1.09000"
        result = self.scraper.parse_heuristic(text)
        self.assertNilOrEmpty(result)

    def assertNilOrEmpty(self, result):
        self.assertTrue(result is None or result.get("action") is None)

class TestRiskSizing(unittest.TestCase):
    def setUp(self):
        # Configure client with fixed and percentage parameters
        self.config = {
            "tradelocker": {
                "environment": "https://demo.tradelocker.com",
                "username": "test@example.com",
                "password": "test",
                "server": "demo"
            },
            "risk": {
                "risk_type": "percentage",
                "fixed_lot_size": 0.05,
                "risk_percentage": 2.0,
                "gold_contract_size": 100.0
            }
        }
        self.client = TradeLockerClient(self.config)
        self.client.get_balance = MagicMock(return_value=10000.0)  # Mock balance to $10,000

    def test_fixed_lot_sizing(self):
        self.client.risk_config["risk_type"] = "fixed"
        lot = self.client.calculate_lot_size(2320.0, 2310.0)
        self.assertEqual(lot, 0.05)

    def test_percentage_lot_sizing(self):
        # Balance = $10,000, Risk = 2.0% ($200 risk amount)
        # Entry = 2320, SL = 2310 (Distance = 10.0 points)
        # Contract size = 100.0
        # Expected lot size = 200 / (100 * 10) = 0.20 lots
        lot = self.client.calculate_lot_size(2320.0, 2310.0)
        self.assertEqual(lot, 0.20)

    def test_percentage_minimum_cap(self):
        # Balance = $10,000, Risk = 2.0% ($200 risk)
        # SL distance = 1000 points
        # Expected lot size = 200 / (100 * 1000) = 0.002 lots -> should round/cap to min lot 0.01
        lot = self.client.calculate_lot_size(2300.0, 1300.0)
        self.assertEqual(lot, 0.01)

class TestStopLossModificationRetry(unittest.IsolatedAsyncioTestCase):
    async def test_retry_loop_success_eventually(self):
        # Setup mock client
        mock_client = MagicMock()
        
        # Simulates 2 failures then 1 success
        mock_client.modify_position_sl = MagicMock(side_effect=[False, Exception("Network Timeout"), True])
        
        config = {
            "risk": {
                "polling_interval": 1.0,
                "breakeven_buffer": 0.2
            }
        }
        
        risk_manager = RiskManager(config, mock_client)
        
        # Run modification with short delay for fast testing
        success = await risk_manager.modify_position_sl_with_retry(
            pos_id=123,
            new_sl=2315.0,
            max_retries=5,
            base_delay=0.1
        )
        
        self.assertTrue(success)
        self.assertEqual(mock_client.modify_position_sl.call_count, 3)

    async def test_retry_loop_total_failure(self):
        mock_client = MagicMock()
        mock_client.modify_position_sl = MagicMock(return_value=False)
        
        config = {
            "risk": {}
        }
        risk_manager = RiskManager(config, mock_client)
        
        success = await risk_manager.modify_position_sl_with_retry(
            pos_id=123,
            new_sl=2315.0,
            max_retries=3,
            base_delay=0.1
        )
        
        self.assertFalse(success)
        self.assertEqual(mock_client.modify_position_sl.call_count, 3)

if __name__ == "__main__":
    unittest.main()
