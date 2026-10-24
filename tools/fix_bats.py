"""把 .bat 文件重写成 Windows cmd 能稳定解析的格式。

之前踩过的两个坑：
  1. 换行是 LF（不是 CRLF）→ cmd 解析多行 if / 括号块会失败，脚本闪退；
  2. 文件是 UTF-8、控制台是 936 → 中文变乱码，甚至被拆断导致语法错误。

现在统一：CRLF 换行 + 纯 ASCII 内容。
所有中文提示交给 Python 打印（它走 Windows 的 Unicode 控制台接口，
不受控制台代码页影响），bat 只负责切目录和调 Python。

用法： python tools/fix_bats.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FILES: dict[str, str] = {
    "proxy-test.bat": """@echo off
title mercari-watch proxy test
cd /d "%~dp0"
echo.
echo Paste your proxy URL below (right-click to paste), then press Enter.
echo Format: http://user:pass@gate.example.com:12345
echo.
set "PROXY="
set /p PROXY=Proxy URL: 
if "%PROXY%"=="" goto empty
echo.
python -m mercari_watch proxy-test --proxy "%PROXY%"
echo.
pause
exit /b 0

:empty
echo.
echo No URL given, nothing to test.
pause
exit /b 2
""",
    "check.bat": """@echo off
title mercari-watch doctor
cd /d "%~dp0"
echo Running self-check (network / parsing / email). Takes 1-2 minutes...
echo.
python -m mercari_watch doctor %*
echo.
pause
""",
    "once.bat": """@echo off
title mercari-watch once
cd /d "%~dp0"
echo Running one cycle...
echo.
python -m mercari_watch once %*
echo.
pause
""",
    "run.bat": """@echo off
title mercari-watch running
cd /d "%~dp0"
echo Watching. Closing this window stops it.
echo For auto-start on login use scripts\\install_task.ps1
echo.
python -m mercari_watch run %*
if errorlevel 1 pause
""",
    "probe.bat": """@echo off
title mercari-watch probe
cd /d "%~dp0"
echo Probing direct access to Mercari (no proxy). About 1 minute...
echo.
python tools\\probe_network.py %*
echo.
pause
""",
}


def main() -> int:
    for name, text in FILES.items():
        path = ROOT / name
        body = text.replace("\r\n", "\n").replace("\n", "\r\n")
        data = body.encode("ascii")
        path.write_bytes(data)
        print(f"  wrote {name}  ({len(data)} bytes, ASCII, CRLF)")
    print(f"{len(FILES)} files updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
