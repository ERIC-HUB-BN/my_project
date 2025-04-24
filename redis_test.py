# redis_test.py
import asyncio
import redis # <<<--- 新增：匯入主要的 redis 函式庫 (為了 exceptions) ---
import redis.asyncio as aredis # <<<--- 修改：異步的改用別名 aredis ---

async def main():
    print("Connecting to Redis using redis-py (asyncio)...")
    r = None # <<<--- 初始化 r ---
    try:
        # <<<--- 修改：使用別名 aredis ---
        r = aredis.Redis.from_url("redis://127.0.0.1:6379", decode_responses=True)
        await r.ping() # 檢查連線是否成功
        print("Connected successfully!")

        key = "my_test_key"
        value = "Hello from Python using redis-py!"
        print(f"Setting key '{key}' to value '{value}'...")
        await r.set(key, value)
        print("Key set successfully.")

        print(f"Getting value for key '{key}'...")
        retrieved_value = await r.get(key)
        print(f"Retrieved value: {retrieved_value}")

        if retrieved_value == value:
            print("SUCCESS: Value matches!")
        else:
            print("ERROR: Value does not match!")

        print(f"Deleting key '{key}'...")
        await r.delete(key)
        print("Key deleted.")

    # <<<--- 修改：捕捉主要的 redis 函式庫的 ConnectionError ---
    except redis.exceptions.ConnectionError as e:
        print(f"ERROR: Could not connect to Redis at redis://localhost:6379")
        print(f"Make sure Redis is running via 'docker-compose up -d redis'.")
        print(f"Error details: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
    finally:
        # 關閉連線 (好習慣)
        if r: # <<<--- 修改：檢查 r 是否被成功賦值 ---
            await r.aclose()
            print("Connection closed.")

if __name__ == "__main__":
    asyncio.run(main())

# 確保檔案結尾有空行