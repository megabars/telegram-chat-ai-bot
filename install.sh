#!/usr/bin/env bash
set -euo pipefail
umask 077

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_SERVICE=false
if [[ "${1:-}" == "--systemd" && $# == 1 ]]; then
    INSTALL_SERVICE=true
elif [[ $# != 0 ]]; then
    echo "Usage: ./install.sh [--systemd]" >&2
    exit 2
fi

if ! command -v "$PYTHON_BIN" >/dev/null; then
    echo "Python missing. Install Python 3.12+ and its venv package." >&2
    exit 1
fi
"$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Python 3.12+ required")'

if $INSTALL_SERVICE; then
    [[ "$EUID" == 0 ]] || { echo "Use sudo for --systemd" >&2; exit 1; }
    [[ "$PROJECT_DIR" == /opt/telegram-openai-bot ]] || {
        echo "Copy project to /opt/telegram-openai-bot first." >&2; exit 1;
    }
    id telegrambot >/dev/null 2>&1 || {
        echo "Create the telegrambot service user first (see README)." >&2; exit 1;
    }
fi

cd -- "$PROJECT_DIR"
if [[ ! -f .env ]]; then
    echo "Missing .env. Run: cp .env.example .env; chmod 600 .env; then fill in keys." >&2
    exit 1
fi
chmod 600 .env
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip check
.venv/bin/python -m app.main --check-config

if $INSTALL_SERVICE; then
    chown root:telegrambot "$PROJECT_DIR"
    chmod 750 "$PROJECT_DIR"
    chown telegrambot:telegrambot .env
    # Restrictive umask during installation requires explicit runtime read access.
    chown -R root:telegrambot app .venv
    chmod -R g+rX app .venv
    chmod -R go-w app .venv
    install -m 644 telegram-openai-bot.service /etc/systemd/system/telegram-openai-bot.service
    systemctl daemon-reload
    echo "Installed. Start with: sudo systemctl enable --now telegram-openai-bot"
    echo "Logs: sudo journalctl -u telegram-openai-bot -f"
else
    echo "Installed. Start locally: .venv/bin/python -m app.main"
    echo "Ubuntu systemd installation: sudo ./install.sh --systemd (see README)"
fi
