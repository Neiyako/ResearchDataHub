"""测试共用的哨兵字符串（放在独立模块，测试文件可直接导入）。

为什么不用相对导入 conftest
--------------------------
``tests/unit/test_environment`` 没有 ``__init__.py``（与项目其它测试目录一致），
因此不能 ``from .conftest import ...``。哨兵值放在这里，conftest 也复用同一来源。
"""

from __future__ import annotations

SECRET_SENTINEL = "SENTINEL-SECRET-4f8a1c9e-DO-NOT-LEAK"
"""用于断言「报告里不含该值」的哨兵字符串。"""

SECRET_SENTINEL_2 = "SENTINEL-SECRET-zzz-second-7b3d"
"""第二个哨兵（验证多个 secret 同时打码）。"""
