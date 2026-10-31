"""Pytest conftest: 让插件可独立运行测试。

独立仓库根目录本身就是插件包（codec.py / plugin.py / settings.py 位于根目录），
plugin.py 内部使用 ``from .codec import ...`` 等相对导入，需要把仓库根目录注册为
``qq_official_adapter`` 包后才能直接导入。部署到 MaiBot 的 ``plugins/`` 目录后
由宿主加载器负责导入，此 conftest 不会产生影响。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = "qq_official_adapter"

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_ROOT)]
    sys.modules[_PACKAGE] = package
