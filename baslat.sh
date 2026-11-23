#!/usr/bin/env bash
# Octo-Replay yerel sunucu (Mac / Linux).  Kullanım:  bash baslat.sh
cd "$(dirname "$0")" || exit 1

if ! command -v php >/dev/null 2>&1; then
  echo "PHP bulunamadı. Mac: 'brew install php'  ·  Ubuntu/Debian: 'sudo apt install php-cli php-curl php-sqlite3'"
  echo "PHP kurmak istemiyorsanız dosyaları hosting'e yükleyip adresten açın."
  exit 1
fi

mkdir -p cache
URL="http://localhost:8000"
echo "Octo-Replay $URL adresinde çalışıyor. Durdurmak için Ctrl+C."
( sleep 1.5; (command -v xdg-open >/dev/null && xdg-open "$URL") || (command -v open >/dev/null && open "$URL") ) >/dev/null 2>&1 &
exec php -S localhost:8000
