FROM python:3.11-slim

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制服务端代码
COPY server/ ./server/

# 设置环境变量
ENV PYTHONUNBUFFERED=1

# 暴露端口（Railway 会自动注入 PORT）
EXPOSE 5000

# 启动命令
CMD cd server && gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 120
