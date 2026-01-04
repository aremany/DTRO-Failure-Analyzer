@echo off
chcp 65001
echo ==========================================
echo 독립형 장애 분석기를 시작합니다...
echo ==========================================

echo 1. Ollama 서버를 별도 창에서 실행합니다...
start "Ollama Server" cmd /k ollama serve

echo 2. Ollama 서버가 준비될 때까지 5초간 대기합니다...
timeout /t 5 >nul

echo 3. 브라우저를 실행합니다...
start http://localhost:8002

echo 4. 분석기 프로그램을 실행합니다...
python main.py

pause
