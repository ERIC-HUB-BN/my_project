import unittest
from my_project.data_computation import DataComputationService  # 修正這一行

class TestDataComputationService(unittest.TestCase):
    def test_initialization(self):
        spot_stream = "wss://stream.binance.com:9443/ws/spot_ticker"
        perp_stream = "wss://fstream.binance.com/ws/perp_ticker"
        service = DataComputationService(spot_stream, perp_stream)
        self.assertEqual(service.spot_stream_url, spot_stream)
        self.assertEqual(service.perp_stream_url, perp_stream)

if __name__ == "__main__":
    unittest.main()