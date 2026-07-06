#!/bin/bash
# Релизный архив Cardboard: zip + контрольная сумма SHA-256.
# Запуск: ./make_release.sh  (после сборки .app через PyInstaller)
# Результат в dist/: Cardboard-<версия>.zip + Cardboard-<версия>.sha256.txt
# Дальше: архив можно проверить на virustotal.com и приложить ссылку на отчёт.
set -e
cd "$(dirname "$0")"

APP="dist/Cardboard.app"
[ -d "$APP" ] || { echo "Нет $APP — сначала соберите: python3 -m PyInstaller Cardboard.spec"; exit 1; }

VERSION=$(plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist")
ZIP="dist/Cardboard-${VERSION}.zip"
SUM="dist/Cardboard-${VERSION}.sha256.txt"

rm -f "$ZIP" "$SUM"
# ditto сохраняет структуру бандла и ресурс-форки корректнее, чем zip -r
ditto -c -k --keepParent "$APP" "$ZIP"
shasum -a 256 "$ZIP" | awk '{print $1 "  Cardboard-'"$VERSION"'.zip"}' > "$SUM"

echo "Готово:"
echo "  $ZIP ($(du -h "$ZIP" | cut -f1))"
echo "  $SUM: $(cat "$SUM")"
echo "Проверка у получателя: shasum -a 256 Cardboard-${VERSION}.zip"
