"""EvoClaw v2.0 — 默认配置。

DEFAULT_CONFIG 现在**从 params.py 的参数注册表派生**，这里只做转发 ——
参数只在 params.py 里定义一次，改默认值去那里改。

保留这个模块名是为了不破坏现有的 `from config import DEFAULT_CONFIG`。
历史沿革：以前默认值写在这里、代码里还有 79 处 `cfg.get(键, 字面量)` 兜底、
DB 是运行时真源 —— 三处任何一处对不上就是「改了没生效」。
"""
from params import build_defaults

DEFAULT_CONFIG = build_defaults()
