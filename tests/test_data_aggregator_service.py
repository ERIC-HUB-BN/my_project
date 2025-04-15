# tests/test_data_aggregator_service.py
import unittest
from unittest.mock import patch, AsyncMock, call
import asyncio
import sys
import os
import json  # <<< --- 新增 import json ---
import redis # <<< --- 新增 import redis --- (用於 exceptions)

# --- 設定主程式碼的路徑 (與之前相同) ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
my_project_path = os.path.join(project_root, 'my_project')
if my_project_path not in sys.path:
    sys.path.insert(0, my_project_path)
# --- 路徑設定結束 ---

# 匯入我們要測試的類別
from data_aggregator_service import DataAggregatorService # noqa: E402

# --- 測試用的假 Redis 客戶端 ---
def create_mock_redis_client():
    mock_client = AsyncMock()
    mock_client.hset = AsyncMock()
    mock_client.ping = AsyncMock(return_value=True)
    mock_client.close = AsyncMock()
    mock_client.aclose = AsyncMock()
    return mock_client

# --- 測試類別 ---
class TestDataAggregatorService(unittest.IsolatedAsyncioTestCase):
    """
    對 DataAggregatorService 進行單元測試
    """
    def setUp(self):
        """同步的設置"""
        self.redis_url = "redis://mock-redis:6379"
        self.service = DataAggregatorService(redis_url=self.redis_url)

    # 測試 Redis 連線邏輯
    # 注意：由於 setUp 不是 async，我們不能在這裡 await _connect_redis
    # 所以 Redis Client 的 mock 需要在每個需要它的 test case 裡處理
    @patch("redis.asyncio.Redis.from_url") # Mock掉實際的 Redis 連線建立
    async def test_connect_redis_success(self, mock_from_url):
        """測試：成功連接 Redis"""
        print("\n--- Running test: test_connect_redis_success ---")
        mock_redis = create_mock_redis_client()
        mock_from_url.return_value = mock_redis # 讓 from_url 返回我們的假 client

        # 手動呼叫內部方法進行測試
        await self.service._connect_redis()

        mock_from_url.assert_called_once_with(self.redis_url, decode_responses=True)
        mock_redis.ping.assert_awaited_once()
        self.assertEqual(self.service.redis_client, mock_redis)
        print(">>> test_connect_redis_success: PASSED")

    @patch("redis.asyncio.Redis.from_url")
    async def test_connect_redis_failure(self, mock_from_url):
        """測試：連接 Redis 失敗"""
        print("\n--- Running test: test_connect_redis_failure ---")
        # 模擬 from_url 或 ping 拋出異常
        # 使用 import redis 之後的 redis.exceptions
        mock_from_url.side_effect = redis.exceptions.ConnectionError("Test connection error")

        with self.assertRaises(ConnectionError):
            await self.service._connect_redis()

        self.assertIsNone(self.service.redis_client)
        print(">>> test_connect_redis_failure: PASSED")

    # 測試處理 Spot Mini Ticker 訊息
    async def test_process_spot_mini_ticker(self):
        """測試：處理來自 !miniTicker@arr 的訊息並寫入 Redis"""
        print("\n--- Running test: test_process_spot_mini_ticker ---")
        # 假裝 Redis 已經連線成功，並賦值給 service 實例
        self.service.redis_client = create_mock_redis_client()

        # 使用 import json 之後的 json.dumps
        mock_spot_message = json.dumps([
            {"e": "24hrMiniTicker", "s": "BTCUSDT", "c": "31000.50", "o": "30000", "h": "32000", "l": "29000", "v": "1000", "q": "31000000"},
            {"e": "24hrMiniTicker", "s": "ETHUSDT", "c": "2050.00", "o": "2000", "h": "2100", "l": "1900", "v": "5000", "q": "10250000"},
            {"e": "24hrMiniTicker", "s": "ETHAUD", "c": "3000.00", "o": "2900", "h": "3100", "l": "2800", "v": "100", "q": "300000"}
        ])

        await self.service._process_message_single(mock_spot_message, "Spot MiniTicker")

        expected_calls = [
            call('prices:BTCUSDT', key='spot', value='31000.50'),
            call('prices:ETHUSDT', key='spot', value='2050.00')
        ]
        self.service.redis_client.hset.assert_has_calls(expected_calls, any_order=True)
        self.assertEqual(self.service.redis_client.hset.call_count, 2)
        print(">>> test_process_spot_mini_ticker: PASSED")

    # 測試處理 Futures Mark Price 訊息
    async def test_process_futures_mark_price(self):
        """測試：處理來自 !markPrice@arr@1s 的訊息並寫入 Redis"""
        print("\n--- Running test: test_process_futures_mark_price ---")
        # 假裝 Redis 已經連線成功
        self.service.redis_client = create_mock_redis_client()

        # 使用 import json 之後的 json.dumps
        mock_futures_message = json.dumps([
            {"e": "markPriceUpdate", "s": "BTCUSDT", "p": "31050.75", "r": "0.0001", "T": 1678886400000},
            {"e": "markPriceUpdate", "s": "ETHUSDT", "p": "2055.25", "r": "0.0002", "T": 1678886400000},
            {"e": "markPriceUpdate", "s": "ADAUSDT", "p": "0.40", "r": "0.0001", "T": 1678886400000}
        ])

        await self.service._process_message_single(mock_futures_message, "Futures MarkPrice")

        expected_calls = [
            call('prices:BTCUSDT', key='perp', value='31050.75'),
            call('prices:ETHUSDT', key='perp', value='2055.25'),
            call('prices:ADAUSDT', key='perp', value='0.40')
        ]
        self.service.redis_client.hset.assert_has_calls(expected_calls, any_order=True)
        self.assertEqual(self.service.redis_client.hset.call_count, 3)
        print(">>> test_process_futures_mark_price: PASSED")


    # E301: 在 asyncTearDown 前需要一個空行
    async def asyncTearDown(self):
        """每個測試結束後執行的清理"""
        # 確保即使 Redis 連線在測試中失敗，也不會在這裡出錯
        if self.service and self.service.redis_client:
             # 如果是用 aclose()
             if hasattr(self.service.redis_client, 'aclose'):
                 await self.service.redis_client.aclose()
             # 如果是用 close()
             elif hasattr(self.service.redis_client, 'close'):
                 await self.service.redis_client.close()
        print(f"--- 結束測試: {self._testMethodName} ---\n")


# E305: 函數/類別結束後需要兩個空行
if __name__ == '__main__':
    unittest.main()

# W292: 確保檔案結尾有空行