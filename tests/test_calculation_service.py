# tests/test_calculation_service.py
import unittest
from unittest.mock import patch, AsyncMock, MagicMock, call
import asyncio
import sys
import os
import json
from decimal import Decimal
from typing import Optional, Dict, Any, List, Tuple

# --- 設定主程式碼的路徑 ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
my_project_path = os.path.join(project_root, 'my_project')
if my_project_path not in sys.path:
    sys.path.insert(0, my_project_path)
# --- 路徑設定結束 ---

import redis # 用於 exceptions
import aiohttp
from calculation_service import CalculationService, MIN_PRICE_DIFF_THRESHOLD, BINANCE_FUNDING_RATE_URL # noqa: E402

# --- 測試用的假 Redis 客戶端 ---
# E306 修正：在函數定義前加空行

def create_mock_redis_client():
    mock_client = AsyncMock()

    # E306 修正：在巢狀函數定義前加空行
    
    async def mock_scan_iter(*args, **kwargs):
        keys = ["prices:BTCUSDT", "prices:ETHUSDT", "prices:NOSPP"]
        for key in keys:
            yield key
    mock_client.scan_iter = mock_scan_iter

    async def mock_hgetall(key, *args, **kwargs):
        if key == "prices:BTCUSDT":
            return {"spot": "30000.0", "perp": "30150.0"}
        elif key == "prices:ETHUSDT":
            return {"spot": "2000.0", "perp": "2005.0"}
        elif key == "prices:NOSPP":
             return {"perp": "100"}
        return {}
    mock_client.hgetall = AsyncMock(side_effect=mock_hgetall)

    mock_client.ping = AsyncMock(return_value=True)
    mock_client.aclose = AsyncMock()
    return mock_client

# --- 測試類別 ---
class TestCalculationService(unittest.IsolatedAsyncioTestCase):
    """
    對 CalculationService 進行單元測試
    """
    def setUp(self):
        """同步的設置"""
        self.redis_url = "redis://mock-redis:6379"
        self.interval = 10
        self.service = CalculationService(redis_url=self.redis_url, interval=self.interval)

    @patch("redis.asyncio.Redis.from_url")
    async def test_connect_redis_success(self, mock_from_url):
        """測試：成功連接 Redis"""
        print("\n--- Running test: test_connect_redis_success ---")
        mock_redis = create_mock_redis_client()
        mock_from_url.return_value = mock_redis

        result = await self.service._connect_redis()

        mock_from_url.assert_called_once_with(self.redis_url, decode_responses=True)
        mock_redis.ping.assert_awaited_once()
        self.assertEqual(self.service.redis_client, mock_redis)
        self.assertTrue(result)
        print(">>> test_connect_redis_success: PASSED")

    @patch("redis.asyncio.Redis.from_url")
    async def test_connect_redis_failure(self, mock_from_url):
        """測試：連接 Redis 失敗"""
        print("\n--- Running test: test_connect_redis_failure ---")
        mock_from_url.side_effect = redis.exceptions.ConnectionError("Test connection error")

        result = await self.service._connect_redis()
        self.assertFalse(result)

        self.assertIsNone(self.service.redis_client)
        print(">>> test_connect_redis_failure: PASSED")

    @patch("redis.asyncio.Redis.from_url")
    async def test_get_all_prices_from_redis(self, mock_from_url):
        """測試：能否正確從模擬的 Redis 讀取並解析價格"""
        print("\n--- Running test: test_get_all_prices_from_redis ---")
        mock_redis = create_mock_redis_client()
        self.service.redis_client = mock_redis
        mock_from_url.return_value = mock_redis

        prices = await self.service._get_all_prices_from_redis()

        self.assertIn("BTCUSDT", prices)
        self.assertEqual(prices["BTCUSDT"]["spot"], Decimal("30000.0"))
        self.assertEqual(prices["BTCUSDT"]["perp"], Decimal("30150.0"))
        self.assertIn("ETHUSDT", prices)
        self.assertEqual(prices["ETHUSDT"]["spot"], Decimal("2000.0"))
        self.assertEqual(prices["ETHUSDT"]["perp"], Decimal("2005.0"))
        self.assertNotIn("NOSPP", prices)
        self.assertEqual(len(prices), 2)

        mock_redis.hgetall.assert_any_call("prices:BTCUSDT")
        mock_redis.hgetall.assert_any_call("prices:ETHUSDT")
        mock_redis.hgetall.assert_any_call("prices:NOSPP")
        print(">>> test_get_all_prices_from_redis: PASSED")

    def test_calculate_opportunities(self):
        """測試：能否根據價格正確計算出符合條件的機會並排序"""
        print("\n--- Running test: test_calculate_opportunities ---")
        mock_prices = {
            "BTCUSDT": {"spot": Decimal("30000"), "perp": Decimal("30150")}, # 0.5%
            "ETHUSDT": {"spot": Decimal("2000"), "perp": Decimal("2100")},  # 5.0%
            "ADAUSDT": {"spot": Decimal("1"), "perp": Decimal("1.003")},   # 0.3%
            "DOTUSDT": {"spot": Decimal("10"), "perp": Decimal("10.05")}, # 0.5%
        }
        opportunities = self.service._calculate_opportunities(mock_prices)

        self.assertEqual(len(opportunities), 3)
        self.assertEqual(opportunities[0][0], "ETHUSDT")
        self.assertAlmostEqual(opportunities[0][1], Decimal("0.05"))
        symbols_found = {opp[0] for opp in opportunities[1:]}
        self.assertEqual(symbols_found, {"BTCUSDT", "DOTUSDT"})

        print(">>> test_calculate_opportunities: PASSED")

    @patch("aiohttp.ClientSession")
    async def test_get_funding_rate_success(self, MockSession):
        """測試：成功從 API 獲取資金費率"""
        print("\n--- Running test: test_get_funding_rate_success ---")
        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(return_value={ "lastFundingRate": "0.0001" })
        mock_get_coro = AsyncMock(return_value=mock_response)
        mock_session_instance = MockSession.return_value
        mock_session_instance.get = mock_get_coro
        self.service.http_session = mock_session_instance

        funding_rate = await self.service._get_funding_rate("BTCUSDT")

        self.assertIsNotNone(funding_rate)
        self.assertEqual(funding_rate, Decimal("0.0001"))
        mock_session_instance.get.assert_awaited_once_with(
            BINANCE_FUNDING_RATE_URL,
            params={'symbol': 'BTCUSDT'}
        )
        print(">>> test_get_funding_rate_success: PASSED")

    @patch("aiohttp.ClientSession")
    async def test_get_funding_rate_api_error(self, MockSession):
        """測試：API 回應錯誤時，獲取資金費率失敗"""
        print("\n--- Running test: test_get_funding_rate_api_error ---")
        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock(side_effect=aiohttp.ClientResponseError(None, None, status=404, message='Not Found'))
        mock_get_coro = AsyncMock(return_value=mock_response)
        mock_session_instance = MockSession.return_value
        mock_session_instance.get = mock_get_coro
        self.service.http_session = mock_session_instance

        funding_rate = await self.service._get_funding_rate("BTCUSDT")

        self.assertIsNone(funding_rate)
        print(">>> test_get_funding_rate_api_error: PASSED")

    @patch("redis.asyncio.Redis.from_url")
    @patch("aiohttp.ClientSession")
    async def test_run_calculation_cycle_opportunity_found(self, MockSession, mock_from_url):
        """測試：一次完整的計算循環，找到有效的套利機會"""
        print("\n--- Running test: test_run_calculation_cycle_opportunity_found ---")
        mock_redis = create_mock_redis_client()
        mock_from_url.return_value = mock_redis
        self.service.redis_client = mock_redis

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = AsyncMock(return_value={ "lastFundingRate": "0.0002" })
        mock_get_coro = AsyncMock(return_value=mock_response)
        mock_session_instance = MockSession.return_value
        mock_session_instance.get = mock_get_coro
        self.service.http_session = mock_session_instance

        with self.assertLogs("CalculationService", level="INFO") as log:
            await self.service._run_calculation_cycle()

        self.assertTrue(any("Top potential opportunity: BTCUSDT (Diff: 0.5000%)" in msg for msg in log.output))
        mock_session_instance.get.assert_awaited_once_with(
            BINANCE_FUNDING_RATE_URL,
            params={'symbol': 'BTCUSDT'}
        )
        self.assertTrue(any("--- Validated Opportunity Found ---" in msg for msg in log.output))
        self.assertTrue(any("Symbol: BTCUSDT" in msg for msg in log.output))
        self.assertTrue(any("Price Diff: 0.5000%" in msg for msg in log.output))
        self.assertTrue(any("Funding Rate: 0.0200%" in msg for msg in log.output))

        print(">>> test_run_calculation_cycle_opportunity_found: PASSED")


    async def asyncTearDown(self):
        """每個測試結束後執行的清理"""
        if self.service and self.service.redis_client:
             if hasattr(self.service.redis_client, 'aclose'):
                 await self.service.redis_client.aclose()
             elif hasattr(self.service.redis_client, 'close'):
                 await self.service.redis_client.close()

        if self.service and self.service.http_session and not self.service.http_session.closed:
            await self.service.http_session.close()

        print(f"--- 結束測試: {self._testMethodName} ---\n")


if __name__ == '__main__':
    unittest.main()

# W292: 確保檔案結尾有空行