# tests/test_orderbook_service.py
import unittest
from unittest.mock import patch, AsyncMock, MagicMock, call, ANY
import asyncio
import sys
import os
import json
from decimal import Decimal
import logging

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
    BINANCE_SPOT_WS_BASE,
    BINANCE_FUTURES_WS_BASE,
    DEPTH_STREAM_PARAM
)

# --- 測試用的假 RabbitMQ 物件 ---
def create_mock_rabbitmq_channel():
    mock_channel = AsyncMock()
    mock_channel.set_qos = AsyncMock()
    mock_channel.declare_queue = AsyncMock()
    mock_queue = AsyncMock()
    mock_queue.consume = AsyncMock()
    mock_channel.declare_queue.return_value = mock_queue
    mock_channel.close = AsyncMock()
    return mock_channel, mock_queue

def create_mock_rabbitmq_connection(mock_channel):
    mock_connection = AsyncMock()
    mock_connection.channel = AsyncMock(return_value=mock_channel)
    mock_connection.close = AsyncMock()
    return mock_connection

def create_mock_incoming_message(body_data: dict) -> MagicMock:
    message = MagicMock(spec=aio_pika.abc.AbstractIncomingMessage)
    message.body = json.dumps(body_data).encode('utf-8')
    async def mock_process(*args, **kwargs):
        yield
    message.process = MagicMock(return_value=AsyncMock())
    message.process.return_value.__aenter__ = AsyncMock(side_effect=mock_process)
    message.process.return_value.__aexit__ = AsyncMock(return_value=None)
    return message

# --- 測試用的假 WebSocket 連線 (再次修改版) ---
def create_mock_websocket(messages_to_send=None):
    """建立一個假的 WebSocket 連線物件"""
    mock_ws = AsyncMock()
    if messages_to_send is None:
        messages_to_send = []
    _messages = list(messages_to_send)

    async def mock_recv_logic(): # 將原始的 recv 邏輯獨立出來
        if _messages:
            return _messages.pop(0)
        else:
            raise asyncio.CancelledError("Mock WS finished sending messages")

    # --- 修改：將 mock_recv_logic 包在 AsyncMock 中 ---
    mock_ws.recv = AsyncMock(side_effect=mock_recv_logic)
    mock_ws.close = AsyncMock()
    mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
    mock_ws.__aexit__ = AsyncMock(return_value=None)
    return mock_ws

# --- 測試類別 ---
class TestOrderBookService(unittest.IsolatedAsyncioTestCase):
    """
    對 OrderBookService 進行單元測試 (包含 WebSocket 部分)
    """
    def setUp(self):
        self.service = OrderBookService(rabbitmq_url="amqp://fake_url/")
        logging.getLogger("OrderBookService").setLevel(logging.DEBUG)
        self.patch_manage_symbol_websockets = patch.object(
            OrderBookService, '_manage_symbol_websockets', new_callable=AsyncMock
        )
        self.mock_manage_symbol_websockets = self.patch_manage_symbol_websockets.start()
        self.patch_create_task = patch('asyncio.create_task')
        self.mock_create_task = self.patch_create_task.start()

    def tearDown(self):
        self.patch_manage_symbol_websockets.stop()
        self.patch_create_task.stop()
        logging.getLogger("OrderBookService").setLevel(logging.INFO)

    @patch("aio_pika.connect_robust")
    async def test_connect_rabbitmq_success(self, mock_connect_robust):
        print("\n--- 測試：成功連接 RabbitMQ ---")
        mock_channel, _ = create_mock_rabbitmq_channel()
        mock_connection = create_mock_rabbitmq_connection(mock_channel)
        mock_connect_robust.return_value = mock_connection
        result = await self.service._connect_rabbitmq()
        self.assertTrue(result)
        print(">>> test_connect_rabbitmq_success: 通過")

    @patch("aio_pika.connect_robust")
    async def test_connect_rabbitmq_failure(self, mock_connect_robust):
        print("\n--- 測試：連接 RabbitMQ 失敗 ---")
        mock_connect_robust.side_effect = aio_pika.exceptions.AMQPConnectionError("測試連線錯誤")
        result = await self.service._connect_rabbitmq()
        self.assertFalse(result)
        print(">>> test_connect_rabbitmq_failure: 通過")

    # --- 修改：test_on_message_starts_new_symbol_task 中的斷言 ---
    async def test_on_message_starts_new_symbol_task(self):
        """測試：收到新 symbol 的訊息時，會啟動 WebSocket 監聽任務"""
        print("\n--- 測試：收到新 symbol 啟動任務 ---")
        symbol = "BTCUSDT"
        test_data = {"symbol": symbol, "price_difference_percent": "0.45%"}
        mock_message = create_mock_incoming_message(test_data)
        mock_task = AsyncMock()
        self.mock_create_task.return_value = mock_task

        await self.service._on_message(mock_message)

        expected_spot_url = f"{BINANCE_SPOT_WS_BASE}/{symbol.lower()}{DEPTH_STREAM_PARAM}"
        expected_perp_url = f"{BINANCE_FUTURES_WS_BASE}/{symbol.lower()}{DEPTH_STREAM_PARAM}"

        # --- 修改：使用 assert_called_once_with 而不是 assert_awaited_once_with ---
        self.mock_manage_symbol_websockets.assert_called_once_with(
            symbol, expected_spot_url, expected_perp_url
        )

        self.mock_create_task.assert_called_once()
        self.assertIn(symbol, self.service.active_symbol_tasks)
        self.assertEqual(self.service.active_symbol_tasks[symbol], mock_task)
        self.assertIn(symbol, self.service.order_books)
        self.assertEqual(self.service.order_books[symbol], {"spot": {}, "perp": {}})
        mock_task.add_done_callback.assert_called_once()
        print(">>> test_on_message_starts_new_symbol_task: 通過")


    async def test_on_message_ignores_existing_symbol(self):
        print("\n--- 測試：忽略已存在的 symbol ---")
        symbol = "ETHUSDT"
        self.service.active_symbol_tasks[symbol] = AsyncMock()
        test_data = {"symbol": symbol, "price_difference_percent": "0.50%"}
        mock_message = create_mock_incoming_message(test_data)
        await self.service._on_message(mock_message)
        # --- 修改：assert_not_awaited -> assert_not_called ---
        # 雖然這裡用 awaited 或 called 結果一樣，但 called 更精確
        self.mock_manage_symbol_websockets.assert_not_called()
        self.mock_create_task.assert_not_called()
        print(">>> test_on_message_ignores_existing_symbol: 通過")

    async def test_on_message_invalid_json(self):
        print("\n--- 測試：處理無效 JSON 訊息 ---")
        mock_message = MagicMock(spec=aio_pika.abc.AbstractIncomingMessage)
        mock_message.body = b"this is not json"
        async def mock_process(*args, **kwargs): yield
        mock_message.process = MagicMock(return_value=AsyncMock())
        mock_message.process.return_value.__aenter__ = AsyncMock(side_effect=mock_process)
        mock_message.process.return_value.__aexit__ = AsyncMock(return_value=None)
        with self.assertLogs(logger="OrderBookService", level="ERROR"):
             await self.service._on_message(mock_message)
        print(">>> test_on_message_invalid_json: 通過")

    # --- 修改：test_listen_depth_stream_parsing 中的斷言 ---
    @patch('websockets.connect')
    async def test_listen_depth_stream_parsing(self, mock_connect):
        """測試：能否正確解析 WebSocket 收到的深度資料 (修改版)"""
        print("\n--- 測試：解析 WebSocket 深度資料 ---")
        symbol = "BTCUSDT"
        stream_type = "spot"
        mock_depth_message_1 = json.dumps({
            "lastUpdateId": 1001,
            "bids": [["30000.00", "1.5"], ["29999.00", "2.0"]],
            "asks": [["30001.00", "0.5"], ["30002.00", "1.0"]]
        })
        mock_depth_message_2 = json.dumps({
            "lastUpdateId": 1002,
            "bids": [["30005.00", "0.8"], ["29999.00", "2.0"]],
            "asks": [["30006.00", "1.2"], ["30007.00", "0.3"]]
        })
        mock_ws = create_mock_websocket(messages_to_send=[mock_depth_message_1, mock_depth_message_2])
        mock_connect.return_value = mock_ws

        self.service.order_books[symbol] = {"spot": {}, "perp": {}}

        try:
            await self.service._listen_depth_stream(symbol, "ws://fake", stream_type)
        except asyncio.CancelledError as e:
            self.assertEqual(str(e), "Mock WS finished sending messages")
            print("--- 測試：捕獲到預期的 CancelledError ---")
        except Exception as e:
             self.fail(f"_listen_depth_stream 拋出了未預期的錯誤: {e}")

        mock_connect.assert_called_once_with("ws://fake", ping_interval=20, ping_timeout=10)
        # --- 修改：現在 mock_ws.recv 是 AsyncMock，可以檢查 call_count ---
        self.assertEqual(mock_ws.recv.call_count, 3) # 前兩次收到訊息，第三次拋出 CancelledError

        expected_bid = ["30005.00", "0.8"]
        expected_ask = ["30006.00", "1.2"]
        self.assertEqual(self.service.order_books[symbol][stream_type].get('bid'), expected_bid)
        self.assertEqual(self.service.order_books[symbol][stream_type].get('ask'), expected_ask)
        print(">>> test_listen_depth_stream_parsing: 通過")


    def test_handle_task_completion_removes_symbol(self):
        print("\n--- 測試：任務完成回調移除 symbol ---")
        symbol = "ETHUSDT"
        self.service.active_symbol_tasks[symbol] = AsyncMock(spec=asyncio.Task)
        self.service.order_books[symbol] = {"spot": {}, "perp": {}}
        mock_task = AsyncMock(spec=asyncio.Task)
        mock_task.exception.return_value = None
        callback = self.service._handle_task_completion(symbol)
        callback(mock_task)
        self.assertNotIn(symbol, self.service.active_symbol_tasks)
        self.assertNotIn(symbol, self.service.order_books)
        print(">>> test_handle_task_completion_removes_symbol: 通過")

    def test_handle_task_completion_logs_exception(self):
        print("\n--- 測試：任務完成回調記錄異常 ---")
        symbol = "ADAUSDT"
        self.service.active_symbol_tasks[symbol] = AsyncMock(spec=asyncio.Task)
        self.service.order_books[symbol] = {"spot": {}, "perp": {}}
        mock_task = AsyncMock(spec=asyncio.Task)
        test_exception = ValueError("測試任務異常")
        mock_task.exception.return_value = test_exception
        callback = self.service._handle_task_completion(symbol)
        with self.assertLogs(logger="OrderBookService", level="ERROR"):
            callback(mock_task)
        self.assertNotIn(symbol, self.service.active_symbol_tasks)
        self.assertNotIn(symbol, self.service.order_books)
        print(">>> test_handle_task_completion_logs_exception: 通過")

    async def test_close_cancels_active_tasks(self):
        print("\n--- 測試：關閉服務取消任務 ---")
        symbol1 = "BTCUSDT"
        symbol2 = "ETHUSDT"
        mock_task1 = AsyncMock(spec=asyncio.Task)
        mock_task2 = AsyncMock(spec=asyncio.Task)
        self.service.active_symbol_tasks = {symbol1: mock_task1, symbol2: mock_task2}
        self.service.rabbitmq_connection = AsyncMock()
        self.service.rabbitmq_channel = AsyncMock()
        with patch('asyncio.gather', new_callable=AsyncMock) as mock_gather:
             await self.service.close()
        mock_task1.cancel.assert_called_once()
        mock_task2.cancel.assert_called_once()
        mock_gather.assert_awaited_once_with(mock_task1, mock_task2, return_exceptions=True)
        print(">>> test_close_cancels_active_tasks: 通過")

    async def asyncTearDown(self):
        pass

if __name__ == '__main__':
    unittest.main()