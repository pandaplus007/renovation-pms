#!/usr/bin/env bash
# 装修项目管理系统 - 极空间 NAS 一键更新脚本
# 用法：把本脚本放在项目根（与 docker-compose.yml 同级），执行 ./update-nas.sh
# 作用：重建镜像（依赖不变则只重打代码层，很快）→ 重启容器 → 校验运行状态
# 数据（data/renovation.db 与 uploads/）在宿主机卷里，升级不影响。

set -e

cd "$(dirname "$0")"

echo "======================================================"
echo "   装修项目管理系统 - 更新"
echo "   当前目录: $(pwd)"
echo "======================================================"

# 1. 是否已安装 docker
if ! command -v docker >/dev/null 2>&1; then
    echo "❌ 未检测到 docker 命令，请确认已在极空间上安装 Docker。"
    exit 1
fi

# 2. 是否在项目根（有 docker-compose.yml）
if [ ! -f docker-compose.yml ]; then
    echo "❌ 当前目录找不到 docker-compose.yml，请把脚本放在与 docker-compose.yml 同级目录。"
    exit 1
fi

# 3. 更新前提示备份（重要）
echo "🔔 升级前请确认已备份 data/renovation.db 与 uploads/（见部署指南第6节）。"
read -r -p "是否继续更新？[y/N] " ans
if [ "${ans:-N}" != "y" ] && [ "${ans:-N}" != "Y" ]; then
    echo "已取消更新。"
    exit 0
fi

# 4. 重新构建镜像并重启容器
echo "→ 正在重新构建镜像并重启容器（依赖未变时只重打代码层，通常很快）..."
docker compose up -d --build

# 5. 校验运行状态
echo "→ 正在校验运行状态..."
sleep 3
if docker compose ps | grep -q "Up \|(healthy)"; then
    echo "✅ 容器运行正常。"
else
    echo "⚠️ 容器状态异常，请查看日志："
    docker compose logs --tail 30
    exit 1
fi

# 6. 展示最近日志 + 数据目录摘要
echo "→ 最近日志（末尾 15 行）："
docker compose logs --tail 15 || true

echo "→ 数据目录确认（应保持不变）："
ls -lh data/ 2>/dev/null | sed 's/^/   /' || echo "   (data/ 目录暂未生成数据库文件，首次启动后会自动创建)"
ls -d uploads/ 2>/dev/null | sed 's/^/   /' || true

echo ""
echo "======================================================"
echo "   ✅ 更新完成"
echo "   访问: http://<极空间IP>:8018  (默认账号 admin / admin123)"
echo "======================================================"
