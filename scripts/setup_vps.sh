#!/usr/bin/env bash
# One-shot setup для свежего Ubuntu 24.04 VPS.
# Использование (на VPS, под root):
#   curl -fsSL https://raw.githubusercontent.com/takinanton/hyperliquid_bot/main/scripts/setup_vps.sh | bash
# либо после `git clone`:
#   bash scripts/setup_vps.sh

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/takinanton/hyperliquid_bot.git}"
INSTALL_DIR="${INSTALL_DIR:-/root/hyperliquid_bot}"

echo "==> 1/6 Обновляю apt и ставлю системные пакеты"
apt update -y
# Определяем версию Python: на Ubuntu 22.04 это 3.10, на 24.04 — 3.12
if apt-cache show python3.12 >/dev/null 2>&1; then
  PYVER="3.12"
elif apt-cache show python3.10 >/dev/null 2>&1; then
  PYVER="3.10"
else
  echo "ERROR: ни python3.12 ни python3.10 не доступны в apt"
  exit 1
fi
echo "==> Use Python $PYVER"
apt install -y "python${PYVER}" "python${PYVER}-venv" "python${PYVER}-dev" git sqlite3 curl ca-certificates build-essential

echo "==> 2/6 Клонирую/обновляю репо в $INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  git -C "$INSTALL_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

echo "==> 3/6 Создаю venv (Python $PYVER)"
"python${PYVER}" -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip --quiet

echo "==> 4/6 Ставлю зависимости"
pip install --quiet -r requirements.txt
pip install --quiet "git+https://github.com/white07S/TradingPatternScanner.git"

echo "==> 5/6 Копирую .env.example → .env (если ещё нет)"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo ""
  echo "  >>> ВАЖНО: отредактируй .env и впиши ключи: <<<"
  echo "       nano $INSTALL_DIR/.env"
  echo "  и ЗАТЕМ запусти dry-run:"
  echo "       cd $INSTALL_DIR && source venv/bin/activate && python -m bot.main --dry-run --once"
  echo ""
fi

echo "==> 6/6 Ставлю systemd-сервис (но НЕ стартую, пока не заполнишь .env)"
SERVICE_SRC="$INSTALL_DIR/systemd/hyperliquid-bot.service"
SERVICE_DST="/etc/systemd/system/hyperliquid-bot.service"
# Подменяем WorkingDirectory/Environment под фактический INSTALL_DIR
sed "s|/root/hyperliquid_bot|$INSTALL_DIR|g" "$SERVICE_SRC" > "$SERVICE_DST"
systemctl daemon-reload
systemctl enable hyperliquid-bot >/dev/null 2>&1 || true

echo ""
echo "✅ Установка завершена."
echo ""
echo "Дальше:"
echo "  1) nano $INSTALL_DIR/.env       # вставить HYPERLIQUID_AGENT_PRIVATE_KEY и HYPERLIQUID_ACCOUNT_ADDRESS"
echo "  2) cd $INSTALL_DIR && source venv/bin/activate && python -m bot.main --dry-run --once"
echo "  3) systemctl start hyperliquid-bot && journalctl -u hyperliquid-bot -f"
