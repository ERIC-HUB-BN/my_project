# tests/test_blacklist_tool.py
import unittest
from unittest.mock import patch, MagicMock, call
import redis # 為了 Mock 和 Exceptions
import sys
import io # 用於捕捉 print 輸出
import os

# --- 路徑處理 (保持不變) ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
tools_dir = os.path.join(project_root, 'tools')
if tools_dir not in sys.path:
    sys.path.insert(0, tools_dir)

# 導入被測試的函數和 main
from blacklist_tool import (
    parse_input,
    add_symbols_to_blacklist,
    get_redis_connection,
    main as blacklist_main, # 導入 main 函數以供測試
    BLACKLIST_KEY
)


class TestBlacklistTool(unittest.TestCase):

    # ... (之前的測試函數保持不變，除了 main 的測試) ...
    def test_parse_input_empty(self):
        """測試：輸入空字串"""
        self.assertEqual(parse_input(""), set())
        self.assertEqual(parse_input("   "), set())

    def test_parse_input_single(self):
        """測試：輸入單一 Symbol"""
        self.assertEqual(parse_input("BTCUSDT"), {"BTCUSDT"})
        self.assertEqual(parse_input("  btcusdt  "), {"BTCUSDT"}) # 測試大小寫和空白

    def test_parse_input_multiple_spaces(self):
        """測試：用空格分隔多個 Symbol"""
        self.assertEqual(parse_input("BTCUSDT ETHUSDT adaUSDT"), {"BTCUSDT", "ETHUSDT", "ADAUSDT"})

    def test_parse_input_multiple_commas(self):
        """測試：用逗號分隔多個 Symbol"""
        self.assertEqual(parse_input("BTCUSDT,ETHUSDT, adaUSDT"), {"BTCUSDT", "ETHUSDT", "ADAUSDT"})

    def test_parse_input_mixed_separators(self):
        """測試：混合使用空格和逗號"""
        self.assertEqual(parse_input("BTCUSDT, ETHUSDT  LTCUSDT,dogeUSDT"), {"BTCUSDT", "ETHUSDT", "LTCUSDT", "DOGEUSDT"})


    @patch('redis.Redis.from_url')
    def test_get_redis_connection_success(self, mock_from_url):
        """測試：成功連接 Redis"""
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_from_url.return_value = mock_client

        # 捕捉 stdout 輸出，避免 "成功連接到 Redis" 影響測試結果觀察
        captured_output = io.StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured_output
        client = get_redis_connection("redis://fake")
        sys.stdout = original_stdout # 恢復 stdout

        mock_from_url.assert_called_once_with("redis://fake", decode_responses=True)
        mock_client.ping.assert_called_once()
        self.assertEqual(client, mock_client)

    @patch('redis.Redis.from_url')
    def test_get_redis_connection_failure(self, mock_from_url):
        """測試：連接 Redis 失敗"""
        mock_from_url.side_effect = redis.exceptions.ConnectionError("Test connection failed")
        captured_output = io.StringIO()
        original_stderr = sys.stderr
        sys.stderr = captured_output
        with self.assertRaises(redis.exceptions.ConnectionError):
            get_redis_connection("redis://fake")
        sys.stderr = original_stderr
        mock_from_url.assert_called_once_with("redis://fake", decode_responses=True)
        self.assertIn("錯誤：無法連接到 Redis", captured_output.getvalue())


    def test_add_symbols_to_blacklist_success(self):
        """測試：成功將 Symbols 加入黑名單 (修正順序問題)"""
        mock_client = MagicMock(spec=redis.Redis)
        mock_client.sadd.return_value = 2
        symbols = {"BTCUSDT", "ETHUSDT"}

        added_count = add_symbols_to_blacklist(mock_client, symbols)

        mock_client.sadd.assert_called_once()
        args, kwargs = mock_client.sadd.call_args
        self.assertEqual(args[0], BLACKLIST_KEY)
        called_symbols = set(args[1:])
        self.assertEqual(called_symbols, symbols)
        self.assertEqual(added_count, 2)

    def test_add_symbols_to_blacklist_empty(self):
        """測試：傳入空的 Symbols 集合"""
        mock_client = MagicMock(spec=redis.Redis)
        captured_output = io.StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured_output
        added_count = add_symbols_to_blacklist(mock_client, set())
        sys.stdout = original_stdout
        mock_client.sadd.assert_not_called()
        self.assertEqual(added_count, 0)
        self.assertIn("沒有有效的 Symbol 需要加入", captured_output.getvalue())


    def test_add_symbols_to_blacklist_redis_error(self):
        """測試：加入時發生 Redis 錯誤"""
        mock_client = MagicMock(spec=redis.Redis)
        mock_client.sadd.side_effect = redis.exceptions.RedisError("Test sadd error")
        symbols = {"BTCUSDT"}

        captured_output = io.StringIO()
        original_stderr = sys.stderr
        sys.stderr = captured_output

        added_count = add_symbols_to_blacklist(mock_client, symbols)

        sys.stderr = original_stderr

        mock_client.sadd.assert_called_once_with(BLACKLIST_KEY, "BTCUSDT")
        self.assertEqual(added_count, 0)
        self.assertIn("錯誤：寫入 Redis 黑名單", captured_output.getvalue())
        self.assertIn("Test sadd error", captured_output.getvalue())

    # --- 修正 main 函數測試的 patch 目標 ---
    @patch('blacklist_tool.get_redis_connection') # <<< 修正：指向 blacklist_tool 模組
    @patch('builtins.input')
    @patch('blacklist_tool.add_symbols_to_blacklist') # <<< 修正：指向 blacklist_tool 模組
    def test_main_flow_add_symbols(self, mock_add_symbols, mock_input, mock_get_conn):
        """測試：完整流程 - 輸入、確認並加入"""
        mock_redis_client = MagicMock()
        mock_redis_client.close = MagicMock()
        mock_get_conn.return_value = mock_redis_client
        mock_add_symbols.return_value = 2

        mock_input.side_effect = [ "BTCUSDT, ethusdt", "y" ]

        captured_output = io.StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured_output

        blacklist_main()

        sys.stdout = original_stdout

        mock_get_conn.assert_called_once() # 現在應該會成功
        expected_symbols = {"BTCUSDT", "ETHUSDT"}
        mock_add_symbols.assert_called_once_with(mock_redis_client, expected_symbols)
        mock_redis_client.close.assert_called_once()

        output_str = captured_output.getvalue()
        print(f"\nCaptured output for test_main_flow_add_symbols:\n{output_str}")
        self.assertIn("準備將以下 Symbol 加入黑名單: BTCUSDT, ETHUSDT", output_str)
        self.assertIn("操作完成。成功將 2 個新 Symbol 加入 Redis 黑名單", output_str)
        self.assertIn("Redis 連線已關閉", output_str)


    @patch('blacklist_tool.get_redis_connection') # <<< 修正：指向 blacklist_tool 模組
    @patch('builtins.input')
    @patch('blacklist_tool.add_symbols_to_blacklist') # <<< 修正：指向 blacklist_tool 模組
    def test_main_flow_cancel(self, mock_add_symbols, mock_input, mock_get_conn):
        """測試：完整流程 - 輸入但取消"""
        mock_redis_client = MagicMock()
        mock_redis_client.close = MagicMock()
        mock_get_conn.return_value = mock_redis_client

        mock_input.side_effect = [ "LUNAUSDT", "n" ]

        captured_output = io.StringIO()
        original_stdout = sys.stdout
        sys.stdout = captured_output

        blacklist_main()

        sys.stdout = original_stdout

        mock_get_conn.assert_called_once() # 現在應該會成功
        mock_add_symbols.assert_not_called()
        mock_redis_client.close.assert_called_once()

        output_str = captured_output.getvalue()
        print(f"\nCaptured output for test_main_flow_cancel:\n{output_str}")
        self.assertIn("準備將以下 Symbol 加入黑名單: LUNAUSDT", output_str)
        self.assertIn("操作已取消", output_str)
        self.assertIn("Redis 連線已關閉", output_str)


if __name__ == '__main__':
    unittest.main()