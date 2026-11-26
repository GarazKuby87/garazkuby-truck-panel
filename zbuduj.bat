@echo off
rem Buduje jeden plik dist\TruckPanel.exe (Python 3.12+ i PyInstaller).
cd /d "%~dp0app"
python -m pip install --upgrade pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --onefile --console --name TruckPanel ^
  --add-data "%~dp0app\www;www" --add-data "%~dp0app\wtyczka;wtyczka" ^
  --distpath "%~dp0dist" --workpath "%TEMP%\truckpanel-build" --specpath "%TEMP%\truckpanel-build" ^
  serwer.py
