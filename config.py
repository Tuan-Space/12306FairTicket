# 12306 抢票配置脚本
#
# 命令行入口（图形界面使用 python gui.py，不读取此文件）:
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

# 每位乘车人可指定 adult（成人票）或 student（学生票），未指定时跟随常用乘车人类型。
# 学生联系人可选择成人票；选择学生票仍需通过 12306 的学生身份、资格和优惠次数校验。
# 例如 PASSENGER_TICKET_TYPES = {"张三": "adult", "李四": "student"}
PASSENGER_TICKET_TYPES = {}


# 座席优先级，从左到右尝试。
# 支持: 商务座、特等座、一等座、二等座、高级软卧、软卧、硬卧、一等卧、二等卧、软座、硬座、无座
SEAT_TYPES = ["二等座", "无座", "一等座"]


# 车次设置。可用 ,，、;；换行或制表符分隔，也可保留列表写法。
# ONLY_PREFERRED_TRAINS=True 时清单不能为空；False 时其他类型车次也可备选。
PREFERRED_TRAINS = ["G1561"]
ONLY_PREFERRED_TRAINS = True
# 仅在清单为空时生效：high_speed=高铁/动车，conventional=普通列车，all=不限类型。
EMPTY_TRAIN_SCOPE = "all"
# train_first=先车次再席别（兼容默认）；seat_first=先席别再车次。
PRIORITY_STRATEGY = "train_first"


# 可选：多站点备选购物车。取消下方示例的注释后，列表顺序就是实际尝试顺序。
# 每项只有一个站对、一个车次范围、一种席别；同车的二等座和一等座分成两项。
# train_scope="specific" 必须填写单个 train_code；"all" 必须将 train_code 留空。
# 所有项目共用 TRAIN_DATE、乘车人、开抢/停止时间和偏好；任意一项成功后整车停止。
# 这些项目是一次出行的备选，不会分别购买。站对越多，一轮查询可能越久。
# 启用购物车后，上面的 FROM_STATION/TO_STATION、SEAT_TYPES、PREFERRED_TRAINS、
# ONLY_PREFERRED_TRAINS、EMPTY_TRAIN_SCOPE、PRIORITY_STRATEGY 不参与任务。
# 未配置 CART_ITEMS 或设为 None 时，完全保留上面的旧逻辑；显式 [] 是空购物车，会报错。
# CART_ITEMS = [
#     {"from_station": "北京南", "to_station": "上海虹桥",
#      "train_scope": "specific", "train_code": "G101", "seat_type": "二等座"},
#     {"from_station": "北京", "to_station": "上海",
#      "train_scope": "all", "train_code": "", "seat_type": "硬卧"},
#     {"from_station": "北京南", "to_station": "上海虹桥",
#      "train_scope": "specific", "train_code": "G101", "seat_type": "一等座"},
# ]
# 示例中的车次只说明填写方式，请替换为出行日期实际开行的车次。


# 定时设置。留空表示立即开始或不自动停止。
# 支持 "HH:MM:SS" 或 "YYYY-MM-DD HH:MM:SS"。
START_AT = "15:00:00"
STOP_AT = "15:05:00"


# 轮询策略
QUERY_INTERVAL_SECONDS = 0.6
MAX_RETRIES = 1000
# 购物车模式下 MAX_RETRIES 计算完整轮询次数；同一轮内相同站对只查一次。
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
