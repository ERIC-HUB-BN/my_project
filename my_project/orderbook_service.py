# my_project/orderbook_service.py
import asyncio
import json
import logging
import os
import aio_pika
import websockets
import time # <<< --- 確保 time 在這裡 ---
from typing import Optional, Dict, Any, Callable
from decimal import Decimal, InvalidOperation

# --- 環境變數 & 常數 ---
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@127.0.0.1/")
OPPORTUNITY_QUEUE_NAME = "opportunity_queue"
ORDER_REQUEST_QUEUE_NAME = "order_request_queue"
BINANCE_SPOT_WS_BASE = "wss://stream.binance.com:9443/ws"
BINANCE_FUTURES_WS_BASE = "wss://fstream.binance.com/ws"
DEPTH_STREAM_PARAM = "@depth5@100ms"
ARBITRAGE_THRESHOLD = Decimal("0.0035")

# --- 日誌設定 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("OrderBookService")

# --- 主要服務類別 ---
class OrderBookService:
    """
    監聽機會，獲取訂單簿，執行精確條件判斷，並發送交易信號。
    """
    def __init__(self, rabbitmq_url: str):
        self.rabbitmq_url = rabbitmq_url
        self.rabbitmq_connection: Optional[aio_pika.abc.AbstractRobustConnection] = None
        self.rabbitmq_channel: Optional[aio_pika.abc.AbstractChannel] = None
        self.consuming_future: Optional[asyncio.Future] = None
        self.active_symbol_tasks: Dict[str, asyncio.Task] = {}
        self.order_books: Dict[str, Dict[str, Dict[str, list[Optional[Decimal]]]]] = {}

    async def _connect_rabbitmq(self) -> bool:
        """建立 RabbitMQ 連線並宣告所需佇列"""
        try:
            self.rabbitmq_connection = await aio_pika.connect_robust(self.rabbitmq_url)
            self.rabbitmq_channel = await self.rabbitmq_connection.channel()
            await self.rabbitmq_channel.set_qos(prefetch_count=1)
            logger.info(f"成功連接到 RabbitMQ 並設定 QoS: {self.rabbitmq_url}")
            await self.rabbitmq_channel.declare_queue(
                OPPORTUNITY_QUEUE_NAME, durable=True
            )
            logger.info(f"已宣告佇列 (用於接收機會): {OPPORTUNITY_QUEUE_NAME}")
            await self.rabbitmq_channel.declare_queue(
                ORDER_REQUEST_QUEUE_NAME, durable=True
            )
            logger.info(f"已宣告佇列 (用於發送下單): {ORDER_REQUEST_QUEUE_NAME}")
            return True
        except aio_pika.exceptions.AMQPConnectionError as e:
            logger.error(f"連接 RabbitMQ 失敗: {e}")
        except Exception as e:
            logger.exception(f"連接 RabbitMQ 或宣告佇列時發生預期外的錯誤: {e}")
        self.rabbitmq_connection = None
        self.rabbitmq_channel = None
        return False

    def _handle_task_completion(self, symbol: str) -> Callable[[asyncio.Task], None]:
        """建立一個回調函數，用於在某個 symbol 的監聽任務結束時進行清理。"""
        def callback(task: asyncio.Task):
            if symbol in self.active_symbol_tasks:
                del self.active_symbol_tasks[symbol]
            if symbol in self.order_books:
                del self.order_books[symbol]
            try:
                exception = task.exception()
                if exception:
                    logger.error(f"交易對 {symbol} 的監聽任務異常結束: {exception}", exc_info=exception)
                else:
                    logger.info(f"交易對 {symbol} 的監聽任務正常結束。")
            except asyncio.CancelledError:
                logger.info(f"交易對 {symbol} 的監聽任務被取消。")
            except Exception as e:
                 logger.exception(f"處理 {symbol} 任務完成回調時發生錯誤: {e}")
        return callback

    async def _on_message(self, message: aio_pika.abc.AbstractIncomingMessage):
        """處理從 RabbitMQ 收到的機會訊息"""
        async with message.process():
            try:
                body_str = message.body.decode('utf-8')
                data = json.loads(body_str)
                symbol = data.get('symbol')

                if not symbol:
                    logger.warning(f"收到的訊息格式不符，缺少 'symbol': {body_str}")
                    return

                logger.info(f"收到套利機會訊息，交易對: {symbol}")

                if symbol in self.active_symbol_tasks:
                    logger.info(f"交易對 {symbol} 已經在監聽中，忽略重複訊息。")
                else:
                    logger.info(f"為交易對 {symbol} 啟動新的 WebSocket 監聽任務...")
                    symbol_lower = symbol.lower()
                    spot_ws_url = f"{BINANCE_SPOT_WS_BASE}/{symbol_lower}{DEPTH_STREAM_PARAM}"
                    perp_ws_url = f"{BINANCE_FUTURES_WS_BASE}/{symbol_lower}{DEPTH_STREAM_PARAM}"

                    task = asyncio.create_task(
                        self._manage_symbol_websockets(symbol, spot_ws_url, perp_ws_url)
                    )
                    self.active_symbol_tasks[symbol] = task
                    self.order_books[symbol] = {
                        "spot": {"bid": [None, None], "ask": [None, None]},
                        "perp": {"bid": [None, None], "ask": [None, None]}
                    }
                    task.add_done_callback(self._handle_task_completion(symbol))
                    logger.info(f"已為 {symbol} 建立監聽任務並加入活躍列表。")

            except json.JSONDecodeError:
                logger.error(f"無法解析收到的訊息 (非 JSON): {message.body[:100]}", exc_info=True)
            except Exception as e:
                logger.exception(f"處理訊息時發生錯誤: {e}")

    async def _manage_symbol_websockets(self, symbol: str, spot_url: str, perp_url: str):
        """使用 asyncio.gather 同時運行 Spot 和 Perp 的 WebSocket 監聽器。"""
        logger.info(f"[{symbol}] 開始管理 Spot ({spot_url}) 和 Perp ({perp_url}) 的 WebSocket...")
        spot_listener = self._listen_depth_stream(symbol, spot_url, 'spot')
        perp_listener = self._listen_depth_stream(symbol, perp_url, 'perp')
        try:
            await asyncio.gather(spot_listener, perp_listener)
        except asyncio.CancelledError:
            logger.info(f"[{symbol}] 管理任務被取消。")
        except Exception as e:
            logger.exception(f"[{symbol}] 管理任務中發生未預期錯誤: {e}")
        finally:
            logger.info(f"[{symbol}] 結束管理 Spot 和 Perp 的 WebSocket。")

    async def _listen_depth_stream(self, symbol: str, url: str, stream_type: str):
        """連接 WebSocket，接收深度資料，轉換價格，並觸發條件檢查。"""
        while True:
            try:
                logger.info(f"[{symbol}-{stream_type.upper()}] 嘗試連接 WebSocket: {url}")
                connect_params = {"ping_interval": 20, "ping_timeout": 10}
                async with websockets.connect(url, **connect_params) as ws:
                    logger.info(f"[{symbol}-{stream_type.upper()}] WebSocket 連接成功！")
                    while True:
                        try:
                            message = await ws.recv()
                            data = json.loads(message)

                            bids_data = None
                            asks_data = None

                            if stream_type == 'spot':
                                bids_data = data.get('bids', [])
                                asks_data = data.get('asks', [])
                            elif stream_type == 'perp':
                                bids_data = data.get('b', [])
                                asks_data = data.get('a', [])

                            if bids_data and asks_data:
                                try:
                                    best_bid_price_str, best_bid_qty_str = bids_data[0]
                                    best_ask_price_str, best_ask_qty_str = asks_data[0]

                                    best_bid_price = Decimal(best_bid_price_str)
                                    best_bid_qty = Decimal(best_bid_qty_str)
                                    best_ask_price = Decimal(best_ask_price_str)
                                    best_ask_qty = Decimal(best_ask_qty_str)

                                    self.order_books[symbol][stream_type]['bid'] = [best_bid_price, best_bid_qty]
                                    self.order_books[symbol][stream_type]['ask'] = [best_ask_price, best_ask_qty]

                                    logger.debug(f"[{symbol}-{stream_type.upper()}] Updated OB: Bid={best_bid_price}({best_bid_qty}), Ask={best_ask_price}({best_ask_qty})")

                                    await self._check_arbitrage_condition(symbol)

                                except (InvalidOperation, ValueError) as conv_err:
                                    logger.error(f"[{symbol}-{stream_type.upper()}] 轉換價格/數量為 Decimal 時失敗: {conv_err} - Data: {bids_data[0]}, {asks_data[0]}", exc_info=True)
                                except Exception as inner_ex:
                                     logger.exception(f"[{symbol}-{stream_type.upper()}] 更新訂單簿或檢查條件時發生未知錯誤: {inner_ex}")

                            else:
                                logger.warning(f"[{symbol}-{stream_type.upper()}] 收到的深度資料鍵名不符或為空: {list(data.keys())}")

                        except websockets.exceptions.ConnectionClosed as e:
                            logger.warning(f"[{symbol}-{stream_type.upper()}] WebSocket 連線關閉: {e}. 準備重連...")
                            break
                        except json.JSONDecodeError:
                            logger.error(f"[{symbol}-{stream_type.upper()}] 無法解析 WebSocket 訊息 (非 JSON): {message[:100]}", exc_info=True)
                            break
                        except Exception as e:
                            logger.exception(f"[{symbol}-{stream_type.upper()}] 處理 WebSocket 訊息時發生錯誤: {e}")
                            await asyncio.sleep(1)

            except websockets.exceptions.InvalidURI:
                 logger.error(f"[{symbol}-{stream_type.upper()}] 無效的 WebSocket URL: {url}. 這個交易對可能不支援。停止嘗試。")
                 return
            except websockets.exceptions.WebSocketException as e:
                 logger.error(f"[{symbol}-{stream_type.upper()}] 連接 WebSocket 失敗: {e}. 10 秒後重試...")
                 await asyncio.sleep(10)
            except Exception as e:
                 logger.exception(f"[{symbol}-{stream_type.upper()}] WebSocket 連線或處理中發生未預期錯誤: {e}. 10 秒後重試...")
                 await asyncio.sleep(10)

    async def _check_arbitrage_condition(self, symbol: str):
        """檢查指定交易對是否滿足精確的套利開倉條件"""
        if symbol not in self.order_books:
            return

        spot_data = self.order_books[symbol].get("spot", {})
        perp_data = self.order_books[symbol].get("perp", {})

        spot_bid_price, spot_bid_qty = spot_data.get("bid", [None, None])
        perp_ask_price, perp_ask_qty = perp_data.get("ask", [None, None])

        if spot_bid_price is None or perp_ask_price is None or spot_bid_price <= 0:
            return

        if perp_ask_price > spot_bid_price * (Decimal(1) + ARBITRAGE_THRESHOLD):
            logger.info(f"✅ [{symbol}] 發現精確套利機會! Perp Ask ({perp_ask_price}) > Spot Bid ({spot_bid_price}) * {1 + ARBITRAGE_THRESHOLD:.4f}")
            logger.info(f"[{symbol}] 深度檢查暫時跳過。")
            await self._publish_trade_signal(symbol, spot_bid_price, spot_bid_qty, perp_ask_price, perp_ask_qty)
        else:
            pass

    async def _publish_trade_signal(self, symbol: str, spot_bid: Decimal, spot_bid_qty: Decimal, perp_ask: Decimal, perp_ask_qty: Decimal):
        """將開倉交易信號發布到 order_request_queue"""
        if not self.rabbitmq_channel or self.rabbitmq_channel.is_closed:
            logger.error(f"[{symbol}] RabbitMQ channel 不可用，無法發送交易信號。")
            return

        # 使用 time 模組需要先 import time
        current_timestamp = time.time()
        trade_signal = {
            "signal_id": f"trade_{symbol}_{int(current_timestamp*1000)}",
            "symbol": symbol,
            "action": "OPEN",
            "timestamp": current_timestamp,
            "spot_target": {
                "price": str(spot_bid),
                "quantity": str(spot_bid_qty)
            },
            "perp_target": {
                "price": str(perp_ask),
                "quantity": str(perp_ask_qty)
            }
        }
        message_body = json.dumps(trade_signal)

        try:
            message = aio_pika.Message(
                body=message_body.encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT
            )
            await self.rabbitmq_channel.default_exchange.publish(
                message, routing_key=ORDER_REQUEST_QUEUE_NAME
            )
            logger.info(f"🚀 [{symbol}] 已發送開倉交易信號到 {ORDER_REQUEST_QUEUE_NAME}: Action={trade_signal['action']}, Spot Price={spot_bid}, Perp Price={perp_ask}")

            if symbol in self.active_symbol_tasks:
                logger.info(f"[{symbol}] 交易信號已發送，正在停止監聽該交易對...")
                self.active_symbol_tasks[symbol].cancel()

        except Exception as e:
            logger.exception(f"[{symbol}] 發送交易信號到 RabbitMQ 時失敗: {e}")


    async def start_consuming(self):
        """開始監聽 RabbitMQ 的佇列"""
        if not self.rabbitmq_channel:
            logger.error("RabbitMQ channel 未建立，無法開始監聽。")
            return
        try:
            queue = await self.rabbitmq_channel.declare_queue(
                OPPORTUNITY_QUEUE_NAME, durable=True
            )
            logger.info(f"開始監聽佇列: '{OPPORTUNITY_QUEUE_NAME}'...")
            await queue.consume(self._on_message)
            self.consuming_future = asyncio.Future()
            await self.consuming_future
        except aio_pika.exceptions.ChannelClosed as e:
             logger.warning(f"監聽時 Channel 被關閉: {e}. 可能需要重連...")
        except Exception as e:
            logger.exception(f"監聽佇列時發生錯誤: {e}")
        finally:
             logger.info("停止監聽佇列。")
             if self.consuming_future and not self.consuming_future.done():
                 self.consuming_future.set_result(None)


    async def start(self):
        """啟動服務"""
        logger.info("正在啟動 OrderBookService...")
        if await self._connect_rabbitmq():
            await self.start_consuming()
        else:
            logger.critical("無法連接到 RabbitMQ，服務無法啟動。")

    async def close(self):
        """關閉服務和連線，並取消所有活躍的監聽任務"""
        logger.info("正在關閉 OrderBookService...")
        if self.active_symbol_tasks:
            logger.info(f"正在取消 {len(self.active_symbol_tasks)} 個活躍的交易對監聽任務...")
            tasks_to_cancel = list(self.active_symbol_tasks.values())
            symbols_cancelled = list(self.active_symbol_tasks.keys())
            for task in tasks_to_cancel:
                task.cancel()
            results = await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
            for i, result in enumerate(results):
                 if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                     try:
                         symbol = symbols_cancelled[i]
                         logger.error(f"取消 {symbol} 任務時發生錯誤: {result}")
                     except IndexError:
                         logger.error(f"取消任務時發生錯誤 (無法確定 symbol): {result}")
            logger.info("所有活躍的交易對監聽任務已處理完畢。")
        else:
            logger.info("沒有活躍的交易對監聽任務需要取消。")

        if self.consuming_future and not self.consuming_future.done():
             self.consuming_future.cancel()
             try:
                 await self.consuming_future
             except asyncio.CancelledError:
                 logger.info("RabbitMQ 監聽任務已被取消。")

        if self.rabbitmq_channel and not self.rabbitmq_channel.is_closed:
            try:
                await self.rabbitmq_channel.close()
                logger.info("RabbitMQ channel 已關閉。")
            except Exception as e:
                logger.error(f"關閉 RabbitMQ channel 時發生錯誤: {e}")
        if self.rabbitmq_connection and not self.rabbitmq_connection.is_closed:
             try:
                 await self.rabbitmq_connection.close()
                 logger.info("RabbitMQ connection 已關閉。")
             except Exception as e:
                  logger.error(f"關閉 RabbitMQ connection 時發生錯誤: {e}")

        logger.info("OrderBookService 關閉完成。")


# --- 主程式執行區塊 ---
async def main():
    """主執行函數"""
    service = None
    try:
        service = OrderBookService(rabbitmq_url=RABBITMQ_URL)
        await service.start()
    except asyncio.CancelledError:
        logger.info("服務被要求取消。")
    except Exception as e:
        logger.exception(f"主服務迴圈發生嚴重錯誤: {e}")
    finally:
        if service:
            await service.close()

if __name__ == "__main__":
    try:
        logger.info("嘗試直接運行 OrderBookService...")
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("使用者按下 Ctrl+C，服務停止。")