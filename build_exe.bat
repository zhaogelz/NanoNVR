@echo off
echo 正在使用 uvx 临时调用 PyInstaller 打包 recorder.py...
echo ========================================================
REM 用 %%~dp0 取本 bat 所在目录的绝对路径，避免相对路径在 specpath 下解析失败；
REM 用独立 build_tmp 工作目录，避免清理已提交的 build\ 缓存被安全机制拦截。
uvx pyinstaller -F -w -n NanoNVR --add-binary "%~dp0ffmpeg.exe;." --noconfirm --workpath "%~dp0build_tmp" --specpath "%~dp0build_tmp" "%~dp0recorder.py"
echo ========================================================
echo 打包完成！dist\NanoNVR.exe 即为单文件可执行程序（已内置 ffmpeg，拷贝到任意 Windows 电脑即可运行）。
if exist "%~dp0build_tmp" rmdir /s /q "%~dp0build_tmp"
pause
