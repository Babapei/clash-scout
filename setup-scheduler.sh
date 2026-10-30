#!/bin/bash
# 安装定时记录任务（macOS launchd）
#
# 用法：
#   bash setup-scheduler.sh          # 默认每 10 分钟
#   bash setup-scheduler.sh 30       # 改成每 30 分钟
#   bash setup-scheduler.sh --remove # 卸载
#
# 会做三件事：
#   1. 找到 scout 命令的真实路径
#   2. 生成一个 wrapper（Verge 没运行就跳过）
#   3. 写 launchd plist 并加载
set -e

INTERVAL="${1:-10}"
LABEL="com.local.scout"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WRAPPER="$HOME/.clash-ip-tracker/run-scheduled.sh"
LOGDIR="$HOME/.clash-ip-tracker"

# ---------- 卸载 ----------
if [ "$1" = "--remove" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST" "$WRAPPER"
  echo "✅ 已卸载定时任务"
  exit 0
fi

# ---------- 定位项目目录（不依赖 pip 安装路径） ----------
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ ! -d "$PROJECT_DIR/clash_ip_tracker" ]; then
  echo "❌ 找不到 clash_ip_tracker 包目录：$PROJECT_DIR"
  exit 1
fi
echo "项目目录: $PROJECT_DIR"

# ---------- 复制运行文件到非受保护目录 ----------
# macOS 的 launchd 不能访问 ~/Documents（TCC 保护），所以把运行所需的
# 文件复制到 ~/.clash-ip-tracker/app/ 再跑。
APP_DIR="$LOGDIR/app"
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/clash_ip_tracker"
cp -R "$PROJECT_DIR/clash_ip_tracker" "$APP_DIR/"
cp "$PROJECT_DIR/scout.py" "$APP_DIR/"
echo "运行副本: $APP_DIR"

# ---------- wrapper：Verge 没跑就跳过 ----------
mkdir -p "$LOGDIR"
cat > "$WRAPPER" <<EOF
#!/bin/bash
# 由 setup-scheduler.sh 生成
if ! pgrep -f "Clash Verge" >/dev/null 2>&1; then
  echo "\$(date '+%F %T') Clash Verge 未运行，跳过"
  exit 0
fi
echo "\$(date '+%F %T') 开始记录"
PYTHONPATH="$APP_DIR" python3 "$APP_DIR/scout.py" --from-clash check 2>&1 | tail -5
EOF
chmod +x "$WRAPPER"

# ---------- plist ----------
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$WRAPPER</string>
    </array>
    <key>StartInterval</key>
    <integer>$((INTERVAL * 60))</integer>
    <key>StandardOutPath</key>
    <string>$LOGDIR/scheduler.log</string>
    <key>StandardErrorPath</key>
    <string>$LOGDIR/scheduler.err</string>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "✅ 已安装：每 $INTERVAL 分钟自动记录一次"
echo "   日志：$LOGDIR/scheduler.log"
echo "   脚本：$WRAPPER"
echo "   卸载：bash setup-scheduler.sh --remove"
