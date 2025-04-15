# redis_test.py
import asyncio
import redis.asyncio as redis # 匯入官方 redis 函式庫的異步版本

async def main():
    print("Connecting to Redis using redis-py (asyncio)...")
    try:
        # 使用 redis.asyncio.from_url 來建立連線
        # 注意 R大寫 Redis
        # decode_responses=True 讓結果自動是文字
        r = redis.Redis.from_url("redis://localhost:6379", decode_responses=True)
        await r.ping() # 檢查連線是否成功
        print("Connected successfully!")

        # 1. 嘗試存一個值到 Redis
        key = "my_test_key"
        value = "Hello from Python using redis-py!"
        print(f"Setting key '{key}' to value '{value}'...")
        await r.set(key, value)
        print("Key set successfully.")

        # 2. 嘗試從 Redis 把值讀出來
        print(f"Getting value for key '{key}'...")
        retrieved_value = await r.get(key)
        print(f"Retrieved value: {retrieved_value}")

        # 3. 檢查讀出來的值對不對
        if retrieved_value == value:
            print("SUCCESS: Value matches!")
        else:
            print("ERROR: Value does not match!")

        # 4. 刪除測試用的 key (保持乾淨)
        print(f"Deleting key '{key}'...")
        await r.delete(key)
        print("Key deleted.")

    except redis.exceptions.ConnectionError as e:
        print(f"ERROR: Could not connect to Redis at redis://localhost:6379")
        print(f"Make sure Redis is running via 'docker-compose up -d redis'.")
        print(f"Error details: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    finally:
        # 關閉連線 (好習慣)
        if 'r' in locals() and r:
            await r.aclose() # <--- 把 close() 改成 aclose()
            print("Connection closed.")

# 執行這個異步的 main 函數
if __name__ == "__main__":
    asyncio.run(main())