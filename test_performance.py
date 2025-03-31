import time
import pytest


@pytest.mark.benchmark
def test_function_speed(benchmark):
    def slow_function():
        time.sleep(0.1)  # 模擬較慢的函式
        return "done"
    
    result = benchmark(slow_function)
    assert result == "done"
