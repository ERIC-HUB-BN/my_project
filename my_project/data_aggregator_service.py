# my_project/data_aggregator_service.py
import asyncio
import json
import logging
import os
import websockets
import redis  # <<< --- 新增：匯入頂層 redis 套件 (用於 exceptions) ---
import redis.asyncio as redis_async # <<< --- 修改：原本的 redis 改名為 redis_async ---

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
logger = logging.getLogger(__name__)

# --- 主要服務類別 ---
class DataAggregatorService:
    """
    負責連接幣安全市場數據流 (Spot Mini Ticker, Futures Mark Price),
    解析數據, 並將 Spot 最新價 和 Perp 標記價 寫入 Redis.
    """
    def __init__(self, redis_url):
        self.redis_url = redis_url
        # <<< --- 修改：使用 redis_async ---
        self.redis_client: redis_async.Redis | None = None

    async def _connect_redis(self):
        """建立 Redis 連線"""
        try:
            # <<< --- 修改：使用 redis_async ---
            self.redis_client = redis_async.Redis.from_url(
                self.redis_url, decode_responses=True
            )
            await self.redis_client.ping()
            logger.info(f"Successfully connected to Redis at {self.redis_url}")
        # <<< --- 修改：使用頂層 redis.exceptions ---
        except redis.exceptions.ConnectionError as e:
            logger.error(f"Failed to connect to Redis: {e}")
            self.redis_client = None
            raise ConnectionError("Cannot connect to Redis") from e
        # <<< --- 修改：捕捉頂層 redis.exceptions.RedisError 或更通用的 Exception ---
        except redis.exceptions.RedisError as e: # 捕捉其他 Redis 相關錯誤
             logger.error(f"Redis error during connection: {e}")
             self.redis_client = None
             raise ConnectionError("Redis error during connection") from e


    async def _process_message(self, message):
        """處理從 WebSocket 收到的單條訊息"""
        # (此函數內部邏輯不變，但要確保 redis_client 是可用的)
        if not self.redis_client:
             logger.warning("Redis client not available, skipping message processing.")
             return
        try:
            data = json.loads(message)
            if 'stream' in data and 'data' in data:
                stream_name = data['stream']
                payload = data['data']

                if stream_name == "!miniTicker@arr":
                    for ticker in payload:
                        symbol = ticker.get('s')
                        if symbol and symbol.endswith('USDT'):
                            price = ticker.get('c')
                            if price:
                                await self.redis_client.hset(f"prices:{symbol}", key="spot", value=price)
                elif stream_name == "!markPrice@arr@1s":
                    for ticker in payload:
                        symbol = ticker.get('s')
                        if symbol and symbol.endswith('USDT'):
                            price = ticker.get('p')
                            if price:
                                await self.redis_client.hset(f"prices:{symbol}", key="perp", value=price)
                else:
                    logger.warning(f"Received message from unexpected stream: {stream_name}")
            else:
                logger.debug(f"Received non-stream message: {message[:100]}")

        except json.JSONDecodeError:
            logger.error(f"Failed to decode JSON: {message[:100]}")
        except redis.exceptions.RedisError as e: # 捕捉 Redis 操作錯誤
             logger.error(f"Redis error processing message: {e}")
        except Exception as e:
            logger.exception(f"Error processing message: {e}")

    async def run_websocket_listener(self):
        """連接到 Combined Stream 並持續監聽/處理訊息"""
        if not self.redis_client:
            logger.error("Redis client not available. Cannot start listener.")
            return

        spot_stream_url = f"{BINANCE_SPOT_WS_BASE}/ws/!miniTicker@arr"
        futures_stream_url = f"{BINANCE_FUTURES_WS_BASE}/ws/!markPrice@arr@1s"

        task1 = asyncio.create_task(self._listen_single_stream(spot_stream_url, "Spot MiniTicker"))
        task2 = asyncio.create_task(self._listen_single_stream(futures_stream_url, "Futures MarkPrice"))

        logger.info("Starting WebSocket listeners for Spot and Futures streams...")
        await asyncio.gather(task1, task2)

    async def _listen_single_stream(self, url, stream_desc):
        """監聽單一 WebSocket Stream 的輔助函數，包含重連邏輯"""
        while True:
            try:
                logger.info(f"Connecting to {stream_desc} stream at {url}...")
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    logger.info(f"Successfully connected to {stream_desc} stream.")
                    while True:
                        try:
                            message = await ws.recv()
                            await self._process_message_single(message, stream_desc)
                        except websockets.exceptions.ConnectionClosed as e:
                            logger.warning(f"{stream_desc} connection closed: {e}. Reconnecting...")
                            break
                        except Exception as e:
                            logger.exception(f"Error receiving/processing {stream_desc} message: {e}")
                            await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Failed to connect to {stream_desc} stream: {e}. Retrying in 10 seconds...")
                await asyncio.sleep(10)

    async def _process_message_single(self, message, stream_desc):
        """處理來自單一聚合流的訊息"""
        if not self.redis_client:
             logger.warning("Redis client not available, skipping message processing.")
             return
        try:
            payload = json.loads(message)

            if stream_desc == "Spot MiniTicker":
                for ticker in payload:
                    symbol = ticker.get('s')
                    if symbol and symbol.endswith('USDT'):
                        price = ticker.get('c')
                        if price:
                            await self.redis_client.hset(f"prices:{symbol}", key="spot", value=price)

            elif stream_desc == "Futures MarkPrice":
                for ticker in payload:
                    symbol = ticker.get('s')
                    if symbol and symbol.endswith('USDT'):
                        price = ticker.get('p')
                        if price:
                            await self.redis_client.hset(f"prices:{symbol}", key="perp", value=price)

        except json.JSONDecodeError:
            logger.error(f"Failed to decode JSON from {stream_desc}: {message[:100]}")
        except redis.exceptions.RedisError as e: # 捕捉 Redis 操作錯誤
             logger.error(f"Redis error processing message: {e}")
        except Exception as e:
            logger.exception(f"Error processing message from {stream_desc}: {e}")

    async def start(self):
        """啟動服務"""
        await self._connect_redis()
        if self.redis_client:
            await self.run_websocket_listener()
        else:
            logger.critical("Redis connection failed. Service cannot start.")

    async def close(self):
        """關閉 Redis 連線"""
        if self.redis_client:
            # <<< --- 修改：使用 redis_async 的 aclose ---
            await self.redis_client.aclose()
            logger.info("Redis connection closed.")


# --- 主程式執行區塊 ---
async def main():
    """主執行函數"""
    service = DataAggregatorService(redis_url=REDIS_URL)
    try:
        await service.start()
    except asyncio.CancelledError:
        logger.info("Service cancellation requested.")
    except Exception as e:
        logger.exception(f"Critical error in main service loop: {e}")
    finally:
        logger.info("Shutting down service...")
        # 確保 service 實例存在才呼叫 close
        if 'service' in locals() and service:
            await service.close()
        logger.info("Service shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service stopped by user (KeyboardInterrupt).")

# 確保檔案結尾有空行