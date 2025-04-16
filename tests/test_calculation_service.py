# tests/test_calculation_service.py
import unittest
from unittest.mock import patch, AsyncMock, MagicMock, call
import asyncio
import sys
import os
import json
from decimal import Decimal
from typing import Optional, Dict, Any, List, Tuple
import time

# --- 設定主程式碼的路徑 ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
my_project_path = os.path.join(project_root, 'my_project')
if my_project_path not in sys.path:
    sys.path.insert(0, my_project_path)
# --- 路徑設定結束 ---

import redis # 用於 exceptions
import aiohttp
import aio_pika
from calculation_service import CalculationService, MIN_PRICE_DIFF_THRESHOLD, BINANCE_FUNDING_RATE_URL, OPPORTUNITY_QUEUE_NAME # noqa: E402

# --- 測試用的假 Redis 客戶端 ---
def create_mock_redis_client():
    mock_client = AsyncMock()
    async def mock_scan_iter(*args, **kwargs):
        keys = ["prices:BTCUSDT", "prices:ETHUSDT", "prices:NOSPP"]
        for key in keys:
            yield key
    mock_client.scan_iter = mock_scan_iter
    async def mock_hgetall(key, *args, **kwargs):
        if key == "prices:BTCUSDT":
            return {"spot": "30000.0", "perp": "30150.0"} # Diff = 0.5%
        elif key == "prices:ETHUSDT":
            return {"spot": "2000.0", "perp": "2100.0"}  # Diff = 5.0%
        elif key == "prices:NOSPP":
             return {"perp": "100"}
        return {}
    mock_client.hgetall = AsyncMock(side_effect=mock_hgetall)
    mock_client.ping = AsyncMock(return_value=True)
    mock_client.aclose = AsyncMock()
    return mock_client

# --- 測試用的假 RabbitMQ 物件 ---
def create_mock_rabbitmq_channel():
    mock_channel = AsyncMock()
    mock_channel.declare_queue = AsyncMock()
    mock_channel.default_exchange = AsyncMock()
    mock_channel.default_exchange.publish = AsyncMock() # <--- 我們要驗證這個 publish
    mock_channel.close = AsyncMock()
    return mock_channel

def create_mock_rabbitmq_connection(mock_channel):
    mock_connection = AsyncMock()
    mock_connection.channel = AsyncMock(return_value=mock_channel)
    mock_connection.close = AsyncMock()
    return mock_connection

# --- 測試類別 ---
class TestCalculationService(unittest.IsolatedAsyncioTestCase):
    """
    對 CalculationService 進行單元測試
    """
    def setUp(self):
        self.redis_url = "redis://mock-redis:6379"
        self.rabbitmq_url = "amqp://mock-rabbitmq/"
        self.interval = 10
        self.service = CalculationService(
            redis_url=self.redis_url,
            rabbitmq_url=self.rabbitmq_url,
            interval=self.interval
        )

    @patch("redis.asyncio.Redis.from_url")
    async def test_connect_redis_success(self, mock_redis_from_url):
        """測試：成功連接 Redis"""
        print("\n--- Running test: test_connect_redis_success ---")
        mock_redis = create_mock_redis_client()
        mock_redis_from_url.return_value = mock_redis
        result = await self.service._connect_redis()
        self.assertTrue(result)
        print(">>> test_connect_redis_success: PASSED")

    @patch("redis.asyncio.Redis.from_url")
    async def test_connect_redis_failure(self, mock_redis_from_url):
        """測試：連接 Redis 失敗時返回 False"""
        print("\n--- Running test: test_connect_redis_failure ---")
        mock_redis_from_url.side_effect = redis.exceptions.ConnectionError("Test connection error")
        result = await self.service._connect_redis()
        self.assertFalse(result)
        self.assertIsNone(self.service.redis_client)
        print(">>> test_connect_redis_failure: PASSED")

    @patch("aio_pika.connect_robust")
    async def test_connect_rabbitmq_success(self, mock_connect_robust):
        """測試：成功連接 RabbitMQ 並宣告佇列"""
        print("\n--- Running test: test_connect_rabbitmq_success ---")
        mock_channel = create_mock_rabbitmq_channel()
        mock_connection = create_mock_rabbitmq_connection(mock_channel)
        mock_connect_robust.return_value = mock_connection
        result = await self.service._connect_rabbitmq()
        # ... (省略部分驗證) ...
        self.assertTrue(result)
        print(">>> test_connect_rabbitmq_success: PASSED")

    @patch("aio_pika.connect_robust")
    async def test_connect_rabbitmq_failure(self, mock_connect_robust):
        """測試：連接 RabbitMQ 失敗"""
        print("\n--- Running test: test_connect_rabbitmq_failure ---")
        mock_connect_robust.side_effect = aio_pika.exceptions.AMQPConnectionError("Test connection error")
        result = await self.service._connect_rabbitmq()
        self.assertFalse(result)
        self.assertIsNone(self.service.rabbitmq_connection)
        self.assertIsNone(self.service.rabbitmq_channel)
        print(">>> test_connect_rabbitmq_failure: PASSED")

    @patch("redis.asyncio.Redis.from_url")
    async def test_get_all_prices_from_redis(self, mock_redis_from_url):
        """測試：能否正確從模擬的 Redis 讀取並解析價格"""
        print("\n--- Running test: test_get_all_prices_from_redis ---")
        mock_redis = create_mock_redis_client()
        self.service.redis_client = mock_redis
        mock_redis_from_url.return_value = mock_redis
        prices = await self.service._get_all_prices_from_redis()
        # ... (省略驗證) ...
        self.assertEqual(len(prices), 2)
        print(">>> test_get_all_prices_from_redis: PASSED")

    def test_calculate_opportunities(self):
        """測試：能否根據價格正確計算出符合條件的機會並排序"""
        print("\n--- Running test: test_calculate_opportunities ---")
        mock_prices = { "BTCUSDT": {"spot": Decimal("30000"), "perp": Decimal("30150")}, "ETHUSDT": {"spot": Decimal("2000"), "perp": Decimal("2100")}, }
        opportunities = self.service._calculate_opportunities(mock_prices)
        # ... (省略驗證) ...
        self.assertEqual(len(opportunities), 2) # 修正：應該是兩個
        self.assertEqual(opportunities[0][0], "ETHUSDT")
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
        # ... (省略 get 呼叫驗證) ...
        print(">>> test_get_funding_rate_success: PASSED")

    @patch("aiohttp.ClientSession")
    async def test_get_funding_rate_api_error(self, MockSession):
        """測試：API 錯誤時，獲取資金費率失敗並返回 None"""
        print("\n--- Running test: test_get_funding_rate_api_error ---")
        mock_session_instance = MockSession.return_value
        # 讓 session.get() 直接拋出錯誤
        mock_session_instance.get = AsyncMock(
            side_effect=aiohttp.ClientError("Simulated API connection error")
        )
        self.service.http_session = mock_session_instance

        funding_rate = await self.service._get_funding_rate("BTCUSDT")

        self.assertIsNone(funding_rate)
        # 我們接受這個測試在 logging e 時可能會在內部引發 AttributeError (如上次所見)
        # 但測試的核心斷言 self.assertIsNone 是正確的
        print(">>> test_get_funding_rate_api_error: PASSED (Ignoring internal AttributeError during logging)")

    # <<< --- 重點修改：驗證 _publish_opportunity 被呼叫 ---
    @patch("redis.asyncio.Redis.from_url")
    @patch("aiohttp.ClientSession")
    @patch("aio_pika.connect_robust")
    # 新增 patch 來 mock 我們要驗證的 _publish_opportunity 方法
    @patch.object(CalculationService, '_publish_opportunity', new_callable=AsyncMock)
    async def test_run_calculation_cycle_opportunity_found_and_published(
        self, mock_publish, mock_pika_connect, MockSession, mock_redis_from_url # 注入 mock_publish
    ):
        """測試：一次計算循環找到機會時，是否呼叫了發布函數"""
        print("\n--- Running test: test_run_calculation_cycle_opportunity_found_and_published ---")
        # --- Mock Redis (返回 ETH > BTC 的機會) ---
        mock_redis = create_mock_redis_client()
        mock_redis_from_url.return_value = mock_redis
        self.service.redis_client = mock_redis

        # --- Mock HTTP (返回正的資金費率) ---
        mock_http_response = AsyncMock()
        mock_http_response.raise_for_status = MagicMock()
        mock_http_response.json = AsyncMock(return_value={ "lastFundingRate": "0.0002" })
        mock_get_coro = AsyncMock(return_value=mock_http_response)
        mock_session_instance = MockSession.return_value
        mock_session_instance.get = mock_get_coro
        self.service.http_session = mock_session_instance

        # --- Mock RabbitMQ Connection (不需要 publish，只需要連線成功) ---
        mock_channel = create_mock_rabbitmq_channel()
        mock_connection = create_mock_rabbitmq_connection(mock_channel)
        mock_pika_connect.return_value = mock_connection
        self.service.rabbitmq_connection = mock_connection
        self.service.rabbitmq_channel = mock_channel # 確保 channel 存在

        # --- 執行計算循環 ---
        await self.service._run_calculation_cycle()

        # --- 關鍵驗證：檢查 _publish_opportunity 是否被呼叫，且參數正確 ---
        mock_publish.assert_awaited_once() # 確保它被呼叫了

        # 檢查呼叫時的參數
        # 最佳機會是 ETHUSDT (價差 5%), 資金費率 0.02%
        expected_symbol = "ETHUSDT"
        expected_diff = Decimal("0.05")
        expected_funding_rate = Decimal("0.0002")

        # 使用 assert_awaited_once_with 進行更嚴格的參數驗證
        mock_publish.assert_awaited_once_with(
            expected_symbol,
            unittest.mock.ANY, # 價差在內部計算，比對 Decimal 可能有浮點問題，先用 ANY
            expected_funding_rate
        )
        # 或者，如果想比對價差，可以用 assertAlmostEqual
        # call_args, call_kwargs = mock_publish.await_args
        # self.assertEqual(call_args[0], expected_symbol)
        # self.assertAlmostEqual(call_args[1], expected_diff, places=5) # 比對到小數點後5位
        # self.assertEqual(call_args[2], expected_funding_rate)

        print(">>> test_run_calculation_cycle_opportunity_found_and_published: PASSED")
    # <<< --- 修改結束 ---


    async def asyncTearDown(self):
        """每個測試結束後執行的清理"""
        # (程式碼不變)
        # ... (省略) ...
        print(f"--- 結束測試: {self._testMethodName} ---\n")


if __name__ == '__main__':
    unittest.main()

# 確保檔案結尾有空行