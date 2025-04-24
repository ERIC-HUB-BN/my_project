# my_project/data_aggregator_service.py
import asyncio
import json
import logging
import os
import websockets
import redis  # 用於 exceptions
import redis.asyncio as redis_async # 保持別名
import aiohttp
from typing import Optional, Dict, Any, List, Tuple, Set # <<< 修正：加入 Set
from collections import defaultdict # <<< 修正：加入 defaultdict

# --- 環境變數 ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379") # Keep 127.0.0.1 from previous fix
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
        self._websocket_listener_tasks: List[asyncio.Task] = []

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
        if not self.http_session or self.http_session.closed:
            logger.warning("HTTP session not available or closed. Cannot fetch.")
            return None
        try:
            async with self.http_session.get(url, timeout=10) as response:
                response.raise_for_status()
                data = await response.json()
                return data
        except aiohttp.ClientResponseError as e:
            logger.error(f"HTTP error fetching {url}: {e.status} {e.message}")
        except aiohttp.ClientConnectionError as e:
             logger.error(f"Connection error fetching {url}: {e}")
        except asyncio.TimeoutError:
             logger.error(f"Timeout error fetching {url}")
        except Exception as e:
            logger.exception(f"Unexpected error fetching {url}: {e}")
        return None

    async def _update_whitelist(self):
        """獲取 Spot 和 Futures 交易對資訊，更新 Redis 和記憶體白名單"""
        if not self.redis_client or not self.http_session:
            logger.warning("Dependencies not available. Skipping whitelist update.")
            return

        logger.info("Starting whitelist update...")
        # 使用 asyncio.gather 同時獲取 Spot 和 Futures 資訊
        results = await asyncio.gather(
            self._fetch_exchange_info(BINANCE_SPOT_EXCHANGE_INFO_URL),
            self._fetch_exchange_info(BINANCE_FUTURES_EXCHANGE_INFO_URL),
            return_exceptions=True # 即使某個 API 失敗，也能繼續處理另一個
        )
        spot_info = results[0] if isinstance(results[0], dict) else None
        futures_info = results[1] if isinstance(results[1], dict) else None

        new_spot_whitelist = set()
        if spot_info and 'symbols' in spot_info:
            for symbol_data in spot_info['symbols']:
                # --- Flake8 E129 Fix (Line ~108): 增加縮排 ---
                if (symbol_data.get('status') == 'TRADING' and
                        symbol_data.get('quoteAsset') == 'USDT'):
                    new_spot_whitelist.add(symbol_data.get('symbol'))
            logger.info(f"Found {len(new_spot_whitelist)} valid Spot pairs.")
        else:
             logger.warning(f"Could not get valid Spot info: {results[0]}")

        new_perp_whitelist = set()
        if futures_info and 'symbols' in futures_info:
            for symbol_data in futures_info['symbols']:
                # --- Flake8 E129 Fix (Line ~119): 增加縮排 ---
                if (symbol_data.get('contractType') == 'PERPETUAL' and
                        symbol_data.get('status') == 'TRADING' and
                        symbol_data.get('quoteAsset') == 'USDT'):
                    new_perp_whitelist.add(symbol_data.get('symbol'))
            logger.info(f"Found {len(new_perp_whitelist)} valid Perp pairs.")
        else:
            logger.warning(f"Could not get valid Futures info: {results[1]}")

        # 使用 Pipeline 更新 Redis
        pipe = self.redis_client.pipeline(transaction=True)
        try:
            pipe.delete(SPOT_WHITELIST_KEY)
            if new_spot_whitelist:
                pipe.sadd(SPOT_WHITELIST_KEY, *new_spot_whitelist)
            pipe.delete(PERP_WHITELIST_KEY)
            if new_perp_whitelist:
                pipe.sadd(PERP_WHITELIST_KEY, *new_perp_whitelist)
            await pipe.execute()

            # 更新記憶體快取
            self._spot_whitelist_cache = new_spot_whitelist
            self._perp_whitelist_cache = new_perp_whitelist
            logger.info(f"Whitelist updated. Spot: {len(self._spot_whitelist_cache)}, Perp: {len(self._perp_whitelist_cache)}")

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
        logger.info(f"Whitelist updater started. Interval: {WHITELIST_UPDATE_INTERVAL_SECONDS}s.")
        while True:
            try:
                await self._update_whitelist()
                await asyncio.sleep(WHITELIST_UPDATE_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                logger.info("Whitelist updater task cancelled.")
                break
            except Exception as e:
                logger.exception(f"Error in scheduled whitelist update: {e}")
                await asyncio.sleep(60)

    async def _write_price_to_redis(self, symbol: str, field: str, price: Any):
        """檢查白名單後寫入 Redis Hash"""
        if not self.redis_client: return
        is_valid = symbol in self._spot_whitelist_cache if field == 'spot' else (symbol in self._perp_whitelist_cache if field == 'perp' else False)
        if not is_valid: return
        try: await self.redis_client.hset(f"prices:{symbol}", key=field, value=price)
        except redis.exceptions.ConnectionError: # 處理可能的連線中斷
             logger.error(f"Redis connection error writing price for {symbol}. Attempting reconnect?")
             # 這裡可以加入重連邏輯，但暫時先只記錄錯誤
        except redis.exceptions.RedisError as e: logger.error(f"Redis error writing price for {symbol}: {e}")
        except Exception as e: logger.exception(f"Unexpected error writing price for {symbol}: {e}")

    async def _process_spot_ticker(self, ticker: Dict[str, Any]):
        symbol, price = ticker.get('s'), ticker.get('c')
        if symbol and price: await self._write_price_to_redis(symbol, "spot", price)

    async def _process_perp_ticker(self, ticker: Dict[str, Any]):
        symbol, price = ticker.get('s'), ticker.get('p')
        if symbol and price: await self._write_price_to_redis(symbol, "perp", price)

    async def _process_message_single(self, message: str, stream_desc: str):
        """處理來自單一 WebSocket 流的訊息"""
        if not self.redis_client: return
        try:
            payload = json.loads(message)
            if not isinstance(payload, list): return
            process_func = self._process_spot_ticker if stream_desc == "Spot MiniTicker" else (self._process_perp_ticker if stream_desc == "Futures MarkPrice" else None)
            if not process_func: return
            # 異步處理 payload 中的每個 ticker
            tasks = [process_func(ticker) for ticker in payload if isinstance(ticker, dict)]
            if tasks: await asyncio.gather(*tasks)
        except json.JSONDecodeError: logger.error(f"JSON decode error from {stream_desc}: {message[:100]}")
        except Exception as e: logger.exception(f"Error processing message batch from {stream_desc}: {e}")

    async def _listen_single_stream(self, url: str, stream_desc: str):
        """監聽單一 WebSocket Stream，含重連"""
        while True:
            try:
                logger.info(f"Connecting to {stream_desc} at {url}...")
                connect_params = {"ping_interval": 20, "ping_timeout": 10}
                async with websockets.connect(url, **connect_params) as ws:
                    logger.info(f"Connected to {stream_desc}.")
                    while True: await self._process_message_single(await ws.recv(), stream_desc)
            except websockets.exceptions.ConnectionClosed as e: logger.warning(f"{stream_desc} connection closed: {e}. Reconnecting...")
            except asyncio.CancelledError: logger.info(f"{stream_desc} listener task cancelled."); break
            except Exception as e: logger.error(f"Error in {stream_desc} listener: {e}. Retrying in 10s..."); await asyncio.sleep(10)

    async def run_websocket_listener(self):
        """啟動並管理 WebSocket 監聽任務"""
        if not self.redis_client: return
        spot_url = f"{BINANCE_SPOT_WS_BASE}/ws/!miniTicker@arr"
        futures_url = f"{BINANCE_FUTURES_WS_BASE}/ws/!markPrice@arr@1s"
        logger.info("Starting WebSocket listeners...")
        task1 = asyncio.create_task(self._listen_single_stream(spot_url, "Spot MiniTicker"), name="SpotListener")
        task2 = asyncio.create_task(self._listen_single_stream(futures_url, "Futures MarkPrice"), name="FuturesListener")
        self._websocket_listener_tasks.clear() # 清空舊的 task
        self._websocket_listener_tasks.extend([task1, task2])
        try:
            await asyncio.gather(*self._websocket_listener_tasks) # 等待所有監聽器結束
        except asyncio.CancelledError:
            logger.info("WebSocket listeners gather cancelled.")
        finally:
             # 更新列表，移除已完成/取消的 task
             self._websocket_listener_tasks = [t for t in self._websocket_listener_tasks if not t.done()]


    async def start(self):
        """啟動服務"""
        await self._connect_redis()
        if not self.redis_client: raise ConnectionError("Redis connect failed.")

        self.http_session = aiohttp.ClientSession()
        logger.info("HTTP session created.")
        logger.info("Performing initial whitelist update...")
        await self._update_whitelist()
        logger.info("Initial whitelist update finished.")

        self._whitelist_update_task = asyncio.create_task(self._run_whitelist_updater(), name="WhitelistUpdater")
        # run_websocket_listener 會阻塞，直到其內部的 gather 結束
        await self.run_websocket_listener()

    async def close(self):
        """關閉服務"""
        logger.info("Shutting down DataAggregatorService...")
        tasks_to_cancel: List[Optional[asyncio.Task]] = []
        if self._whitelist_update_task and not self._whitelist_update_task.done():
            tasks_to_cancel.append(self._whitelist_update_task)
        tasks_to_cancel.extend([t for t in self._websocket_listener_tasks if t and not t.done()])

        if tasks_to_cancel:
             logger.info(f"Cancelling {len(tasks_to_cancel)} background tasks...")
             for task in tasks_to_cancel:
                 if task: task.cancel() # 確認 task 不是 None
             # 等待取消完成
             await asyncio.gather(*[t for t in tasks_to_cancel if t], return_exceptions=True)
             logger.info("Background tasks cancelled.")

        if self.http_session and not self.http_session.closed:
             await self.http_session.close()
             logger.info("HTTP session closed.")
        if self.redis_client:
            await self.redis_client.aclose()
            logger.info("Redis connection closed.")
        logger.info("DataAggregatorService shutdown complete.")

async def main():
    service = None
    try:
        service = DataAggregatorService(redis_url=REDIS_URL)
        await service.start()
    except asyncio.CancelledError: logger.info("Main service loop cancelled.")
    except ConnectionError as e: logger.critical(f"Service start failed: {e}")
    except Exception as e: logger.exception(f"Critical error in main loop: {e}")
    finally:
        logger.info("Initiating service shutdown...")
        if service: await service.close()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: logger.info("Service stopped by user.")

# 確保檔案結尾有空行