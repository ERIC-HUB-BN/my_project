import websocket
import json
import logging

# Logger setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

class DataComputationService:
    def __init__(self, spot_stream_url, perp_stream_url):
        self.spot_stream_url = spot_stream_url
        self.perp_stream_url = perp_stream_url

    def on_message(self, ws, message):
        data = json.loads(message)
        symbol = data.get("s")  # Trading pair symbol
        price = data.get("c")  # Last price
        if symbol and price:
            logging.info(f"Received price update: {symbol} -> {price}")

    def on_error(self, ws, error):
        logging.error(f"WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        logging.info("WebSocket connection closed")

    def on_open(self, ws):
        logging.info("WebSocket connection established")

    def start(self):
        # Connect to Spot WebSocket stream
        logging.info("Connecting to Spot WebSocket...")
        spot_ws = websocket.WebSocketApp(
            self.spot_stream_url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        spot_ws.run_forever()

        # Connect to Perp WebSocket stream (this will be done in parallel in the future)
        logging.info("Connecting to Perp WebSocket...")
        perp_ws = websocket.WebSocketApp(
            self.perp_stream_url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        perp_ws.run_forever()

if __name__ == "__main__":
    spot_stream = "wss://stream.binance.com:9443/ws/spot_ticker"  # Example Spot WebSocket URL
    perp_stream = "wss://fstream.binance.com/ws/perp_ticker"  # Example Perp WebSocket URL
    service = DataComputationService(spot_stream, perp_stream)
    service.start()