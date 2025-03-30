# 使用官方的 Python 影像作為基礎
FROM python:3.9-slim

# 設定工作目錄
WORKDIR /app

# 將本機的 requirements.txt 複製到容器中的 /app
COPY requirements.txt /app/

# 安裝依賴的套件
RUN pip install -r requirements.txt

# 複製剩下的程式碼到容器
COPY . /app/

# 設定容器啟動時執行的命令
CMD ["pytest", "tests/"]

