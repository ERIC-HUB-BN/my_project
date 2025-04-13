import unittest
from unittest.mock import AsyncMock, patch
from my_project.data_computation import DataComputationService
import asyncio

class TestDataComputationService(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 初始化測試對象
        self.spot_stream_url = "wss://mock-spot.url"
        self.perp_stream_url = "wss://mock-perp.url"
        self.service = DataComputationService(self.spot_stream_url, self.perp_stream_url)

    @patch("websockets.connect", new_callable=AsyncMock)
    async def test_fetch_spot_data(self, mock_connect):
        # 模擬 Spot WebSocket 數據
        mock_ws = mock_connect.return_value.__aenter__.return_value
        mock_ws.recv = AsyncMock(side_effect=[
            '{"s": "BTCUSDT", "c": "30000.00"}',
            '{"s": "ETHUSDT", "c": "2000.00"}',
            asyncio.CancelledError  # 用於結束測試
        ])
        
        # 運行 fetch_spot_data
        with self.assertLogs(level="INFO") as log:
            try:
                await self.service.fetch_spot_data()
            except asyncio.CancelledError:
                pass  # 結束測試
            
        # 檢查 Spot 數據是否正確存儲
        self.assertEqual(self.service.prices["BTCUSDT"]["spot"], 30000.00)
        self.assertEqual(self.service.prices["ETHUSDT"]["spot"], 2000.00)
        self.assertIn("Spot Price Update: BTCUSDT -> 30000.0", log.output[0])

    @patch("websockets.connect", new_callable=AsyncMock)
    async def test_fetch_perp_data(self, mock_connect):
        # 模擬 Perp WebSocket 數據
        mock_ws = mock_connect.return_value.__aenter__.return_value
        mock_ws.recv = AsyncMock(side_effect=[
            '{"s": "BTCUSDT", "c": "30105.00"}',
            '{"s": "ETHUSDT", "c": "2010.00"}',
            asyncio.CancelledError  # 用於結束測試
        ])
        
        # 運行 fetch_perp_data
        with self.assertLogs(level="INFO") as log:
            try:
                await self.service.fetch_perp_data()
            except asyncio.CancelledError:
                pass  # 結束測試
            
        # 檢查 Perp 數據是否正確存儲
        self.assertEqual(self.service.prices["BTCUSDT"]["perp"], 30105.00)
        self.assertEqual(self.service.prices["ETHUSDT"]["perp"], 2010.00)
        self.assertIn("Perp Price Update: BTCUSDT -> 30105.0", log.output[0])

    async def test_calculate_price_difference(self):
        # 模擬 Spot 和 Perp 的價格
        self.service.prices = {
            "BTCUSDT": {"spot": 30000.00, "perp": 30105.00},
            "ETHUSDT": {"spot": 2000.00, "perp": 1990.00}
        }
        
        # 運行價差計算
        with self.assertLogs(level="INFO") as log:
            await self.service.calculate_price_difference()
        
        # 驗證價差計算結果
        self.assertIn("Opportunity Found: BTCUSDT -> Perp > Spot by 0.35%", log.output[0])
        self.assertNotIn("Opportunity Found: ETHUSDT", log.output)

    @patch("websockets.connect", new_callable=AsyncMock)
    async def test_run(self, mock_connect):
        # 模擬 Spot 和 Perp 的 WebSocket 數據
        mock_ws = mock_connect.return_value.__aenter__.return_value
        mock_ws.recv = AsyncMock(side_effect=[
            '{"s": "BTCUSDT", "c": "30000.00"}',  # Spot
            '{"s": "BTCUSDT", "c": "30105.00"}',  # Perp
            asyncio.CancelledError  # 用於結束測試
        ])
        
        # 運行整體流程
        with self.assertLogs(level="INFO") as log:
            try:
                await self.service.run()
            except asyncio.CancelledError:
                pass
        
        # 驗證整體流程的數據處理與計算
        self.assertIn("Spot Price Update: BTCUSDT -> 30000.0", log.output[0])
        self.assertIn("Perp Price Update: BTCUSDT -> 30105.0", log.output[1])
        self.assertIn("Opportunity Found: BTCUSDT -> Perp > Spot by 0.35%", log.output[2])

if __name__ == "__main__":
    unittest.main()