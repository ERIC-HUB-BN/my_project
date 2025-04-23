# tests/test_orderbook_service.py
import unittest
from unittest.mock import patch, AsyncMock, MagicMock, call, ANY
import asyncio
import sys
import os
import json
from decimal import Decimal, InvalidOperation
import logging
import time

# --- 設定主程式碼的路徑 ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
my_project_path = os.path.join(project_root, 'my_project')
if my_project_path not in sys.path:
    sys.path.insert(0, my_project_path)
# --- 路徑設定結束 ---

import aio_pika
import websockets
from orderbook_service import ( # noqa: E402
    OrderBookService,
    RABBITMQ_URL,
    OPPORTUNITY_QUEUE_NAME,
    ORDER_REQUEST_QUEUE_NAME,
    BINANCE_SPOT_WS_BASE,
    BINANCE_FUTURES_WS_BASE,
    DEPTH_STREAM_PARAM,
    ARBITRAGE_THRESHOLD,
    TRADE_AMOUNT_USDT # <<< --- 匯入交易金額常數 ---
)

# --- 測試用的假 RabbitMQ 物件 (保持不變) ---
def create_mock_rabbitmq_channel():
    mock_channel = AsyncMock(); mock_channel.set_qos = AsyncMock(); mock_channel.declare_queue = AsyncMock()
    mock_queue = AsyncMock(); mock_queue.consume = AsyncMock(); mock_channel.declare_queue.return_value = mock_queue
    mock_exchange = AsyncMock(); mock_exchange.publish = AsyncMock(); mock_channel.default_exchange = mock_exchange
    mock_channel.close = AsyncMock(); mock_channel.is_closed = False
    return mock_channel, mock_queue, mock_exchange

def create_mock_rabbitmq_connection(mock_channel):
    mock_connection = AsyncMock(); mock_connection.channel = AsyncMock(return_value=mock_channel); mock_connection.close = AsyncMock()
    return mock_connection

def create_mock_incoming_message(body_data: dict) -> MagicMock:
    message = MagicMock(spec=aio_pika.abc.AbstractIncomingMessage); message.body = json.dumps(body_data).encode('utf-8')
    async def mock_process(*args, **kwargs): yield
    message.process = MagicMock(return_value=AsyncMock()); message.process.return_value.__aenter__ = AsyncMock(side_effect=mock_process); message.process.return_value.__aexit__ = AsyncMock(return_value=None)
    return message

# --- 測試用的假 WebSocket 連線 (保持不變) ---
def create_mock_websocket(messages_to_send=None):
    mock_ws = AsyncMock(); _messages = list(messages_to_send or [])
    async def mock_recv_logic():
        if _messages: return _messages.pop(0)
        else: raise asyncio.CancelledError("Mock WS finished sending messages")
    mock_ws.recv = AsyncMock(side_effect=mock_recv_logic); mock_ws.close = AsyncMock()
    mock_ws.__aenter__ = AsyncMock(return_value=mock_ws); mock_ws.__aexit__ = AsyncMock(return_value=None)
    return mock_ws

# --- 測試類別 ---
class TestOrderBookService(unittest.IsolatedAsyncioTestCase):
    """
    對 OrderBookService 進行單元測試 (包含條件和深度檢查)
    """
    def setUp(self):
        self.service = OrderBookService(rabbitmq_url="amqp://fake_url/")
        logging.getLogger("OrderBookService").setLevel(logging.DEBUG)
        self.patch_manage_symbol_websockets = patch.object(OrderBookService, '_manage_symbol_websockets', new_callable=AsyncMock); self.mock_manage_symbol_websockets = self.patch_manage_symbol_websockets.start()
        self.patch_create_task = patch('asyncio.create_task'); self.mock_create_task = self.patch_create_task.start()
        self.patch_time = patch('time.time', return_value=1678889999.0); self.mock_time = self.patch_time.start()

    def tearDown(self):
        self.patch_manage_symbol_websockets.stop(); self.patch_create_task.stop(); self.patch_time.stop()
        logging.getLogger("OrderBookService").setLevel(logging.INFO)

    @patch("aio_pika.connect_robust")
    async def test_connect_rabbitmq_success(self, mock_connect_robust):
        # (保持不變)
        print("\n--- 測試：成功連接 RabbitMQ 並宣告佇列 ---")
        mock_channel, _, _ = create_mock_rabbitmq_channel(); mock_connection = create_mock_rabbitmq_connection(mock_channel); mock_connect_robust.return_value = mock_connection
        result = await self.service._connect_rabbitmq(); self.assertTrue(result)
        expected_calls = [call(OPPORTUNITY_QUEUE_NAME, durable=True), call(ORDER_REQUEST_QUEUE_NAME, durable=True)]
        mock_channel.declare_queue.assert_has_calls(expected_calls, any_order=True); self.assertEqual(mock_channel.declare_queue.call_count, 2)
        print(">>> test_connect_rabbitmq_success: 通過")

    @patch("aio_pika.connect_robust")
    async def test_connect_rabbitmq_failure(self, mock_connect_robust):
        # (保持不變)
        print("\n--- 測試：連接 RabbitMQ 失敗 ---")
        mock_connect_robust.side_effect = aio_pika.exceptions.AMQPConnectionError("測試連線錯誤"); result = await self.service._connect_rabbitmq(); self.assertFalse(result)
        print(">>> test_connect_rabbitmq_failure: 通過")

    async def test_on_message_starts_new_symbol_task(self):
        # (保持不變)
        print("\n--- 測試：收到新 symbol 啟動任務 ---")
        symbol = "BTCUSDT"; test_data = {"symbol": symbol, "price_difference_percent": "0.45%"}; mock_message = create_mock_incoming_message(test_data)
        mock_task = AsyncMock(); self.mock_create_task.return_value = mock_task; await self.service._on_message(mock_message)
        expected_spot_url = f"{BINANCE_SPOT_WS_BASE}/{symbol.lower()}{DEPTH_STREAM_PARAM}"; expected_perp_url = f"{BINANCE_FUTURES_WS_BASE}/{symbol.lower()}{DEPTH_STREAM_PARAM}"
        self.mock_manage_symbol_websockets.assert_called_once_with(symbol, expected_spot_url, expected_perp_url); self.mock_create_task.assert_called_once()
        self.assertIn(symbol, self.service.active_symbol_tasks); self.assertEqual(self.service.active_symbol_tasks[symbol], mock_task)
        self.assertIn(symbol, self.service.order_books); self.assertEqual(self.service.order_books[symbol]['spot']['bid'], [None, None]); mock_task.add_done_callback.assert_called_once()
        print(">>> test_on_message_starts_new_symbol_task: 通過")

    async def test_on_message_ignores_existing_symbol(self):
         # (保持不變)
        print("\n--- 測試：忽略已存在的 symbol ---")
        symbol = "ETHUSDT"; self.service.active_symbol_tasks[symbol] = AsyncMock(); test_data = {"symbol": symbol, "price_difference_percent": "0.50%"}; mock_message = create_mock_incoming_message(test_data)
        await self.service._on_message(mock_message); self.mock_manage_symbol_websockets.assert_not_called(); self.mock_create_task.assert_not_called()
        print(">>> test_on_message_ignores_existing_symbol: 通過")

    async def test_on_message_invalid_json(self):
         # (保持不變)
        print("\n--- 測試：處理無效 JSON 訊息 ---")
        mock_message = MagicMock(spec=aio_pika.abc.AbstractIncomingMessage); mock_message.body = b"this is not json"
        async def mock_process(*args, **kwargs): yield
        mock_message.process = MagicMock(return_value=AsyncMock()); mock_message.process.return_value.__aenter__ = AsyncMock(side_effect=mock_process); mock_message.process.return_value.__aexit__ = AsyncMock(return_value=None)
        with self.assertLogs(logger="OrderBookService", level="ERROR"): await self.service._on_message(mock_message)
        print(">>> test_on_message_invalid_json: 通過")

    @patch('websockets.connect')
    @patch.object(OrderBookService, '_check_arbitrage_condition', new_callable=AsyncMock)
    async def test_listen_depth_stream_parses_and_triggers_check(self, mock_check_condition, mock_connect):
        # (保持不變)
        print("\n--- 測試：解析深度資料並觸發條件檢查 ---")
        symbol = "BTCUSDT"; stream_type = "spot"; mock_depth_message = json.dumps({"lastUpdateId": 1001, "bids": [["30000.00", "1.5"]], "asks": [["30001.00", "0.5"]]})
        mock_ws = create_mock_websocket(messages_to_send=[mock_depth_message]); mock_connect.return_value = mock_ws; self.service.order_books[symbol] = {"spot": {}, "perp": {}}
        try: await self.service._listen_depth_stream(symbol, "ws://fake", stream_type)
        except asyncio.CancelledError: pass
        self.assertEqual(self.service.order_books[symbol][stream_type]['bid'], [Decimal("30000.00"), Decimal("1.5")]); self.assertEqual(self.service.order_books[symbol][stream_type]['ask'], [Decimal("30001.00"), Decimal("0.5")])
        mock_check_condition.assert_awaited_once_with(symbol)
        print(">>> test_listen_depth_stream_parses_and_triggers_check: 通過")

    # --- 修改/新增：測試條件判斷邏輯 (包含深度) ---
    @patch.object(OrderBookService, '_publish_trade_signal', new_callable=AsyncMock)
    async def test_check_condition_price_met_depth_met(self, mock_publish):
        """測試：價格和深度條件都滿足"""
        print("\n--- 測試：條件滿足 (價格 + 深度) ---")
        symbol = "TESTUSDT"
        spot_bid = Decimal("100.00")
        perp_ask = Decimal("100.50") # 價差 0.5% > 0.35%
        # 深度計算: Spot Bid Value = 100 * 1.1 = 110 >= 100; Perp Ask Value = 100.50 * 1 = 100.5 >= 100
        spot_qty = Decimal("1.1")
        perp_qty = Decimal("1.0")
        self.service.order_books[symbol] = {
            "spot": {"bid": [spot_bid, spot_qty], "ask": [Decimal("100.10"), Decimal("1")]},
            "perp": {"bid": [Decimal("100.40"), Decimal("2")], "ask": [perp_ask, perp_qty]}
        }
        await self.service._check_arbitrage_condition(symbol)
        # 期望 publish 被呼叫
        mock_publish.assert_awaited_once_with(symbol, spot_bid, spot_qty, perp_ask, perp_qty)
        print(">>> test_check_condition_price_met_depth_met: 通過")

    @patch.object(OrderBookService, '_publish_trade_signal', new_callable=AsyncMock)
    async def test_check_condition_price_met_spot_depth_fail(self, mock_publish):
        """測試：價格滿足，但 Spot 深度不足"""
        print("\n--- 測試：條件不滿足 (Spot 深度不足) ---")
        symbol = "TESTUSDT"
        spot_bid = Decimal("100.00")
        perp_ask = Decimal("100.50") # 價差 0.5% > 0.35%
        # 深度計算: Spot Bid Value = 100 * 0.9 = 90 < 100
        spot_qty = Decimal("0.9")
        perp_qty = Decimal("1.0") # Perp 深度足夠
        self.service.order_books[symbol] = {
            "spot": {"bid": [spot_bid, spot_qty], "ask": [Decimal("100.10"), Decimal("1")]},
            "perp": {"bid": [Decimal("100.40"), Decimal("2")], "ask": [perp_ask, perp_qty]}
        }
        await self.service._check_arbitrage_condition(symbol)
        # 期望 publish *不* 被呼叫
        mock_publish.assert_not_awaited()
        print(">>> test_check_condition_price_met_spot_depth_fail: 通過")

    @patch.object(OrderBookService, '_publish_trade_signal', new_callable=AsyncMock)
    async def test_check_condition_price_met_perp_depth_fail(self, mock_publish):
        """測試：價格滿足，但 Perp 深度不足"""
        print("\n--- 測試：條件不滿足 (Perp 深度不足) ---")
        symbol = "TESTUSDT"
        spot_bid = Decimal("100.00")
        perp_ask = Decimal("100.50") # 價差 0.5% > 0.35%
        spot_qty = Decimal("1.1") # Spot 深度足夠
        # 深度計算: Perp Ask Value = 100.50 * 0.9 = 90.45 < 100
        perp_qty = Decimal("0.9")
        self.service.order_books[symbol] = {
            "spot": {"bid": [spot_bid, spot_qty], "ask": [Decimal("100.10"), Decimal("1")]},
            "perp": {"bid": [Decimal("100.40"), Decimal("2")], "ask": [perp_ask, perp_qty]}
        }
        await self.service._check_arbitrage_condition(symbol)
        # 期望 publish *不* 被呼叫
        mock_publish.assert_not_awaited()
        print(">>> test_check_condition_price_met_perp_depth_fail: 通過")

    @patch.object(OrderBookService, '_publish_trade_signal', new_callable=AsyncMock)
    async def test_check_condition_price_not_met(self, mock_publish):
        """測試：價格條件不滿足"""
        print("\n--- 測試：條件不滿足 (價格) ---")
        symbol = "TESTUSDT"
        spot_bid = Decimal("100.00")
        perp_ask = Decimal("100.10") # 價差 0.1% < 0.35%
        # 深度足夠，但不重要
        spot_qty = Decimal("1.1")
        perp_qty = Decimal("1.0")
        self.service.order_books[symbol] = {
            "spot": {"bid": [spot_bid, spot_qty], "ask": [Decimal("100.05"), Decimal("1")]},
            "perp": {"bid": [Decimal("100.08"), Decimal("2")], "ask": [perp_ask, perp_qty]}
        }
        await self.service._check_arbitrage_condition(symbol)
        # 期望 publish *不* 被呼叫
        mock_publish.assert_not_awaited()
        print(">>> test_check_condition_price_not_met: 通過")

    @patch.object(OrderBookService, '_publish_trade_signal', new_callable=AsyncMock)
    async def test_check_condition_data_incomplete(self, mock_publish):
        """測試：數據不完整"""
        print("\n--- 測試：數據不完整，不觸發 ---")
        symbol = "TESTUSDT"; self.service.order_books[symbol] = {"spot": {"bid": [Decimal("100.00"), Decimal("10")]}, "perp": {}} # Perp 數據不完整
        await self.service._check_arbitrage_condition(symbol); mock_publish.assert_not_awaited()
        print(">>> test_check_condition_data_incomplete: 通過")

    # --- 測試發送交易信號 (保持不變) ---
    async def test_publish_trade_signal(self):
        print("\n--- 測試：發送交易信號 ---")
        symbol = "LINKUSDT"; spot_bid = Decimal("15.00"); spot_qty = Decimal("100"); perp_ask = Decimal("15.10"); perp_qty = Decimal("50")
        mock_channel, _, mock_exchange = create_mock_rabbitmq_channel(); mock_channel.is_closed = False; self.service.rabbitmq_channel = mock_channel
        mock_task = AsyncMock(spec=asyncio.Task); self.service.active_symbol_tasks[symbol] = mock_task
        await self.service._publish_trade_signal(symbol, spot_bid, spot_qty, perp_ask, perp_qty)
        mock_channel.default_exchange.publish.assert_awaited_once()
        args, kwargs = mock_channel.default_exchange.publish.await_args; sent_message = args[0]; routing_key = kwargs.get('routing_key')
        self.assertEqual(routing_key, ORDER_REQUEST_QUEUE_NAME); self.assertIsInstance(sent_message, aio_pika.Message)
        sent_data = json.loads(sent_message.body.decode()); self.assertEqual(sent_data['symbol'], symbol); self.assertEqual(sent_data['action'], "OPEN")
        self.assertEqual(sent_data['spot_target']['price'], str(spot_bid)); self.assertEqual(sent_data['perp_target']['price'], str(perp_ask))
        self.assertAlmostEqual(sent_data['timestamp'], self.mock_time.return_value, places=2); mock_task.cancel.assert_called_once()
        print(">>> test_publish_trade_signal: 通過")

    # --- 其他輔助測試 (保持不變) ---
    def test_handle_task_completion_removes_symbol(self):
        print("\n--- 測試：任務完成回調移除 symbol ---")
        symbol = "ETHUSDT"; self.service.active_symbol_tasks[symbol] = AsyncMock(spec=asyncio.Task); self.service.order_books[symbol] = {"spot": {}, "perp": {}}
        mock_task = AsyncMock(spec=asyncio.Task); mock_task.exception.return_value = None; callback = self.service._handle_task_completion(symbol); callback(mock_task)
        self.assertNotIn(symbol, self.service.active_symbol_tasks); self.assertNotIn(symbol, self.service.order_books)
        print(">>> test_handle_task_completion_removes_symbol: 通過")

    def test_handle_task_completion_logs_exception(self):
        print("\n--- 測試：任務完成回調記錄異常 ---")
        symbol = "ADAUSDT"; self.service.active_symbol_tasks[symbol] = AsyncMock(spec=asyncio.Task); self.service.order_books[symbol] = {"spot": {}, "perp": {}}
        mock_task = AsyncMock(spec=asyncio.Task); test_exception = ValueError("測試任務異常"); mock_task.exception.return_value = test_exception
        callback = self.service._handle_task_completion(symbol);
        with self.assertLogs(logger="OrderBookService", level="ERROR"): callback(mock_task)
        self.assertNotIn(symbol, self.service.active_symbol_tasks); self.assertNotIn(symbol, self.service.order_books)
        print(">>> test_handle_task_completion_logs_exception: 通過")

    async def test_close_cancels_active_tasks(self):
        print("\n--- 測試：關閉服務取消任務 ---")
        symbol1 = "BTCUSDT"; symbol2 = "ETHUSDT"; mock_task1 = AsyncMock(spec=asyncio.Task); mock_task2 = AsyncMock(spec=asyncio.Task)
        self.service.active_symbol_tasks = {symbol1: mock_task1, symbol2: mock_task2}; self.service.rabbitmq_connection = AsyncMock(); self.service.rabbitmq_channel = AsyncMock()
        with patch('asyncio.gather', new_callable=AsyncMock) as mock_gather: await self.service.close()
        mock_task1.cancel.assert_called_once(); mock_task2.cancel.assert_called_once(); mock_gather.assert_awaited_once_with(mock_task1, mock_task2, return_exceptions=True)
        print(">>> test_close_cancels_active_tasks: 通過")

    async def asyncTearDown(self): pass

if __name__ == '__main__': unittest.main()