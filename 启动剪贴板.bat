@echo off
rem 启动剪贴板历史工具(托盘常驻，无黑窗口)
cd /d "%~dp0"
start "" "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\pythonw.exe" "%~dp0main.py"