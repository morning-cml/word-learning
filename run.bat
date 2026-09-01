@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Word Learning

rem ======================================================================
rem  一键启动。
rem
rem  界面是 web/ 那一套页面，由 app.py 起一个本地服务再用 Qt 窗口显示出来。
rem  用起来还是一个应用窗口：没有浏览器、没有地址栏、不用先启动什么。
rem
rem  这不是打包好的可执行文件——它每次都直接跑当前源码，并且在启动前
rem  自己把环境修好：
rem     .venv 不存在        -> 自动创建
rem     requirements 变了   -> 自动重装依赖
rem     依赖被删/装坏       -> 自动补装
rem     CEFR 词表缺失       -> 自动下载
rem  所以之后不管代码怎么改、加了什么依赖，双击它都还能用。
rem
rem  用法：
rem     双击即可。首次启动会自动建环境装依赖，之后每次约 1 秒。
rem
rem     run.bat            正常启动，不留控制台窗口
rem     run.bat -c         带控制台启动，能看到运行日志，排查问题用
rem     run.bat --check    只检查并修好环境，不启动应用
rem     run.bat --reset    删掉 .venv 重建，环境彻底坏了用这个
rem ======================================================================

set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "STAMP=%VENV%\.deps-hash"
set "CONSOLE="
set "RESET="
set "CHECKONLY="

for %%A in (%*) do (
    if /i "%%~A"=="-c"        set "CONSOLE=1"
    if /i "%%~A"=="--console" set "CONSOLE=1"
    if /i "%%~A"=="--reset"   set "RESET=1"
    if /i "%%~A"=="--check"   set "CHECKONLY=1"
)

if defined RESET (
    echo [重置] 正在删除 %VENV% ...
    rmdir /s /q "%VENV%" 2>nul
)

rem ---------------------------------------------------------------- 环境
rem venv 存在不等于能用：项目被移动、整个文件夹拷到别的机器、或者底层
rem Python 被卸载 / 升级，都会让它变成一个跑不起来的壳。先实际执行一下。
if exist "%PY%" (
    "%PY%" -c "" >nul 2>&1
    if errorlevel 1 (
        echo [环境] 现有虚拟环境已失效（项目被移动、换了机器，或底层 Python 变了），正在重建 ...
        rmdir /s /q "%VENV%" 2>nul
    )
)
if not exist "%PY%" (
    rem 目录残留但没有解释器 = 半个坏环境，先清干净；
    rem 直接 python -m venv 只会「更新」它，坏内容会原样留下。
    if exist "%VENV%" rmdir /s /q "%VENV%" 2>nul
    call :make_venv
    if errorlevel 1 goto :fatal
)

rem ---------------------------------------------------------------- 依赖
call :sync_deps
if errorlevel 1 goto :fatal

rem ------------------------------------------------- 词表（缺了也能跑）
if not exist "data\cefr.csv" (
    echo [数据] 首次运行，正在下载 CEFR-J 词表 ...
    "%PY%" scripts\fetch_cefr.py
    if errorlevel 1 (
        echo [数据] 下载失败，先用内置兜底词表运行；
        echo        联网后可以随时再跑一次 scripts\fetch_cefr.py 提高难度判定精度。
    )
)

rem ---------------------------------------------------------------- 启动
if defined CHECKONLY (
    echo.
    "%PY%" -c "import sys,PySide6,fastapi,sqlalchemy,httpx; print('[检查] 环境正常  Python ' + sys.version.split()[0] + '  PySide6 ' + PySide6.__version__ + '  FastAPI ' + fastapi.__version__)"
    "%PY%" -c "import sys; sys.path.insert(0,'.'); from core.lexicon import cefr; print('[检查] 词表 ' + ('CEFR-J ' if cefr.is_real_data() else '内置兜底 ') + str(cefr.size()) + ' 词')"
    "%PY%" -c "import sys; sys.path.insert(0,'.'); from core import settings; p,m=settings.active(); print('[检查] 模型 ' + p + ' / ' + m + ('  Key 已配置' if settings.api_key(p) else '  尚未配置 Key'))"
    "%PY%" -c "import sys; sys.path.insert(0,'.'); from core.store import db; db.init_db(); s=db.backup_state(); print('[检查] 备份 ' + (('失败：' + s['error']) if not s['ok'] else (str(s['count']) + ' 份快照，最近 ' + s['latest']) if s['count'] else '暂无数据可备份'))"
    echo.
    echo 环境就绪，可以直接双击 run.bat 启动。
    exit /b 0
)

if defined CONSOLE (
    echo [启动] 带控制台运行 ...
    echo.
    "%PY%" app.py
    set "CODE=!errorlevel!"
    echo.
    echo 应用已退出，退出码 !CODE!
    pause
    exit /b !CODE!
)

rem 用 pythonw 启动：没有控制台窗口。异常不会因此丢失——app.py 会把
rem 完整堆栈写进 data\last-error.log 并弹一个系统对话框。
rem
rem 这里不要加 >nul 2>&1 之类的重定向：实测会让 start 起不来应用。
start "" "%PYW%" app.py
exit /b 0


rem ======================================================================
rem  子过程
rem ======================================================================

:make_venv
echo [环境] 没找到虚拟环境，正在创建（首次启动需要一两分钟）...

rem py 启动器最可靠，按版本从新到旧试；都没有再退回 PATH 里的 python
set "BOOTPY="
for %%V in (3.13 3.12 3.11 3.10) do (
    if not defined BOOTPY (
        py -%%V -c "" >nul 2>&1 && set "BOOTPY=py -%%V"
    )
)
if not defined BOOTPY py -3 -c "" >nul 2>&1 && set "BOOTPY=py -3"
if not defined BOOTPY python -c "" >nul 2>&1 && set "BOOTPY=python"

if not defined BOOTPY (
    echo.
    echo [错误] 没有找到 Python。
    echo        请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/
    echo        安装时务必勾选 "Add python.exe to PATH"。
    exit /b 1
)

!BOOTPY! -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [错误] 找到的 Python 版本太低，本项目需要 3.10 或更高。
    !BOOTPY! -c "import sys; print('        当前版本：' + sys.version)"
    exit /b 1
)

!BOOTPY! -m venv "%VENV%"
if errorlevel 1 (
    echo.
    echo [错误] 创建虚拟环境失败。
    exit /b 1
)

rem MSYS2 / Cygwin 版的 Python 会建出 bin/ 布局的 venv，而且装不上带
rem 编译扩展的包（PySide6、pydantic 都会失败）。这里提前拦下来。
if not exist "%PY%" (
    echo.
    echo [错误] 虚拟环境建好了，但里面没有 Scripts\python.exe。
    echo        这通常说明用的是 MSYS2 / Cygwin 版 Python。
    echo        请安装官方 Windows 版 Python 后重试。
    rmdir /s /q "%VENV%" 2>nul
    exit /b 1
)
echo [环境] 虚拟环境创建完成。
exit /b 0


:sync_deps
rem 依赖是否需要动，看两件事：
rem   1. requirements.txt 的哈希跟上次装的时候是否一致
rem   2. 关键包是不是真的还在（防止 site-packages 被删或装坏）
set "REQHASH="
for /f "skip=1 delims=" %%H in ('certutil -hashfile "requirements.txt" SHA256 2^>nul') do (
    if not defined REQHASH set "REQHASH=%%H"
)
set "OLDHASH="
if exist "%STAMP%" set /p OLDHASH=<"%STAMP%"

set "NEED="
set "BROKEN="
if not "!REQHASH!"=="!OLDHASH!" set "NEED=1"
"%PY%" -c "import importlib.util as u,sys; sys.exit(0 if all(u.find_spec(m) for m in ('PySide6','fastapi','uvicorn','jinja2','sqlalchemy','httpx','yaml')) else 1)" >nul 2>&1
if errorlevel 1 (
    set "NEED=1"
    set "BROKEN=1"
)
if not defined NEED exit /b 0

rem 环境被破坏时必须 --force-reinstall。pip 只看 .dist-info 元数据，
rem 包目录被删了它照样报 "already satisfied" 然后跳过，文件永远补不回来。
set "FORCE="
if defined BROKEN (
    echo [依赖] 检测到有包损坏或缺失，强制重装 ...
    set "FORCE=--force-reinstall"
) else if defined OLDHASH (
    echo [依赖] requirements.txt 有变化，正在同步 ...
) else (
    echo [依赖] 正在安装依赖（首次需要几分钟，PySide6 带着 Chromium 比较大）...
)

"%PY%" -m pip install --quiet --disable-pip-version-check --upgrade pip
"%PY%" -m pip install --disable-pip-version-check !FORCE! -r requirements.txt
if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败。常见原因：网络不通，或者需要换国内镜像源。
    echo        换源示例：
    echo          "%PY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    exit /b 1
)

rem 装完再确认一遍，避免 pip 返回 0 但包其实没装上
"%PY%" -c "import importlib.util as u,sys; sys.exit(0 if all(u.find_spec(m) for m in ('PySide6','fastapi','uvicorn','jinja2','sqlalchemy','httpx','yaml')) else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [错误] 依赖装完了但关键包仍然导入不了。
    echo        请运行：run.bat --reset   （删掉 .venv 完全重建，不会影响你的文章和词表）
    exit /b 1
)

> "%STAMP%" echo !REQHASH!
echo [依赖] 就绪。
exit /b 0


:fatal
echo.
echo 启动中止，原因见上。
pause
exit /b 1
