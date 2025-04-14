# data_computation.py

import json
import logging
from collections import defaultdict
import asyncio
import websockets

# Logger setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

class DataComputationService:
    def __init__(self, trading_pairs):
        self.trading_pairs = trading_pairs
        self.prices = defaultdict(dict)

    async def fetch_data(self, pair, stream_type, stop_after=None):
        url = (
            f"wss://stream.binance.com:9443/ws/{pair.lower()}@ticker"
            if stream_type == "spot"
            else f"wss://fstream.binance.com/ws/{pair.lower()}@ticker"
        )
        async with websockets.connect(url) as ws:
            logging.info(f"Connected to {stream_type.capitalize()} WebSocket for {pair}")
            count = 0
            while True:
                try:
                    message = await ws.recv()
                    data = json.loads(message)
                    price = float(data.get("c"))
                    if price:
                        self.prices[pair][stream_type] = price
                        logging.info(f"{stream_type.capitalize()} Price Update: {pair} -> {price}")
                    count += 1
                    if stop_after and count >= stop_after:
                        break
                except websockets.exceptions.ConnectionClosed as e:
                    logging.error(f"WebSocket connection closed for {pair} ({stream_type}): {e}")
                    break
                except json.JSONDecodeError as e:
                    logging.error(f"JSON decoding error for {pair} ({stream_type}): {e}")
                    break

    async def _calc_once(self):
        opportunities = []
        for pair, data in self.prices.items():
            spot_price = data.get('spot')
            perp_price = data.get('perp')
            if spot_price and perp_price:
                difference = (perp_price - spot_price) / spot_price
                if difference > 0.0035:
                    opportunities.append((pair, difference))

        if opportunities:
            opportunities.sort(key=lambda x: x[1], reverse=True)
            top_opportunity = opportunities[0]
            logging.info(
                "Top Opportunity: %s -> Perp > Spot by %.2f%%",
                top_opportunity[0],
                top_opportunity[1] * 100
            )

    async def calculate_price_difference(self):
        while True:
            await asyncio.sleep(10)
            await self._calc_once()

    async def run(self):
        tasks = []
        for pair in self.trading_pairs:
            tasks.append(self.fetch_data(pair, "spot"))
            tasks.append(self.fetch_data(pair, "perp"))
        tasks.append(self.calculate_price_difference())
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    TRADING_PAIRS = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
    service = DataComputationService(TRADING_PAIRS)
    asyncio.run(service.run())
