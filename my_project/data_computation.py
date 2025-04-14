# my_project/data_computation.py
import json
import logging
from collections import defaultdict
import asyncio
import websockets

# Logger setup
# flake8: noqa E402 (忽略 E402 檢查，如果 logging 需要在 import 之後設定)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')


# E302 要求 class 前面有兩個空行
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
                    # 通常 JSON 錯誤後也應該中斷這個特定連線的迴圈
                    break
                except Exception as e:
                    # 加入一個通用的 Exception 捕捉，避免未知錯誤讓整個服務崩潰
                    logging.exception(f"Unexpected error for {pair} ({stream_type}): {e}")
                    break # 也中斷迴圈

    async def _calc_once(self):
        opportunities = []
        for pair, data in self.prices.items():
            spot_price = data.get('spot')
            perp_price = data.get('perp')
            if spot_price and perp_price and spot_price != 0: # 避免除以零
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
        # 可選：如果沒有機會，也可以印個 Log
        # else:
        #     logging.debug("No arbitrage opportunity found meeting criteria.")

    async def calculate_price_difference(self):
        while True:
            await asyncio.sleep(10) # 每 10 秒計算一次
            await self._calc_once()

    async def run(self):
        tasks = []
        for pair in self.trading_pairs:
            tasks.append(self.fetch_data(pair, "spot"))
            tasks.append(self.fetch_data(pair, "perp"))
        tasks.append(self.calculate_price_difference())
        await asyncio.gather(*tasks)


# E305 要求函數/類別定義後有兩個空行
# E305 要求函數/類別定義後有兩個空行
if __name__ == "__main__":
    TRADING_PAIRS = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
    service = DataComputationService(TRADING_PAIRS)
    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        logging.info("Service stopped by user.")

# W292 要求檔案結尾有空行 (這裡補上)