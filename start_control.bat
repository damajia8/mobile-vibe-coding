@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo.
echo ╔══════════════════════════════════════════════════════╗
echo ║     Mobile-Vibe-Monitor  启动控制器  v3.0            ║
echo ╚══════════════════════════════════════════════════════╝
echo.

:: ── 1. 检测 .vibe_env 虚拟环境是否存在 ──
if not exist "%~dp0.vibe_env\Scripts\python.exe" (
    echo [1/3] .vibe_env 未找到，正在创建隔离虚拟环境...
    python -m venv ".vibe_env"
    if errorlevel 1 (
        echo [ERROR] 虚拟环境创建失败，请确认系统已安装 Python 3.8+
        pause
        exit /b 1
    )
    echo [✓] .vibe_env 虚拟环境已创建

    :: ── 2. 激活并安装依赖 ──
    echo [2/3] 正在安装依赖到 .vibe_env ...
    call "%~dp0.vibe_env\Scripts\activate.bat" >nul 2>&1
    python -m pip install --quiet --upgrade pip
    python -m pip install --quiet fastapi uvicorn[standard] watchdog psutil
    if errorlevel 1 (
        echo [ERROR] 依赖安装失败，请检查网络连接
        pause
        exit /b 1
    )
    echo [✓] 依赖安装完成 (fastapi + uvicorn + watchdog + psutil)
) else (
    echo [1/3] .vibe_env 已存在，跳过创建
    echo [2/3] 检测依赖完整性...
    call "%~dp0.vibe_env\Scripts\activate.bat" >nul 2>&1
    python -c "import fastapi, uvicorn, watchdog, psutil" 2>nul
    if errorlevel 1 (
        echo [2/3] 依赖缺失，正在补装...
        python -m pip install --quiet --upgrade pip
        python -m pip install --quiet fastapi uvicorn[standard] watchdog psutil
        if errorlevel 1 (
            echo [ERROR] 依赖安装失败，请检查网络连接
            pause
            exit /b 1
        )
    )
    echo [✓] 依赖就绪 (fastapi + uvicorn + watchdog + psutil)
)

:: ── 3. 使用 .vibe_env 的解释器启动服务 ──
echo [3/3] 正在启动 Mobile-Vibe-Monitor 服务...
echo.
call "%~dp0.vibe_env\Scripts\activate.bat" >nul 2>&1
python "%~dp0vibe_server.py"

pause
