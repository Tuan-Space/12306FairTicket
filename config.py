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


# 选座，例如 "1A"。不需要指定时留空。
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
