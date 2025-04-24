# tests/test_calculation_service.py
import unittest
from unittest.mock import patch, AsyncMock, MagicMock, call
import asyncio
import sys
import os
import json
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any, List, Tuple
import time
import logging

# --- 設定主程式碼的路徑 (保持不變) ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
my_project_path = os.path.join(project_root, 'my_project')
if my_project_path not in sys.path:
    sys.path.insert(0, my_project_path)

import redis # 用於 exceptions
import aiohttp
import aio_pika
from calculation_service import (
    CalculationService,
    MIN_PRICE_DIFF_THRESHOLD,
    BINANCE_FUNDING_RATE_URL,
    OPPORTUNITY_QUEUE_NAME,
    BLACKLIST_KEY
) # noqa: E402

# --- 測試用的假 Redis 客戶端 (保持不變) ---
def create_mock_redis_client(blacklist_members: Optional[set] = None):
    mock_client = AsyncMock()
    blacklist = blacklist_members if blacklist_members is not None else set()
    mock_prices = {
        "prices:BTCUSDT": {"spot": "30000.0", "perp": "30150.0"}, # 0.5%
        "prices:ETHUSDT": {"spot": "2000.0", "perp": "2100.0"},  # 5.0%
        "prices:ADAUSDT": {"spot": "1.0", "perp": "1.01"},       # 1.0%
        "prices:SKIPUSDT": {"spot": "100.0", "perp": "106.0"},   # 6.0% <--- 最高價差
        "prices:NOSPP": {"perp": "100"},
        "prices:NOPERP": {"spot": "100"},
        "prices:ZERO": {"spot": "0", "perp": "1"}
    }
    async def mock_scan_iter(*args, **kwargs):
        for key in mock_prices.keys(): yield key
    mock_client.scan_iter = mock_scan_iter
    async def mock_hgetall(key, *args, **kwargs): return mock_prices.get(key, {})
    mock_client.hgetall = AsyncMock(side_effect=mock_hgetall)
    async def mock_sismember(key, member, *args, **kwargs): return key == BLACKLIST_KEY and member in blacklist
    mock_client.sismember = AsyncMock(side_effect=mock_sismember)
    mock_client.ping = AsyncMock(return_value=True)
    mock_client.aclose = AsyncMock()
    return mock_client

# --- 測試用的假 RabbitMQ 物件 (保持不變) ---
def create_mock_rabbitmq_channel():
    mock_channel = AsyncMock(spec=aio_pika.abc.AbstractChannel)
    mock_channel.declare_queue = AsyncMock()
    mock_exchange = AsyncMock(spec=aio_pika.abc.AbstractExchange)
    mock_exchange.publish = AsyncMock()
    mock_channel.default_exchange = mock_exchange
    mock_channel.close = AsyncMock()
    mock_channel.is_closed = False
    return mock_channel

def create_mock_rabbitmq_connection(mock_channel):
    mock_connection = AsyncMock(spec=aio_pika.abc.AbstractConnection)
    mock_connection.channel = AsyncMock(return_value=mock_channel)
    mock_connection.close = AsyncMock()
    mock_connection.is_closed = False
    return mock_connection

# --- 測試用的假 HTTP Session (保持不變) ---
def create_mock_http_session(symbol_funding_rates: Dict[str, Optional[str]]):
    mock_session = MagicMock(spec=aiohttp.ClientSession)
    def configure_response_mock(symbol):
        response_mock = AsyncMock(spec=aiohttp.ClientResponse)
        rate_str = symbol_funding_rates.get(symbol)
        if rate_str is None:
            response_mock.status = 404
            response_mock.raise_for_status = MagicMock(side_effect=aiohttp.ClientResponseError(MagicMock(), (), status=404, message="Not Found"))
            response_mock.json.side_effect = RuntimeError("Should not call json on error response")
        elif rate_str == "invalid_format":
             response_mock.status = 200
             response_mock.raise_for_status = MagicMock()
             response_mock.json.return_value = {"wrong_key": "some_value"}
        elif rate_str == "invalid_decimal":
             response_mock.status = 200
             response_mock.raise_for_status = MagicMock()
             response_mock.json.return_value = {"lastFundingRate": "not_a_decimal"}
        else:
            response_mock.status = 200
            response_mock.raise_for_status = MagicMock()
            response_mock.json.return_value = {"lastFundingRate": rate_str}
        return response_mock
    def get_side_effect(url, params=None, **kwargs):
        mgr_mock = MagicMock()
        if url == BINANCE_FUNDING_RATE_URL and params and 'symbol' in params:
            symbol = params['symbol']
            response_to_yield = configure_response_mock(symbol)
        else:
            response_to_yield = configure_response_mock("DEFAULT_ERROR_CASE")
        mgr_mock.__aenter__ = AsyncMock(return_value=response_to_yield)
        mgr_mock.__aexit__ = AsyncMock(return_value=None)
        return mgr_mock
    mock_session.get = MagicMock(side_effect=get_side_effect)
    mock_session.close = AsyncMock()
    mock_session.closed = False
    return mock_session


# --- 測試類別 ---
class TestCalculationService(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.redis_url = "redis://mock-redis:6379"
        self.rabbitmq_url = "amqp://mock-rabbitmq/"
        self.interval = 10
        self.service = CalculationService(
            redis_url=self.redis_url,
            rabbitmq_url=self.rabbitmq_url,
            interval=self.interval
        )
        self.mock_rabbitmq_channel = create_mock_rabbitmq_channel()
        self.service.rabbitmq_channel = self.mock_rabbitmq_channel
        logging.getLogger("CalculationService").setLevel(logging.WARNING)

    # ... (test_connect_*, test_get_all_prices, test_calculate_opportunities, test_is_symbol_blacklisted_*, test_get_funding_rate_*, test_publish_opportunity 保持不變) ...
    @patch("redis.asyncio.Redis.from_url")
    async def test_connect_redis_success(self, mock_redis_from_url):
        mock_redis = create_mock_redis_client(); mock_redis_from_url.return_value = mock_redis
        self.assertTrue(await self.service._connect_redis())

    @patch("redis.asyncio.Redis.from_url")
    async def test_connect_redis_failure(self, mock_redis_from_url):
        mock_redis_from_url.side_effect = redis.exceptions.ConnectionError("Test connection error")
        self.assertFalse(await self.service._connect_redis())

    @patch("aio_pika.connect_robust")
    async def test_connect_rabbitmq_success(self, mock_connect_robust):
        mock_channel = create_mock_rabbitmq_channel(); mock_connection = create_mock_rabbitmq_connection(mock_channel); mock_connect_robust.return_value = mock_connection
        result = await self.service._connect_rabbitmq()
        self.assertTrue(result); mock_channel.declare_queue.assert_awaited_once_with(OPPORTUNITY_QUEUE_NAME, durable=True)

    @patch("aio_pika.connect_robust")
    async def test_connect_rabbitmq_failure(self, mock_connect_robust):
        mock_connect_robust.side_effect = aio_pika.exceptions.AMQPConnectionError("Test connection error")
        self.assertFalse(await self.service._connect_rabbitmq())

    async def test_get_all_prices_from_redis(self):
        mock_redis = create_mock_redis_client(); self.service.redis_client = mock_redis
        prices = await self.service._get_all_prices_from_redis(); self.assertEqual(len(prices), 4)

    def test_calculate_opportunities(self):
        mock_prices = { "BTCUSDT": {"spot": Decimal("30000"), "perp": Decimal("30150")}, "ETHUSDT": {"spot": Decimal("2000"), "perp": Decimal("2100")}, "ADAUSDT": {"spot": Decimal("1.0"), "perp": Decimal("1.01")}, "LINKUSDT": {"spot": Decimal("10"), "perp": Decimal("10.01")}, }
        opportunities = self.service._calculate_opportunities(mock_prices)
        self.assertEqual(len(opportunities), 3); self.assertEqual(opportunities[0][0], "ETHUSDT") # ETH 價差還是最大 (5%)

    async def test_is_symbol_blacklisted_true(self):
        symbol = "SKIPUSDT"; mock_redis = create_mock_redis_client(blacklist_members={symbol, "OTHER"}); self.service.redis_client = mock_redis
        result = await self.service._is_symbol_blacklisted(symbol); self.assertTrue(result); mock_redis.sismember.assert_awaited_once_with(BLACKLIST_KEY, symbol)

    async def test_is_symbol_blacklisted_false(self):
        symbol = "GOODUSDT"; mock_redis = create_mock_redis_client(blacklist_members={"OTHER"}); self.service.redis_client = mock_redis
        result = await self.service._is_symbol_blacklisted(symbol); self.assertFalse(result); mock_redis.sismember.assert_awaited_once_with(BLACKLIST_KEY, symbol)

    async def test_is_symbol_blacklisted_redis_error(self):
        symbol = "ANYUSDT"; mock_redis = create_mock_redis_client(); mock_redis.sismember.side_effect = redis.exceptions.RedisError("Simulated error"); self.service.redis_client = mock_redis
        result = await self.service._is_symbol_blacklisted(symbol); self.assertFalse(result); mock_redis.sismember.assert_awaited_once_with(BLACKLIST_KEY, symbol)

    async def test_get_funding_rate_success(self):
        symbol = "BTCUSDT"; expected_rate = "0.0001"; mock_http = create_mock_http_session({symbol: expected_rate}); self.service.http_session = mock_http
        funding_rate = await self.service._get_funding_rate(symbol); self.assertEqual(funding_rate, Decimal(expected_rate))
        mock_http.get.assert_called_once_with(BINANCE_FUNDING_RATE_URL, params={'symbol': symbol}, timeout=5)

    async def test_get_funding_rate_api_error(self):
        symbol = "ETHUSDT"; mock_http = create_mock_http_session({symbol: None}); self.service.http_session = mock_http
        funding_rate = await self.service._get_funding_rate(symbol); self.assertIsNone(funding_rate)
        mock_http.get.assert_called_once_with(BINANCE_FUNDING_RATE_URL, params={'symbol': symbol}, timeout=5)

    async def test_get_funding_rate_invalid_response_format(self):
        symbol = "ADAUSDT"; mock_http = create_mock_http_session({symbol: "invalid_format"}); self.service.http_session = mock_http
        funding_rate = await self.service._get_funding_rate(symbol); self.assertIsNone(funding_rate)
        mock_http.get.assert_called_once_with(BINANCE_FUNDING_RATE_URL, params={'symbol': symbol}, timeout=5)

    async def test_get_funding_rate_invalid_decimal_value(self):
        symbol = "LINKUSDT"; mock_http = create_mock_http_session({symbol: "invalid_decimal"}); self.service.http_session = mock_http
        funding_rate = await self.service._get_funding_rate(symbol); self.assertIsNone(funding_rate)
        mock_http.get.assert_called_once_with(BINANCE_FUNDING_RATE_URL, params={'symbol': symbol}, timeout=5)

    async def test_publish_opportunity(self):
        symbol = "BTCUSDT"; diff = Decimal("0.005"); rate = Decimal("0.0001"); mock_channel = create_mock_rabbitmq_channel(); self.service.rabbitmq_channel = mock_channel
        await self.service._publish_opportunity(symbol, diff, rate); mock_channel.default_exchange.publish.assert_awaited_once()
        args, kwargs = mock_channel.default_exchange.publish.await_args; self.assertEqual(kwargs.get('routing_key'), OPPORTUNITY_QUEUE_NAME)


    # --- 修正：調整 _run_calculation_cycle 相關測試的預期 Symbol ---
    @patch.object(CalculationService, '_get_funding_rate', new_callable=AsyncMock)
    @patch.object(CalculationService, '_publish_opportunity', new_callable=AsyncMock)
    async def test_run_cycle_skips_blacklisted_symbol(self, mock_publish, mock_get_funding_rate):
        """測試：當最佳機會在黑名單中時，直接跳過"""
        blacklisted_symbol = "SKIPUSDT" # 這個在 mock_prices 中有最高價差 (6%)
        mock_redis = create_mock_redis_client(blacklist_members={blacklisted_symbol})
        self.service.redis_client = mock_redis

        await self.service._run_calculation_cycle()

        # 驗證：因為 SKIPUSDT 是最高價差且在黑名單，不應查詢資金費率，也不該發布機會
        mock_get_funding_rate.assert_not_awaited()
        mock_publish.assert_not_awaited()

    @patch.object(CalculationService, '_get_funding_rate', new_callable=AsyncMock)
    @patch.object(CalculationService, '_publish_opportunity', new_callable=AsyncMock)
    async def test_run_cycle_processes_non_blacklisted_symbol_positive_rate(self, mock_publish, mock_get_funding_rate):
        """測試：最佳機會不在黑名單且資金費率為正，應發布機會 (修正預期 symbol)"""
        # 根據 mock_prices，SKIPUSDT (6%) 價差最高
        expected_top_symbol = "SKIPUSDT" # <<< 修正
        mock_redis = create_mock_redis_client(blacklist_members=set()) # 黑名單為空
        self.service.redis_client = mock_redis
        # 模擬 SKIPUSDT 的正資金費率
        mock_get_funding_rate.return_value = Decimal("0.0001")

        await self.service._run_calculation_cycle()

        # 驗證：應查詢 SKIPUSDT 的資金費率
        mock_get_funding_rate.assert_awaited_once_with(expected_top_symbol)
        # 驗證 publish 被呼叫，參數基於 SKIPUSDT (價差 6%, 費率 0.01%)
        mock_publish.assert_awaited_once_with(
            expected_top_symbol,
            Decimal("0.06"), # <<< 修正：對應 SKIPUSDT 的價差
            Decimal("0.0001")
        )

    @patch.object(CalculationService, '_get_funding_rate', new_callable=AsyncMock)
    @patch.object(CalculationService, '_publish_opportunity', new_callable=AsyncMock)
    async def test_run_cycle_processes_non_blacklisted_symbol_negative_rate(self, mock_publish, mock_get_funding_rate):
        """測試：最佳機會不在黑名單但資金費率為負，不應發布機會 (修正預期 symbol)"""
        expected_top_symbol = "SKIPUSDT" # <<< 修正
        mock_redis = create_mock_redis_client(blacklist_members=set())
        self.service.redis_client = mock_redis
        # 模擬 SKIPUSDT 的負資金費率
        mock_get_funding_rate.return_value = Decimal("-0.0001")

        await self.service._run_calculation_cycle()

        # 驗證：應查詢 SKIPUSDT 的資金費率
        mock_get_funding_rate.assert_awaited_once_with(expected_top_symbol) # <<< 修正
        # 驗證 publish *不* 被呼叫
        mock_publish.assert_not_awaited()

    @patch.object(CalculationService, '_get_funding_rate', new_callable=AsyncMock)
    @patch.object(CalculationService, '_publish_opportunity', new_callable=AsyncMock)
    async def test_run_cycle_processes_non_blacklisted_symbol_no_rate(self, mock_publish, mock_get_funding_rate):
        """測試：最佳機會不在黑名單但無法獲取資金費率，不應發布機會 (修正預期 symbol)"""
        expected_top_symbol = "SKIPUSDT" # <<< 修正
        mock_redis = create_mock_redis_client(blacklist_members=set())
        self.service.redis_client = mock_redis
        # 模擬 SKIPUSDT 無法獲取資金費率
        mock_get_funding_rate.return_value = None

        await self.service._run_calculation_cycle()

        # 驗證：應嘗試查詢 SKIPUSDT 的資金費率
        mock_get_funding_rate.assert_awaited_once_with(expected_top_symbol) # <<< 修正
        # 驗證 publish *不* 被呼叫
        mock_publish.assert_not_awaited()
    # --- 修正結束 ---

if __name__ == '__main__':
    unittest.main()