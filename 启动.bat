@echo off
rem 双击这个文件打开图形界面（不弹黑框）
cd /d "%~dp0"
start "" pythonw.exe "%~dp0ui.py"
exit
