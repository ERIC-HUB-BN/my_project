# my_project/orderbook_service.py
import asyncio
import json # 用來處理 JSON 格式的訊息
import logging # 用來印出訊息，方便追蹤程式狀況
import os # 用來讀取環境變數 (像 RabbitMQ 的連線位址)
import aio_pika # RabbitMQ 的非同步函式庫
from typing import Optional # 型別提示用

# --- 環境變數 & 常數 ---
# 讀取 RabbitMQ 的連線位址，如果環境變數沒有設，就用本地端的預設值
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@127.0.0.1/")
# 我們要監聽的佇列名稱，必須跟 CalculationService 送出的地方一樣
OPPORTUNITY_QUEUE_NAME = "opportunity_queue"

# --- 日誌設定 ---
# 設定 Log 要怎麼顯示
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
# 給我們的 Logger 取個名字
logger = logging.getLogger("OrderBookService")

# --- 主要服務類別 ---
class OrderBookService:
    """
    負責監聽 RabbitMQ 中的套利機會，並處理收到的訊息。
    (目前只會印出收到的交易對 Symbol)
    """
    def __init__(self, rabbitmq_url: str):
        self.rabbitmq_url = rabbitmq_url
        # 這幾個變數先設成 None，連線成功後才會被賦值
        self.rabbitmq_connection: Optional[aio_pika.abc.AbstractRobustConnection] = None
        self.rabbitmq_channel: Optional[aio_pika.abc.AbstractChannel] = None
        self.consuming_future: Optional[asyncio.Future] = None # 用來保持程式運行

    async def _connect_rabbitmq(self) -> bool:
        """建立 RabbitMQ 連線並取得 Channel"""
        try:
            # 建立連線 (robust 會自動處理重連)
            self.rabbitmq_connection = await aio_pika.connect_robust(self.rabbitmq_url)
            # 建立一個 Channel (可以想成是跟 RabbitMQ 溝通的管道)
            self.rabbitmq_channel = await self.rabbitmq_connection.channel()
            # 設定 QoS (Quality of Service)，prefetch_count=1 表示一次只處理一個訊息
            # 這樣可以避免一個 Service 太忙，其他 Service 沒事做
            await self.rabbitmq_channel.set_qos(prefetch_count=1)
            logger.info(f"成功連接到 RabbitMQ 並設定 QoS: {self.rabbitmq_url}")
            return True
        except aio_pika.exceptions.AMQPConnectionError as e:
            logger.error(f"連接 RabbitMQ 失敗: {e}")
        except Exception as e:
            logger.exception(f"連接 RabbitMQ 時發生預期外的錯誤: {e}")
        # 如果連線失敗，把變數設回 None
        self.rabbitmq_connection = None
        self.rabbitmq_channel = None
        return False

    async def _on_message(self, message: aio_pika.abc.AbstractIncomingMessage):
        """
        這是收到訊息時會被呼叫的函數 (Callback)。
        message: 代表收到的那則訊息。
        """
        # 我們用 'async with message.process():' 來確保訊息最終會被確認或拒絕
        async with message.process():
            try:
                # 訊息的 body 是 bytes 型態，要解碼成 utf-8 字串
                body_str = message.body.decode('utf-8')
                # 把 JSON 字串解析成 Python 的字典 (dictionary)
                data = json.loads(body_str)
                # 從字典中取出 'symbol' 這個 key 對應的值
                symbol = data.get('symbol')

                if symbol:
                    # 如果成功拿到 symbol，就印出來
                    logger.info(f"收到套利機會訊息，交易對: {symbol}")
                    # 在這裡，未來我們會根據這個 symbol 去訂閱 WebSocket
                    # TODO: 在後續步驟加入訂閱 WebSocket 的邏輯
                else:
                    # 如果訊息裡面沒有 'symbol'
                    logger.warning(f"收到的訊息格式不符，缺少 'symbol': {body_str}")

                # 處理完成，告訴 RabbitMQ 這個訊息已經 OK 了 (ACK)
                # 因為用了 message.process()，這裡不需要明確呼叫 message.ack()
                # 如果在 process() 區塊內沒有發生錯誤，離開區塊時會自動 ack
                # 如果發生錯誤，離開區塊時會自動 nack (negative acknowledgement)

            except json.JSONDecodeError:
                # 如果收到的訊息不是合法的 JSON 格式
                logger.error(f"無法解析收到的訊息 (非 JSON): {message.body[:100]}") # 只顯示前 100 個字元
                # 訊息會自動 nack
            except Exception as e:
                # 其他可能發生的錯誤
                logger.exception(f"處理訊息時發生錯誤: {e}")
                # 訊息會自動 nack

    async def start_consuming(self):
        """開始監聽 RabbitMQ 的佇列"""
        if not self.rabbitmq_channel:
            logger.error("RabbitMQ channel 未建立，無法開始監聽。")
            return

        try:
            # 取得我們要監聽的那個佇列 (Queue)
            # durable=True 要跟 CalculationService 宣告時一樣，確保佇列存在
            queue = await self.rabbitmq_channel.declare_queue(
                OPPORTUNITY_QUEUE_NAME, durable=True
            )

            # 開始監聽！
            # consume 會一直執行，當有新訊息來時，會呼叫 _on_message 函數
            logger.info(f"開始監聽佇列: '{OPPORTUNITY_QUEUE_NAME}'...")
            await queue.consume(self._on_message)

            # 保持程式運行，直到被外部取消
            self.consuming_future = asyncio.Future()
            await self.consuming_future

        except aio_pika.exceptions.ChannelClosed as e:
             logger.warning(f"監聽時 Channel 被關閉: {e}. 可能需要重連...")
             # 或是可以在這裡加入重試邏輯
        except Exception as e:
            logger.exception(f"監聽佇列時發生錯誤: {e}")
        finally:
             logger.info("停止監聽佇列。")
             # 如果 future 還在，就設個結果讓它結束
             if self.consuming_future and not self.consuming_future.done():
                 self.consuming_future.set_result(None)


    async def start(self):
        """啟動服務"""
        logger.info("正在啟動 OrderBookService...")
        if await self._connect_rabbitmq():
            # 連線成功才開始監聽
            await self.start_consuming()
        else:
            logger.critical("無法連接到 RabbitMQ，服務無法啟動。")
            # 可以加上重試機制，或讓程式直接結束

    async def close(self):
        """關閉服務和連線"""
        logger.info("正在關閉 OrderBookService...")
        # 取消監聽任務
        if self.consuming_future and not self.consuming_future.done():
             self.consuming_future.cancel()
             try:
                 await self.consuming_future # 等待 future 結束
             except asyncio.CancelledError:
                 logger.info("監聽任務已被取消。")

        # 關閉 Channel
        if self.rabbitmq_channel:
            try:
                await self.rabbitmq_channel.close()
                logger.info("RabbitMQ channel 已關閉。")
            except Exception as e:
                logger.error(f"關閉 RabbitMQ channel 時發生錯誤: {e}")
        # 關閉 Connection
        if self.rabbitmq_connection:
            try:
                await self.rabbitmq_connection.close()
                logger.info("RabbitMQ connection 已關閉。")
            except Exception as e:
                logger.error(f"關閉 RabbitMQ connection 時發生錯誤: {e}")
        logger.info("OrderBookService 關閉完成。")


# --- 主程式執行區塊 ---
# 這段是讓你可以直接執行 python my_project/orderbook_service.py 來測試
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

# E305: 兩個空行
# E305: 兩個空行
if __name__ == "__main__":
    try:
        logger.info("嘗試直接運行 OrderBookService...")
        asyncio.run(main())
    except KeyboardInterrupt:
        # 按下 Ctrl+C 可以停止程式
        logger.info("使用者按下 Ctrl+C，服務停止。")

# C0304/W292: 確保檔案結尾有空行