# my_project/data_aggregator_service.py
import asyncio
import json
import logging
import os
import websockets
import redis  # 用於 exceptions
import redis.asyncio as redis_async # 保持別名
import aiohttp
from typing import Optional, Dict, Any, List, Tuple, Set
from collections import defaultdict

# --- 環境變數 ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")
BINANCE_FUTURES_WS_BASE = "wss://fstream.binance.com"
BINANCE_SPOT_WS_BASE = "wss://stream.binance.com:9443"
BINANCE_SPOT_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
BINANCE_FUTURES_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
WHITELIST_UPDATE_INTERVAL_SECONDS = 3600
SPOT_WHITELIST_KEY = "valid_symbols:spot"
PERP_WHITELIST_KEY = "valid_symbols:perp"

# --- 日誌設定 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("DataAggregatorService")

# --- 主要服務類別 ---
class DataAggregatorService:
    """
    負責連接幣安全市場數據流 (Spot Mini Ticker, Futures Mark Price),
    解析數據, 過濾無效交易對後, 將 Spot 最新價 和 Perp 標記價 寫入 Redis.
    同時定期更新有效的交易對白名單。
    """
    def __init__(self, redis_url):
        self.redis_url = redis_url
        self.redis_client: Optional[redis_async.Redis] = None
        self.http_session: Optional[aiohttp.ClientSession] = None
        self._whitelist_update_task: Optional[asyncio.Task] = None
        self._spot_whitelist_cache: Set[str] = set()
        self._perp_whitelist_cache: Set[str] = set()
        self._websocket_listener_tasks: List[asyncio.Task] = [] # 用來追蹤 WS 任務

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

    async def _fetch_exchange_info(self, url: str) -> Optional[Dict[str, Any]]:
        """使用 aiohttp 呼叫 ExchangeInfo API"""
        if not self.http_session:
            logger.warning("HTTP session not available. Cannot fetch exchange info.")
            return None
        try:
            # 確保 http_session 存在且未關閉
            if self.http_session.closed:
                 logger.warning("HTTP session is closed. Cannot fetch exchange info.")
                 # 可以選擇重新建立 session 或直接返回 None
                 # self.http_session = aiohttp.ClientSession() # 或重新建立
                 return None

            async with self.http_session.get(url, timeout=10) as response:
                response.raise_for_status()
                data = await response.json()
                # logger.info(f"Successfully fetched exchange info from {url}") # Log 太頻繁，先註解
                return data
        except aiohttp.ClientResponseError as e:
            logger.error(f"HTTP error fetching exchange info from {url}: {e.status} {e.message}")
        except aiohttp.ClientConnectionError as e:
             logger.error(f"Connection error fetching exchange info from {url}: {e}")
        except asyncio.TimeoutError:
             logger.error(f"Timeout error fetching exchange info from {url}")
        except Exception as e:
            logger.exception(f"Unexpected error fetching exchange info from {url}: {e}")
        return None

    async def _update_whitelist(self):
        """獲取 Spot 和 Futures 的交易對資訊，並更新 Redis 和記憶體中的白名單"""
        if not self.redis_client or not self.http_session:
            logger.warning("Redis client or HTTP session not available. Skipping whitelist update.")
            return

        logger.info("Starting whitelist update...")
        spot_info = await self._fetch_exchange_info(BINANCE_SPOT_EXCHANGE_INFO_URL)
        futures_info = await self._fetch_exchange_info(BINANCE_FUTURES_EXCHANGE_INFO_URL)

        new_spot_whitelist = set()
        if spot_info and 'symbols' in spot_info:
            for symbol_data in spot_info['symbols']:
                if (symbol_data.get('status') == 'TRADING' and
                    symbol_data.get('quoteAsset') == 'USDT'):
                     new_spot_whitelist.add(symbol_data.get('symbol'))
            logger.info(f"Found {len(new_spot_whitelist)} valid Spot trading pairs.")
        else:
             logger.warning("Could not get valid Spot exchange info.")

        new_perp_whitelist = set()
        if futures_info and 'symbols' in futures_info:
            for symbol_data in futures_info['symbols']:
                if (symbol_data.get('contractType') == 'PERPETUAL' and
                    symbol_data.get('status') == 'TRADING' and
                    symbol_data.get('quoteAsset') == 'USDT'):
                     new_perp_whitelist.add(symbol_data.get('symbol'))
            logger.info(f"Found {len(new_perp_whitelist)} valid Perp trading pairs.")
        else:
            logger.warning("Could not get valid Futures exchange info.")

        pipe = self.redis_client.pipeline(transaction=True)
        try:
            pipe.delete(SPOT_WHITELIST_KEY)
            if new_spot_whitelist:
                pipe.sadd(SPOT_WHITELIST_KEY, *new_spot_whitelist)
            pipe.delete(PERP_WHITELIST_KEY)
            if new_perp_whitelist:
                pipe.sadd(PERP_WHITELIST_KEY, *new_perp_whitelist)
            await pipe.execute()

            self._spot_whitelist_cache = new_spot_whitelist
            self._perp_whitelist_cache = new_perp_whitelist
            logger.info(f"Whitelist updated in Redis and memory cache. Spot: {len(self._spot_whitelist_cache)}, Perp: {len(self._perp_whitelist_cache)}")

        except redis.exceptions.RedisError as e:
            logger.error(f"Redis error updating whitelist: {e}")
            self._spot_whitelist_cache = set()
            self._perp_whitelist_cache = set()
        except Exception as e:
             logger.exception(f"Unexpected error updating whitelist: {e}")
             self._spot_whitelist_cache = set()
             self._perp_whitelist_cache = set()

    async def _run_whitelist_updater(self):
        """定期執行白名單更新"""
        logger.info(f"Whitelist updater started. Update interval: {WHITELIST_UPDATE_INTERVAL_SECONDS} seconds.")
        while True:
            try:
                await self._update_whitelist()
                await asyncio.sleep(WHITELIST_UPDATE_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                logger.info("Whitelist updater task cancelled.")
                break # 被取消時跳出迴圈
            except Exception as e:
                logger.exception(f"Error during scheduled whitelist update: {e}")
                await asyncio.sleep(60) # 出錯時，稍後再試

    async def _write_price_to_redis(self, symbol: str, field: str, price: Any):
        """將價格寫入 Redis Hash (在寫入前檢查白名單)"""
        if not self.redis_client: return
        is_valid = symbol in self._spot_whitelist_cache if field == 'spot' else (symbol in self._perp_whitelist_cache if field == 'perp' else False)
        if not is_valid: return
        try: await self.redis_client.hset(f"prices:{symbol}", key=field, value=price)
        except redis.exceptions.RedisError as e: logger.error(f"Redis error writing price for {symbol}: {e}")
        except Exception as e: logger.exception(f"Unexpected error writing price for {symbol}: {e}")

    async def _process_spot_ticker(self, ticker: Dict[str, Any]):
        symbol, price = ticker.get('s'), ticker.get('c')
        if symbol and price: await self._write_price_to_redis(symbol, "spot", price)

    async def _process_perp_ticker(self, ticker: Dict[str, Any]):
        symbol, price = ticker.get('s'), ticker.get('p')
        if symbol and price: await self._write_price_to_redis(symbol, "perp", price)

    async def _process_message_single(self, message: str, stream_desc: str):
        if not self.redis_client: return
        try:
            payload = json.loads(message)
            if not isinstance(payload, list): return
            process_func = self._process_spot_ticker if stream_desc == "Spot MiniTicker" else (self._process_perp_ticker if stream_desc == "Futures MarkPrice" else None)
            if not process_func: return
            tasks = [process_func(ticker) for ticker in payload if isinstance(ticker, dict)]
            if tasks: await asyncio.gather(*tasks)
        except json.JSONDecodeError: logger.error(f"Failed to decode JSON from {stream_desc}: {message[:100]}")
        except Exception as e: logger.exception(f"Error processing message batch from {stream_desc}: {e}")

    async def _listen_single_stream(self, url: str, stream_desc: str):
        while True:
            try:
                logger.info(f"Connecting to {stream_desc} stream at {url}...")
                connect_params = {"ping_interval": 20, "ping_timeout": 10}
                async with websockets.connect(url, **connect_params) as ws:
                    logger.info(f"Successfully connected to {stream_desc} stream.")
                    while True: await self._process_message_single(await ws.recv(), stream_desc)
            except websockets.exceptions.ConnectionClosed as e: logger.warning(f"{stream_desc} connection closed: {e}. Reconnecting...")
            except asyncio.CancelledError: logger.info(f"{stream_desc} listener task cancelled."); break
            except Exception as e: logger.error(f"Error in {stream_desc} listener: {e}. Retrying in 10s..."); await asyncio.sleep(10)

    async def run_websocket_listener(self):
        if not self.redis_client: return
        spot_url = f"{BINANCE_SPOT_WS_BASE}/ws/!miniTicker@arr"
        futures_url = f"{BINANCE_FUTURES_WS_BASE}/ws/!markPrice@arr@1s"
        logger.info("Starting WebSocket listeners...")
        # 把任務加到列表，方便 close 時取消
        task1 = asyncio.create_task(self._listen_single_stream(spot_url, "Spot MiniTicker"), name="SpotListener")
        task2 = asyncio.create_task(self._listen_single_stream(futures_url, "Futures MarkPrice"), name="FuturesListener")
        self._websocket_listener_tasks.extend([task1, task2])
        try:
            await asyncio.gather(task1, task2) # 持續運行
        except asyncio.CancelledError:
            logger.info("WebSocket listeners gather cancelled.")
        finally:
             # 從列表中移除已完成的任務
             self._websocket_listener_tasks = [t for t in self._websocket_listener_tasks if t not in (task1, task2)]


    async def start(self):
        """啟動服務"""
        await self._connect_redis()
        if not self.redis_client: raise ConnectionError("Redis connection failed. Service cannot start.")

        self.http_session = aiohttp.ClientSession()
        logger.info("HTTP session created.")
        logger.info("Performing initial whitelist update...")
        await self._update_whitelist()
        logger.info("Initial whitelist update finished.")

        # --- 簡化：只創建任務，讓它們在背景跑 ---
        self._whitelist_update_task = asyncio.create_task(self._run_whitelist_updater(), name="WhitelistUpdater")
        # WebSocket listener 會在 run_websocket_listener 內部創建並添加到 self._websocket_listener_tasks
        await self.run_websocket_listener() # 直接 await 這個函數，它內部會 gather

    async def close(self):
        """關閉服務"""
        logger.info("Shutting down DataAggregatorService...")

        # 1. 取消背景任務 (Updater + WS Listeners)
        tasks_to_cancel = []
        if self._whitelist_update_task and not self._whitelist_update_task.done():
            tasks_to_cancel.append(self._whitelist_update_task)
        tasks_to_cancel.extend([t for t in self._websocket_listener_tasks if not t.done()])

        if tasks_to_cancel:
             logger.info(f"Cancelling {len(tasks_to_cancel)} background tasks...")
             for task in tasks_to_cancel:
                 task.cancel()
             # 等待取消完成，忽略 CancelledError
             await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
             logger.info("Background tasks cancelled.")

        # 2. 關閉 HTTP Session
        if self.http_session and not self.http_session.closed:
             await self.http_session.close()
             logger.info("HTTP session closed.")

        # 3. 關閉 Redis 連線
        if self.redis_client:
            await self.redis_client.aclose()
            logger.info("Redis connection closed.")

        logger.info("DataAggregatorService shutdown complete.")

async def main():
    """主執行函數"""
    service = None
    try:
        service = DataAggregatorService(redis_url=REDIS_URL)
        await service.start()
    except asyncio.CancelledError:
        logger.info("Main service loop cancelled.")
    except ConnectionError as e:
         logger.critical(f"Service start failed: {e}")
    except Exception as e:
        logger.exception(f"Critical error in main service loop: {e}")
    finally:
        logger.info("Initiating service shutdown process...")
        if service: await service.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Service stopped by user (KeyboardInterrupt).")

# 確保檔案結尾有空行