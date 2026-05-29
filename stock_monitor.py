"""
A股信息收集 & 邮件通知
纯数据管道 — 只收集公开信息并推送，不做任何分析或建议。
"""

import json
import os
import re
import smtplib
import subprocess
import sys
import threading
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"


def _get_version() -> str:
    """从 git 提交数获取版本号，失败则用日期。"""
    try:
        r = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, cwd=str(BASE), timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return datetime.now().strftime("%y%m%d")


def _get_push_time() -> str:
    """从 git 获取最近一次提交时间作为推送时间，失败则用当前时间。"""
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%ci"],
            capture_output=True, text=True, cwd=str(BASE), timeout=5,
        )
        if r.returncode == 0:
            # git format: "2026-05-25 00:29:30 +0800" -> "2026-05-25 00:29:30"
            return r.stdout.strip()[:19]
    except Exception:
        pass
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
CACHE_DIR = BASE / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# ── HTTP session (绕过系统代理) ──
session = requests.Session()
session.trust_env = False
session.headers["User-Agent"] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════
# 行情 — 新浪 API (稳定直连)
# ═══════════════════════════════════════════════════════

def _code_to_sina(code: str) -> str:
    """600519 -> sh600519, 000858 -> sz000858"""
    if code.startswith(("6", "5", "9")):
        return "sh" + code
    return "sz" + code


def _code_to_tencent(code: str) -> str:
    """600519 -> sh600519, 000858 -> sz000858"""
    if code.startswith(("6", "5", "9")):
        return "sh" + code
    return "sz" + code


def _format_volume(vol_val) -> str:
    try:
        vol = int(vol_val)
        if vol >= 1e8:
            return f"{vol/1e8:.2f}亿"
        elif vol >= 1e4:
            return f"{vol/1e4:.2f}万"
        return str(vol)
    except (ValueError, TypeError):
        return str(vol_val) if vol_val else "—"


def _format_amount(amt_val) -> str:
    try:
        amt = float(amt_val)
        if amt >= 1e8:
            return f"{amt/1e8:.2f}亿"
        elif amt >= 1e4:
            return f"{amt/1e4:.2f}万"
        return f"{amt:.2f}"
    except (ValueError, TypeError):
        return str(amt_val) if amt_val else "—"


def _make_quote_dict(code, name, price, prev_close, change, change_pct,
                     open_price, high, low, volume, amount) -> dict:
    return {
        "代码": code,
        "名称": name,
        "最新价": f"{price:.2f}" if price else "—",
        "涨跌幅": f"{change_pct:+.2f}%",
        "涨跌额": f"{change:+.2f}",
        "今开": open_price if open_price and float(open_price) > 0 else "—",
        "昨收": str(prev_close) if prev_close else "—",
        "最高": str(high) if high else "—",
        "最低": str(low) if low else "—",
        "成交量": _format_volume(volume),
        "成交额": _format_amount(amount),
        "_涨跌数值": change_pct,
    }


def _fetch_sina_quotes(codes: list[str]) -> list[dict] | None:
    """新浪行情源，失败返回None。"""
    sina_codes = [_code_to_sina(c) for c in codes]
    url = "https://hq.sinajs.cn/list=" + ",".join(sina_codes)

    for attempt in range(3):
        try:
            r = session.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=15)
            r.encoding = "gbk"
            if r.status_code == 200 and r.text.strip():
                break
        except Exception:
            if attempt < 2:
                time.sleep((attempt + 1) * 3)

    if r.status_code != 200 or not r.text.strip():
        print("  新浪行情源无数据")
        return None

    results = []
    lines = r.text.strip().split("\n")
    for line in lines:
        match = re.search(r'="(.+)"', line)
        if not match:
            continue
        parts = match.group(1).split(",")
        if len(parts) < 33:
            continue

        name = parts[0]
        raw_code = ""
        for c in codes:
            if _code_to_sina(c) in line:
                raw_code = c
                break

        try:
            price = float(parts[3])
            prev_close = float(parts[2])
            change = price - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0
        except (ValueError, ZeroDivisionError):
            price, prev_close, change, change_pct = 0, 0, 0, 0

        results.append(_make_quote_dict(
            raw_code or parts[0], name, price, prev_close, change, change_pct,
            parts[1], parts[4], parts[5], parts[8], parts[9],
        ))

    if results:
        print(f"  新浪行情: {len(results)} 只")
        return results
    return None


def _fetch_tencent_quotes(codes: list[str]) -> list[dict] | None:
    """腾讯财经行情源，失败返回None。"""
    tc_codes = [_code_to_tencent(c) for c in codes]
    url = "http://qt.gtimg.cn/q=" + ",".join(tc_codes)

    try:
        r = session.get(url, timeout=10)
        r.encoding = "gbk"
        if r.status_code != 200 or not r.text.strip():
            print("  腾讯行情源无数据")
            return None
    except Exception as e:
        print(f"  腾讯行情源失败: {e}")
        return None

    results = []
    for line in r.text.strip().split("\n"):
        match = re.search(r'="(.+)"', line)
        if not match:
            continue
        parts = match.group(1).split("~")
        if len(parts) < 40:
            continue

        raw_code = parts[2] if len(parts) > 2 else ""
        name = parts[1] if len(parts) > 1 else ""

        try:
            price = float(parts[3])
            prev_close = float(parts[4])
            change = price - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0
            open_price = parts[5]
            high = parts[33]
            low = parts[34]
            volume = parts[6]  # 成交量(股)
            # 腾讯成交额 [57] 为万元，转元
            amount_val = float(parts[57]) * 10000 if parts[57] else 0
        except (ValueError, ZeroDivisionError, IndexError):
            continue

        results.append(_make_quote_dict(
            raw_code, name, price, prev_close, change, change_pct,
            open_price, high, low, volume, str(amount_val),
        ))

    if results:
        print(f"  腾讯行情: {len(results)} 只")
        return results
    return None


def fetch_realtime_quotes(stocks: list[dict]) -> list[dict]:
    """拉取实时行情，新浪优先，腾讯备用。"""
    print("[1/3] 拉取实时行情...")
    codes = [s["code"] for s in stocks]

    # 首选新浪
    results = _fetch_sina_quotes(codes)
    if results:
        return results

    # 备用腾讯
    print("  切换到腾讯财经备用源...")
    results = _fetch_tencent_quotes(codes)
    if results:
        return results

    print("  所有行情源均失败")
    return []


# ═══════════════════════════════════════════════════════
# 公告 — 东方财富 + 巨潮资讯 (互为备份)
# ═══════════════════════════════════════════════════════

def _fetch_eastmoney_announcements(codes: list[str]) -> list[dict]:
    """东方财富公告源。"""
    all_notices = []
    for code in codes:
        try:
            url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
            params = {
                "page_size": 10, "page_index": 1,
                "stock_list": code, "ann_type": "A",
            }
            r = session.get(url, params=params, timeout=15)
            if r.status_code != 200:
                continue
            items = r.json().get("data", {}).get("list", [])
            for item in items:
                item_codes = item.get("codes", [])
                if not item_codes or not any(
                    c.get("stock_code", "") == code for c in item_codes
                ):
                    continue
                art_code = item.get("art_code", "")
                raw_time = item.get("display_time", "") or item.get("notice_date", "") or ""
                detail_url = f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html" if art_code else ""
                all_notices.append({
                    "股票代码": code,
                    "标题": item.get("title_ch", item.get("title", "")),
                    "日期": (item.get("notice_date", "") or "")[:10],
                    "发布时间": raw_time,
                    "详情链接": detail_url,
                })
        except Exception as e:
            print(f"  东方财富公告获取失败 {code}: {e}")
        time.sleep(0.3)
    return all_notices


def _fetch_cninfo_announcements(codes: list[str]) -> list[dict]:
    """巨潮资讯公告源（证监会指定披露平台）。"""
    all_notices = []
    for code in codes:
        try:
            url = "http://www.cninfo.com.cn/new/fulltextSearch/full"
            r = session.post(url, data={
                "searchkey": code,
                "sdate": "",
                "edate": "",
                "isfulltext": "false",
                "sortName": "pubdate",
                "sortType": "desc",
                "pageNum": 1,
                "pageSize": 10,
            }, headers={
                "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }, timeout=15)
            if r.status_code != 200:
                continue
            items = r.json().get("announcements") or []
            for item in items:
                sec_code = item.get("secCode", "")
                if sec_code != code:
                    continue
                ts = item.get("announcementTime", 0)
                dt_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
                org_id = item.get("orgId", "")
                ann_id = item.get("announcementId", "")
                title = re.sub(r"<[^>]+>", "", item.get("announcementTitle", ""))
                detail_url = ""
                if sec_code and ann_id and org_id:
                    detail_url = f"http://www.cninfo.com.cn/new/disclosure/detail?stockCode={sec_code}&announcementId={ann_id}&orgId={org_id}"
                all_notices.append({
                    "股票代码": code,
                    "标题": title,
                    "日期": dt_str[:10],
                    "发布时间": dt_str,
                    "详情链接": detail_url,
                })
        except Exception as e:
            print(f"  巨潮公告获取失败 {code}: {e}")
        time.sleep(0.3)
    return all_notices


def fetch_announcements(codes: list[str]) -> list[dict]:
    """拉取追踪股票的近期公告，东方财富优先，巨潮备份+补充。"""
    print("[2/3] 拉取公司公告...")

    # 首选东方财富
    notices = _fetch_eastmoney_announcements(codes)
    if notices:
        print(f"  东方财富: {len(notices)} 条公告")
        return notices

    # 备用巨潮
    print("  切换到巨潮资讯备用源...")
    notices = _fetch_cninfo_announcements(codes)
    if notices:
        print(f"  巨潮资讯: {len(notices)} 条公告")
        return notices

    print("  所有公告源均失败")
    return []





# ═══════════════════════════════════════════════════════
# AI 解读
# ═══════════════════════════════════════════════════════

def generate_interpretations(quotes: list[dict], announcements: list[dict],
                             ai_cfg: dict) -> dict:
    """调用AI生成解读，失败时返回空结果。"""
    if not ai_cfg.get("enabled"):
        print("[AI] 未启用，跳过解读")
        return {}

    print("[AI] 生成解读...")
    api_key = ai_cfg.get("api_key", "")
    api_url = ai_cfg.get("api_url", "https://api.deepseek.com/v1/chat/completions")
    model = ai_cfg.get("model", "deepseek-chat")

    if not api_key or "你的" in api_key:
        print("  AI API Key 未配置，跳过解读")
        return {}

    # 构建prompt
    stock_lines = ""
    for q in quotes:
        stock_lines += f"- {q['名称']}({q['代码']}): 最新价{q['最新价']}，涨跌幅{q['涨跌幅']}\n"

    notice_lines = ""
    for a in announcements[:10]:
        notice_lines += f"- [{a['股票代码']}] {a['标题'][:60]}\n"

    news_lines = ""
    prompt = f"""你是一个A股市场分析助手。请为以下数据生成简短解读（每条15字以内）：

【股票行情】
{stock_lines or '无'}

【公告】
{notice_lines or '无'}

请以JSON格式返回解读，key分别为stocks(对象，key为股票代码)、announcements(数组，与输入顺序对应)。
只返回JSON，不要其他文字。"""

    try:
        r = session.post(api_url, json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 800,
        }, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }, timeout=30)
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"]
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("\n```", 1)[0]
            result = json.loads(content)
            print(f"  AI解读生成成功")
            return result
        else:
            print(f"  AI API返回错误: {r.status_code} {r.text[:200]}")
            return {}
    except Exception as e:
        print(f"  AI解读失败: {e}")
        return {}


# ═══════════════════════════════════════════════════════
# 股指期货期权交割日
# ═══════════════════════════════════════════════════════

def _get_expiration_dates(year: int) -> list[dict]:
    """计算当年每月第三个周五（A股股指期货期权交割日）。"""
    from datetime import date, timedelta

    dates = []
    for month in range(1, 13):
        first = date(year, month, 1)
        days_until_friday = (4 - first.weekday()) % 7
        third_friday = first + timedelta(days=days_until_friday + 14)
        is_past = third_friday <= date.today()
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        dates.append({
            "month": month,
            "date": third_friday.strftime("%Y-%m-%d"),
            "weekday": weekdays[third_friday.weekday()],
            "is_past": is_past,
            "is_today": third_friday == date.today(),
        })
    return dates


def _get_a50_expiration_dates(year: int) -> list[dict]:
    """计算A50(富时中国A50指数期货, SGX)交割日 — 每月最后一个营业日。"""
    from datetime import date, timedelta

    _SGX_HOLIDAYS_2026 = {
        date(2026, 1, 1), date(2026, 2, 17), date(2026, 2, 18),
        date(2026, 4, 3), date(2026, 5, 1), date(2026, 8, 10),
        date(2026, 12, 25),
    }

    def _is_biz(d: date) -> bool:
        return d.weekday() < 5 and d not in _SGX_HOLIDAYS_2026

    def _last_biz(month: int) -> date:
        # 从下个月第一天往前推
        if month == 12:
            d = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            d = date(year, month + 1, 1) - timedelta(days=1)
        while not _is_biz(d):
            d -= timedelta(days=1)
        return d

    dates = []
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    for month in range(1, 13):
        last = _last_biz(month)
        is_past = last <= date.today()
        dates.append({
            "month": month,
            "date": last.strftime("%Y-%m-%d"),
            "weekday": weekdays[last.weekday()],
            "is_past": is_past,
            "is_today": last == date.today(),
        })
    return dates


def _send_expiration_reminder(domestic: list[dict], a50: list[dict],
                              notify_cfg: dict) -> None:
    """交割日前发送Server酱推送提醒。"""
    if not notify_cfg.get("enabled"):
        return
    sendkey = notify_cfg.get("sendkey", "")
    if not sendkey or "你的" in sendkey:
        print("  [提醒] SendKey未配置，跳过推送")
        return

    days_before = notify_cfg.get("days_before", [0, 1])
    if not isinstance(days_before, list):
        days_before = [0, 1]

    from datetime import date, timedelta
    today = date.today()
    reminders = []

    for d in domestic:
        dt = date.fromisoformat(d["date"])
        for db in days_before:
            if today + timedelta(days=db) == dt:
                label = "今天" if db == 0 else f"{db}天后（{d['date']} 周{d['weekday']}）"
                reminders.append(
                    f"【{label}】\n"
                    f"品种：国内股指期货期权 IF/IH/IC/IM\n"
                    f"     {d['date']} 周{d['weekday']} 15:00 到期交割"
                )

    for a in a50:
        dt = date.fromisoformat(a["date"])
        for db in days_before:
            if today + timedelta(days=db) == dt:
                label = "今天" if db == 0 else f"{db}天后（{a['date']} 周{a['weekday']}）"
                reminders.append(
                    f"【{label}】\n"
                    f"品种：富时中国A50 (SGX)\n"
                    f"     {a['date']} 周{a['weekday']} 到期交割"
                )

    if not reminders:
        print("  [提醒] 近期无交割日")
        return

    # 只在9:30和11:30附近各推一次，避免重复轰炸
    h, m = datetime.now().hour, datetime.now().minute
    allowed = [(9, 30), (11, 30)]
    if not any(abs(h * 60 + m - (ah * 60 + am)) <= 5 for ah, am in allowed):
        print(f"  [提醒] 当前{h:02d}:{m:02d}不在推送时段(9:30/11:30±5分钟)，跳过")
        return

    msg = "\n---\n".join(reminders)
    msg += "\n\n交割日前后波动往往加剧，务必小心出货，控制仓位。"

    title = "股指期货交割日提醒"
    if len(reminders) == 1:
        r0 = reminders[0]
        if "今天" in r0:
            title = "今日交割日 小心出货！"
        else:
            title = "临近交割日 注意风险"

    print(f"  [提醒] 发送推送: {len(reminders)} 条")
    try:
        r = session.post(
            f"https://sctapi.ftqq.com/{sendkey}.send",
            data={"title": title, "desp": msg},
            timeout=10,
        )
        if r.status_code == 200:
            print("  [提醒] 推送成功")
        else:
            print(f"  [提醒] 推送失败: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"  [提醒] 推送异常: {e}")


BREADTH_CACHE = BASE / "breadth.json"


def _load_breadth_cache() -> dict[str, dict]:
    """加载涨跌家数缓存 {日期: {up, down, flat}}。"""
    if BREADTH_CACHE.exists():
        try:
            return json.loads(BREADTH_CACHE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_breadth_cache(cache: dict[str, dict]) -> None:
    """保存涨跌家数缓存，只保留最近一年。"""
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
    trimmed = {k: v for k, v in cache.items() if k >= cutoff}
    BREADTH_CACHE.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")


def _fetch_today_breadth() -> dict[str, int] | None:
    """从Sina获取当天涨跌家数，返回 {up, down, flat} 或 None。"""
    try:
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeDataSimple"
        )
        params = {"page": 1, "num": 5000, "sort": "symbol", "asc": 1, "node": "hs_a"}
        r = session.get(url, params=params, timeout=30)
        if r.status_code != 200:
            return None
        data = r.json()
        up = sum(1 for s in data if float(s.get("changepercent", 0)) > 0)
        down = sum(1 for s in data if float(s.get("changepercent", 0)) < 0)
        flat = sum(1 for s in data if float(s.get("changepercent", 0)) == 0)
        print(f"  涨跌家数: 涨{up} 跌{down} 平{flat}")
        return {"up": up, "down": down, "flat": flat}
    except Exception as e:
        print(f"  涨跌家数获取失败: {e}")
        return None


def _backfill_breadth() -> int:
    """补全涨跌家数缓存，返回补全条数。
    优先用push2his(东方财富历史K线)，本地被封则在Actions环境生效。"""
    cache = _load_breadth_cache()
    today = date.today()
    year_start = f"{today.year}-01-01"

    # 找出今年缺失的交易日范围
    missing_dates = set()
    for d in _get_expiration_dates(today.year):
        if d["is_past"] or d["is_today"]:
            missing_dates.add(d["date"])
    for d in _get_a50_expiration_dates(today.year):
        if d["is_past"] or d["is_today"]:
            missing_dates.add(d["date"])

    missing_dates -= set(cache.keys())
    if not missing_dates:
        print("  涨跌家数无需补全")
        return 0

    # 尝试push2his (东方财富历史K线扩展字段)
    try:
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {
            "secid": "1.000001", "klt": "101", "fqt": "0",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f92,f93,f94",
            "lmt": "300", "beg": year_start.replace("-", ""),
            "end": today.strftime("%Y%m%d"),
        }
        r = session.get(url, params=params, timeout=15)
        if r.status_code == 200:
            data_json = r.json()
            if data_json and "data" in data_json and "klines" in data_json["data"]:
                klines = data_json["data"]["klines"]
                if klines:
                    n_fields = len(klines[0].split(","))
                    print(f"  push2his响应: {len(klines)}条K线, 每条{len(klines[0].split(','))}个字段")
                    added = 0
                    for line in klines:
                        parts = line.split(",")
                        k_date = parts[0]
                        if k_date in missing_dates:
                            # f51=日期, ..., f92=上涨家数(idx 11), f93=下跌家数(idx 12), f94=平盘(idx 13)
                            if len(parts) >= 14:
                                try:
                                    adv = int(parts[11]) if parts[11] not in ("", "-") else 0
                                    decl = int(parts[12]) if parts[12] not in ("", "-") else 0
                                    flat_count = int(parts[13]) if parts[13] not in ("", "-") else 0
                                except (ValueError, IndexError):
                                    continue
                                cache[k_date] = {"up": adv, "down": decl, "flat": flat_count}
                                added += 1
                            elif len(parts) >= 12:
                                # 尝试只有12个字段的情况
                                try:
                                    adv = int(parts[11]) if parts[11] not in ("", "-") else 0
                                    decl = 0
                                    flat_count = 0
                                except ValueError:
                                    continue
                                cache[k_date] = {"up": adv, "down": decl, "flat": flat_count}
                                added += 1
                    if added:
                        _save_breadth_cache(cache)
                    print(f"  涨跌家数补全(push2his): {added}天 (字段数={n_fields}, 缺失={len(missing_dates)})")
                    return added
                else:
                    print("  涨跌家数补全: push2his返回空klines")
            else:
                print(f"  涨跌家数补全: push2his返回结构异常")
        else:
            print(f"  涨跌家数补全: push2his HTTP {r.status_code}")
    except Exception as e:
        print(f"  涨跌家数补全(push2his异常): {e}")

    print(f"  涨跌家数补全: 无法获取, 缺失{len(missing_dates)}天的数据将随每日运行逐步补全")
    return 0
        return 0


def _get_breadth_for_date(date_str: str) -> dict | None:
    """获取指定日期的涨跌家数（从缓存），当天则尝试实时拉取。"""
    cache = _load_breadth_cache()
    today_str = datetime.now().strftime("%Y-%m-%d")

    if date_str == today_str:
        data = _fetch_today_breadth()
        if data:
            cache[today_str] = data
            _save_breadth_cache(cache)
            return data

    return cache.get(date_str)


def _fetch_sse_daily_kline() -> dict[str, dict]:
    """获取上证指数日K线数据，返回 {日期: {open, close, high, low}}。"""
    try:
        url = (
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "CN_MarketData.getKLineData?symbol=sh000001&scale=240&ma=no&datalen=300"
        )
        r = session.get(url, timeout=15)
        if r.status_code != 200:
            print("  上证日K线数据获取失败")
            return {}
        data = r.json()
        kline_map = {}
        for item in data:
            day = item.get("day", "")
            try:
                kline_map[day] = {
                    "open": float(item["open"]),
                    "close": float(item["close"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                }
            except (ValueError, KeyError):
                continue
        print(f"  上证日K线: {len(kline_map)} 天")
        return kline_map
    except Exception as e:
        print(f"  上证日K线获取失败: {e}")
        return {}


def _build_combined_expiration_table(domestic: list[dict], a50: list[dict],
                                     kline: dict[str, dict]) -> str:
    """生成合并交割日表格（国内 + A50，各列独立上证涨跌）。"""
    from datetime import date

    def _sse_cell(date_str: str, is_past: bool) -> str:
        if date_str in kline:
            k = kline[date_str]
            chg = k["close"] - k["open"]
            pct = (chg / k["open"] * 100)
            color = "#e74c3c" if pct > 0 else "#27ae60" if pct < 0 else "#666"
            sse = (
                f'{k["close"]:.2f} '
                f'<span style="color:{color};font-size:11px">'
                f'{( "+" if pct > 0 else "" )}{pct:.2f}%</span>'
            )
            # 涨跌家数
            bd = _get_breadth_for_date(date_str)
            if bd:
                sse += (
                    f'<br><span style="font-size:10px;color:#888">'
                    f'涨<span style="color:#e74c3c">{bd["up"]}</span> '
                    f'跌<span style="color:#27ae60">{bd["down"]}</span></span>'
                )
            return sse
        if is_past:
            return '<span style="color:#888;font-size:11px">休市</span>'
        return '<span style="color:#aaa;font-size:11px">—</span>'

    def _fmt_date(date_str, is_today_flag):
        if is_today_flag:
            return f'<span style="font-weight:bold;color:#c0392b">{date_str}</span>'
        return date_str

    rows = ""
    for d, a in zip(domestic, a50):
        dom_today = d["is_today"]
        a50_today = a["is_today"]
        is_today = dom_today or a50_today
        row_style = 'style="background:#fffbe6"' if is_today else ""

        tags = []
        if dom_today:
            tags.append("今日IF/IH/IC/IM交割")
        if a50_today:
            tags.append("今日A50交割")
        if not tags:
            tags.append("已过" if d["is_past"] else "未到")

        rows += f"""            <tr {row_style}>
              <td>{d["month"]}月</td>
              <td>{_fmt_date(d["date"], dom_today)}<br><span style="color:#888;font-size:10px">{d["weekday"]}</span></td>
              <td>{_sse_cell(d["date"], d["is_past"])}</td>
              <td>{_fmt_date(a["date"], a50_today)}<br><span style="color:#888;font-size:10px">{a["weekday"]}</span></td>
              <td>{_sse_cell(a["date"], a["is_past"])}</td>
              <td style="font-size:11px;color:#888">{' · '.join(tags)}</td>
            </tr>\n"""

    return rows


def _build_expiration_card(year: str) -> str:
    """主页上的交割日链接卡片（点击弹窗显示）。"""
    return f"""<div class="card">
    <div class="card-title" style="text-align:center;cursor:pointer" onclick="openExpModal()">
      <span style="color:#2c3e50;text-decoration:none;font-size:16px;font-weight:bold;">
        {year}年 股指期货期权交割日 →
      </span>
    </div>
  </div>

<div id="expModal" style="display:none;position:fixed;z-index:1000;left:0;top:0;width:100%;height:100%;background:rgba(0,0,0,0.5)" onclick="if(event.target===this)closeExpModal()">
  <div style="position:relative;margin:30px auto;width:95%;max-width:800px;height:90vh;background:#fff;border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,0.3);overflow:hidden">
    <div style="background:#1a1a2e;color:#fff;padding:12px 20px;display:flex;justify-content:space-between;align-items:center">
      <span style="font-weight:bold">{year}年 股指期货期权交割日</span>
      <span onclick="closeExpModal()" style="cursor:pointer;font-size:22px;line-height:1">&times;</span>
    </div>
    <iframe src="expiration.html" style="width:100%;height:calc(100% - 44px);border:none"></iframe>
  </div>
</div>
<script>
function openExpModal(){{document.getElementById('expModal').style.display='block'}}
function closeExpModal(){{document.getElementById('expModal').style.display='none'}}
</script>"""


def build_expiration_page(dates: list[dict], a50_dates: list[dict],
                         kline: dict[str, dict]) -> str:
    """生成独立的交割日页面（合并表格）。"""
    year = dates[0]["date"][:4]
    rows = _build_combined_expiration_table(dates, a50_dates, kline)

    today_str = datetime.now().strftime("%Y-%m-%d")
    sse_current = ""
    if today_str in kline:
        k = kline[today_str]
        sse_current = f"上证指数 {k['close']:.2f}"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{year}年股指期货期权交割日</title>
<style>
  body {{ font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif; background: #f5f6fa; padding: 20px; }}
  .container {{ max-width: 800px; margin: 0 auto; }}
  .header {{ background: linear-gradient(135deg, #1a1a2e, #16213e); color: #fff; padding: 25px; border-radius: 12px 12px 0 0; text-align: center; }}
  .header h1 {{ margin: 0; font-size: 22px; }}
  .header p {{ margin: 8px 0 0; opacity: 0.7; font-size: 13px; }}
  .card {{ background: #fff; margin: 0 0 20px; border-radius: 0 0 12px 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.08); overflow: hidden; }}
  .card-title {{ background: #fafbfc; padding: 14px 20px; font-size: 16px; font-weight: bold; color: #2c3e50; border-bottom: 1px solid #eee; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #f8f9fa; padding: 10px 8px; text-align: center; font-weight: 600; color: #555; border-bottom: 2px solid #e9ecef; }}
  td {{ padding: 10px 8px; border-bottom: 1px solid #f0f0f0; text-align: center; }}
  td:first-child {{ text-align: left; }}
  td:last-child {{ text-align: left; }}
  tr:hover {{ background: #fafbfe; }}
  .back {{ text-align: center; padding: 16px; }}
  .back a {{ color: #2980b9; text-decoration: none; font-size: 14px; }}
  .back a:hover {{ text-decoration: underline; }}
  .note {{ background: #fff; padding: 14px 20px; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.08); font-size: 12px; color: #888; line-height: 1.8; }}
</style></head><body>
<div class="container">
  <div class="header">
    <h1>{year}年 股指期货期权交割日</h1>
    <p>{sse_current} &nbsp;|&nbsp; 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
  </div>

  <div class="card">
    <div class="card-title">交割日一览
      <span style="font-weight:normal;font-size:11px;color:#999;margin-left:8px">
        IF/IH/IC/IM (第三周五) &nbsp;|&nbsp; A50/SGX (最后营业日)
      </span>
    </div>
    <div style="overflow-x:auto">
    <table>
      <tr><th>月份</th><th>国内交割日</th><th>上证涨跌</th><th>A50交割日</th><th>上证涨跌</th><th>备注</th></tr>
{rows}    </table>
    </div>
  </div>

  <div class="note">
    <strong>说明</strong><br>
    1. 国内股指期货期权(IF/IH/IC/IM)交割日为每月第三个周五，遇节假日顺延<br>
    2. A50(富时中国A50指数期货)在新加坡SGX交易，交割日为每月最后营业日<br>
    3. 上证涨跌为交割日当天收盘价相对开盘价的涨跌幅，日期休市则显示"休市"<br>
    4. 交割日前后市场可能出现较大波动，请注意风险
  </div>
  <div class="back">
    <a href="index.html">← 返回主页</a>
  </div>
</div>
</body></html>"""
    return html
    return html

# ═══════════════════════════════════════════════════════
# HTML 邮件
# ═══════════════════════════════════════════════════════

def build_html(quotes: list[dict], announcements: list[dict],
               interpretations: dict = None,
               expiration_dates: list[dict] = None,
               kline: dict[str, dict] = None) -> str:
    """拼装 HTML 邮件正文。"""
    interp = interpretations or {}

    # ── 行情表格 ──
    if quotes:
        stock_ai = interp.get("stocks", {})
        quote_rows = ""
        for q in quotes:
            code = q['代码']
            pct = q.get("_涨跌数值", 0)
            color = "#e74c3c" if pct > 0 else "#27ae60" if pct < 0 else "#666"
            ai_text = stock_ai.get(code, '')
            ai_cell = f'<div style="color:#888;font-size:11px;font-style:italic;margin-top:2px">AI: {ai_text}</div>' if ai_text else ''
            quote_rows += f"""
            <tr id="row-{code}">
              <td>{code}<br><span style="color:#888;font-size:11px">{q['名称']}</span></td>
              <td id="price-{code}" style="color:#2c3e50;font-weight:bold;font-size:15px">{q['最新价']}</td>
              <td id="chgpct-{code}" style="color:{color}">{q['涨跌幅']}</td>
              <td id="chg-{code}" style="color:{color}">{q['涨跌额']}</td>
              <td id="open-{code}">{q.get('今开', '—')}</td>
              <td id="high-{code}">{q.get('最高', '—')}</td>
              <td id="low-{code}">{q.get('最低', '—')}</td>
              <td id="vol-{code}">{q.get('成交量', '—')}</td>
              <td id="amount-{code}">{q.get('成交额', '—')}</td>
              <td style="color:#666;font-size:11px;max-width:120px">{ai_text}</td>
            </tr>"""
    else:
        quote_rows = '<tr><td colspan="11" style="text-align:center;color:#999">今日无行情数据（可能非交易日）</td></tr>'

    # ── 交割日链接 ──
    expiration_html = ""
    if expiration_dates:
        year_str = expiration_dates[0]["date"][:4]
        expiration_html = _build_expiration_card(year_str)

    # ── 公告列表（按发布时间排序，10分钟内整理在一起）──
    if announcements:
        notice_ai = interp.get("announcements", [])
        # 预分配AI解读到公告对象
        for i, a in enumerate(announcements):
            a["_ai"] = notice_ai[i] if i < len(notice_ai) else ""

        # 解析发布时间并排序
        def _parse_dt(a):
            raw = a.get("发布时间", "")
            for fmt in ("%Y-%m-%d %H:%M:%S:%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(raw, fmt)
                except ValueError:
                    continue
            return datetime.min

        parsed = [(_parse_dt(a), a) for a in announcements]
        parsed.sort(key=lambda x: x[0], reverse=True)

        # 按10分钟窗口分组
        groups = []
        cur = []
        for dt, a in parsed:
            if not cur:
                cur.append((dt, a))
            else:
                gap = abs((cur[-1][0] - dt).total_seconds()) if dt != datetime.min else 99999
                if gap <= 600:
                    cur.append((dt, a))
                else:
                    groups.append(cur)
                    cur = [(dt, a)]
        if cur:
            groups.append(cur)

        # 渲染分组
        notice_items = ""
        for group in groups:
            group_dt = group[0][0]
            if group_dt != datetime.min:
                time_str = group_dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                time_str = group[0][1].get("日期", "")
            notice_items += f'<li style="color:#888;font-size:11px;font-weight:bold;padding-top:10px;border-bottom:none">⏰ {time_str}</li>'
            for dt, a in group:
                ai_text = a.get("_ai", "")
                ai_line = f'<div style="color:#888;font-size:11px;font-style:italic;margin-top:1px">AI: {ai_text}</div>' if ai_text else ''
                detail_url = a.get("详情链接", "")
                title_html = f'<a href="{detail_url}" target="_blank" style="color:#2c3e50;text-decoration:none" onmouseover="this.style.textDecoration=\'underline\'" onmouseout="this.style.textDecoration=\'none\'">{a["标题"]}</a>' if detail_url else a["标题"]
                notice_items += f"""
            <li>
              <span class="tag">{a['股票代码']}</span>
              {title_html}
              {ai_line}
            </li>"""
    else:
        notice_items = '<li style="color:#999">近期无新公告</li>'


    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    version = _get_version()
    push_time = _get_push_time()

    # 构建前端腾讯行情映射
    tc_entries = []
    for q in quotes:
        code = q['代码']
        tc_entries.append(f'"{code}": "{_code_to_tencent(code)}"')
    tc_map_js = "{" + ", ".join(tc_entries) + "}"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif; background: #f5f6fa; padding: 20px; }}
  .container {{ max-width: 860px; margin: 0 auto; }}
  .header {{ background: linear-gradient(135deg, #1a1a2e, #16213e); color: #fff; padding: 30px; border-radius: 12px 12px 0 0; text-align: center; }}
  .header h1 {{ margin: 0; font-size: 24px; }}
  .header p {{ margin: 8px 0 0; opacity: 0.7; font-size: 13px; }}
  .card {{ background: #fff; margin: 0 0 20px; border-radius: 0 0 12px 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.08); overflow: hidden; }}
  .card-title {{ background: #fafbfc; padding: 14px 20px; font-size: 16px; font-weight: bold; color: #2c3e50; border-bottom: 1px solid #eee; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #f8f9fa; padding: 9px 6px; text-align: left; font-weight: 600; color: #555; border-bottom: 2px solid #e9ecef; white-space: nowrap; }}
  td {{ padding: 9px 6px; border-bottom: 1px solid #f0f0f0; }}
  tr:hover {{ background: #fafbfe; }}
  ul {{ list-style: none; padding: 12px 20px; margin: 0; }}
  li {{ padding: 8px 0; border-bottom: 1px solid #f5f5f5; font-size: 13px; line-height: 1.6; }}
  li:last-child {{ border-bottom: none; }}
  .tag {{ display: inline-block; background: #e8f4fd; color: #2980b9; padding: 1px 6px; border-radius: 3px; font-size: 11px; margin-right: 6px; }}
  .disclaimer {{ text-align: center; color: #aaa; font-size: 11px; padding: 20px; line-height: 1.8; }}
  .refresh-bar {{ background: #fff; padding: 10px 20px; text-align: center; color: #888; font-size: 12px; border-top: 1px solid #eee; }}
</style></head><body>
<div class="container">
  <div class="header">
    <h1>陈姝宝的A股信息收集</h1>
    <p>数据采集时间: {now} &nbsp;|&nbsp; 数据来源: 新浪财经/东方财富 &nbsp;|&nbsp; 仅供参考</p>
  </div>

  <div class="card">
    <div class="card-title">追踪股票行情</div>
    <div style="overflow-x:auto">
    <table>
      <tr><th>代码/名称</th><th>最新价</th><th>涨跌幅</th><th>涨跌额</th><th>今开</th><th>最高</th><th>最低</th><th>成交量</th><th>成交额</th><th>AI解读</th></tr>
      {quote_rows}
    </table>
    </div>
  </div>

  {expiration_html}

  <div class="card">
    <div class="card-title">最新公告</div>
    <ul>{notice_items}</ul>
  </div>

  <div class="refresh-bar">
    <span>行情实时更新 &nbsp;|&nbsp; 整页刷新: <span id="timer">300</span>秒</span>
    <span style="float:right">数据采集: {now}</span>
  </div>

  <div class="disclaimer">
    本页面为自动化数据采集结果，仅收集公开市场信息<br>
    不构成任何投资建议，请独立判断与决策<br>
    数据可能存在延迟，以交易所官方数据为准<br>
    <span style="color:#bbb">v{version} | 部署时间: {push_time}</span>
  </div>
</div>
<script>
// ── 股票代码-腾讯格式映射 (由Python端填充) ──
var TC_MAP = {tc_map_js};
var PAGE_REFRESH = 300;
var QUOTE_IV = 5;

function _fmtVol(v) {{
    if (v >= 1e8) return (v / 1e8).toFixed(2) + "亿";
    if (v >= 1e4) return (v / 1e4).toFixed(2) + "万";
    return String(v);
}}
function _fmtAmt(v) {{
    if (v >= 1e8) return (v / 1e8).toFixed(2) + "亿";
    if (v >= 1e4) return (v / 1e4).toFixed(2) + "万";
    return v.toFixed(2);
}}

function updateQuotes() {{
    var codes = Object.values(TC_MAP).join(",");
    if (!codes) return;
    var s = document.createElement("script");
    s.src = "https://qt.gtimg.cn/q=" + codes + "&_=" + Date.now();
    s.onload = function() {{
        Object.keys(TC_MAP).forEach(function(code) {{
            var raw = window["v_" + TC_MAP[code]];
            if (!raw) return;
            var p = raw.split("~");
            if (p.length < 40) return;

            var price = parseFloat(p[3]), prev = parseFloat(p[4]);
            var chg = price - prev;
            var pct = prev ? (chg / prev * 100) : 0;
            var color = pct > 0 ? "#e74c3c" : pct < 0 ? "#27ae60" : "#666";

            var set = function(id, txt, c) {{
                var el = document.getElementById(id);
                if (el) {{ el.textContent = txt; if (c) el.style.color = c; }}
            }};

            set("price-" + code, price.toFixed(2));
            set("chgpct-" + code, (pct > 0 ? "+" : "") + pct.toFixed(2) + "%", color);
            set("chg-" + code, (chg > 0 ? "+" : "") + chg.toFixed(2), color);
            set("open-" + code, parseFloat(p[5]) > 0 ? p[5] : "—");
            set("high-" + code, p[33]);
            set("low-" + code, p[34]);
            set("vol-" + code, _fmtVol(parseInt(p[6]) || 0));
            set("amount-" + code, _fmtAmt((parseFloat(p[57]) || 0) * 10000));
        }});
        s.remove();
    }};
    s.onerror = function() {{ s.remove(); }};
    document.head.appendChild(s);
}}

var sec = PAGE_REFRESH;
setInterval(function() {{
    sec--;
    document.getElementById("timer").textContent = sec;
    if (sec <= 0) location.reload();
}}, 1000);

setInterval(updateQuotes, QUOTE_IV * 1000);
updateQuotes();
</script>
</body></html>"""

    return html


def send_email(html: str, config: dict):
    """发送 HTML 邮件。失败则保存到本地。"""
    print("[4/4] 发送邮件...")
    email_cfg = config["email"]

    if "你的邮箱" in email_cfg["sender"] or "你的SMTP" in email_cfg["password"]:
        # 还没配置邮箱 — 仅保存本地
        backup = CACHE_DIR / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        backup.write_text(html, encoding="utf-8")
        print(f"  邮箱未配置，报告已保存至本地: {backup}")
        print(f"  请编辑 config.json 填入真实邮箱信息后即可发送邮件。")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"陈姝宝的A股信息收集 — {datetime.now().strftime('%Y-%m-%d')}"
    msg["From"] = email_cfg["sender"]
    msg["To"] = ", ".join(email_cfg["receivers"])
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        smtp = smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"], timeout=30)
        smtp.starttls()
        smtp.login(email_cfg["sender"], email_cfg["password"])
        smtp.sendmail(email_cfg["sender"], email_cfg["receivers"], msg.as_string())
        smtp.quit()
        print(f"  邮件已发送至: {', '.join(email_cfg['receivers'])}")
        return True
    except Exception as e:
        print(f"  邮件发送失败: {e}")
        backup = CACHE_DIR / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        backup.write_text(html, encoding="utf-8")
        print(f"  报告已保存至本地: {backup}")
        return False


# ═══════════════════════════════════════════════════════
# 网页服务
# ═══════════════════════════════════════════════════════

WEB_HTML = CACHE_DIR / "index.html"

# 服务模式下缓存最新HTML
_cached_html = ""
_cache_lock = threading.Lock()


def _refresh_loop(config: dict, interval: int):
    """后台线程：每interval秒拉取数据，更新缓存。"""
    global _cached_html
    while True:
        try:
            print(f"\n{'='*50}")
            print(f"  [后台刷新] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            html = run_collect(config)
            with _cache_lock:
                _cached_html = html
            print(f"  [后台刷新] 完成，下次刷新: {interval}秒后")
        except Exception as e:
            print(f"  [后台刷新] 失败: {e}")
        time.sleep(interval)


def start_server(port: int = 8080, refresh_interval: int = 30):
    """启动本地 HTTP 服务器，后台每refresh_interval秒自动拉取最新数据。"""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                with _cache_lock:
                    html = _cached_html if _cached_html else "<h2>正在采集数据，请稍后刷新...</h2>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] {args[0]}")

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"  网页地址: http://localhost:{port}")
    print(f"  数据刷新间隔: {refresh_interval}秒")
    print(f"  按 Ctrl+C 停止服务")

    # 后台线程定时刷新数据
    t = threading.Thread(target=_refresh_loop, args=(config, refresh_interval), daemon=True)
    t.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  服务已停止")


# ═══════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════

def run_collect(config: dict) -> str:
    """执行数据采集，返回 HTML 报告字符串。"""
    stocks = config["stocks"]
    codes = [s["code"] for s in stocks]

    if not codes:
        print("未配置追踪股票，请编辑 config.json 添加股票代码")
        sys.exit(1)

    stock_names = [f"{s['name']}({s['code']})" for s in stocks]
    print(f"追踪股票: {', '.join(stock_names)}")

    quotes = fetch_realtime_quotes(stocks)
    announcements = fetch_announcements(codes)

    # 股指期货期权交割日
    exp_dates = []
    a50_dates = []
    kline = {}
    try:
        year = datetime.now().year
        exp_dates = _get_expiration_dates(year)
        a50_dates = _get_a50_expiration_dates(year)
        kline = _fetch_sse_daily_kline()
        _backfill_breadth()
        exp_page = build_expiration_page(exp_dates, a50_dates, kline)
        (BASE / "expiration.html").write_text(exp_page, encoding="utf-8")
        print(f"  交割日表: 国内{len(exp_dates)}月 + A50{len(a50_dates)}月, 页面已保存")

        # 交割日微信提醒
        notify_cfg = config.get("notify", {})
        _send_expiration_reminder(exp_dates, a50_dates, notify_cfg)
    except Exception as e:
        print(f"  交割日表生成失败: {e}")

    ai_cfg = config.get("ai", {})
    interpretations = generate_interpretations(quotes, announcements, ai_cfg)

    return build_html(quotes, announcements, interpretations, exp_dates, kline)


def run():
    import argparse

    parser = argparse.ArgumentParser(description="A股信息收集器")
    parser.add_argument("-s", "--serve", nargs="?", const=8080, type=int, metavar="PORT",
                        help="启动本地网页服务, 默认端口8080")
    parser.add_argument("-o", "--output", nargs="?", const=str(WEB_HTML), type=str, metavar="PATH",
                        help="输出HTML报告到指定文件")
    parser.add_argument("--no-email", action="store_true", help="跳过邮件发送")
    args = parser.parse_args()

    print("=" * 50)
    print(f"  A股信息收集器 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    config = load_config()
    html = run_collect(config)

    # 输出文件
    if args.output:
        out_path = Path(args.output)
        out_path.write_text(html, encoding="utf-8")
        print(f"  报告已保存: {out_path}")

    # 邮件
    if not args.no_email and not args.serve:
        send_email(html, config)

    # 网页服务
    if args.serve is not None:
        port = args.serve
        print("=" * 50)
        # 先立即采集一次数据作为初始缓存
        print(f"  [初始采集] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        try:
            html = run_collect(config)
            with _cache_lock:
                _cached_html = html
            print("  初始数据采集完成")
        except Exception as e:
            print(f"  初始采集失败: {e}")
        start_server(port, 30)
    else:
        # 非serve模式也保存一份到 web 目录
        WEB_HTML.write_text(html, encoding="utf-8")
        print("=" * 50)
        print("  完成")
        print("=" * 50)


if __name__ == "__main__":
    run()
