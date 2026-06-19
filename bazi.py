"""八字计算模块：根据公历出生日期计算四柱、五行，供 Agent 解读"""

from datetime import date

# ── 天干地支 ──
TIAN_GAN = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛", "壬", "癸"]
DI_ZHI = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉", "戌", "亥"]

SHENG_XIAO = ["鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪"]

# 天干五行
GAN_WU_XING = ["木", "木", "火", "火", "土", "土", "金", "金", "水", "水"]

# 地支五行（本气）
ZHI_WU_XING = ["水", "土", "木", "木", "土", "火", "火", "土", "金", "金", "土", "水"]

# ── 年干 → 正月月干（五虎遁）──
# 甲己→丙, 乙庚→戊, 丙辛→庚, 丁壬→壬, 戊癸→甲
MONTH_START_GAN = {0: 2, 1: 4, 2: 6, 3: 8, 4: 0}

# ── 日干 → 子时时干（五鼠遁）──
# 甲己→甲, 乙庚→丙, 丙辛→戊, 丁壬→庚, 戊癸→壬
HOUR_START_GAN = {0: 0, 1: 2, 2: 4, 3: 6, 4: 8}

# ── 节气边界（月建，近似日期）──
# ((月, 日), 地支索引)：该节气后进入此月
SOLAR_TERMS = [
    ((2, 4), 2),   # 立春 → 寅
    ((3, 6), 3),   # 惊蛰 → 卯
    ((4, 5), 4),   # 清明 → 辰
    ((5, 6), 5),   # 立夏 → 巳
    ((6, 6), 6),   # 芒种 → 午
    ((7, 7), 7),   # 小暑 → 未
    ((8, 7), 8),   # 立秋 → 申
    ((9, 8), 9),   # 白露 → 酉
    ((10, 8), 10), # 寒露 → 戌
    ((11, 7), 11), # 立冬 → 亥
    ((12, 7), 0),  # 大雪 → 子
    ((1, 6), 1),   # 小寒 → 丑
]


def is_leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def day_of_year(y: int, m: int, d: int) -> int:
    days_in_month = [31, 29 if is_leap(y) else 28, 31, 30, 31, 30,
                     31, 31, 30, 31, 30, 31]
    return sum(days_in_month[:m - 1]) + d


def days_from_1900_01_01(y: int, m: int, d: int) -> int:
    total = 0
    for yr in range(1900, y):
        total += 366 if is_leap(yr) else 365
    total += day_of_year(y, m, d) - 1  # 1900-01-01 为第0天
    return total


# ── 四柱计算 ──

def solar_year(y: int, m: int, d: int) -> int:
    """八字用年，以立春（约2月4日）为界。"""
    if m < 2 or (m == 2 and d < 4):
        return y - 1
    return y


def year_pillar(y: int, m: int, d: int):
    """年柱，以立春为界换年。"""
    sy = solar_year(y, m, d)
    gan = (sy - 4) % 10
    zhi = (sy - 4) % 12
    return gan, zhi


def get_month_zhi(m: int, d: int) -> int:
    """根据节气（月建）确定月支。

    节气顺序：立春(2/4→寅) → 惊蛰(3/6→卯) → … → 大雪(12/7→子) → 小寒(1/6→丑)
    丑月跨年：从小寒(1/6)到立春(2/3)。
    """
    # 小寒前（1/1~1/5）：上一年 子月
    if m == 1 and d < 6:
        return 0
    # 丑月（1/6~2/3）
    if (m == 1 and d >= 6) or (m == 2 and d < 4):
        return 1

    # 立春后：遍历节气（跳过已经处理的小寒 1/6）
    month_zhi = 2  # 默认寅
    for (tm, td), mz in SOLAR_TERMS:
        if tm == 1:  # 跳过小寒（已在上面处理）
            continue
        if (m > tm) or (m == tm and d >= td):
            month_zhi = mz
    return month_zhi


def month_pillar(y: int, m: int, d: int):
    """月柱，基于节气（月建）确定月份。"""
    sy = solar_year(y, m, d)
    month_zhi = get_month_zhi(m, d)

    # 五虎遁求月干
    year_gan = (sy - 4) % 10
    start = MONTH_START_GAN[year_gan % 5]
    offset = (month_zhi - 2) % 12  # 寅=2 的偏移
    gan = (start + offset) % 10
    return gan, month_zhi


def day_pillar(y: int, m: int, d: int):
    """日柱——基于 1900-01-01 甲戌日推算"""
    days = days_from_1900_01_01(y, m, d)
    # 1900-01-01 = 甲戌日 = 六十甲子索引 10（甲子=0）
    sexagenary = (10 + days) % 60
    gan = sexagenary % 10
    zhi = sexagenary % 12
    return gan, zhi


def hour_pillar(day_gan: int, hour: int, minute: int = 0):
    """时柱——2小时一个时辰，23:00 起算子时"""
    if hour < 0:
        return None
    # 地支：子(23:00-00:59)=0, 丑(01:00-02:59)=1, 寅(03:00-04:59)=2, …
    if hour == 23:
        zhi = 0
    else:
        zhi = (hour + 1) // 2 % 12

    # 五鼠遁：日干 → 子时时干
    start = HOUR_START_GAN[day_gan % 5]
    gan = (start + zhi) % 10
    return gan, zhi


# ── 五行统计 ──

def wu_xing_counts(gan_zhi_list: list) -> dict:
    """统计五行数量。gan_zhi_list: [(gan, zhi), ...] 其中 gan/zhi 为 None 时跳过"""
    wx_names = ["木", "火", "土", "金", "水"]
    counts = {n: 0 for n in wx_names}

    for gan, zhi in gan_zhi_list:
        if gan is not None:
            counts[GAN_WU_XING[gan]] += 1
        if zhi is not None:
            counts[ZHI_WU_XING[zhi]] += 1

    return counts


def analyze_wu_xing(gan_zhi_list: list) -> dict:
    """五行分析：各元素数量、最强、最弱、喜神"""
    counts = wu_xing_counts(gan_zhi_list)

    # 找出最多和最少的
    sorted_wx = sorted(counts.items(), key=lambda x: -x[1])
    max_count = sorted_wx[0][1]
    min_count = sorted_wx[-1][1]

    strongest = [n for n, c in sorted_wx if c == max_count]
    weakest = [n for n, c in sorted_wx if c == min_count]

    # 简化喜神：最弱的五行补足就是喜神（实际命理更复杂，此处做简化）
    favorable = weakest

    return {
        "counts": counts,
        "strongest": strongest,
        "weakest": weakest,
        "favorable_elements": favorable,
    }


# ── 主入口 ──

def calculate_bazi(birth_year: int, birth_month: int, birth_day: int,
                   birth_hour: int = -1, birth_minute: int = 0,
                   gender: str = None) -> dict:
    """计算八字命盘"""
    # 四柱
    y_gan, y_zhi = year_pillar(birth_year, birth_month, birth_day)
    m_gan, m_zhi = month_pillar(birth_year, birth_month, birth_day)
    d_gan, d_zhi = day_pillar(birth_year, birth_month, birth_day)
    h_result = hour_pillar(d_gan, birth_hour, birth_minute) if birth_hour >= 0 else None

    pillars = [
        ("年柱", y_gan, y_zhi),
        ("月柱", m_gan, m_zhi),
        ("日柱", d_gan, d_zhi),
    ]
    gan_zhi_list = [(y_gan, y_zhi), (m_gan, m_zhi), (d_gan, d_zhi)]

    if h_result:
        h_gan, h_zhi = h_result
        pillars.append(("时柱", h_gan, h_zhi))
        gan_zhi_list.append((h_gan, h_zhi))

    # 五行分析
    wx_analysis = analyze_wu_xing(gan_zhi_list)

    # 日主（日柱天干）
    ri_zhu = TIAN_GAN[d_gan]
    ri_zhu_wx = GAN_WU_XING[d_gan]

    # 生肖
    sheng_xiao = SHENG_XIAO[y_zhi]

    time_str = ""
    if birth_hour >= 0:
        time_str = f" {birth_hour}时"
        if birth_minute > 0:
            time_str += f"{birth_minute}分"

    return {
        "birth_date": f"{birth_year}年{birth_month}月{birth_day}日"
                      + (time_str if time_str else "（时辰未知）"),
        "sheng_xiao": sheng_xiao,
        "four_pillars": [
            {
                "name": name,
                "tian_gan": TIAN_GAN[gan],
                "di_zhi": DI_ZHI[zhi],
                "gan_wx": GAN_WU_XING[gan],
                "zhi_wx": ZHI_WU_XING[zhi],
            }
            for name, gan, zhi in pillars
        ],
        "ri_zhu": ri_zhu,
        "ri_zhu_element": ri_zhu_wx,
        "wu_xing": wx_analysis,
        "pillar_count": len(pillars),
        "dayun": calculate_dayun(y_gan, m_gan, m_zhi, gender),
    }


# ── 大运计算 ──

def calculate_dayun(year_gan: int, month_gan: int, month_zhi: int, gender: str = None) -> dict:
    """计算大运序列（简化版，起运岁数按近似值）

    year_gan: 年柱天干索引
    month_gan: 月柱天干索引
    month_zhi: 月柱地支索引
    gender: "male" 或 "female"

    阳年（甲丙戊庚壬 = 偶数索引）：男顺女逆
    阴年（乙丁己辛癸 = 奇数索引）：男逆女顺
    大运天干顺排：甲→乙→丙→丁→… 逆排：甲→癸→壬→辛→…
    大运地支顺排：寅→卯→辰→巳→… 逆排：寅→丑→子→亥→…
    """
    is_yang = year_gan % 2 == 0  # 阳干
    is_male = gender == "male"

    # 阳男阴女 → 顺排；阴男阳女 → 逆排
    forward = (is_yang and is_male) or (not is_yang and not is_male)

    dayun_list = []
    for i in range(8):  # 8 步大运，80 年
        if forward:
            zhi = (month_zhi + 1 + i) % 12
            gan = (month_gan + 1 + i) % 10
        else:
            zhi = (month_zhi - 1 - i) % 12
            gan = (month_gan - 1 - i) % 10
        dayun_list.append({
            "index": i + 1,
            "pillar": TIAN_GAN[gan] + DI_ZHI[zhi],
            "tian_gan": TIAN_GAN[gan],
            "di_zhi": DI_ZHI[zhi],
        })

    return {
        "direction": "顺排" if forward else "逆排",
        "reason": "阳男阴女顺排，阴男阳女逆排",
        "start_age_note": "起运岁数需结合节气精确计算，仅供参考。可按每3天=1岁估算，或请专业命理师核定。",
        "list": dayun_list,
    }
