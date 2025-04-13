import asyncio
import websockets
import json
import logging
from collections import defaultdict

# Logger setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')


class DataComputationService:
    def __init__(self, spot_stream_url, perp_stream_url):
        self.spot_stream_url = spot_stream_url
        self.perp_stream_url = perp_stream_url
        self.prices = defaultdict(dict)  # 用於存儲 Spot 和 Perp 的實時價格

    async def fetch_spot_data(self):
        async with websockets.connect(self.spot_stream_url) as ws:
            logging.info("Connected to Spot WebSocket")
            while True:
                try:
                    message = await ws.recv()
                    data = json.loads(message)
                    symbol = data.get("s")  # 交易對
                    price = float(data.get("c"))  # 最新價格
                    if symbol and price:
                        self.prices[symbol]['spot'] = price
                        logging.info(f"Spot Price Update: {symbol} -> {price}")
                except Exception as e:
                    logging.error(f"Error in Spot WebSocket: {e}")
                    break

    async def fetch_perp_data(self):
        async with websockets.connect(self.perp_stream_url) as ws:
            logging.info("Connected to Perp WebSocket")
            while True:
                try:
                    message = await ws.recv()
                    data = json.loads(message)
                    symbol = data.get("s")  # 交易對
                    price = float(data.get("c"))  # 最新價格
                    if symbol and price:
                        self.prices[symbol]['perp'] = price
                        logging.info(f"Perp Price Update: {symbol} -> {price}")
                except Exception as e:
                    logging.error(f"Error in Perp WebSocket: {e}")
                    break

    async def calculate_price_difference(self):
        while True:
            await asyncio.sleep(10)  # 每10秒進行一次價差計算
            for symbol, data in self.prices.items():
                spot_price = data.get('spot')
                perp_price = data.get('perp')
                if spot_price and perp_price:
                    difference = (perp_price - spot_price) / spot_price
                    if difference > 0.0035:  # 價差大於 0.35%
                        logging.info(
                            f"Opportunity Found: {symbol} -> Perp > Spot by {difference:.2%}"
                        )


    async def run(self):
        # 同時運行 Spot 和 Perp 的數據抓取，以及價差計算
        await asyncio.gather(
            self.fetch_spot_data(),
            self.fetch_perp_data(),
            self.calculate_price_difference()
        )


if __name__ == "__main__":
    spot_stream = "wss://stream.binance.com:9443/ws/btcusdt@ticker"  # Spot WebSocket URL
    perp_stream = "wss://fstream.binance.com/ws/btcusdt@ticker"  # Perp WebSocket URL
    service = DataComputationService(spot_stream, perp_stream)
    asyncio.run(service.run())
