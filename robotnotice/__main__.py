"""支持 ``python -m robotnotice`` 调用 CLI。"""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
