#!/bin/bash
# scout 安装脚本：创建一个 ~/bin/scout 包装器，不污染 Python 环境
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_DIR="$HOME/bin"
TARGET="$TARGET_DIR/scout"

mkdir -p "$TARGET_DIR"

cat > "$TARGET" <<EOF
#!/bin/bash
exec python3 "$DIR/scout.py" "\$@"
EOF
chmod +x "$TARGET"

echo "✅ 已创建 $TARGET"

case ":$PATH:" in
  *":$TARGET_DIR:"*)
    echo "✅ $TARGET_DIR 已在 PATH 中"
    echo
    echo "试试："
    echo "  scout --help"
    echo "  scout --from-clash current"
    ;;
  *)
    echo "⚠️  $TARGET_DIR 不在 PATH 中。把下面这行加到 ~/.zshrc："
    echo
    echo "    export PATH=\"\$HOME/bin:\$PATH\""
    echo
    echo "然后 source ~/.zshrc 或新开终端。"
    ;;
esac

echo
echo "另一种方式（生成全局命令，需要 pip）："
echo "  python3 -m pip install --user -e \"$DIR\""
