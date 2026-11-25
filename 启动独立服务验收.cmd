@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo PaperLens 独立账户本机验收
echo 浏览器打开 http://127.0.0.1:8080
echo 此模式不发送邮件；验证码保存在 .standalone\test-mail 文件夹。
echo 数据独立保存在 .standalone，不会修改现用网站的数据。
".venv\Scripts\python.exe" -X utf8 -m paperlens.standalone --data ".standalone" --origin "http://127.0.0.1:8080" --port 8080 --dev-mail
pause
