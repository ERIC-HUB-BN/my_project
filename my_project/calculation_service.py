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

# --- 環境變數 & 常數 ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
BINANCE_FUNDING_RATE_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
CALCULATION_INTERVAL_SECONDS = 10
MIN_PRICE_DIFF_THRESHOLD = Decimal("0.0035")

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
    並記錄符合條件的機會。
    """
    def __init__(self, redis_url: str, interval: int):
        self.redis_url = redis_url
        self.interval = interval
        self.redis_client: Optional[redis_async.Redis] = None
        self.http_session: Optional[aiohttp.ClientSession] = None

    async def _connect_redis(self) -> bool:
        """建立 Redis 連線"""
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

    async def _get_all_prices_from_redis(self) -> Dict[str, Dict[str, Decimal]]:
        """從 Redis 獲取所有交易對的 Spot 和 Perp 價格"""
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

                except InvalidOperation:
                    logger.warning(f"Could not convert prices to Decimal for key {key}. Data: {price_data}")
                except IndexError:
                     logger.warning(f"Could not extract symbol from key: {key}")
                except Exception as e:
                    logger.exception(f"Error processing Redis data for key {key}: {e}")

            logger.info(f"Fetched prices for {len(all_prices)} pairs from Redis.")
            return all_prices

        except redis.exceptions.RedisError as e:
            logger.error(f"Redis error fetching prices: {e}")
            return {}
        except TypeError as e:
             logger.error(f"Type error likely processing keys from Redis scan_iter: {e}")
             return {}
        except Exception as e:
            logger.exception(f"Unexpected error fetching prices from Redis: {e}")
            return {}

    def _calculate_opportunities(self, prices: Dict[str, Dict[str, Decimal]]) -> List[Tuple[str, Decimal]]:
        """根據價格計算符合價差條件的機會"""
        opportunities = []
        for symbol, data in prices.items():
            spot = data.get("spot")
            perp = data.get("perp")
            if spot and perp and spot > Decimal(0):
                try:
                    difference = (perp - spot) / spot
                    if difference > MIN_PRICE_DIFF_THRESHOLD:
                        opportunities.append((symbol, difference))
                        logger.debug(f"Potential opportunity: {symbol}, Diff: {difference:.4%}")
                except Exception as e:
                    logger.exception(f"Error calculating difference for {symbol}: {e}")

        opportunities.sort(key=lambda x: x[1], reverse=True)
        return opportunities

    async def _get_funding_rate(self, symbol: str) -> Optional[Decimal]:
        """使用 aiohttp 查詢指定交易對的資金費率"""
        if not self.http_session:
            logger.warning("HTTP session not available. Cannot fetch funding rate.")
            return None

        params = {'symbol': symbol}
        request_url = BINANCE_FUNDING_RATE_URL
        try:
            response = await self.http_session.get(request_url, params=params)
            response.raise_for_status()
            data = await response.json()

            # E701 修正：將 if 和賦值分開
            if isinstance(data, list):
                if not data:
                    return None
                funding_info = data[0]
            elif isinstance(data, dict):        
                
                funding_info = data
            else:
                logger.warning(f"Unexpected funding rate API response format for {symbol}: {data}")
                return None

            funding_rate_str = funding_info.get('lastFundingRate')
            if funding_rate_str:
                try:
                    funding_rate = Decimal(funding_rate_str)
                    logger.debug(f"Fetched funding rate for {symbol}: {funding_rate:.4%}")
                    return funding_rate
                except InvalidOperation:
                    logger.error(f"Could not convert funding rate '{funding_rate_str}' to Decimal for {symbol}")
                    return None
            else:
                logger.warning(f"Could not find 'lastFundingRate' in API response for {symbol}: {funding_info}")
                return None

        except aiohttp.ClientResponseError as e:
             logger.error(
                 f"HTTP error fetching funding rate for {symbol}: Status={e.status}, Message='{e.message}'"
             )
             return None
        except aiohttp.ClientError as e:
             logger.error(f"Connection error fetching funding rate for {symbol}: {e}")
             return None
        except asyncio.TimeoutError:
             logger.error(f"Timeout fetching funding rate for {symbol}")
             return None
        except Exception as e:
             logger.exception(f"Unexpected error fetching funding rate for {symbol}: {e}")
             return None

    async def _run_calculation_cycle(self):
        """執行一次計算循環"""
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

        top_pair, top_diff = opportunities[0]
        logger.info(f"Top potential opportunity: {top_pair} (Diff: {top_diff:.4%})")

        funding_rate = await self._get_funding_rate(top_pair)

        if funding_rate is not None and funding_rate > Decimal(0):
            logger.info(f"--- Validated Opportunity Found ---")
            logger.info(f"    Symbol: {top_pair}")
            logger.info(f"    Price Diff: {top_diff:.4%}")
            logger.info(f"    Funding Rate: {funding_rate:.4%}")
            logger.info(f"---------------------------------")
        elif funding_rate is not None:
             logger.info(f"Opportunity {top_pair} rejected: Funding rate ({funding_rate:.4%}) is not positive.")
        else:
             logger.info(f"Opportunity {top_pair} rejected: Could not fetch funding rate.")

        end_time = time.monotonic()
        logger.info(f"Calculation cycle finished in {end_time - start_time:.2f} seconds.")

    async def start(self):
        """啟動服務的主循環"""
        if not await self._connect_redis():
             logger.critical("Initial Redis connection failed. Service cannot start.")
             return

        self.http_session = aiohttp.ClientSession()
        logger.info("HTTP session created.")

        try:
            while True:
                await self._run_calculation_cycle()
                logger.info(f"Waiting for next cycle ({self.interval} seconds)...")
                await asyncio.sleep(self.interval)
        finally:
            if self.http_session:
                await self.http_session.close()
                logger.info("HTTP session closed.")
            # Redis 連線在 close() 中關閉

    async def close(self):
        """關閉 Redis 連線 (如果有的話)"""
        if self.redis_client:
            await self.redis_client.aclose()
            logger.info("Redis connection closed by close() method.")


# --- 主程式執行區塊 ---
async def main():
    """主執行函數"""
    service = None
    try:
        service = CalculationService(
            redis_url=REDIS_URL,
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
            await service.close()
        logger.info("Calculation service shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Calculation service stopped by user (KeyboardInterrupt).")

# 確保檔案結尾有空行