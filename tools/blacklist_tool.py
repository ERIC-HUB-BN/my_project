# tools/blacklist_tool.py
import os
import redis
import sys
from typing import List, Set

# --- 設定 ---
# 從環境變數讀取 Redis URL，若無則預設連本地
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
BLACKLIST_KEY = "blacklist:manual" # 跟 CalculationService 用的 Key 要一致

def get_redis_connection(url: str) -> redis.Redis:
    """
    嘗試建立到 Redis 的同步連接。
    如果失敗會拋出 ConnectionError。
    """
    try:
        # 使用 decode_responses=True 讓取出的資料是字串
        client = redis.Redis.from_url(url, decode_responses=True)
        client.ping()
        print(f"成功連接到 Redis: {url}")
        return client
    except redis.exceptions.ConnectionError as e:
        print(f"錯誤：無法連接到 Redis ({url})。請確保 Redis 正在運行。", file=sys.stderr)
        print(f"詳細錯誤: {e}", file=sys.stderr)
        raise  # 將錯誤重新拋出，讓主函數知道連線失敗

def parse_input(input_string: str) -> Set[str]:
    """
    解析使用者輸入的字串，轉換成大寫的 Symbol 集合。
    用空格或逗號分隔。
    """
    if not input_string:
        return set()
    # 替換逗號為空格，方便統一處理
    processed_string = input_string.replace(",", " ")
    # 分割字串、移除多餘空白、轉大寫、過濾空字串
    symbols = {s.strip().upper() for s in processed_string.split() if s.strip()}
    return symbols

def add_symbols_to_blacklist(client: redis.Redis, symbols: Set[str]) -> int:
    """
    將 Symbols 加入 Redis 的 Set。
    返回成功加入的數量。
    """
    if not symbols:
        print("沒有有效的 Symbol 需要加入。")
        return 0
    try:
        # SADD 返回新加入集合的元素數量
        added_count = client.sadd(BLACKLIST_KEY, *symbols)
        return added_count
    except redis.exceptions.RedisError as e:
        print(f"錯誤：寫入 Redis 黑名單 ({BLACKLIST_KEY}) 時發生錯誤。", file=sys.stderr)
        print(f"詳細錯誤: {e}", file=sys.stderr)
        return 0 # 表示加入失敗

def main():
    """主執行函數"""
    redis_client = None
    try:
        redis_client = get_redis_connection(REDIS_URL)

        # --- 主要互動邏輯 ---
        print("-" * 30)
        print("幣安交易對黑名單添加工具")
        print(f"將會寫入 Redis Key: '{BLACKLIST_KEY}'")
        print("請輸入要加入黑名單的交易對 Symbol (例如：LUNAUSDT)，")
        print("可以用空格或逗號分隔多個 Symbol。")
        print("-" * 30)

        input_str = input("請輸入 Symbol(s): ").strip()

        symbols_to_add = parse_input(input_str)

        if not symbols_to_add:
            print("您沒有輸入任何有效的 Symbol。")
            return # 直接結束

        print(f"\n準備將以下 Symbol 加入黑名單: {', '.join(sorted(list(symbols_to_add)))}")
        confirm = input("確定要加入嗎？ (y/N): ").strip().lower()

        if confirm == 'y':
            added_count = add_symbols_to_blacklist(redis_client, symbols_to_add)
            print(f"\n操作完成。成功將 {added_count} 個新 Symbol 加入 Redis 黑名單 '{BLACKLIST_KEY}'。")
            # 可以選擇性地顯示目前黑名單內容
            # current_blacklist = redis_client.smembers(BLACKLIST_KEY)
            # print(f"目前黑名單內容: {sorted(list(current_blacklist))}")
        else:
            print("操作已取消。")

    except redis.exceptions.ConnectionError:
        # 連線錯誤已在 get_redis_connection 中處理，這裡直接結束
        pass
    except Exception as e:
        print(f"發生未預期的錯誤: {e}", file=sys.stderr)
    finally:
        if redis_client:
            redis_client.close()
            print("Redis 連線已關閉。")

if __name__ == "__main__":
    main()