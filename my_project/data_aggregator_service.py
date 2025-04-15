# my_project/data_aggregator_service.py
import asyncio
import json
import logging
import os
import websockets
import redis  # 用於 exceptions
import redis.asyncio as redis_async # 保持別名
from typing import Optional, Dict, Any, List, Tuple # <<< --- 新增：匯入 Optional ---

# --- 環境變數 ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
BINANCE_FUTURES_WS_BASE = "wss://fstream.binance.com"
BINANCE_SPOT_WS_BASE = "wss://stream.binance.com:9443"

# --- 日誌設定 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("DataAggregatorService") # 給 logger 一個名字


# --- 主要服務類別 ---
class DataAggregatorService:
    """
    負責連接幣安全市場數據流 (Spot Mini Ticker, Futures Mark Price),
    解析數據, 並將 Spot 最新價 和 Perp 標記價 寫入 Redis.
    """
    def __init__(self, redis_url):
        self.redis_url = redis_url
        # E1131 修正：使用 Optional[...] 而不是 | None (兼容 Python 3.9)
        self.redis_client: Optional[redis_async.Redis] = None
        # 使用字典暫存價格，減少對 Redis 的頻繁 hset (可選優化)
        # self.price_cache: Dict[str, Dict[str, Any]] = defaultdict(dict)
        # self.last_redis_update: Dict[str, float] = defaultdict(float)

    async def _connect_redis(self):
        """建立 Redis 連線"""
        try:
            self.redis_client = redis_async.Redis.from_url(
                self.redis_url, decode_responses=True
            )
            await self.redis_client.ping()
            logger.info(f"Successfully connected to Redis at {self.redis_url}")
        except redis.exceptions.ConnectionError as e:
            logger.error(f"Failed to connect to Redis: {e}")
            self.redis_client = None
            raise ConnectionError("Cannot connect to Redis") from e
        except redis.exceptions.RedisError as e:
            logger.error(f"Redis error during connection: {e}")
            self.redis_client = None
            raise ConnectionError("Redis error during connection") from e

    async def _write_price_to_redis(self, symbol: str, field: str, price: Any):
        """將價格寫入 Redis Hash"""
        if not self.redis_client:
            return
        try:
            # Key: "prices:BTCUSDT", Field: "spot" or "perp", Value: price
            # W0311: 修正縮排
            await self.redis_client.hset(f"prices:{symbol}", key=field, value=price)
            # logger.debug(f"Updated {field} price for {symbol} in Redis: {price}")
        except redis.exceptions.RedisError as e:
            logger.error(f"Redis error writing price for {symbol}: {e}")
        except Exception as e:
            logger.exception(f"Unexpected error writing price for {symbol}: {e}")

    async def _process_spot_ticker(self, ticker: Dict[str, Any]):
        """處理單個 Spot Ticker 數據"""
        symbol = ticker.get('s')
        if symbol and symbol.endswith('USDT'):
            price = ticker.get('c')  # Spot 最新成交價
            if price:
                # W0311: 修正縮排
                await self._write_price_to_redis(symbol, "spot", price)

    async def _process_perp_ticker(self, ticker: Dict[str, Any]):
        """處理單個 Perp Ticker 數據"""
        symbol = ticker.get('s')
        if symbol and symbol.endswith('USDT'):
            price = ticker.get('p')  # Perp 標記價格
            if price:
                # W0311: 修正縮排
                await self._write_price_to_redis(symbol, "perp", price)

    async def _process_message_single(self, message: str, stream_desc: str):
        """
        處理來自單一聚合流的訊息
        Refactored to reduce branches and nesting.
        """
        if not self.redis_client:
            logger.warning("Redis client not available, skipping message processing.")
            return
        try:
            payload = json.loads(message)
            # 聚合流通常是列表
            if not isinstance(payload, list):
                 logger.warning(f"Unexpected payload type from {stream_desc}: {type(payload)}")
                 return

            process_func = None
            if stream_desc == "Spot MiniTicker":
                process_func = self._process_spot_ticker
            elif stream_desc == "Futures MarkPrice":
                process_func = self._process_perp_ticker
            else:
                # C0301: 修正 - 換行使行長度不超過 100
                logger.warning(
                    f"Unknown stream description for processing: {stream_desc}"
                )
                return

            # 使用 asyncio.gather 併發處理列表中的 ticker
            tasks = [process_func(ticker) for ticker in payload if isinstance(ticker, dict)]
            if tasks:
                await asyncio.gather(*tasks)

        except json.JSONDecodeError:
            # C0301: 修正 - 換行
            logger.error(
                f"Failed to decode JSON from {stream_desc}: {message[:100]}"
            )
        except redis.exceptions.RedisError as e:
            logger.error(f"Redis error processing message from {stream_desc}: {e}")
        except Exception as e:
            # C0301: 修正 - 換行
            logger.exception(
                f"Error processing message batch from {stream_desc}: {e}"
            )

    async def _listen_single_stream(self, url: str, stream_desc: str):
        """監聽單一 WebSocket Stream 的輔助函數，包含重連邏輯"""
        while True:
            try:
                # C0301: 修正 - 換行
                logger.info(
                    f"Connecting to {stream_desc} stream at {url}..."
                )
                connect_params = {"ping_interval": 20, "ping_timeout": 10}
                async with websockets.connect(url, **connect_params) as ws:
                    logger.info(f"Successfully connected to {stream_desc} stream.")
                    while True:
                        try:
                            message = await ws.recv()
                            await self._process_message_single(message, stream_desc)
                        except websockets.exceptions.ConnectionClosed as e:
                            # C0301: 修正 - 換行
                            logger.warning(
                                f"{stream_desc} connection closed: {e}. Reconnecting..."
                            )
                            break # 跳出接收訊息迴圈，外層會重連
                        except Exception as e:
                            # C0301: 修正 - 換行
                            logger.exception(
                                f"Error receiving/processing {stream_desc} message: {e}"
                            )
                            await asyncio.sleep(1)

            # W0718: Catching too general exception (Pylint warning - kept for resilience)
            except Exception as e:
                # C0301: 修正 - 換行
                logger.error(
                    f"Failed to connect to {stream_desc} stream: {e}. Retrying in 10 seconds..."
                )
                await asyncio.sleep(10) # 連線失敗，等待 10 秒重試

    async def run_websocket_listener(self):
        """連接到 WebSocket Streams 並持續監聽/處理訊息"""
        if not self.redis_client:
            logger.error("Redis client not available. Cannot start listener.")
            return

        spot_stream_url = f"{BINANCE_SPOT_WS_BASE}/ws/!miniTicker@arr"
        futures_stream_url = f"{BINANCE_FUTURES_WS_BASE}/ws/!markPrice@arr@1s"

        # 異步地運行兩個 listener
        # W0311: 修正縮排
        task1 = asyncio.create_task(
            self._listen_single_stream(spot_stream_url, "Spot MiniTicker")
        )
        # W0311: 修正縮排
        task2 = asyncio.create_task(
            self._listen_single_stream(futures_stream_url, "Futures MarkPrice")
        )

        logger.info("Starting WebSocket listeners for Spot and Futures streams...")
        await asyncio.gather(task1, task2) # 同時運行兩個 listener

    async def start(self):
        """啟動服務"""
        await self._connect_redis()
        if self.redis_client:
            await self.run_websocket_listener() # 啟動 WebSocket 監聽
        else:
            logger.critical("Redis connection failed. Service cannot start.")

    async def close(self):
        """關閉 Redis 連線"""
        if self.redis_client:
            await self.redis_client.aclose()
            logger.info("Redis connection closed.")


# --- 主程式執行區塊 ---
async def main():
    """主執行函數"""
    # W0311: 修正縮排
    service = DataAggregatorService(redis_url=REDIS_URL)
    try:
        # W0311: 修正縮排
        await service.start()
    except asyncio.CancelledError:
        # W0311: 修正縮排
        logger.info("Service cancellation requested.")
    except Exception as e:
        # W0311: 修正縮排
        logger.exception(f"Critical error in main service loop: {e}")
    finally:
        # W0311: 修正縮排
        logger.info("Shutting down service...")
        if 'service' in locals() and service:
            # W0311: 修正縮排
            await service.close()
        # W0311: 修正縮排
        logger.info("Service shutdown complete.")

# E305: 兩個空行
# E305: 兩個空行
if __name__ == "__main__":
    # W0311: 修正縮排
    try:
        # W0311: 修正縮排
        asyncio.run(main())
    except KeyboardInterrupt:
        # W0311: 修正縮排
        logger.info("Service stopped by user (KeyboardInterrupt).")

# C0304/W292: 確保檔案結尾有空行