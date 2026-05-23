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

# ── 年干 → 正月月干 ──
# 甲己→丙, 乙庚→戊, 丙辛→庚, 丁壬→壬, 戊癸→甲
MONTH_START_GAN = {0: 2, 1: 4, 2: 6, 3: 8, 4: 0}


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

def year_pillar(y: int):
    """年柱"""
    gan = (y - 4) % 10
    zhi = (y - 4) % 12
    return gan, zhi


def month_pillar(y: int, m: int):
    """月柱（简化算法，基于公历月近似，未精确到节气）"""
    year_gan = (y - 4) % 10
    start = MONTH_START_GAN[year_gan % 5]
    gan = (start + (m - 1)) % 10
    # 正月寅(2), 二月卯(3), ...
    zhi = (m + 1) % 12
    return gan, zhi


def day_pillar(y: int, m: int, d: int):
    """日柱——基于 1900-01-01 庚子日推算"""
    days = days_from_1900_01_01(y, m, d)
    # 1900-01-01 = 庚子 = 六十四柱索引 36（甲子=0, 乙丑=1, ...）
    sexagenary = (36 + days) % 60
    gan = sexagenary % 10
    zhi = sexagenary % 12
    return gan, zhi


def hour_pillar(day_gan: int, hour: int):
    """时柱——2小时一个时辰"""
    if hour < 0:
        return None
    # 地支：子(23-01)=0, 丑(01-03)=1, ...
    zhi = (hour + 1) // 2 % 12

    # 日干 → 时干起点：甲己→甲, 乙庚→丙, 丙辛→戊, 丁壬→庚, 戊癸→壬
    start = MONTH_START_GAN[day_gan % 5]
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
                   birth_hour: int = -1, gender: str = None) -> dict:
    """计算八字命盘"""
    # 四柱
    y_gan, y_zhi = year_pillar(birth_year)
    m_gan, m_zhi = month_pillar(birth_year, birth_month)
    d_gan, d_zhi = day_pillar(birth_year, birth_month, birth_day)
    h_result = hour_pillar(d_gan, birth_hour) if birth_hour >= 0 else None

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

    return {
        "birth_date": f"{birth_year}年{birth_month}月{birth_day}日"
                      + (f" {birth_hour}时" if birth_hour >= 0 else "（时辰未知）"),
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
    }
