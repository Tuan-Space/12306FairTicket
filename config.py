# 12306 抢票配置脚本
#
# 运行入口只有一个:
#   python main.py
#
# 你日常需要改的信息都集中在这个文件里。站名请使用 12306 显示的标准站名，
# 乘车人姓名需要和当前 12306 账号的“常用乘车人”完全一致。


# 行程信息
FROM_STATION = "开封北"
TO_STATION = "北京西"
TRAIN_DATE = "2026-05-05"  # YYYY-MM-DD


# 乘车人。程序会登录后从 12306 常用乘车人中按姓名匹配，不需要在本地写身份证和手机号。
PASSENGER_NAMES = ["XXX"]


# 座席优先级，从左到右尝试。
# 支持: 商务座、特等座、一等座、二等座、高级软卧、软卧、硬卧、软座、硬座、无座
SEAT_TYPES = ["二等座", "无座", "一等座"]


# 车次设置。为空表示不限制车次。
PREFERRED_TRAINS = ["G1561"]
ONLY_PREFERRED_TRAINS = True


# 定时设置。留空表示立即开始或不自动停止。
# 支持 "HH:MM:SS" 或 "YYYY-MM-DD HH:MM:SS"。
START_AT = "15:00:00"
STOP_AT = "15:05:00"


# 轮询策略
QUERY_INTERVAL_SECONDS = 0.6
MAX_RETRIES = 1000


# 热身查询策略：START_AT 是目标开售时间，程序会提前 PRE_QUERY_SECONDS 开始查票。
PRE_QUERY_SECONDS = 1.5
HOT_QUERY_INTERVAL_SECONDS = 0.25
HOT_WINDOW_SECONDS = 5.0


# True: 发现票源后自动提交订单；False: 只查询和打印票源。
AUTO_SUBMIT = True


# 座位位置偏好（不是具体排号）。每个乘车人选择一个关系格子，例如两人同排靠窗
# 可填写 ["1A", "1F"]；可用字母会在下单时按席别和实际车型能力校验。
# 12306 无法满足时会自动分配其他位置。留空表示不指定。
SEAT_POSITION_PREFERENCES = []


# 铺位数量偏好，顺序含义为下/中/上。三项之和应等于乘车人数；全 0 表示不指定。
# 部分卧铺车型没有中铺，程序会依据 12306 实时返回的能力自动回退为系统分配。
BERTH_PREFERENCE = {"lower": 0, "middle": 0, "upper": 0}


# 旧版兼容项。新配置请使用 SEAT_POSITION_PREFERENCES；只有上面的新项不存在时
# 才会读取 CHOOSE_SEATS，例如 "1A" 或 "1A1F"。
CHOOSE_SEATS = ""


# 常规运行参数。通常不用改。
PURPOSE_CODES = "ADULT"
REQUEST_TIMEOUT_SECONDS = 10
LOGIN_QR_TIMEOUT_SECONDS = 180
LOGIN_QR_POLL_SECONDS = 1.0
TIME_SYNC_SAMPLES = 7
TIME_SYNC_MAX_RTT_SECONDS = 1.0
ORDER_WAIT_ATTEMPTS = 300
ORDER_WAIT_INTERVAL_SECONDS = 2.0
STATION_CACHE_DAYS = 7
LOG_LEVEL = "INFO"
PERF_LOG = True


# 本地运行文件。可改路径，但不建议提交这些文件。
QR_CODE_FILE = ".runtime/login_qr.png"
SESSION_FILE = ".runtime/session.cookies"
STATION_CACHE_FILE = ".runtime/stations.json"

# False 时本次运行既不读取也不写入登录 Cookie。
PERSIST_SESSION = True
