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

    @patch("websockets.connect")
    async def test_fetch_spot_data(self, mock_connect):
        # 模擬 WebSocket 行為
        mock_ws = AsyncMock()
        mock_connect.return_value.__aenter__.return_value = mock_ws

        # 模擬 recv 方法返回的消
        mock_ws.recv = AsyncMock(side_effect=[
            '{"s": "BTCUSDT", "c": "30000.00"}',
            '{"s": "ETHUSDT", "c": "2000.00"}',
            asyncio.CancelledError  # 用於結束測試
        ])

        # 運行 fetch_spot_data 方法
        with self.assertLogs(level="INFO") as log:
            try:
                await self.service.fetch_spot_data()
            except asyncio.CancelledError:
                pass  # 測試結束

        # 驗證日誌內容是否包含期望的條目
        expected_logs = [
            "Spot Price Update: BTCUSDT -> 30000.0",
            "Spot Price Update: ETHUSDT -> 2000.0"
        ]
        for expected_log in expected_logs:
            self.assertTrue(
                any(expected_log in entry for entry in log.output),
                f"Expected log '{expected_log}' not found in logs: {log.output}"
            )

    async def asyncTearDown(self):
        # 測試結束時清理異步任務
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        [task.cancel() for task in tasks]
        await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    unittest.main()