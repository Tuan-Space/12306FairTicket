# 12306 抢票配置脚本
#
# 命令行入口（图形界面使用 python gui.py，不读取此文件）:
#   python main.py
#
# 你日常需要改的信息都集中在这个文件里。站名请使用 12306 显示的标准站名，
# 乘车人姓名需要和当前 12306 账号的“常用乘车人”完全一致。


# 一次出行的备选，按列表顺序尝试；任意一项成功后停止。
# 每项填写一个站对、一个车次范围和一种席别；请替换为实际出行信息。
# train_scope="specific" 指定单个 train_code；"all" 接受这一站对的所有车次，train_code 留空。
# 支持席别：商务座、特等座、一等座、二等座、高级软卧、软卧、硬卧、一等卧、二等卧、软座、硬座、无座。
CART_ITEMS = [
    {"from_station": "北京南", "to_station": "上海虹桥",
     "train_scope": "specific", "train_code": "G101", "seat_type": "二等座"},
    {"from_station": "北京", "to_station": "上海",
     "train_scope": "all", "train_code": "", "seat_type": "硬卧"},
]

# 所有备选共用乘车日期和乘车人。请填写实际日期，格式 YYYY-MM-DD。
TRAIN_DATE = ""
PASSENGER_NAMES = []

# 每位乘车人可指定 adult（成人票）或 student（学生票），未指定时使用 12306 联系人类型。
# 学生联系人可改买成人票；学生资格和优惠次数仍由 12306 校验。
# 示例：PASSENGER_TICKET_TYPES = {"张三": "adult", "李四": "student"}
PASSENGER_TICKET_TYPES = {}


# 定时设置。留空表示立即开始或不自动停止。
# 支持 "HH:MM:SS" 或 "YYYY-MM-DD HH:MM:SS"。
START_AT = "15:00:00"
STOP_AT = "15:05:00"


# 轮询策略
QUERY_INTERVAL_SECONDS = 0.6
MAX_RETRIES = 1000
# MAX_RETRIES 计算完整轮询次数；同一轮内相同站对只查一次。
# 所有站对共用上述查询间隔，不会因增加项目而提高总查询频率。


# 热身查询策略：START_AT 是目标开售时间，程序会提前 PRE_QUERY_SECONDS 开始查票。
PRE_QUERY_SECONDS = 1.5
HOT_QUERY_INTERVAL_SECONDS = 0.25
HOT_WINDOW_SECONDS = 5.0


# True: 发现票源后自动提交订单；False: 只查询和打印票源。
AUTO_SUBMIT = True


# 座位位置偏好（不是具体排号）。每个乘车人选择一个关系格子，例如两人同排靠窗
# 可填写 ["1A", "1F"]；可用字母会在下单时按席别和实际车型能力校验。
# 12306 无法满足时会自动分配其他位置。留空表示不指定。
# 未选择支持 ABCDF 的席别时，已保存的座位关系保留但不启用。
SEAT_POSITION_PREFERENCES = []


# 铺位数量偏好，顺序含义为下/中/上。三项之和应等于乘车人数；全 0 表示不指定。
# 部分卧铺车型没有中铺，程序会依据 12306 实时返回的能力自动回退为系统分配。
# 未选择卧铺席别时，已保存的数量保留但不启用。
BERTH_PREFERENCE = {"lower": 0, "middle": 0, "upper": 0}

# 静音车厢偏好，仅在 12306 明确开放且本次候选为二等座时提交。
# 未开放或其他席别时按普通车厢分配，不保证静音车厢席位。
QUIET_CARRIAGE_PREFERENCE = False


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
