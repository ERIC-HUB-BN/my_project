# my_project/calculation_service.py
import asyncio
import json
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any, List, Tuple

import aiohttp
import redis # 用於 exceptions
import redis.asyncio as redis_async
import aio_pika

# --- 環境變數 & 常數 ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@127.0.0.1/")
BINANCE_FUNDING_RATE_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
CALCULATION_INTERVAL_SECONDS = 10
MIN_PRICE_DIFF_THRESHOLD = Decimal("0.0035")
OPPORTUNITY_QUEUE_NAME = "opportunity_queue"
# --- 新增：黑名單 Redis Key ---
BLACKLIST_KEY = "blacklist:manual"

# --- 日誌設定 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("CalculationService")

# --- 主要服務類別 ---
class CalculationService:
    """
    負責從 Redis 讀取價格，計算套利機會，查詢資金費率，
    檢查黑名單，並將符合條件的機會發布到 RabbitMQ。
    """
    def __init__(self, redis_url: str, rabbitmq_url: str, interval: int):
        self.redis_url = redis_url
        self.rabbitmq_url = rabbitmq_url
        self.interval = interval
        self.redis_client: Optional[redis_async.Redis] = None
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.rabbitmq_connection: Optional[aio_pika.abc.AbstractRobustConnection] = None
        self.rabbitmq_channel: Optional[aio_pika.abc.AbstractChannel] = None

    async def _connect_redis(self) -> bool:
        """建立 Redis 連線"""
        # (程式碼不變)
        try:
            self.redis_client = redis_async.Redis.from_url(
                self.redis_url, decode_responses=True
            )
            await self.redis_client.ping()
            logger.info(f"Successfully connected to Redis at {self.redis_url}")
            return True
        except redis.exceptions.ConnectionError as e:
            logger.error(f"Failed to connect to Redis: {e}")
        except redis.exceptions.RedisError as e:
            logger.error(f"Redis error during connection: {e}")
        except Exception as e:
             logger.exception(f"Unexpected error connecting to Redis: {e}")
        self.redis_client = None
        return False

    async def _connect_rabbitmq(self) -> bool:
        """建立 RabbitMQ 連線並宣告佇列"""
        # (程式碼不變)
        try:
            self.rabbitmq_connection = await aio_pika.connect_robust(self.rabbitmq_url)
            self.rabbitmq_channel = await self.rabbitmq_connection.channel()
            await self.rabbitmq_channel.declare_queue(
                OPPORTUNITY_QUEUE_NAME, durable=True
            )
            logger.info(f"Successfully connected to RabbitMQ and declared queue '{OPPORTUNITY_QUEUE_NAME}'")
            return True
        except aio_pika.exceptions.AMQPConnectionError as e:
            logger.error(f"Failed to connect to RabbitMQ: {e}")
        except Exception as e:
            logger.exception(f"Unexpected error connecting to RabbitMQ: {e}")
        self.rabbitmq_connection = None
        self.rabbitmq_channel = None
        return False

    async def _get_all_prices_from_redis(self) -> Dict[str, Dict[str, Decimal]]:
        """從 Redis 獲取所有交易對的 Spot 和 Perp 價格"""
        # (程式碼不變)
        if not self.redis_client:
            logger.warning("Redis client not available. Cannot fetch prices.")
            return {}
        all_prices: Dict[str, Dict[str, Decimal]] = {}
        try:
            async for key in self.redis_client.scan_iter("prices:*"):
                try:
                    symbol = key.split(":", 1)[1]
                    price_data = await self.redis_client.hgetall(key)
                    spot_price_str = price_data.get("spot")
                    perp_price_str = price_data.get("perp")
                    if spot_price_str and perp_price_str:
                         spot_price = Decimal(spot_price_str)
                         perp_price = Decimal(perp_price_str)
                         if spot_price > 0 and perp_price > 0:
                             all_prices[symbol] = {"spot": spot_price, "perp": perp_price}
                except (InvalidOperation, IndexError, Exception) as e:
                    logger.warning(f"Error processing key {key}: {e}")
            logger.info(f"Fetched prices for {len(all_prices)} pairs from Redis.")
        except redis.exceptions.RedisError as e:
            logger.error(f"Redis error during scan_iter: {e}")
        except Exception as e:
            logger.exception(f"Unexpected error fetching prices from Redis: {e}")
        return all_prices


    def _calculate_opportunities(self, prices: Dict[str, Dict[str, Decimal]]) -> List[Tuple[str, Decimal]]:
        """根據價格計算符合價差條件的機會"""
        # (程式碼不變)
        opportunities = []
        for symbol, data in prices.items():
            spot = data.get("spot")
            perp = data.get("perp")
            if spot and perp and spot > Decimal(0):
                try:
                    difference = (perp - spot) / spot
                    if difference > MIN_PRICE_DIFF_THRESHOLD:
                        opportunities.append((symbol, difference))
                except Exception as e:
                    logger.exception(f"Error calculating difference for {symbol}: {e}")
        opportunities.sort(key=lambda x: x[1], reverse=True)
        return opportunities

    # --- 新增：檢查是否在黑名單 ---
    async def _is_symbol_blacklisted(self, symbol: str) -> bool:
        """檢查指定的 Symbol 是否在 Redis 黑名單中"""
        if not self.redis_client:
            logger.warning("Redis client not available. Cannot check blacklist.")
            return False # 預設為 False，避免 Redis 故障時擋住所有交易
        try:
            is_member = await self.redis_client.sismember(BLACKLIST_KEY, symbol)
            return is_member
        except redis.exceptions.RedisError as e:
            logger.error(f"Redis error checking blacklist for {symbol}: {e}")
            return False # 出錯時也預設為 False
        except Exception as e:
            logger.exception(f"Unexpected error checking blacklist for {symbol}: {e}")
            return False

    async def _get_funding_rate(self, symbol: str) -> Optional[Decimal]:
        """使用 aiohttp 查詢指定交易對的資金費率"""
        # (程式碼不變)
        if not self.http_session:
            logger.warning("HTTP session not available. Cannot fetch funding rate.")
            return None
        params = {'symbol': symbol}
        request_url = BINANCE_FUNDING_RATE_URL
        try:
            async with self.http_session.get(request_url, params=params, timeout=5) as response: # 加入 timeout
                response.raise_for_status()
                data = await response.json()
                funding_info = None
                if isinstance(data, list):
                    if data: funding_info = data[0]
                elif isinstance(data, dict):
                    funding_info = data
                if not funding_info:
                     logger.warning(f"Unexpected or empty funding rate API response for {symbol}: {data}")
                     return None

                funding_rate_str = funding_info.get('lastFundingRate')
                if funding_rate_str:
                    try:
                        funding_rate = Decimal(funding_rate_str)
                        logger.debug(f"Fetched funding rate for {symbol}: {funding_rate:.4%}")
                        return funding_rate
                    except InvalidOperation:
                        logger.error(f"Could not convert funding rate '{funding_rate_str}' to Decimal for {symbol}")
                else:
                    logger.warning(f"Could not find 'lastFundingRate' in API response for {symbol}: {funding_info}")
        except aiohttp.ClientResponseError as e:
             logger.error(f"HTTP error fetching funding rate for {symbol}: Status {e.status}, Message: {e.message}")
        except asyncio.TimeoutError:
             logger.error(f"Timeout error fetching funding rate for {symbol}")
        except aiohttp.ClientError as e: # 更廣泛的 Client 錯誤
             logger.error(f"Client error fetching funding rate for {symbol}: {e}")
        except Exception as e:
            logger.exception(f"Error fetching or processing funding rate for {symbol}: {e}")
        return None

    async def _publish_opportunity(self, symbol: str, difference: Decimal, funding_rate: Decimal):
        """將驗證過的機會發布到 RabbitMQ"""
        # (程式碼不變)
        if not self.rabbitmq_channel or self.rabbitmq_channel.is_closed:
            logger.error("RabbitMQ channel not available. Cannot publish opportunity.")
            return

        message_body = json.dumps({
            "symbol": symbol,
            "price_difference_percent": f"{difference:.4%}",
            "funding_rate_percent": f"{funding_rate:.4%}",
            "timestamp": time.time()
        })

        try:
            message = aio_pika.Message(
                body=message_body.encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT
            )
            await self.rabbitmq_channel.default_exchange.publish(
                message, routing_key=OPPORTUNITY_QUEUE_NAME
            )
            logger.info(f"Successfully published opportunity for {symbol} to RabbitMQ.")
        except Exception as e:
            logger.exception(f"Failed to publish opportunity for {symbol} to RabbitMQ: {e}")

    # --- 修改：加入黑名單檢查流程 ---
    async def _run_calculation_cycle(self):
        """執行一次計算循環，包含黑名單檢查"""
        start_time = time.monotonic()
        logger.info("Starting calculation cycle...")

        prices = await self._get_all_prices_from_redis()
        if not prices:
            logger.warning("No prices fetched from Redis, skipping cycle.")
            return

        opportunities = self._calculate_opportunities(prices)
        if not opportunities:
            logger.info("No potential opportunities found meeting price difference criteria.")
            return

        # 目前策略只處理價差最大的機會
        top_pair, top_diff = opportunities[0]
        logger.info(f"Top potential opportunity: {top_pair} (Diff: {top_diff:.4%})")

        # --- 新增：在查詢資金費率前，先檢查黑名單 ---
        is_blacklisted = await self._is_symbol_blacklisted(top_pair)
        if is_blacklisted:
            logger.info(f"Symbol {top_pair} is in the blacklist, skipping.")
            return # 直接結束這次循環，不處理這個幣種
        # --- 新增結束 ---

        # 如果不在黑名單，才繼續查詢資金費率
        funding_rate = await self._get_funding_rate(top_pair)

        if funding_rate is not None and funding_rate > Decimal(0):
            # 機會驗證通過！
            log_message = (
                f"Validated Opportunity: Symbol={top_pair}, "
                f"Diff={top_diff:.4%}, Funding Rate={funding_rate:.4%}"
            )
            logger.info(log_message)
            await self._publish_opportunity(top_pair, top_diff, funding_rate)
        elif funding_rate is not None:
             logger.info(f"Opportunity {top_pair} rejected: Funding rate ({funding_rate:.4%}) is not positive.")
        else:
             logger.info(f"Opportunity {top_pair} rejected: Could not fetch funding rate.")

        end_time = time.monotonic()
        logger.info(f"Calculation cycle finished in {end_time - start_time:.2f} seconds.")
    # --- 修改結束 ---

    async def start(self):
        """啟動服務的主循環"""
        # (程式碼不變)
        if not await self._connect_redis() or not await self._connect_rabbitmq():
             logger.critical("Initial Redis or RabbitMQ connection failed. Service cannot start.")
             await self.close()
             return

        self.http_session = aiohttp.ClientSession()
        logger.info("HTTP session created.")

        try:
            while True:
                await self._run_calculation_cycle()
                logger.info(f"Waiting for next cycle ({self.interval} seconds)...")
                await asyncio.sleep(self.interval)
        finally:
            await self.close() # 確保資源被關閉

    async def close(self):
        """關閉 Redis, RabbitMQ 和 HTTP Session 連線"""
        # (程式碼不變，但加入關閉 HTTP Session)
        if self.http_session and not self.http_session.closed: # 檢查 http_session 是否存在且未關閉
             try:
                 await self.http_session.close()
                 logger.info("HTTP session closed.")
             except Exception as e:
                  logger.error(f"Error closing HTTP session: {e}")
        if self.rabbitmq_channel:
            try:
                await self.rabbitmq_channel.close()
                logger.info("RabbitMQ channel closed.")
            except Exception as e:
                logger.error(f"Error closing RabbitMQ channel: {e}")
        if self.rabbitmq_connection:
             try:
                 await self.rabbitmq_connection.close()
                 logger.info("RabbitMQ connection closed.")
             except Exception as e:
                  logger.error(f"Error closing RabbitMQ connection: {e}")
        if self.redis_client:
            try:
                await self.redis_client.aclose()
                logger.info("Redis connection closed.")
            except Exception as e:
                 logger.error(f"Error closing Redis connection: {e}")


# --- 主程式執行區塊 ---
async def main():
    """主執行函數"""
    # (程式碼不變)
    service = None
    try:
        service = CalculationService(
            redis_url=REDIS_URL,
            rabbitmq_url=RABBITMQ_URL,
            interval=CALCULATION_INTERVAL_SECONDS
        )
        await service.start()
    except ConnectionError as e:
         logger.critical(f"Service start failed due to connection error: {e}")
    except asyncio.CancelledError:
        logger.info("Service cancellation requested.")
    except Exception as e:
        logger.exception(f"Critical error in main service loop: {e}")
    finally:
        logger.info("Shutting down calculation service...")
        if 'service' in locals() and service:
            await service.close() # 在 finally 中確保 close 被呼叫
        logger.info("Calculation service shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Calculation service stopped by user (KeyboardInterrupt).")

# 確保檔案結尾有空行