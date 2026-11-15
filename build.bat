@echo off
rem =====================================================================
rem  HandFont build script - 64-bit Python (Qt6 / PySide6)
rem
rem  Usage:
rem    build.bat             -> folder build (onedir, fast start, default)
rem    build.bat onefile     -> single exe (onefile, slower start)
rem
rem  Output (onedir): dist\HandFont\HandFont.exe
rem  Output (onefile): dist\HandFont.exe
rem
rem  Self-checking: every component is verified before building, and any
rem  missing Python package is installed automatically. Python itself is
rem  installed through winget when it cannot be found on this machine.
rem
rem  No project data is shipped. The program creates an EMPTY
rem  handfont_project.db next to the exe on its first run.
rem
rem  After PyInstaller finishes, slim.py removes the Qt modules this app
rem  never loads, which takes the output from ~128 MB down to ~65 MB.
rem =====================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "MODE=onedir"
if /i "%~1"=="onefile" set "MODE=onefile"

echo.
echo === HandFont build [%MODE%] ===
echo.

rem ---------- 1/7 locate a 64-bit Python ----------
call :FIND_PY
if defined PY goto FOUND_PY
echo [1/8] Python not found, trying winget ...
call :INSTALL_PY
call :FIND_PY
if not defined PY goto ERR_NO_PYTHON
:FOUND_PY
echo [1/8] Python: %PY%
"%PY%" -c "import struct,sys;print('      Python',sys.version.split()[0],'|',struct.calcsize('P')*8,'bit')"
"%PY%" -c "import struct,sys;sys.exit(0 if struct.calcsize('P')==8 else 1)"
if errorlevel 1 goto ERR_NOT_64

rem ---------- 2/7 make sure pip itself works ----------
"%PY%" -m pip --version >nul 2>nul
if not errorlevel 1 goto PIP_OK
echo [2/8] pip is missing, bootstrapping ...
"%PY%" -m ensurepip --upgrade
if errorlevel 1 goto ERR_NO_PIP
"%PY%" -m pip --version >nul 2>nul
if errorlevel 1 goto ERR_NO_PIP
:PIP_OK
echo [2/8] pip ok

rem ---------- 3/7 runtime packages: PySide6 + fontTools ----------
"%PY%" -c "import PySide6" >nul 2>nul
if not errorlevel 1 goto PYSIDE_OK
echo [3/8] Installing PySide6 (about 200 MB, one time) ...
"%PY%" -m pip install --disable-pip-version-check PySide6
if errorlevel 1 goto ERR_NO_PYSIDE
"%PY%" -c "import PySide6" >nul 2>nul
if errorlevel 1 goto ERR_NO_PYSIDE
:PYSIDE_OK

"%PY%" -c "import fontTools" >nul 2>nul
if not errorlevel 1 goto FONTTOOLS_OK
echo [3/8] Installing fontTools ...
"%PY%" -m pip install --disable-pip-version-check fonttools
if errorlevel 1 goto ERR_PIP
"%PY%" -c "import fontTools" >nul 2>nul
if errorlevel 1 goto ERR_PIP
:FONTTOOLS_OK
"%PY%" -c "import PySide6,fontTools;print('      Qt',PySide6.__version__,'| fontTools',fontTools.version)"
if errorlevel 1 goto ERR_NO_PYSIDE

rem ---------- 4/7 build venv + PyInstaller ----------
if not exist ".venv_build\Scripts\python.exe" goto MAKE_VENV
".venv_build\Scripts\python.exe" -c "import sys" >nul 2>nul
if not errorlevel 1 goto VENV_OK
echo [4/8] .venv_build is broken, rebuilding ...
rmdir /s /q .venv_build
:MAKE_VENV
echo [4/8] Creating isolated build venv ...
"%PY%" -m venv .venv_build --system-site-packages
if errorlevel 1 goto ERR_NO_VENV
:VENV_OK
set "VPY=.venv_build\Scripts\python.exe"
if not exist "%VPY%" goto ERR_NO_VENV
"%VPY%" -c "import PyInstaller,sys;v=tuple(int(x) for x in PyInstaller.__version__.split('.')[:2]);sys.exit(0 if v>=(6,0) else 1)" >nul 2>nul
if not errorlevel 1 goto PYI_READY
echo [4/8] Installing PyInstaller into the build venv ...
"%VPY%" -m pip install --disable-pip-version-check -q "pyinstaller>=6.10"
if errorlevel 1 goto ERR_PIP
"%VPY%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 goto ERR_PIP
:PYI_READY
"%VPY%" -c "import PyInstaller;print('      PyInstaller',PyInstaller.__version__)"
if errorlevel 1 goto ERR_PIP

rem ---------- 5/7 icon (app.ico is the only icon source) ----------
if not exist "app.ico" goto ERR_NO_ICO
echo [5/8] Icon: app.ico

rem ---------- 6/7 clean old output and build ----------
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist HandFont.spec del /q HandFont.spec

set "ONEFILE="
if /i "%MODE%"=="onefile" set "ONEFILE=--onefile"
echo [6/8] Building, this takes 1-3 minutes ...
"%VPY%" -m PyInstaller --noconfirm --clean --windowed --name HandFont --icon app.ico %ONEFILE% --contents-directory _internal --add-data "char_freq.txt;." --add-data "app.ico;." handfont.py
if errorlevel 1 goto ERR_BUILD

rem ---------- 7/8 trim unused Qt payload ----------
if /i "%MODE%"=="onefile" goto SLIM_SKIP
echo [7/8] Trimming unused Qt files ...
"%VPY%" slim.py "dist\HandFont"
if errorlevel 1 goto ERR_SLIM
goto SLIM_DONE
:SLIM_SKIP
echo [7/8] Slim step skipped (onefile is already compressed)
:SLIM_DONE

rem ---------- 8/8 result ----------
if /i "%MODE%"=="onefile" goto OUT_ONEFILE
set "EXE=dist\HandFont\HandFont.exe"
goto CHECK_EXE
:OUT_ONEFILE
set "EXE=dist\HandFont.exe"
:CHECK_EXE
if not exist "%EXE%" goto ERR_BUILD
echo [8/8] Done
echo.
echo =====================================================
echo  EXE : %CD%\%EXE%
for %%A in ("%EXE%") do echo  Size: %%~zA bytes
echo  Data: empty. handfont_project.db is created on first run.
echo =====================================================
echo.
pause
exit /b 0

rem ===================== subroutines =====================
:FIND_PY
set "PY="
for /f "delims=" %%I in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%I"
if defined PY goto :EOF
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY goto :EOF
if exist "C:\Program Files\Python312\python.exe" set "PY=C:\Program Files\Python312\python.exe"
if defined PY goto :EOF
if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"
if defined PY goto :EOF
for /f "delims=" %%I in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%I"
goto :EOF

:INSTALL_PY
where winget >nul 2>nul
if errorlevel 1 goto :EOF
echo        Installing Python 3.12 with winget, this may take a few minutes ...
winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
rem winget returns non-zero when it is already installed, so ignore the code here
goto :EOF

rem ===================== errors =====================
:ERR_NO_PYTHON
echo [ERROR] Python 3.12 (64-bit) is not available.
echo         Install it from https://www.python.org/downloads/windows/
echo         and tick "Add python.exe to PATH".
goto FAIL
:ERR_NOT_64
echo [ERROR] This Python is 32-bit. PySide6 needs 64-bit Python.
echo         Run build_x86_py38.bat for a 32-bit build instead.
goto FAIL
:ERR_NO_PIP
echo [ERROR] pip is not available for this Python.
echo         Try:  "%PY%" -m ensurepip --upgrade
goto FAIL
:ERR_NO_PYSIDE
echo [ERROR] PySide6 is still not importable after installation.
echo         Run manually:  "%PY%" -m pip install PySide6
goto FAIL
:ERR_NO_VENV
echo [ERROR] Could not create .venv_build.
goto FAIL
:ERR_PIP
echo [ERROR] pip install failed (check network / proxy).
goto FAIL
:ERR_NO_ICO
echo [ERROR] app.ico is missing in this folder.
echo         app.ico is the single icon source for the build.
goto FAIL
:ERR_SLIM
echo [ERROR] slim.py failed. The build itself is in dist\HandFont and still usable.
goto FAIL
:ERR_BUILD
echo [ERROR] PyInstaller failed. Read the messages above.
goto FAIL
:FAIL
echo.
pause
exit /b 1
