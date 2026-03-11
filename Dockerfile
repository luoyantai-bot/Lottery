FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制所有 server 文件到 /app（扁平化，不用子目录）
COPY server/app.py .
COPY server/config.py .
COPY server/scraper.py .
COPY server/templates/ ./templates/

# 环境变量
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

# 启动命令 - 不使用 cd，不使用 shell 变量
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 120"]


