"""
澳门六合彩数据抓取引擎 v3
- 动态生肖计算（根据农历年自动切换）
- 繁体→简体生肖转换
"""

import requests
import time
import random
import logging
import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    LATEST_ISSUE_APIS, HISTORY_API_BASE, MACAUJC_API_URL,
    REQUEST_TIMEOUT, REQUEST_RETRIES,
    CACHE_DURATION, MAX_DRAWS, CONCURRENT_WORKERS, CNY_DATES
)

logger = logging.getLogger(__name__)

# ==================== 号码属性映射 ====================
RED_NUMS = {1, 2, 7, 8, 12, 13, 18, 19, 23, 24, 29, 30, 34, 35, 40, 45, 46}
BLUE_NUMS = {3, 4, 9, 10, 14, 15, 20, 25, 26, 31, 36, 37, 41, 42, 47, 48}
GREEN_NUMS = {5, 6, 11, 16, 17, 21, 22, 27, 28, 32, 33, 38, 39, 43, 44, 49}

ZODIAC_CYCLE = ['鼠', '牛', '虎', '兔', '龙', '蛇', '马', '羊', '猴', '鸡', '狗', '猪']

# 繁体→简体生肖映射
ZODIAC_TC_TO_SC = {
    '鼠': '鼠', '牛': '牛', '虎': '虎', '兔': '兔',
    '龍': '龙', '蛇': '蛇', '馬': '马', '羊': '羊',
    '猴': '猴', '雞': '鸡', '狗': '狗', '豬': '猪',
    # 已简体的也映射
    '龙': '龙', '马': '马', '鸡': '鸡', '猪': '猪',
}

ELEMENT_GROUPS = {
    '金': {2, 3, 10, 11, 24, 25, 32, 33, 40, 41},
    '木': {6, 7, 14, 15, 22, 23, 36, 37, 44, 45},
    '水': {12, 13, 20, 21, 28, 29, 42, 43},
    '火': {8, 9, 16, 17, 30, 31, 38, 39, 46, 47},
    '土': {1, 4, 5, 18, 19, 26, 27, 34, 35, 48, 49},
}


def get_color(n):
    if n in RED_NUMS:
        return '红波'
    elif n in BLUE_NUMS:
        return '蓝波'
    elif n in GREEN_NUMS:
        return '绿波'
    return '未知'


def get_year_animal(date_str=None):
    """根据日期计算当年的生肖属相"""
    if date_str and isinstance(date_str, str):
        try:
            dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
        except (ValueError, TypeError):
            dt = datetime.now()
    elif isinstance(date_str, datetime):
        dt = date_str
    else:
        dt = datetime.now()

    year = dt.year
    if year in CNY_DATES:
        cny_m, cny_d = CNY_DATES[year]
        if dt.month < cny_m or (dt.month == cny_m and dt.day < cny_d):
            year -= 1
    # 2020年=鼠年(index 0)
    return ZODIAC_CYCLE[(year - 2020) % 12]


def get_zodiac(n, date_str=None):
    """根据号码和日期，计算对应的生肖
    规则：号码1 = 当年生肖，号码2 = 上一个生肖，依次类推
    """
    animal = get_year_animal(date_str)
    year_idx = ZODIAC_CYCLE.index(animal)
    return ZODIAC_CYCLE[(year_idx - (n - 1)) % 12]


def tc_to_sc(zodiac_str):
    """繁体生肖转简体"""
    return ZODIAC_TC_TO_SC.get(zodiac_str.strip(), zodiac_str.strip()) if zodiac_str else '未知'


def translate_api_color(color_en):
    """API 返回的英文波色转中文"""
    mapping = {'red': '红波', 'blue': '蓝波', 'green': '绿波'}
    return mapping.get(color_en.strip().lower(), '未知') if color_en else '未知'


def get_element(n):
    for elem, nums in ELEMENT_GROUPS.items():
        if n in nums:
            return elem
    return '未知'


# ==================== 抓取引擎 ====================
class LotteryScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Cache-Control': 'no-cache',
        })
        self._cache = None
        self._cache_time = 0
        self._cache_source = ''

    def fetch_draws(self, count=MAX_DRAWS, force_refresh=False):
        """获取开奖数据，优先使用缓存"""
        if not force_refresh and self._cache and (time.time() - self._cache_time) < CACHE_DURATION:
            return {
                'success': True,
                'source': self._cache_source,
                'data': self._cache[:count],
                'cached': True,
                'total': len(self._cache),
            }

        # 策略1: history API 批量拉取（主策略）
        draws, source = self._fetch_via_history(count)

        # 策略2: macaujc.org 批量 API（备用）
        if not draws:
            draws, source = self._fetch_via_macaujc()

        # 策略3: 模拟数据
        if not draws:
            draws = self._generate_mock_data(count)
            source = '模拟数据（所有 API 暂不可用）'
            logger.warning("所有 API 不可用，使用模拟数据")

        # 更新缓存
        self._cache = draws
        self._cache_time = time.time()
        self._cache_source = source

        return {
            'success': True,
            'source': source,
            'data': draws[:count],
            'cached': False,
            'total': len(draws),
        }

    # ========== 策略1: history API ==========

    def _fetch_via_history(self, count):
        logger.info("=" * 50)
        logger.info("策略1: 通过 history API 批量拉取")
        logger.info("=" * 50)

        latest_issue = self._get_latest_issue()
        if not latest_issue:
            logger.error("无法获取最新期号，策略1失败")
            return [], ''

        logger.info("最新期号: %s", latest_issue)

        candidates = self._generate_issue_candidates(latest_issue, count + 80)
        logger.info("生成 %d 个候选期号", len(candidates))

        draws = self._batch_fetch_history(candidates, count)

        if draws:
            draws.sort(key=lambda x: x['period'], reverse=True)
            result = draws[:count]
            logger.info("✅ 通过 history API 成功获取 %d 期数据", len(result))
            if result:
                d = result[0]
                logger.info(
                    "   最新: 第%s期 特码%s 生肖%s",
                    d['period'], str(d['special']).zfill(2), d.get('zodiac', '?')
                )
            return result, 'history.macaumarksix.com (实时数据)'

        return [], ''

    def _get_latest_issue(self):
        """从 marksix6 获取最新期号，或根据日期推算"""
        for api in LATEST_ISSUE_APIS:
            name = api['name']
            url = api['url']
            logger.info("获取最新期号 [%s]: %s", name, url)

            try:
                headers = {'Referer': url.split('/api')[0] + '/'}
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT, headers=headers)
                logger.info("[%s] HTTP %d, 响应长度 %d 字节", name, resp.status_code, len(resp.content))

                if resp.status_code != 200:
                    logger.warning("[%s] HTTP %d", name, resp.status_code)
                    continue

                data = resp.json()

                if isinstance(data, list) and data:
                    issue = data[0].get('expect', '') or data[0].get('issue', '')
                    if issue:
                        logger.info("[%s] 获取到最新期号: %s", name, issue)
                        return str(issue)

                if isinstance(data, dict):
                    items = data.get('data', [])
                    if isinstance(items, list) and items:
                        issue = items[0].get('expect', '') or items[0].get('issue', '')
                        if issue:
                            logger.info("[%s] 获取到最新期号: %s", name, issue)
                            return str(issue)

            except requests.Timeout:
                logger.error("[%s] 请求超时", name)
            except requests.ConnectionError as e:
                logger.error("[%s] 连接失败: %s", name, e)
            except json.JSONDecodeError as e:
                logger.error("[%s] JSON 解析失败: %s", name, e)
            except Exception as e:
                logger.error("[%s] 未知异常: %s", name, e)

        logger.info("所有 API 获取最新期号失败，根据日期推算")
        return self._estimate_latest_issue()

    def _estimate_latest_issue(self):
        now = datetime.now()
        year = now.year
        day_of_year = now.timetuple().tm_yday
        if now.hour < 22:
            day_of_year -= 1
        if day_of_year < 1:
            year -= 1
            day_of_year = 365
        issue = "%d%s" % (year, str(day_of_year).zfill(3))
        logger.info("推算最新期号: %s", issue)
        return issue

    def _generate_issue_candidates(self, latest_issue, count):
        candidates = []
        try:
            year = int(latest_issue[:4])
            num = int(latest_issue[4:])
        except (ValueError, IndexError):
            logger.error("无法解析期号格式: %s", latest_issue)
            return candidates

        while len(candidates) < count:
            issue_str = "%d%s" % (year, str(num).zfill(3))
            candidates.append(issue_str)
            num -= 1
            if num < 1:
                year -= 1
                num = 366

        return candidates

    def _batch_fetch_history(self, candidates, target_count):
        draws = []
        total_candidates = len(candidates)
        batch_size = 20

        for batch_start in range(0, total_candidates, batch_size):
            if len(draws) >= target_count:
                break

            batch = candidates[batch_start:batch_start + batch_size]
            batch_end = batch_start + len(batch)
            logger.info(
                "批次 %d-%d/%d, 已获取 %d 条",
                batch_start + 1, batch_end, total_candidates, len(draws)
            )

            with ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as executor:
                future_map = {}
                for issue in batch:
                    future = executor.submit(self._fetch_one_history, issue)
                    future_map[future] = issue

                for future in as_completed(future_map):
                    result = future.result()
                    if result:
                        draws.append(result)

            if len(draws) < target_count and batch_end < total_candidates:
                time.sleep(0.3)

        logger.info("history API 总共获取 %d 条有效数据", len(draws))
        return draws

    def _fetch_one_history(self, issue_number):
        url = HISTORY_API_BASE + str(issue_number)

        try:
            headers = {'Referer': 'https://history.macaumarksix.com/'}
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT, headers=headers)

            if resp.status_code != 200:
                return None

            data = resp.json()

            is_success = (
                data.get('result', False) or
                data.get('code') == 200 or
                data.get('code') == 0
            )
            if not is_success:
                return None

            items = data.get('data', [])
            if not items:
                return None

            return self._parse_history_item(items[0])

        except (requests.Timeout, requests.ConnectionError):
            return None
        except Exception:
            return None

    def _parse_history_item(self, item):
        """解析 history API 返回的数据项，支持动态生肖"""
        try:
            issue = str(item.get('expect', '') or item.get('issue', '') or '')
            open_code = str(item.get('openCode', '') or '')
            open_time = str(item.get('openTime', '') or '')

            if not issue or not open_code:
                return None

            nums = [int(n.strip()) for n in open_code.split(',') if n.strip()]
            if len(nums) != 7:
                return None
            if not all(1 <= n <= 49 for n in nums):
                return None

            normal_nums = nums[:6]
            special = nums[6]

            # 波色 - 优先用 API 返回的
            api_wave = item.get('wave', '')
            if api_wave:
                wave_parts = api_wave.split(',')
                if len(wave_parts) >= 7:
                    special_color = translate_api_color(wave_parts[6])
                    normal_colors = [translate_api_color(w) for w in wave_parts[:6]]
                else:
                    special_color = get_color(special)
                    normal_colors = [get_color(n) for n in normal_nums]
            else:
                special_color = get_color(special)
                normal_colors = [get_color(n) for n in normal_nums]

            # 生肖 - 始终根据开奖日期动态计算（确保跨年生肖正确）
            special_zodiac = get_zodiac(special, open_time)
            normal_zodiacs = [get_zodiac(n, open_time) for n in normal_nums]

            return {
                'period': issue,
                'date': open_time,
                'numbers': normal_nums,
                'special': special,
                'color': special_color,
                'zodiac': special_zodiac,
                'wuxing': get_element(special),
                'head': special // 10,
                'tail': special % 10,
                'normal_colors': normal_colors,
                'normal_zodiacs': normal_zodiacs,
            }

        except (ValueError, TypeError) as e:
            logger.debug("解析 history 记录失败: %s", e)
            return None

    # ========== 策略2: macaujc.org ==========

    def _fetch_via_macaujc(self):
        logger.info("=" * 50)
        logger.info("策略2: 尝试 macaujc.org 批量 API")
        logger.info("=" * 50)

        for attempt in range(1, REQUEST_RETRIES + 1):
            logger.info("尝试 macaujc.org (第%d/%d次)", attempt, REQUEST_RETRIES)
            try:
                resp = self.session.get(MACAUJC_API_URL, timeout=REQUEST_TIMEOUT)
                if resp.status_code != 200:
                    logger.warning("macaujc.org HTTP %d", resp.status_code)
                    if resp.status_code in (504, 503) and attempt < REQUEST_RETRIES:
                        time.sleep(3)
                        continue
                    break

                data = resp.json()
                if isinstance(data, dict) and data.get('code') == 0:
                    items = data.get('data', [])
                    draws = []
                    for item in items:
                        draw = self._parse_macaujc_item(item)
                        if draw:
                            draws.append(draw)
                    if draws:
                        draws.sort(key=lambda x: x['period'], reverse=True)
                        logger.info("✅ macaujc.org 获取到 %d 条数据", len(draws))
                        return draws, 'macaujc.org (实时数据)'

            except Exception as e:
                logger.error("macaujc.org 异常: %s", e)
                if attempt < REQUEST_RETRIES:
                    time.sleep(2)

        return [], ''

    def _parse_macaujc_item(self, item):
        try:
            issue = str(item.get('issue', '') or item.get('expect', '') or '')
            open_code = str(item.get('openCode', '') or '')
            open_time = str(item.get('openTime', '') or '')

            if not issue or not open_code:
                return None

            nums = [int(n.strip()) for n in open_code.split(',') if n.strip()]
            if len(nums) != 7 or not all(1 <= n <= 49 for n in nums):
                return None

            special = nums[6]
            normal_nums = nums[:6]

            return {
                'period': issue,
                'date': open_time,
                'numbers': normal_nums,
                'special': special,
                'color': get_color(special),
                'zodiac': get_zodiac(special, open_time),
                'wuxing': get_element(special),
                'head': special // 10,
                'tail': special % 10,
                'normal_colors': [get_color(n) for n in normal_nums],
                'normal_zodiacs': [get_zodiac(n, open_time) for n in normal_nums],
            }
        except (ValueError, TypeError):
            return None

    # ========== 模拟数据 ==========

    def _generate_mock_data(self, count):
        draws = []
        base_date = datetime.now()
        random.seed(20250615)

        for i in range(count):
            d = base_date - timedelta(days=i)
            period = "%d%s" % (d.year, str(d.timetuple().tm_yday).zfill(3))
            date_str = d.strftime('%Y-%m-%d 21:32:00')

            pool = list(range(1, 50))
            random.shuffle(pool)
            picked = pool[:7]
            normal = picked[:6]
            special = picked[6]

            draws.append({
                'period': period,
                'date': date_str,
                'numbers': normal,
                'special': special,
                'color': get_color(special),
                'zodiac': get_zodiac(special, date_str),
                'wuxing': get_element(special),
                'head': special // 10,
                'tail': special % 10,
                'normal_colors': [get_color(n) for n in normal],
                'normal_zodiacs': [get_zodiac(n, date_str) for n in normal],
            })

        return draws

    # ========== 分析摘要（给 AI 用）==========

    def get_analysis_summary(self, draws):
        if not draws:
            return "暂无数据"

        total = len(draws)

        special_freq = {}
        for i in range(1, 50):
            special_freq[i] = 0

        color_stats = {'红波': 0, '蓝波': 0, '绿波': 0}
        zodiac_stats = {}
        element_stats = {}
        head_stats = {i: 0 for i in range(5)}
        tail_stats = {i: 0 for i in range(10)}

        for draw in draws:
            sp = draw['special']
            special_freq[sp] = special_freq.get(sp, 0) + 1

            c = draw.get('color', get_color(sp))
            if c in color_stats:
                color_stats[c] += 1

            z = draw.get('zodiac', get_zodiac(sp))
            zodiac_stats[z] = zodiac_stats.get(z, 0) + 1

            e = draw.get('wuxing', get_element(sp))
            element_stats[e] = element_stats.get(e, 0) + 1

            head_stats[sp // 10] = head_stats.get(sp // 10, 0) + 1
            tail_stats[sp % 10] = tail_stats.get(sp % 10, 0) + 1

        # 遗漏值
        missing = {}
        for i in range(1, 50):
            m = 0
            for draw in draws:
                if draw['special'] == i:
                    break
                m += 1
            missing[i] = m

        hot = sorted(special_freq.items(), key=lambda x: x[1], reverse=True)[:10]
        cold = sorted(special_freq.items(), key=lambda x: x[1])[:10]
        miss_top = sorted(missing.items(), key=lambda x: x[1], reverse=True)[:10]

        hot_str = ', '.join(['%s(%d次)' % (str(n).zfill(2), c) for n, c in hot])
        cold_str = ', '.join(['%s(%d次)' % (str(n).zfill(2), c) for n, c in cold])
        miss_str = ', '.join(['%s(遗漏%d期)' % (str(n).zfill(2), c) for n, c in miss_top])

        color_list = []
        for k, v in color_stats.items():
            pct = v / total * 100 if total else 0
            color_list.append('%s%d次(%.1f%%)' % (k, v, pct))
        color_str = ', '.join(color_list)

        zodiac_sorted = sorted(zodiac_stats.items(), key=lambda x: x[1], reverse=True)
        zodiac_str = ', '.join(['%s%d次' % (k, v) for k, v in zodiac_sorted])

        element_str = ', '.join(['%s%d次' % (k, v) for k, v in element_stats.items()])
        head_str = ', '.join(['%d头%d次' % (k, v) for k, v in sorted(head_stats.items())])
        tail_str = ', '.join(['%d尾%d次' % (k, v) for k, v in sorted(tail_stats.items())])

        # 当前生肖年
        year_animal = get_year_animal()

        # 最近10期
        recent = []
        for d in draws[:10]:
            nums_str = ','.join([str(n).zfill(2) for n in d['numbers']])
            sp = d['special']
            line = '第%s期(%s): 正码[%s] 特码%s(%s/%s/%s)' % (
                d['period'], d['date'][:10],
                nums_str, str(sp).zfill(2),
                d.get('color', ''), d.get('zodiac', ''), d.get('wuxing', '')
            )
            recent.append(line)

        summary = (
            '澳门六合彩最近%d期真实开奖数据分析摘要：\n'
            '【当前生肖年】%s年\n\n'
            '【最近10期开奖】\n%s\n\n'
            '【特码热号TOP10】%s\n'
            '【特码冷号TOP10】%s\n'
            '【特码遗漏TOP10】%s\n'
            '【特码波色分布】%s\n'
            '【特码生肖分布】%s\n'
            '【特码五行分布】%s\n'
            '【特码头数分布】%s\n'
            '【特码尾数分布】%s'
        ) % (
            total, year_animal,
            '\n'.join(recent),
            hot_str, cold_str, miss_str,
            color_str, zodiac_str, element_str,
            head_str, tail_str
        )
        return summary


# 全局实例
scraper = LotteryScraper()
