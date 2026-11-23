@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Octo-Replay (yerel sunucu)

where php >nul 2>nul
if errorlevel 1 (
  echo.
  echo PHP bulunamadi. Yerel olarak calistirmak icin PHP 8 gerekir:
  echo   1^) https://windows.php.net/download adresinden "Thread Safe" zip dosyasini indirin
  echo   2^) Zip'i C:\php klasorune cikarin
  echo   3^) C:\php klasorunu Windows PATH'ine ekleyin ve bu dosyayi yeniden calistirin
  echo.
  echo PHP kurmak istemiyorsaniz dosyalari hosting'e yukleyip adresten acin.
  echo.
  pause
  exit /b 1
)

if not exist cache mkdir cache
echo Octo-Replay http://localhost:8000 adresinde calisiyor. Kapatmak icin bu pencereyi kapatin.
start "" cmd /c "timeout /t 2 >nul & start http://localhost:8000"
php -S localhost:8000
