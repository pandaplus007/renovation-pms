# 装修项目管家 - Docker 镜像
# 适用于极空间 Z4 Pro 等支持 Docker 的 NAS
FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY . .

# 数据卷挂载点（SQLite 数据库与上传文件）
RUN mkdir -p /app/data /app/uploads

EXPOSE 8000

CMD ["python", "run.py"]
