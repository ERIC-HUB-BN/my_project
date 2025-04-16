# tests/test_orderbook_service.py
import unittest
from unittest.mock import patch, AsyncMock, MagicMock, call # 用來建立假的物件和檢查函數呼叫
import asyncio
import sys
import os
import json # 用來建立測試訊息
from decimal import Decimal
import logging # <<< --- 確保這一行 import logging 在 ---

# --- 設定主程式碼的路徑 (跟其他測試檔一樣) ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
my_project_path = os.path.join(project_root, 'my_project')
if my_project_path not in sys.path:
    sys.path.insert(0, my_project_path)
# --- 路徑設定結束 ---

import aio_pika # 匯入 aio_pika 來模擬它的物件和錯誤
# 匯入我們要測試的 OrderBookService 類別
from orderbook_service import OrderBookService, RABBITMQ_URL, OPPORTUNITY_QUEUE_NAME # noqa: E402

# --- 測試用的假 RabbitMQ 物件 (跟 CalculationService 測試類似) ---
def create_mock_rabbitmq_channel():
    mock_channel = AsyncMock()
    mock_channel.set_qos = AsyncMock() # 我們需要模擬 set_qos
    mock_channel.declare_queue = AsyncMock() # 需要模擬 declare_queue
    # 模擬 declare_queue 返回一個假的 Queue 物件
    mock_queue = AsyncMock()
    mock_queue.consume = AsyncMock() # Queue 物件需要有 consume 方法
    mock_channel.declare_queue.return_value = mock_queue
    mock_channel.close = AsyncMock()
    return mock_channel, mock_queue # 返回 channel 和 queue

def create_mock_rabbitmq_connection(mock_channel):
    mock_connection = AsyncMock()
    mock_connection.channel = AsyncMock(return_value=mock_channel)
    mock_connection.close = AsyncMock()
    return mock_connection

# --- 建立假的訊息物件 ---
def create_mock_incoming_message(body_data: dict) -> MagicMock:
    """建立一個假的 aio_pika 傳入訊息物件"""
    message = MagicMock(spec=aio_pika.abc.AbstractIncomingMessage)
    message.body = json.dumps(body_data).encode('utf-8') # 將字典轉成 JSON 字串再轉 bytes

    # 模擬 process() 的 context manager
    # 讓 async with message.process(): 可以運作
    async def mock_process(*args, **kwargs):
        yield # 模擬進入 async with 區塊
    message.process = MagicMock(return_value=AsyncMock())
    message.process.return_value.__aenter__ = AsyncMock(side_effect=mock_process)
    message.process.return_value.__aexit__ = AsyncMock(return_value=None) # 模擬離開區塊

    return message

# --- 測試類別 ---
class TestOrderBookService(unittest.IsolatedAsyncioTestCase):
    """
    對 OrderBookService 進行單元測試
    """
    def setUp(self):
        """每個測試開始前的準備"""
        self.service = OrderBookService(rabbitmq_url="amqp://fake_url/")
        # 把 logger 的等級調到 DEBUG，這樣才能捕捉 info 等級的訊息
        logging.getLogger("OrderBookService").setLevel(logging.DEBUG)

    @patch("aio_pika.connect_robust") # 把實際的 aio_pika.connect_robust 換成假的
    async def test_connect_rabbitmq_success(self, mock_connect_robust):
        """測試：成功連接 RabbitMQ"""
        print("\n--- 測試：成功連接 RabbitMQ ---")
        mock_channel, _ = create_mock_rabbitmq_channel()
        mock_connection = create_mock_rabbitmq_connection(mock_channel)
        mock_connect_robust.return_value = mock_connection # 設定假連線的回傳值

        result = await self.service._connect_rabbitmq() # 呼叫要測試的函數

        self.assertTrue(result) # 斷言結果應該是 True
        mock_connect_robust.assert_awaited_once_with(self.service.rabbitmq_url) # 檢查 connect_robust 是否被正確呼叫
        mock_connection.channel.assert_awaited_once() # 檢查是否有取得 channel
        mock_channel.set_qos.assert_awaited_once_with(prefetch_count=1) # 檢查是否有設定 QoS
        self.assertIsNotNone(self.service.rabbitmq_connection) # 檢查 service 內的變數是否有被設定
        self.assertIsNotNone(self.service.rabbitmq_channel)
        print(">>> test_connect_rabbitmq_success: 通過")

    @patch("aio_pika.connect_robust")
    async def test_connect_rabbitmq_failure(self, mock_connect_robust):
        """測試：連接 RabbitMQ 失敗"""
        print("\n--- 測試：連接 RabbitMQ 失敗 ---")
        # 讓假的 connect_robust 在被呼叫時，丟出一個連線錯誤
        mock_connect_robust.side_effect = aio_pika.exceptions.AMQPConnectionError("測試連線錯誤")

        result = await self.service._connect_rabbitmq()

        self.assertFalse(result) # 斷言結果應該是 False
        self.assertIsNone(self.service.rabbitmq_connection) # 檢查 service 內的變數是否還是 None
        self.assertIsNone(self.service.rabbitmq_channel)
        print(">>> test_connect_rabbitmq_failure: 通過")

    async def test_on_message_success(self):
        """測試：成功處理一則有效的機會訊息"""
        print("\n--- 測試：成功處理有效訊息 ---")
        # 建立一則假的訊息，內容包含 symbol
        test_data = {
            "symbol": "BTCUSDT",
            "price_difference_percent": "0.45%",
            "funding_rate_percent": "0.01%",
            "timestamp": 1678886400.0
        }
        mock_message = create_mock_incoming_message(test_data)

        # 使用 assertLogs 來檢查是否有印出我們預期的 Log
        with self.assertLogs(logger="OrderBookService", level="INFO") as cm:
            await self.service._on_message(mock_message) # 直接呼叫處理訊息的函數

        # 檢查 Log 輸出裡面是否包含 "收到套利機會訊息，交易對: BTCUSDT"
        self.assertTrue(any("收到套利機會訊息，交易對: BTCUSDT" in log for log in cm.output))
        print(">>> test_on_message_success: Log 檢查通過")
        # 檢查 process context manager 被正確使用
        mock_message.process.assert_called_once()


    async def test_on_message_missing_symbol(self):
        """測試：處理一則缺少 symbol 的訊息"""
        print("\n--- 測試：處理缺少 symbol 的訊息 ---")
        test_data = { # 故意不放 symbol
            "price_difference_percent": "0.45%",
            "funding_rate_percent": "0.01%",
            "timestamp": 1678886400.0
        }
        mock_message = create_mock_incoming_message(test_data)

        # 這次預期會印出 WARNING 等級的 Log
        with self.assertLogs(logger="OrderBookService", level="WARNING") as cm:
            await self.service._on_message(mock_message)

        self.assertTrue(any("收到的訊息格式不符，缺少 'symbol'" in log for log in cm.output))
        print(">>> test_on_message_missing_symbol: Log 檢查通過")
        mock_message.process.assert_called_once()


    async def test_on_message_invalid_json(self):
        """測試：處理一則內容不是 JSON 的訊息"""
        print("\n--- 測試：處理無效 JSON 訊息 ---")
        # 建立一個假的訊息，但 body 不是 JSON
        mock_message = MagicMock(spec=aio_pika.abc.AbstractIncomingMessage)
        mock_message.body = b"this is not json" # 給它一個不是 JSON 的 bytes

        # 模擬 process() context manager
        async def mock_process(*args, **kwargs):
            yield
        mock_message.process = MagicMock(return_value=AsyncMock())
        mock_message.process.return_value.__aenter__ = AsyncMock(side_effect=mock_process)
        mock_message.process.return_value.__aexit__ = AsyncMock(return_value=None)

        # 這次預期會印出 ERROR 等級的 Log
        with self.assertLogs(logger="OrderBookService", level="ERROR") as cm:
            await self.service._on_message(mock_message)

        self.assertTrue(any("無法解析收到的訊息 (非 JSON)" in log for log in cm.output))
        print(">>> test_on_message_invalid_json: Log 檢查通過")
        mock_message.process.assert_called_once()

    # --- 以下是 test_start_consuming 的修正版本 ---
    @patch("orderbook_service.OrderBookService._connect_rabbitmq") # <--- @patch 在這裡
    async def test_start_consuming(self, mock_connect): # <--- mock_connect 參數在這裡
        """測試：start_consuming 是否正確設定並呼叫 consume"""
        print("\n--- 測試：start_consuming 流程 ---")
        # 假設連線成功
        mock_connect.return_value = True # 使用 mock_connect
        mock_channel, mock_queue = create_mock_rabbitmq_channel()
        self.service.rabbitmq_channel = mock_channel # 手動設定 channel

        # 執行 start_consuming (它會一直跑到 await future)
        # 我們用 asyncio.wait_for 來設定一個超時，避免測試卡住
        try:
             # 這裡的 service.start_consuming() 內部現在會用 *真的* asyncio.Future
             await asyncio.wait_for(self.service.start_consuming(), timeout=0.1)
        except asyncio.TimeoutError:
             pass # 預期會超時，因為 start_consuming 會卡在 await future 等待

        # 檢查 declare_queue 是否被呼叫來取得佇列
        mock_channel.declare_queue.assert_awaited_once_with(
            OPPORTUNITY_QUEUE_NAME, durable=True
        )
        # 檢查 queue.consume 是否被呼叫，並且傳入了正確的回調函數
        mock_queue.consume.assert_awaited_once_with(self.service._on_message)
        # 我們不再檢查 asyncio.Future 的 mock_future.assert_called_once()
        print(">>> test_start_consuming: 通過")
    # --- test_start_consuming 函數結束 ---

    # E301: 在 asyncTearDown 前需要一個空行
    async def asyncTearDown(self):
        """每個測試結束後的清理"""
        # 把 logger 等級設回來，避免影響其他測試
        logging.getLogger("OrderBookService").setLevel(logging.INFO)
        # 如果 service 有模擬的連線，嘗試關閉 (雖然是假的，但好習慣)
        if self.service and hasattr(self.service, 'close'):
            await self.service.close()
        print(f"--- 測試結束: {self._testMethodName} ---\n")


# E305: 函數/類別結束後需要兩個空行
if __name__ == '__main__':
    unittest.main()

# W292: 確保檔案結尾有空行