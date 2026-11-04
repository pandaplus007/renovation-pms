#!/usr/bin/env bash
# ==============================================================
#  装修项目管理系统 - 从极空间 NAS 每日备份到本机
#  用法：bash scripts/backup-from-nas.sh
#  产出：
#    data/nas-backups/renovation_<日期>_<时间>.db   历史数据库快照（保留最近 30 份）
#    data/nas-backups/uploads/                      附件/照片镜像（每次覆盖为最新）
#    data/renovation.db                             同步为最新生产库（供本地脚本/预警使用）
#  说明：数据库使用 sqlite3 .backup 做一致性快照（容器运行中也可安全执行）。
# ==============================================================
set -uo pipefail

NAS_USER="your-nas-user"
NAS_HOST="192.168.1.100"
NAS_PORT="10999"
REMOTE_DIR="/data_s007/renovation-pms"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="$PROJ_DIR/data/nas-backups"
LOG_DIR="$BACKUP_DIR/logs"

SSH="ssh -p $NAS_PORT -o BatchMode=yes -o ConnectTimeout=10"
SCP="scp -P $NAS_PORT -o BatchMode=yes -o ConnectTimeout=10 -q"
STAMP="$(date +%Y%m%d_%H%M)"
mkdir -p "$BACKUP_DIR" "$LOG_DIR"

log() { echo "[$(date '+%F %T')] $*"; }

log "=== 开始从 NAS 备份 ==="

# 0) 连通性
if ! $SSH "$NAS_USER@$NAS_HOST" "echo ok" >/dev/null 2>&1; then
    log "❌ 无法连接 NAS ($NAS_HOST:$NAS_PORT)，本次备份中止"
    exit 1
fi

# 1) 一致性数据库快照（在 NAS 上做 .backup）
REMOTE_DB="/tmp/reno_backup_${STAMP}.db"
if ! $SSH "$NAS_USER@$NAS_HOST" "rm -f $REMOTE_DB && sqlite3 '$REMOTE_DIR/data/renovation.db' \".backup '$REMOTE_DB'\"" ; then
    log "❌ 远端数据库快照失败"
    exit 1
fi

if ! $SCP "$NAS_USER@$NAS_HOST:$REMOTE_DB" "$BACKUP_DIR/renovation_${STAMP}.db"; then
    log "❌ 数据库下载失败"
    exit 1
fi
$SSH "$NAS_USER@$NAS_HOST" "rm -f $REMOTE_DB" >/dev/null 2>&1 || true
DB_SIZE=$(stat -c %s "$BACKUP_DIR/renovation_${STAMP}.db" 2>/dev/null || echo 0)
log "✅ 数据库快照: renovation_${STAMP}.db (${DB_SIZE} 字节)"

# 2) 用最新快照刷新本地生产库副本（供预警脚本等本地程序使用）
cp -f "$BACKUP_DIR/renovation_${STAMP}.db" "$PROJ_DIR/data/renovation.db" && log "✅ 已刷新本地 data/renovation.db"

# 3) 附件/照片镜像（流式 tar，覆盖式更新）
TMP_UP="$BACKUP_DIR/uploads.new"
rm -rf "$TMP_UP"; mkdir -p "$TMP_UP"
if $SSH "$NAS_USER@$NAS_HOST" "tar -cf - -C '$REMOTE_DIR' uploads" | tar -xf - -C "$TMP_UP" 2>/dev/null; then
    if [ -d "$TMP_UP/uploads" ]; then
        rm -rf "$BACKUP_DIR/uploads.old"
        [ -d "$BACKUP_DIR/uploads" ] && mv "$BACKUP_DIR/uploads" "$BACKUP_DIR/uploads.old"
        mv "$TMP_UP/uploads" "$BACKUP_DIR/uploads"
        rm -rf "$BACKUP_DIR/uploads.old" "$TMP_UP"
        UP_COUNT=$(find "$BACKUP_DIR/uploads" -type f 2>/dev/null | wc -l)
        UP_SIZE=$(du -sh "$BACKUP_DIR/uploads" 2>/dev/null | cut -f1)
        log "✅ 附件镜像已更新: $UP_COUNT 个文件, $UP_SIZE"
    else
        log "⚠️ 附件下载结果为空，保留上一份镜像"
        rm -rf "$TMP_UP"
    fi
else
    log "⚠️ 附件下载失败，保留上一份镜像"
    rm -rf "$TMP_UP"
fi

# 4) 保留最近 30 份数据库快照
KEEP=30
ls -1t "$BACKUP_DIR"/renovation_*.db 2>/dev/null | tail -n +$((KEEP+1)) | while read -r f; do rm -f "$f"; done
SNAP_COUNT=$(ls -1 "$BACKUP_DIR"/renovation_*.db 2>/dev/null | wc -l)
log "✅ 保留数据库快照 $SNAP_COUNT 份（上限 $KEEP）"

log "=== 备份完成 ==="
