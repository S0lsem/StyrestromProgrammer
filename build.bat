@echo off
REM -------------------------------------------------------
REM  Build MRS_Programmer.exe
REM  Run this on a Windows 10/11 machine with Python 3.12
REM -------------------------------------------------------

echo Installing / updating build dependencies...
pip install pyinstaller PyQt6 python-can[pcan] cryptography --quiet

echo.
echo Building executable...
pyinstaller programmer_app.spec --clean

echo.
if exist "dist\MRS_Programmer.exe" (
    echo  SUCCESS — dist\MRS_Programmer.exe is ready.
) else (
    echo  BUILD FAILED — check the output above for errors.
)

pause
