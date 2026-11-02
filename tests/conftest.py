"""以宿主的插件包名加载当前工作树；SDK/HTTP/加密等依赖均使用真实安装包。"""
from pathlib import Path
from types import ModuleType

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for name, path in (("plugins", ROOT.parent), ("plugins.qq_official_adapter", ROOT)):
    module = ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module
sys.modules["plugins"].qq_official_adapter = sys.modules["plugins.qq_official_adapter"]
