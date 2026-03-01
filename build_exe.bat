@echo off
echo 正在使用 uvx 临时调用 PyInstaller 打包 recorder.py...
echo ========================================================
uvx pyinstaller -F -w -n NanoNVR --add-binary "ffmpeg.exe;." --noconfirm recorder.py
echo ========================================================
echo 打包完成！请在当前目录下新生成的 dist 文件夹中查找 NanoNVR.exe。
pause
