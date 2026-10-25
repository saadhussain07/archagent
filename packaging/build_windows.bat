@echo off
REM Local Windows build (needs Python 3.10+ on the BUILD machine only).
REM The resulting dist\ArchAgent.exe runs on machines without Python.
python -m pip install -r requirements-dev.txt || exit /b 1
python -m pytest -q || exit /b 1
pyinstaller packaging\ArchAgent.spec --noconfirm --clean || exit /b 1
echo.
echo Built: dist\ArchAgent.exe  (GUI)   dist\ArchAgent-cli.exe  (console)
