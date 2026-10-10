"""EvoClaw v2.0 — 默认配置。

DB 是唯一真源（config 表）。这里的 DEFAULT_CONFIG 只在数据库为空时用于播种。
所有键都可以在「交易配置」页面里改，改完即时生效（保留 dry_run 与 mode 的安全默认）。
"""

DEFAULT_CONFIG = {
    # ===== 全局 =====
    # 模式开关（同时只跑一种）：
    #   "off"  = 不交易（引擎空转，只做监控）
    #   "grid" = 方案B：低杠杆网格
    #   "dca"  = 方案C：现货定投
    "mode": "off",
    # 模拟盘：True = 完整计算并记录「本应下的单」，但不发真实订单。
    # 新策略上线必须先 dry-run 验证，确认无误后再改为 False。
    "dry_run": True,
    "exchange": "binanceusdm",
    "loop_interval": 10,          # 主循环间隔（秒）

    # ===== 方案B：低杠杆网格 =====
    # 每个 symbol 一个独立网格：在 [lower, upper] 区间内低买高卖。
    # 不设止损：价格跌出区间就持有底仓等回来；跌不到就不会被强平（靠低杠杆）。
    "grid": {
        "leverage": 3,             # 逐仓杠杆（越低越安全，建议 2~3）
        "max_exposure_ratio": 3.0, # 总名义价值 / 权益 的上限（超过则停止买入）
        "size_usdt": 20,           # 单格下单金额（名义）—— 决定每档多大
        "grids": 9,                # 每个标的的档位数
        "auto_range_pct": 0,       # 0 = 随标的波动率自适应（推荐）；>0 = 固定百分比

        # ---- 自动选币 ----
        # true = 系统按下列标准自动挑标的（无需手动维护）；
        #        掉出名单的持仓不会抛弃，而是转入「只卖不买」，卖光后退役。
        # false = 使用下面的 symbols 手写清单。
        "auto_select": True,
        "max_symbols": 3,              # 同时跑几个标的
        "refresh_hours": 6,            # 选币刷新周期
        "min_quote_volume": 100000000, # 24h 成交额下限（1 亿 U）
        "min_daily_range": 0.02,       # 24h 振幅下限 2%
        "max_daily_range": 0.20,       # 24h 振幅上限 20%
        "blacklist": [],               # 额外排除的标的（稳定币已内置排除）

        # 手动模式（auto_select=false 时生效）：
        #   symbol / lower / upper / grids / size_usdt（lower/upper 填 0 = 自动生成）
        "symbols": [],
    },

    # ===== 方案C：现货定投 =====
    # 定时定额买入现货主流币，无杠杆、无强平、无止损。
    "dca": {
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "amount_usdt": 20,         # 每期每币买入金额
        "interval_hours": 24,      # 买入间隔
        "max_total_usdt": 500,     # 累计投入上限（达到后停止买入）
        "first_buy_immediately": True,  # 启动时若从未买过，是否立刻买第一期
    },

    # ===== 风控（两模式共用） =====
    # 最小下单金额（USDT）：低于此值的下单会被跳过（交易所还有各自下限，取较大者）
    "min_order_notional": 5,
    # 单次下单相对最小额的放大倍数（交易所对最小名义值有硬性要求）
    "order_notional_safety": 1.02,

    # ===== 网页鉴权 =====
    # 写接口（改配置 / 启停）需要此密码。首次启动自动生成随机密码并打印到日志。
    "web_password": "",
}
