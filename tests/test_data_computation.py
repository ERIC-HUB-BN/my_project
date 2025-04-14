# tests/test_data_computation.py
import unittest
from unittest.mock import AsyncMock, patch
import asyncio
import sys
import os
import websockets  #  E262: 確保註解前和 # 後都有空格

# --- 重要：設定主程式碼的路徑 ---
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
my_project_path = os.path.join(project_root, 'my_project')
if my_project_path not in sys.path:
    sys.path.insert(0, my_project_path)
# --- 路徑設定結束 ---

# E402: Import 保持在這裡，因為需要先設定 sys.path
from data_computation import DataComputationService  # noqa: E402


# E302: Class 前面需要兩個空行
class TestDataComputationService(unittest.IsolatedAsyncioTestCase):
    """
    對 DataComputationService 進行單元測試
    """
    async def asyncSetUp(self):
        """每個測試開始前執行的設置"""
        self.trading_pairs = ["BTCUSDT", "ETHUSDT"]
        self.service = DataComputationService(self.trading_pairs)
        print(f"\n--- 開始測試: {self._testMethodName} ---")

    @patch("websockets.connect")
    async def test_fetch_data_single_message(self, mock_connect):
        """
        測試：假裝只收到一次 WebSocket 訊息，檢查價格和日誌是否正確
        """
        mock_ws = AsyncMock()
        mock_connect.return_value.__aenter__.return_value = mock_ws
        mock_message = '{"s": "BTCUSDT", "c": "30000.00"}'
        mock_ws.recv = AsyncMock(side_effect=[mock_message])

        with self.assertLogs(level="INFO") as log:
            await self.service.fetch_data("BTCUSDT", "spot", stop_after=1)

        self.assertIn("BTCUSDT", self.service.prices)
        self.assertEqual(self.service.prices["BTCUSDT"]["spot"], 30000.0)
        print("\n--- Captured Logs for test_fetch_data_single_message ---")
        for i, entry in enumerate(log.output):
            print(f"Log[{i}]: {entry}")
        print("--- End Captured Logs ---")
        self.assertTrue(log.output[0].endswith("Connected to Spot WebSocket for BTCUSDT"))
        self.assertTrue(log.output[1].endswith("Spot Price Update: BTCUSDT -> 30000.0"))
        print(">>> test_fetch_data_single_message: 價格和精確日誌檢查通過！")

    @patch("websockets.connect")
    async def test_fetch_data_continuous(self, mock_connect):
        """
        測試：假裝連續收到多次 WebSocket 訊息，檢查最終價格和每次的日誌
        """
        mock_ws = AsyncMock()
        mock_connect.return_value.__aenter__.return_value = mock_ws
        mock_messages = [
            '{"s": "BTCUSDT", "c": "30000.00"}',
            '{"s": "BTCUSDT", "c": "30050.50"}',
            '{"s": "BTCUSDT", "c": "30010.00"}'
        ]
        mock_ws.recv = AsyncMock(side_effect=mock_messages + [asyncio.CancelledError])

        with self.assertLogs(level="INFO") as log:
            try:
                await self.service.fetch_data("BTCUSDT", "spot", stop_after=len(mock_messages))
            except asyncio.CancelledError:
                pass

        self.assertEqual(self.service.prices["BTCUSDT"]["spot"], 30010.00)
        print("\n--- Captured Logs for test_fetch_data_continuous ---")
        for i, entry in enumerate(log.output):
            print(f"Log[{i}]: {entry}")
        print("--- End Captured Logs ---")
        self.assertTrue(log.output[0].endswith("Connected to Spot WebSocket for BTCUSDT"))
        self.assertTrue(log.output[1].endswith("Spot Price Update: BTCUSDT -> 30000.0"))
        self.assertTrue(log.output[2].endswith("Spot Price Update: BTCUSDT -> 30050.5"))
        self.assertTrue(log.output[3].endswith("Spot Price Update: BTCUSDT -> 30010.0"))
        print(">>> test_fetch_data_continuous: 連續價格和日誌檢查通過！")

    @patch("asyncio.sleep", return_value=None)
    async def test_calc_once(self, mock_sleep):
        """
        測試：只算一次價差，檢查計算結果和 Log 是否正確
        """
        self.service.prices = {
            "BTCUSDT": {"spot": 30000, "perp": 30100},
            "ETHUSDT": {"spot": 2000, "perp": 2100},
        }
        with self.assertLogs(level="INFO") as log:
            await self.service._calc_once()
        print("\n--- Captured Logs for test_calc_once ---")
        for i, entry in enumerate(log.output):
            print(f"Log[{i}]: {entry}")
        print("--- End Captured Logs ---")
        expected_log = "Top Opportunity: ETHUSDT -> Perp > Spot by 5.00%"
        self.assertTrue(log.output[0].endswith(expected_log))
        print(">>> test_calc_once: 價差計算和精確日誌檢查通過！")

    @patch("websockets.connect")
    async def test_fetch_data_with_connection_closed(self, mock_connect):
        """
        測試：假裝網路斷線 (ConnectionClosed)，檢查 Log 是否正確記錄錯誤
        *** 使用最簡單的 ConnectionClosed 實例 ***
        """
        mock_ws = AsyncMock()
        mock_connect.return_value.__aenter__.return_value = mock_ws

        # 嘗試用最簡單的方式產生 ConnectionClosed 實例
        mock_ws.recv = AsyncMock(side_effect=websockets.exceptions.ConnectionClosed(None, None))

        # 捕捉 ERROR 等級的日誌
        with self.assertLogs(level="ERROR") as log:
            # 期望主程式的 except ConnectionClosed 捕捉到上面模擬的錯誤
            await self.service.fetch_data("BTCUSDT", "spot", stop_after=1)

        # --- 檢查測試結果 ---
        print("\n--- Captured Logs for test_fetch_data_with_connection_closed ---")
        for i, entry in enumerate(log.output):
            print(f"Log[{i}]: {entry}")
        print("--- End Captured Logs ---")

        # 斷言日誌包含預期的錯誤訊息
        self.assertTrue(
            any("WebSocket connection closed for BTCUSDT (spot)" in entry for entry in log.output),
            "Expected 'WebSocket connection closed' log not found"
        )
        print(">>> test_fetch_data_with_connection_closed: 斷線錯誤日誌檢查通過！")


    @patch("websockets.connect")
    async def test_fetch_data_with_json_decode_error(self, mock_connect):
        """
        測試：假裝收到壞掉的資料 (JSONDecodeError)，檢查 Log 是否正確記錄錯誤
        """
        mock_ws = AsyncMock()
        mock_connect.return_value.__aenter__.return_value = mock_ws
        # --- 語法錯誤修正點 ---
        invalid_json = 'THIS IS NOT JSON'  # 確保字串是完整的
        # --- 修正結束 ---
        mock_ws.recv = AsyncMock(side_effect=[invalid_json])  #  主程式會在 json.loads 時出錯

        with self.assertLogs(level='ERROR') as log:
             await self.service.fetch_data("BTCUSDT", "spot", stop_after=1)

        print("\n--- Captured Logs for test_fetch_data_with_json_decode_error ---")
        for i, entry in enumerate(log.output):
            print(f"Log[{i}]: {entry}")  # E111/E117: 確保縮排正確
        print("--- End Captured Logs ---")
        self.assertTrue(
            any("JSON decoding error for BTCUSDT (spot)" in entry for entry in log.output),
            "Expected JSON decode error log not found"
        )
        print(">>> test_fetch_data_with_json_decode_error: JSON解碼錯誤日誌檢查通過！")


    # E301: 在 asyncTearDown 前需要一個空行
    async def asyncTearDown(self):
        """每個測試結束後執行的清理"""
        print(f"--- 結束測試: {self._testMethodName} ---\n")


# E305: 函數/類別結束後需要兩個空行
if __name__ == '__main__':
    unittest.main()

# W292: 確保檔案結尾有空行