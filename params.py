"""参数注册表 —— 每个配置参数的**唯一真源**。

以前一个参数散落在三处：`config.py` 的默认值、代码里 `cfg.get("键", 字面量)`
的兜底、SQLite config 表。任何一处对不上就是「改了没生效」——
实测出现过同一参数三处默认值不同（`auto_range_pct` 有 0 / 0.25 / 0.06 三种）。

现在：**只在这里定义一次**，其余全部派生
  · `config.DEFAULT_CONFIG`  ← `build_defaults()`
  · 接口校验规则             ← `validate()`
  · 前端参数表的元数据        ← `groups()`
  · 启动时补齐缺失的键        ← `build_defaults()`

改参数的默认值 = 改这个文件的一行，全局生效。

⚠ 两个不能碰的东西（都是实测踩出来的）：
1. **`grid_selection` 不是参数**，它是每轮选币写回 config 表的运行时状态。
   任何「按注册表重建 config」的逻辑都会把它删掉 → 选币结果丢失、持仓币被
   误判掉出清单。补齐逻辑一律「只增不改」，且必须放行注册表里没有的键。
2. **带 sentinel 的参数不能被「补默认值」**：`auto_select` 补成 True 会从手动币
   切到自动选币（真金白银买完全不同的币）；`size_usdt` 补成 20 会让本来
   「跳过」的标的变成每格 20U 真实下单。见各条的 `sentinel` 字段。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ===== 分类（决定前端 tab 的顺序与标题）=====
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("global",       "全局",       "模式、循环、下单下限 —— 影响所有策略"),
    ("grid_basic",   "网格·基础",  "杠杆、单格金额、档位数量"),
    ("grid_follow",  "网格·跟随",  "区间重排、最小止盈、不追高"),
    ("grid_entry",   "网格·入场",  "首次建仓的择时闸门"),
    ("grid_short",   "网格·空单",  "高位试空（独立开关，默认关）"),
    ("grid_select",  "网格·选币",  "自动选币的周期与合格线"),
    ("dca",          "定投",       "方案C · 现货定投"),
    ("advanced",     "高级",       "密钥与密码（敏感，不在参数表内编辑）"),
)

# 哨兵语义：0 / False 不是「小」或「关」，而是「自动/无上限/跳过/回退」
SENTINEL_HINT = {
    "auto": "0 = 按波动率/资金自动推导（推荐）",
    "unlimited": "0 = 不设上限",
    "skip": "0 = 跳过该标的，不建仓",
    "fallback": "0 = 回退到旧字段（不推荐）",
    "off": "关闭该机制",
}


@dataclass(frozen=True)
class Param:
    key: str                      # 点分路径，如 "grid.short_stop_pct"
    type: str                     # bool|int|float|str|enum|str_list|symbol_list|kwargs|password
    default: Any
    group: str
    label: str                    # 中文短名（表格第一列）
    desc: str = ""                # 说人话的说明（表格最后一列 / tooltip）
    widget: str = "number"        # switch|number|percent|select|multiselect|symbols|password|hidden
    hot: bool = True              # 改完是否下一个 tick 生效（False = 需重启）
    unit: str = ""
    scale: float = 1.0            # UI 显示倍率；校验一律针对**原始值**
    min: float | None = None
    max: float | None = None
    step: float | None = None
    sentinel: str | None = None   # auto|unlimited|skip|fallback|off
    choices: tuple | None = None
    sensitive: bool = False       # 不回显明文
    in_defaults: bool = True      # 是否属于 DEFAULT_CONFIG（False = 仅运行时键）
    hidden: bool = False          # 不进参数表（已废弃/从未被读取）
    enabled_when: tuple | None = None   # 联动：仅当 (另一个键, 期望值) 成立时可编辑

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0] if "." in self.key else ""


# ==========================================================================
# 注册表本体
# ==========================================================================
PARAMS: tuple[Param, ...] = (
    # ---------------- 全局 ----------------
    Param("mode", "enum", "off", "global", "策略模式",
          "off=只监控不下单；grid=低杠杆网格；dca=现货定投",
          widget="select", choices=("off", "grid", "dca")),
    Param("dry_run", "bool", True, "global", "模拟盘",
          "开启时完整计算并记录「本应下的单」，但不发真实订单", widget="switch",
          sentinel="off"),
    Param("loop_interval", "int", 10, "global", "循环间隔", "主循环多少秒跑一轮",
          unit="秒", min=3, max=600, step=1),
    Param("min_order_notional", "float", 5, "global", "最小下单金额",
          "低于此值的下单会被跳过（交易所自身下限更大时取交易所的）",
          unit="U", min=0, max=100000),
    Param("order_notional_safety", "float", 1.02, "global", "下单金额安全系数",
          "下单金额相对交易所最小名义值的放大倍数，防止卡在边界被拒",
          unit="倍", min=1.0, max=3.0, step=0.01),
    Param("exchange", "str", "binanceusdm", "global", "交易所", "已废弃：代码从未读取",
          hidden=True),

    # ---------------- 网格·基础 ----------------
    Param("grid.leverage", "int", 3, "grid_basic", "杠杆",
          "逐仓杠杆，越低越安全（2~3 足够）", unit="倍", min=1, max=20, step=1),
    Param("grid.size_usdt", "float", 20, "grid_basic", "单格金额",
          "每一档的名义金额，决定网格的颗粒度", unit="U", min=0, max=100000,
          sentinel="skip"),
    Param("grid.max_exposure_ratio", "float", 3.0, "grid_basic", "敞口上限",
          "总名义价值 ÷ 权益 的上限，超过就停止买入", unit="倍",
          min=0.1, max=20, step=0.1),
    Param("grid.grids", "int", 0, "grid_basic", "档位数量",
          "留 0 按波动率自动推导（推荐），填正数则固定档数", unit="档",
          min=0, max=200, step=1, sentinel="auto"),
    Param("grid.auto_range_pct", "float", 0, "grid_basic", "区间宽度",
          "网格上下轨相对现价的比例。留 0 随标的波动率自适应（推荐）",
          widget="percent", unit="%", scale=100, min=0, max=0.5, step=0.001,
          sentinel="auto"),
    Param("grid.max_grids", "int", 15, "grid_basic", "档数上限",
          "自动推导档位时的上限，防止高波动币档数爆炸", unit="档",
          min=2, max=100, step=1),

    # ---------------- 网格·跟随 ----------------
    Param("grid.auto_recenter", "bool", True, "grid_follow", "自动重排区间",
          "价格突破区间后网格自动重新居中", widget="switch", sentinel="off"),
    Param("grid.recenter_margin", "float", 0.01, "grid_follow", "重排触发幅度",
          "突破区间多少才触发重排", widget="percent", unit="%", scale=100,
          min=0, max=0.2, step=0.001),
    Param("grid.recenter_cooldown_min", "int", 30, "grid_follow", "重排冷却",
          "同一标的两次重排的最小间隔", unit="分钟", min=0, max=1440, step=1),
    Param("grid.min_tp_ratio", "float", 0.002, "grid_follow", "最小止盈率",
          "卖价 ≥ 成本×(1+此值)，保证每笔卖出真的赚钱", widget="percent",
          unit="%", scale=100, min=0, max=0.1, step=0.0005),
    Param("grid.step_ratio", "float", 0.3, "grid_follow", "档距系数",
          "自动推导的档距 = 日振幅 × 此值，越小档越密", unit="倍",
          min=0.05, max=2.0, step=0.05),
    Param("grid.reentry_tol", "float", 0.003, "grid_follow", "不追高容差",
          "清仓后只有价格回落到「上次清仓价×(1+此值)」以内才允许重新建仓",
          widget="percent", unit="%", scale=100, min=0, max=0.1, step=0.0005),
    Param("grid.ext_close_cooldown_min", "int", 30, "grid_follow", "被平仓后冷静期",
          "检测到被手工平仓后，多少分钟内（且不在选币清单里时）不重新建仓",
          unit="分钟", min=0, max=1440, step=1),

    # ---------------- 网格·入场 ----------------
    Param("grid.entry_limit_only", "bool", True, "grid_entry", "只用限价建仓",
          "首次建仓一律挂限价买单等回落，绝不市价追高", widget="switch",
          sentinel="off"),
    Param("grid.entry_pos_max", "float", 0.6, "grid_entry", "建仓位置门槛",
          "现价在 24h 区间的位置高于此值就不建仓（等回落）。0.6 即 60%",
          widget="percent", unit="%", scale=100, min=0, max=1, step=0.05),
    Param("grid.entry_min_discount", "float", 0.004, "grid_entry", "最小建仓折价",
          "位置极低时挂单只比现价低这么多（越容易成交）", widget="percent",
          unit="%", scale=100, min=0, max=0.1, step=0.0005),

    # ---------------- 网格·空单 ----------------
    Param("grid.short_enabled", "bool", False, "grid_short", "高位试空",
          "开启后：价格到 24h 区间高位并确认回落时，挂一张最小仓空单并带交易所止损；"
          "平掉后该币不再开空。关闭时不再扫描机会，未成交的挂单会被撤掉，"
          "已成交的持仓仍继续止盈/止损", widget="switch", sentinel="off"),
    Param("grid.short_pos_min", "float", 0.85, "grid_short", "高位门槛",
          "现价在 24h 区间的位置 ≥ 此值才算高位", widget="percent", unit="%",
          scale=100, min=0, max=1, step=0.01),
    Param("grid.short_confirm", "float", 0.01, "grid_short", "回落确认",
          "还要从当日最高回落这么多才开（避免还在往上冲）", widget="percent",
          unit="%", scale=100, min=0, max=0.2, step=0.001),
    Param("grid.short_entry_offset", "float", 0.025, "grid_short", "挂单偏移",
          "开空挂单价 = 现价×(1+此值)。挂得越高越不容易成交",
          widget="percent", unit="%", scale=100, min=0, max=0.2, step=0.001),
    Param("grid.short_stop_pct", "float", 0.035, "grid_short", "止损幅度",
          "价格涨到 入场×(1+此值) 就市价平掉（交易所条件单）", widget="percent",
          unit="%", scale=100, min=0.001, max=0.5, step=0.001),
    Param("grid.short_tp_pct", "float", 0.10, "grid_short", "止盈幅度",
          "价格跌到 入场×(1−此值) 就市价平掉", widget="percent", unit="%",
          scale=100, min=0.001, max=0.9, step=0.001),

    # ---------------- 网格·移动止盈 ----------------
    Param("grid.trail_tp", "bool", True, "grid_follow", "移动止盈",
          "浮盈冲高后回撤时卖出剩余仓位，把利润落袋。只止盈、绝不割肉",
          widget="switch", sentinel="off"),
    Param("grid.trail_arm_bands", "float", 0.5, "grid_follow", "移动止盈·武装",
          "浮盈达到几个档距才开始记录最高价", unit="档", min=0, max=10, step=0.1),
    Param("grid.trail_bands", "float", 1.0, "grid_follow", "移动止盈·回撤",
          "从最高价回撤几个档距触发卖出，越大越钝", unit="档",
          min=0, max=10, step=0.1),

    # ---------------- 网格·选币 ----------------
    Param("grid.auto_select", "bool", True, "grid_select", "自动选币",
          "开启=每轮按振幅与成交额自动挑标的；关闭=只用下面的手写清单。"
          "⚠ 切换会改变实际交易的币种", widget="switch", sentinel="off"),
    Param("grid.max_symbols", "int", 0, "grid_select", "同时跑几个",
          "留 0 按资金自动推导（推荐）", unit="个", min=0, max=50, step=1,
          sentinel="auto"),
    Param("grid.refresh_min", "int", 10, "grid_select", "选币周期",
          "多久重新选一次币。留 0 会回退到旧的「选币周期（小时）」",
          unit="分钟", min=0, max=1440, step=1, sentinel="fallback"),
    Param("grid.refresh_hours", "int", 6, "grid_select", "选币周期（旧）",
          "已废弃：仅当上面的「选币周期（分钟）」为 0 时才生效",
          unit="小时", min=1, max=168, step=1),
    Param("grid.min_quote_volume", "float", 100000000, "grid_select", "成交额下限",
          "24h 成交额低于此值的标的直接排除", unit="U", min=0, max=1e12),
    Param("grid.min_daily_range", "float", 0.02, "grid_select", "振幅下限",
          "24h 振幅低于此值的不选（太死板没得做）", widget="percent", unit="%",
          scale=100, min=0, max=1.0, step=0.005),
    Param("grid.max_daily_range", "float", 0.2, "grid_select", "振幅上限",
          "24h 振幅高于此值的不选（太疯容易穿仓）", widget="percent", unit="%",
          scale=100, min=0, max=2.0, step=0.005),
    Param("grid.blacklist", "symbol_list", [], "grid_select", "黑名单",
          "额外排除的标的。稳定币等由系统强制排除，不需要手填",
          widget="multiselect", in_defaults=True),
    Param("grid.symbols", "symbol_list", [], "grid_select", "手写标的清单",
          "仅在「自动选币」关闭时生效", widget="multiselect",
          enabled_when=("grid.auto_select", False)),

    # ---------------- 定投 ----------------
    Param("dca.symbols", "str_list", ["BTCUSDT", "ETHUSDT"], "dca", "定投标的",
          "现货定投的币种", widget="multiselect"),
    Param("dca.amount_usdt", "float", 20, "dca", "每期金额",
          "每期每个币投入多少 U", unit="U", min=0, max=100000, sentinel="skip"),
    Param("dca.interval_hours", "int", 24, "dca", "买入间隔", "多久买一期",
          unit="小时", min=1, max=720, step=1),
    Param("dca.max_total_usdt", "float", 500, "dca", "累计投入上限",
          "达到后停止买入", unit="U", min=0, max=1e9, sentinel="unlimited"),
    Param("dca.first_buy_immediately", "bool", True, "dca", "启动立即买首期",
          "从未买过时，启动就先买一期", widget="switch", sentinel="off"),

    # ---------------- 高级（不进参数表）----------------
    Param("exchange_kwargs", "kwargs", {}, "advanced", "API 密钥",
          "币安 API Key / Secret。改完需重启进程才生效（交易所客户端不会热重建）",
          widget="password", hot=False, sensitive=True, in_defaults=False),
    Param("web_password", "password", "", "advanced", "网页管理密码",
          "配置页的解锁密码", widget="password", sensitive=True),
)


# ==========================================================================
# 派生：默认配置 / 索引 / 校验
# ==========================================================================
def by_key() -> dict[str, Param]:
    return {p.key: p for p in PARAMS}


_INDEX = by_key()


def build_defaults() -> dict:
    """生成 DEFAULT_CONFIG。**这是「一处定义」的落点** —— config.py 只是转发。"""
    out: dict = {}
    for p in PARAMS:
        if not p.in_defaults:
            continue
        if p.section:
            out.setdefault(p.section, {})[p.key.split(".", 1)[1]] = p.default
        else:
            out[p.key] = p.default
    return out


def groups() -> list[dict]:
    """给前端的分组元数据（隐藏项不进表）。"""
    out = []
    for gid, title, note in GROUPS:
        items = [p for p in PARAMS if p.group == gid and not p.hidden]
        if items:
            out.append({"id": gid, "title": title, "note": note})
    return out


def dotted_get(config: dict, key: str, fallback: Any = None) -> Any:
    """按点分路径从 config 取值。**不做兜底推导** —— 缺键就返回 fallback。"""
    if "." in key:
        head, tail = key.split(".", 1)
        return (config.get(head) or {}).get(tail, fallback)
    return config.get(key, fallback)


def _fmt(p: Param, raw: float) -> str:
    """把原始值按显示倍率格式化 —— 报错信息要给用户看得懂的数。

    否则会出现「不能大于 0.5%」这种把人绕晕的提示（真实含义是 50%）。
    """
    return f"{raw * p.scale:g}{p.unit}"


def coerce(key: str, value: Any) -> Any:
    """按注册表做类型转换；不合法抛 ValueError（消息可直接展示给用户）。"""
    p = _INDEX.get(key)
    if p is None:
        raise ValueError(f"未知参数 {key}")

    if p.type == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        raise ValueError(f"{p.label} 只能是 true/false")

    if p.type in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"{p.label} 必须是数字")
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{p.label} 必须是数字") from None
        if num != num or num in (float("inf"), float("-inf")):
            raise ValueError(f"{p.label} 不是有效数字")
        # 哨兵值（如 0 = 自动）必须放行，哪怕它低于 min
        if not (p.sentinel and num == 0):
            if p.min is not None and num < p.min:
                raise ValueError(f"{p.label} 不能小于 {_fmt(p, p.min)}")
            if p.max is not None and num > p.max:
                raise ValueError(f"{p.label} 不能大于 {_fmt(p, p.max)}")
        return int(num) if p.type == "int" else num

    if p.type == "enum":
        if value not in (p.choices or ()):
            raise ValueError(f"{p.label} 只能是 {'/'.join(p.choices or ())}")
        return value

    if p.type in ("str_list", "symbol_list"):
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{p.label} 必须是列表")
        out = []
        for v in value:
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"{p.label} 的元素必须是非空字符串")
            out.append(v.strip().upper())
        return out

    if p.type == "password":
        if not isinstance(value, str) or not value.strip():
            # 空密码 = 谁都能改配置（_auth_ok 在密码为空时一律拒绝，
            # 但也不能让页面把密码清空成「没有密码」这种自锁状态）
            raise ValueError(f"{p.label} 不能为空")
        return value

    if p.type == "str":
        if not isinstance(value, str):
            raise ValueError(f"{p.label} 必须是字符串")
        return value

    if p.type == "kwargs":
        if not isinstance(value, dict):
            raise ValueError(f"{p.label} 必须是对象")
        return value

    return value


def validate(config_key: str, value: Any) -> tuple[bool, Any, str]:
    """返回 (是否合法, 转换后的值, 错误消息)。

    config_key 用**短名或点分名**都能找到（`grid` 的子键在 config 里是短名）。
    """
    p = _INDEX.get(config_key) or _find_short(config_key)
    if p is None:
        return True, value, ""            # 不在注册表里 → 不是我们要管的参数，放行
    try:
        return True, coerce(p.key, value), ""
    except ValueError as e:
        return False, value, str(e)


def _find_short(short: str) -> Param | None:
    hits = [p for p in PARAMS if p.key == short or p.key.endswith("." + short)]
    return hits[0] if len(hits) == 1 else None


def fallback_of(p: Param) -> Any:
    """该参数在「键缺失」时实际生效的兜底值 = 它自己的 default。

    存在的意义：代码里那 79 处 `cfg.get(键, 字面量)` 以后都可以换成
    `params.dotted_get(cfg, 键, params.fallback_of(P))` —— 单一真源。
    """
    return p.default
