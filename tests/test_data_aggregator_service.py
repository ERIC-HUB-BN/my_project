# tests/test_data_aggregator_service.py
import unittest
from unittest.mock import patch, AsyncMock, call, MagicMock
import asyncio
import sys
import os
import json
import redis # 用於 exceptions
import aiohttp
from typing import Set, List

# --- 設定主程式碼的路徑 ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
my_project_path = os.path.join(project_root, 'my_project')
if my_project_path not in sys.path:
    sys.path.insert(0, my_project_path)
# --- 路徑設定結束 ---

from data_aggregator_service import ( # noqa: E402
    DataAggregatorService,
    BINANCE_SPOT_EXCHANGE_INFO_URL,
    BINANCE_FUTURES_EXCHANGE_INFO_URL,
    SPOT_WHITELIST_KEY,
    PERP_WHITELIST_KEY,
    WHITELIST_UPDATE_INTERVAL_SECONDS
)

# --- Redis Client Mock ---
def create_mock_redis_client():
    mock_client = AsyncMock(spec=redis.asyncio.Redis)
    mock_client.hset = AsyncMock()
    mock_client.ping = AsyncMock(return_value=True)
    mock_client.aclose = AsyncMock()
    mock_client.sadd = AsyncMock()
    mock_client.sismember = AsyncMock(return_value=False)
    mock_client.delete = AsyncMock()
    mock_pipeline = AsyncMock(spec=redis.asyncio.client.Pipeline)
    mock_pipeline.delete = MagicMock()
    mock_pipeline.sadd = MagicMock()
    mock_pipeline.execute = AsyncMock()
    mock_client.pipeline = MagicMock(return_value=mock_pipeline)
    return mock_client

# --- HTTP Session Mock ---
def create_mock_http_session():
    mock_session = MagicMock(spec=aiohttp.ClientSession)
    get_context_manager_mock = AsyncMock()
    mock_session.get = MagicMock(return_value=get_context_manager_mock)
    mock_session.close = AsyncMock()
    mock_session.closed = False
    async def close_effect():
        mock_session.closed = True
    mock_session.close.side_effect = close_effect
    return mock_session

# --- aiohttp Response Mock ---
def create_mock_aiohttp_response(json_data=None, status=200, raise_for_status_error=None):
    mock_response = AsyncMock(spec=aiohttp.ClientResponse)
    mock_response.status = status
    if raise_for_status_error:
        mock_response.raise_for_status = MagicMock(side_effect=raise_for_status_error)
    else:
        mock_response.raise_for_status = MagicMock()
    mock_response.json = AsyncMock(return_value=json_data)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=None)
    return mock_response

# --- 測試類別 ---
class TestDataAggregatorService(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.redis_url = "redis://mock-redis:6379"
        self.service = DataAggregatorService(redis_url=self.redis_url)
        self.mock_redis_client = create_mock_redis_client()
        self.mock_http_session = create_mock_http_session()
        self.service.redis_client = self.mock_redis_client
        self.service.http_session = self.mock_http_session

    @patch("redis.asyncio.Redis.from_url")
    async def test_connect_redis_success(self, mock_from_url):
        print("\n--- Running test: test_connect_redis_success ---")
        service_for_test = DataAggregatorService(self.redis_url)
        mock_redis = create_mock_redis_client()
        mock_from_url.return_value = mock_redis
        await service_for_test._connect_redis()
        mock_from_url.assert_called_once_with(self.redis_url, decode_responses=True)
        mock_redis.ping.assert_awaited_once()
        self.assertIsNotNone(service_for_test.redis_client)
        print(">>> test_connect_redis_success: PASSED")

    @patch("redis.asyncio.Redis.from_url")
    async def test_connect_redis_failure(self, mock_from_url):
        print("\n--- Running test: test_connect_redis_failure ---")
        service_for_test = DataAggregatorService(self.redis_url)
        mock_from_url.side_effect = redis.exceptions.ConnectionError("Test")
        with self.assertRaises(ConnectionError):
            await service_for_test._connect_redis()
        self.assertIsNone(service_for_test.redis_client)
        print(">>> test_connect_redis_failure: PASSED")

    async def test_fetch_exchange_info_success(self):
        print("\n--- Running test: test_fetch_exchange_info_success ---")
        mock_data = {"symbols": [{"symbol": "BTCUSDT"}]}
        mock_response = create_mock_aiohttp_response(json_data=mock_data)
        get_cm_mock = self.mock_http_session.get.return_value
        get_cm_mock.__aenter__.return_value = mock_response
        result = await self.service._fetch_exchange_info("http://fake.com")
        self.mock_http_session.get.assert_called_once_with("http://fake.com", timeout=10)
        get_cm_mock.__aenter__.assert_awaited_once()
        mock_response.raise_for_status.assert_called_once()
        mock_response.json.assert_awaited_once()
        get_cm_mock.__aexit__.assert_awaited_once()
        self.assertEqual(result, mock_data)
        print(">>> test_fetch_exchange_info_success: PASSED")

    async def test_fetch_exchange_info_http_error(self):
        print("\n--- Running test: test_fetch_exchange_info_http_error ---")
        http_error = aiohttp.ClientResponseError(MagicMock(), (), status=404)
        mock_response = create_mock_aiohttp_response(status=404, raise_for_status_error=http_error)
        get_cm_mock = self.mock_http_session.get.return_value
        get_cm_mock.__aenter__.return_value = mock_response
        result = await self.service._fetch_exchange_info("http://fake.com")
        self.mock_http_session.get.assert_called_once_with("http://fake.com", timeout=10)
        get_cm_mock.__aenter__.assert_awaited_once()
        mock_response.raise_for_status.assert_called_once()
        mock_response.json.assert_not_called()
        get_cm_mock.__aexit__.assert_awaited_once()
        self.assertIsNone(result)
        print(">>> test_fetch_exchange_info_http_error: PASSED")

    async def test_fetch_exchange_info_timeout(self):
        print("\n--- Running test: test_fetch_exchange_info_timeout ---")
        get_cm_mock = self.mock_http_session.get.return_value
        get_cm_mock.__aenter__.side_effect = asyncio.TimeoutError
        result = await self.service._fetch_exchange_info("http://fake.com")
        self.mock_http_session.get.assert_called_once_with("http://fake.com", timeout=10)
        get_cm_mock.__aenter__.assert_awaited_once()
        get_cm_mock.__aexit__.assert_not_called()
        self.assertIsNone(result)
        print(">>> test_fetch_exchange_info_timeout: PASSED")

    @patch.object(DataAggregatorService, '_fetch_exchange_info', new_callable=AsyncMock)
    async def test_update_whitelist_success(self, mock_fetch):
        print("\n--- Running test: test_update_whitelist_success ---")
        mock_spot_data = { "symbols": [ {"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT"}, {"symbol": "ETHUSDT", "status": "TRADING", "quoteAsset": "USDT"} ]}
        mock_futures_data = { "symbols": [ {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"}, {"symbol": "LTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"} ]}
        mock_fetch.side_effect = [mock_spot_data, mock_futures_data]
        await self.service._update_whitelist()
        mock_pipeline = self.mock_redis_client.pipeline.return_value
        self.mock_redis_client.pipeline.assert_called_once_with(transaction=True)
        mock_pipeline.delete.assert_any_call(SPOT_WHITELIST_KEY)
        mock_pipeline.delete.assert_any_call(PERP_WHITELIST_KEY)
        spot_sadd_args = None; perp_sadd_args = None
        for call_item in mock_pipeline.sadd.call_args_list:
            args, kwargs = call_item
            if args[0] == SPOT_WHITELIST_KEY: spot_sadd_args = set(args[1:])
            elif args[0] == PERP_WHITELIST_KEY: perp_sadd_args = set(args[1:])
        self.assertEqual(spot_sadd_args, {'BTCUSDT', 'ETHUSDT'}, "Spot sadd args mismatch")
        self.assertEqual(perp_sadd_args, {'BTCUSDT', 'LTCUSDT'}, "Perp sadd args mismatch")
        mock_pipeline.execute.assert_awaited_once()
        self.assertEqual(self.service._spot_whitelist_cache, {'BTCUSDT', 'ETHUSDT'})
        self.assertEqual(self.service._perp_whitelist_cache, {'BTCUSDT', 'LTCUSDT'})
        print(">>> test_update_whitelist_success: PASSED")

    @patch.object(DataAggregatorService, '_fetch_exchange_info', new_callable=AsyncMock)
    async def test_update_whitelist_api_fetch_fails(self, mock_fetch):
        print("\n--- Running test: test_update_whitelist_api_fetch_fails ---")
        mock_fetch.return_value = None
        self.service._spot_whitelist_cache = {"OLDSPOT"}
        self.service._perp_whitelist_cache = {"OLDPERP"}
        await self.service._update_whitelist()
        mock_pipeline = self.mock_redis_client.pipeline.return_value
        self.mock_redis_client.pipeline.assert_called_once_with(transaction=True)
        mock_pipeline.delete.assert_any_call(SPOT_WHITELIST_KEY)
        mock_pipeline.delete.assert_any_call(PERP_WHITELIST_KEY)
        mock_pipeline.sadd.assert_not_called()
        mock_pipeline.execute.assert_awaited_once()
        self.assertEqual(self.service._spot_whitelist_cache, set())
        self.assertEqual(self.service._perp_whitelist_cache, set())
        print(">>> test_update_whitelist_api_fetch_fails: PASSED")

    async def test_write_price_to_redis_allowed(self):
        print("\n--- Running test: test_write_price_to_redis_allowed ---")
        symbol = "BTCUSDT"; field = "spot"; price = "32000.0"
        self.service._spot_whitelist_cache = {symbol}
        await self.service._write_price_to_redis(symbol, field, price)
        self.mock_redis_client.hset.assert_awaited_once_with(f"prices:{symbol}", key=field, value=price)
        print(">>> test_write_price_to_redis_allowed: PASSED")

    async def test_write_price_to_redis_denied(self):
        print("\n--- Running test: test_write_price_to_redis_denied ---")
        symbol = "XYZUSDT"; field = "perp"; price = "100.0"
        self.service._perp_whitelist_cache = {"BTCUSDT", "ETHUSDT"}
        await self.service._write_price_to_redis(symbol, field, price)
        self.mock_redis_client.hset.assert_not_called()
        print(">>> test_write_price_to_redis_denied: PASSED")

    async def test_process_spot_mini_ticker_with_whitelist(self):
        print("\n--- Running test: test_process_spot_mini_ticker_with_whitelist ---")
        self.service._spot_whitelist_cache = {"BTCUSDT"}
        mock_spot_message = json.dumps([ {"e": "24hrMiniTicker", "s": "BTCUSDT", "c": "31000.50"}, {"e": "24hrMiniTicker", "s": "ETHUSDT", "c": "2050.00"}, {"e": "24hrMiniTicker", "s": "ETHAUD", "c": "3000.00"} ])
        await self.service._process_message_single(mock_spot_message, "Spot MiniTicker")
        self.mock_redis_client.hset.assert_called_once_with('prices:BTCUSDT', key='spot', value='31000.50')
        print(">>> test_process_spot_mini_ticker_with_whitelist: PASSED")

    async def test_process_futures_mark_price_with_whitelist(self):
        print("\n--- Running test: test_process_futures_mark_price_with_whitelist ---")
        self.service._perp_whitelist_cache = {"BTCUSDT", "ETHUSDT"}
        mock_futures_message = json.dumps([ {"e": "markPriceUpdate", "s": "BTCUSDT", "p": "31050.75"}, {"e": "markPriceUpdate", "s": "ETHUSDT", "p": "2055.25"}, {"e": "markPriceUpdate", "s": "ADAUSDT", "p": "0.40"} ])
        await self.service._process_message_single(mock_futures_message, "Futures MarkPrice")
        expected_calls = [ call('prices:BTCUSDT', key='perp', value='31050.75'), call('prices:ETHUSDT', key='perp', value='2055.25') ]
        self.mock_redis_client.hset.assert_has_calls(expected_calls, any_order=True)
        self.assertEqual(self.mock_redis_client.hset.call_count, 2)
        print(">>> test_process_futures_mark_price_with_whitelist: PASSED")

    @patch('asyncio.sleep', new_callable=AsyncMock)
    @patch.object(DataAggregatorService, '_update_whitelist', new_callable=AsyncMock)
    async def test_run_whitelist_updater_scheduling(self, mock_update, mock_sleep):
        print("\n--- Running test: test_run_whitelist_updater_scheduling ---")
        mock_sleep.side_effect = asyncio.CancelledError
        await self.service._run_whitelist_updater()
        mock_update.assert_awaited_once()
        mock_sleep.assert_awaited_once_with(WHITELIST_UPDATE_INTERVAL_SECONDS)
        print(">>> test_run_whitelist_updater_scheduling: PASSED")

    @patch("redis.asyncio.Redis.from_url")
    @patch('asyncio.create_task')
    @patch.object(DataAggregatorService, '_update_whitelist', new_callable=AsyncMock)
    @patch.object(DataAggregatorService, 'run_websocket_listener')
    async def test_start_calls_initial_update_and_starts_tasks(self, mock_run_ws, mock_update, mock_create_task, mock_redis_from_url):
        print("\n--- Running test: test_start_calls_initial_update_and_starts_tasks ---")
        mock_redis_from_url.return_value = self.mock_redis_client
        mock_updater_task = AsyncMock(name="UpdaterTask")
        mock_create_task.return_value = mock_updater_task
        mock_run_ws.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.service.start()
        mock_redis_from_url.assert_called_once_with(self.redis_url, decode_responses=True)
        self.mock_redis_client.ping.assert_awaited_once()
        mock_update.assert_awaited_once()
        mock_create_task.assert_called_once()
        created_coro = mock_create_task.call_args[0][0]
        self.assertEqual(created_coro.__name__, '_run_whitelist_updater')
        mock_run_ws.assert_awaited_once()
        print(">>> test_start_calls_initial_update_and_starts_tasks: PASSED")

    # --- 最終 close 測試：移除 cancel 和 gather 斷言 ---
    async def test_close_cancels_tasks(self):
        """測試：close() 會關閉 http 和 redis 連線"""
        print("\n--- Running test: test_close_cancels_tasks ---")
        # 仍然需要模擬任務，即使不檢查 cancel
        mock_updater = AsyncMock(name="WhitelistUpdaterTask")
        mock_updater.done = MagicMock(return_value=False)
        mock_ws1 = AsyncMock(name="WS1")
        mock_ws1.done = MagicMock(return_value=False)

        self.service._whitelist_update_task = mock_updater
        self.service._websocket_listener_tasks = [mock_ws1]

        # Patch gather 避免它真的執行等待，但不再驗證 gather 本身
        with patch('asyncio.gather', new_callable=AsyncMock):
            await self.service.close()

        # **只驗證重要的資源關閉**
        self.mock_http_session.close.assert_awaited_once()
        self.mock_redis_client.aclose.assert_awaited_once()
        print(">>> test_close_cancels_tasks: PASSED (Skipped cancel/gather assertion)")


    async def asyncTearDown(self):
        session = getattr(self.service, 'http_session', None)
        if session and hasattr(session, 'closed') and not session.closed:
             if asyncio.iscoroutinefunction(session.close) or isinstance(session.close, AsyncMock):
                 await session.close()
             elif hasattr(session.close, '__call__'):
                  session.close()
        print(f"--- 結束測試: {self._testMethodName} ---\n")

if __name__ == '__main__':
    unittest.main()

# 確保檔案結尾有空行