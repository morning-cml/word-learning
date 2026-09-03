@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
rem cd 失败不会让批处理停下来——它会**接着在原来的目录里跑**。双击时那通常是
rem System32，于是下面会往那里建 .venv、找不到 requirements.txt，报出来的错
rem 和真正的原因（路径）毫无关系。两种常见的失败法，都不报错：
rem   · 路径里有感叹号。上一行开了 EnableDelayedExpansion，感叹号会被吃掉，
rem     实测 "danci!xuexi" 下 cd 直接失败，%CD% 还停在 C:\Windows。
rem   · 从网络路径运行（\\服务器\共享）。cmd 不支持把 UNC 当工作目录。
rem 拿一个必然存在的项目文件当判据，比逐个去猜原因可靠。
if not exist "requirements.txt" (
    echo.
    echo [错误] 没能切换到项目目录，现在还在 "%CD%"。
    echo        最常见的原因是项目路径里带了感叹号，其次是从网络共享上运行。
    echo        把整个文件夹挪到一个不带感叹号的本地路径下，再双击一次。
    echo.
    pause
    exit /b 1
)
title Word Learning

rem ======================================================================
rem  一键启动。
rem
rem  做的事只有一件：起一个只监听 127.0.0.1 的本地服务，再用 Edge 打开它。
rem  界面是 web/ 那一套 HTML/CSS/JS，跑在浏览器里。
rem
rem  这个窗口就是服务本身，关掉它程序就退出。
rem
rem  它不是打包好的可执行文件——每次都直接跑当前源码，并且在启动前
rem  自己把环境修好：
rem     .venv 不存在        -> 自动创建
rem     requirements 变了   -> 自动重装依赖
rem     依赖被删/装坏       -> 自动补装
rem     CEFR 词表缺失       -> 自动下载
rem  所以之后不管代码怎么改、加了什么依赖，双击它都还能用。
rem
rem  用法：
rem     双击即可。首次启动会自动建环境装依赖，之后每次一两秒。
rem
rem     run.bat                  正常启动
rem     run.bat --check          只检查并修好环境，不启动应用
rem     run.bat --reset          删掉 .venv 重建，然后照常启动
rem     run.bat --reset --check  重建完就停下，不启动
rem
rem  --reset 是「环境坏了，重置一下继续用」，所以重建完会接着启动。
rem ======================================================================

set "VENV=.venv"
set "PY=%VENV%\Scripts\python.exe"
set "STAMP=%VENV%\.deps-hash"
set "RESET="
set "CHECKONLY="

for %%A in (%*) do (
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
    "%PY%" -c "import sys,fastapi,uvicorn,sqlalchemy,httpx; print('[检查] 环境正常  Python ' + sys.version.split()[0] + '  FastAPI ' + fastapi.__version__)"
    "%PY%" -c "import sys; sys.path.insert(0,'.'); from core.lexicon import cefr; print('[检查] 词表 ' + ('CEFR-J ' if cefr.is_real_data() else '内置兜底 ') + str(cefr.size()) + ' 词')"
    "%PY%" -c "import sys; sys.path.insert(0,'.'); from core import settings; p,m=settings.active(); print('[检查] 模型 ' + p + ' / ' + m + ('  Key 已配置' if settings.api_key(p) else '  尚未配置 Key'))"
    "%PY%" -c "import sys; sys.path.insert(0,'.'); from core.store import db; db.init_db(); s=db.backup_state(); print('[检查] 备份 ' + (('失败：' + s['error']) if not s['ok'] else (str(s['count']) + ' 份快照，最近 ' + s['latest']) if s['count'] else '暂无数据可备份'))"
    "%PY%" -c "import sys; sys.path.insert(0,'.'); import main; p=main._edge_path(); print('[检查] Edge ' + (p if p else '没找到，启动时会退回系统默认浏览器'))"
    echo.
    echo 环境就绪，可以直接双击 run.bat 启动。
    exit /b 0
)

rem 前台跑，这个窗口就是服务。不用 start / pythonw 把它藏起来：
rem 藏了之后用户没有任何办法结束它，只能去任务管理器找 python.exe。
"%PY%" main.py
set "CODE=!errorlevel!"
if not "!CODE!"=="0" (
    echo.
    echo 应用异常退出，退出码 !CODE!
    pause
)
exit /b !CODE!


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
rem 编译扩展的包（pydantic 就会失败）。这里提前拦下来。
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
"%PY%" -c "import importlib.util as u,sys; sys.exit(0 if all(u.find_spec(m) for m in ('fastapi','uvicorn','jinja2','sqlalchemy','httpx','yaml')) else 1)" >nul 2>&1
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
    echo [依赖] 正在安装依赖（首次需要一两分钟）...
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
"%PY%" -c "import importlib.util as u,sys; sys.exit(0 if all(u.find_spec(m) for m in ('fastapi','uvicorn','jinja2','sqlalchemy','httpx','yaml')) else 1)" >nul 2>&1
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
