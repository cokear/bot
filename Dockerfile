# 使用官方 Python 轻量级镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 复制依赖列表并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目所有文件到工作目录
COPY . .

# 运行机器人脚本
CMD ["python", "main.py"]
