"""
This module fetches spot and perpetual prices from Binance WebSocket streams,
calculates price differences, and logs trading opportunities.
"""

import json
import logging
from collections import defaultdict
import asyncio  # 修正：添加 asyncio 的導入

import websockets

# Logger setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')


class DataComputationService:
    """
    A service to fetch spot and perpetual prices from WebSocket streams,
    calculate price differences, and log trading opportunities.
    """

    def __init__(self, spot_stream_url, perp_stream_url):
        """
        Initializes the service with WebSocket URLs for spot and perpetual streams.

        Args:
            spot_stream_url (str): The WebSocket URL for spot prices.
            perp_stream_url (str): The WebSocket URL for perpetual prices.
        """
        self.spot_stream_url = spot_stream_url
        self.perp_stream_url = perp_stream_url
        self.prices = defaultdict(dict)  # Used to store Spot and Perp real-time prices.

    async def fetch_spot_data(self):
        """
        Connects to the spot WebSocket stream, fetches real-time prices,
        and stores them in the self.prices dictionary.
        """
        async with websockets.connect(self.spot_stream_url) as ws:
            logging.info("Connected to Spot WebSocket")
            while True:
                try:
                    message = await ws.recv()
                    data = json.loads(message)
                    symbol = data.get("s")  # Trading pair
                    price = float(data.get("c"))  # Latest price
                    if symbol and price:
                        self.prices[symbol]['spot'] = price
                        logging.info("Spot Price Update: %s -> %s", symbol, price)
                except websockets.exceptions.ConnectionClosed as e:
                    logging.error("WebSocket connection closed: %s", e)
                    break
                except json.JSONDecodeError as e:
                    logging.error("JSON decoding error: %s", e)
                    break

    async def fetch_perp_data(self):
        """
        Connects to the perpetual WebSocket stream, fetches real-time prices,
        and stores them in the self.prices dictionary.
        """
        async with websockets.connect(self.perp_stream_url) as ws:
            logging.info("Connected to Perp WebSocket")
            while True:
                try:
                    message = await ws.recv()
                    data = json.loads(message)
                    symbol = data.get("s")  # Trading pair
                    price = float(data.get("c"))  # Latest price
                    if symbol and price:
                        self.prices[symbol]['perp'] = price
                        logging.info("Perp Price Update: %s -> %s", symbol, price)
                except websockets.exceptions.ConnectionClosed as e:
                    logging.error("WebSocket connection closed: %s", e)
                    break
                except json.JSONDecodeError as e:
                    logging.error("JSON decoding error: %s", e)
                    break

    async def calculate_price_difference(self):
        """
        Periodically calculates the price difference between spot and perpetual prices,
        and logs trading opportunities if the difference exceeds a threshold.
        """
        while True:
            await asyncio.sleep(10)  # Perform calculation every 10 seconds.
            for symbol, data in self.prices.items():
                spot_price = data.get('spot')
                perp_price = data.get('perp')
                if spot_price and perp_price:
                    difference = (perp_price - spot_price) / spot_price
                    if difference > 0.0035:  # Price difference exceeds 0.35%.
                        logging.info(
                            "Opportunity Found: %s -> Perp > Spot by %.2f%%",
                            symbol, difference * 100
                        )

    async def run(self):
        """
        Runs the service by concurrently fetching spot and perpetual prices,
        and calculating price differences.
        """
        await asyncio.gather(
            self.fetch_spot_data(),
            self.fetch_perp_data(),
            self.calculate_price_difference()
        )


if __name__ == "__main__":
    SPOT_STREAM = "wss://stream.binance.com:9443/ws/btcusdt@ticker"  # Spot WebSocket URL
    PERP_STREAM = "wss://fstream.binance.com/ws/btcusdt@ticker"  # Perp WebSocket URL
    service = DataComputationService(SPOT_STREAM, PERP_STREAM)
    asyncio.run(service.run())  # 修正：asyncio 導入後不再報錯
