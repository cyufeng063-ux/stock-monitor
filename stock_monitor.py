"""
A股信息收集 & 邮件通知
纯数据管道 — 只收集公开信息并推送，不做任何分析或建议。
"""

import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"
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


def fetch_realtime_quotes(stocks: list[dict]) -> list[dict]:
    """通过新浪API拉取实时行情。"""
    print("[1/4] 拉取实时行情 (新浪)...")
    codes = [s["code"] for s in stocks]
    sina_codes = [_code_to_sina(c) for c in codes]
    url = "https://hq.sinajs.cn/list=" + ",".join(sina_codes)

    for attempt in range(3):
        try:
            r = session.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=15)
            r.encoding = "gbk"
            if r.status_code == 200 and r.text.strip():
                break
        except Exception as e:
            if attempt < 2:
                time.sleep((attempt + 1) * 3)
            else:
                print(f"  新浪行情拉取失败: {e}")
                return []
    else:
        return []

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
        # 从新浪代码反查原始代码
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
            price, change, change_pct = 0, 0, 0

        volume_val = parts[8]  # 成交量 (股)
        amount_val = parts[9]  # 成交额

        # 格式化成交量
        try:
            vol = int(volume_val)
            if vol >= 1e8:
                volume_str = f"{vol/1e8:.2f}亿"
            elif vol >= 1e4:
                volume_str = f"{vol/1e4:.2f}万"
            else:
                volume_str = str(vol)
        except ValueError:
            volume_str = volume_val

        # 格式化成交额
        try:
            amt = float(amount_val)
            if amt >= 1e8:
                amount_str = f"{amt/1e8:.2f}亿"
            elif amt >= 1e4:
                amount_str = f"{amt/1e4:.2f}万"
            else:
                amount_str = f"{amt:.2f}"
        except ValueError:
            amount_str = amount_val

        results.append({
            "代码": raw_code or parts[0],
            "名称": name,
            "最新价": f"{price:.2f}" if price else "—",
            "涨跌幅": f"{change_pct:+.2f}%",
            "涨跌额": f"{change:+.2f}",
            "今开": parts[1] if float(parts[1]) > 0 else "—",
            "昨收": parts[2],
            "最高": parts[4],
            "最低": parts[5],
            "成交量": volume_str,
            "成交额": amount_str,
            "_涨跌数值": change_pct,
        })

    print(f"  获取到 {len(results)} 只股票的实时行情")
    return results


# ═══════════════════════════════════════════════════════
# 公告 — 巨潮资讯网
# ═══════════════════════════════════════════════════════

def fetch_announcements(codes: list[str]) -> list[dict]:
    """拉取追踪股票的近期公告 (东方财富)。"""
    print("[2/4] 拉取公司公告...")
    all_notices = []

    for code in codes:
        try:
            url = (
                "https://np-anotice-stock.eastmoney.com/api/security/ann?"
                "page_size=5&page_index=1&"
                f"stock_list={code}&begin_time=&end_time="
            )
            r = session.get(url, timeout=15)
            if r.status_code != 200:
                continue
            data = r.json()
            items = data.get("data", {}).get("list", [])
            for item in items:
                all_notices.append({
                    "股票代码": code,
                    "标题": item.get("title_ch", item.get("title", "")),
                    "日期": (item.get("notice_date", "") or "")[:10],
                })
        except Exception as e:
            print(f"  公告获取失败 {code}: {e}")
        time.sleep(0.3)

    print(f"  获取到 {len(all_notices)} 条公告")
    return all_notices


# ═══════════════════════════════════════════════════════
# 新闻 — 东方财富 / 新浪
# ═══════════════════════════════════════════════════════

def fetch_news() -> list[dict]:
    """拉取最新财经新闻 (多个源)。"""
    print("[3/4] 拉取财经新闻...")

    # 源1: 东方财富
    try:
        url = "https://np-listpage.eastmoney.com/api/finance/news"
        params = {
            "page_size": 25, "page_index": 1,
            "client": "web", "biz": "finance_news",
        }
        r = session.get(url, params=params, timeout=15)
        if r.status_code == 200:
            items = r.json().get("data", {}).get("list", [])
            if items:
                news_list = []
                for item in items:
                    news_list.append({
                        "标题": item.get("title", ""),
                        "摘要": (item.get("digest", "") or item.get("summary", ""))[:200],
                        "时间": item.get("show_time", item.get("publish_time", "")),
                        "来源": "东方财富",
                    })
                print(f"  获取到 {len(news_list)} 条新闻 (东方财富)")
                return news_list
    except Exception as e:
        print(f"  东方财富新闻源失败: {e}")

    # 源2: 新浪财经
    try:
        url = "https://feed.mix.sina.com.cn/api/roll/get"
        params = {
            "pageid": 153, "lid": 2512, "k": "", "num": 25, "page": 1,
            "r": str(time.time()).replace(".", "")[:13],
        }
        r = session.get(url, params=params, timeout=15)
        if r.status_code == 200:
            items = r.json().get("result", {}).get("data", [])
            news_list = []
            for item in items:
                news_list.append({
                    "标题": item.get("title", ""),
                    "摘要": (item.get("intro", "") or item.get("summary", ""))[:200],
                    "时间": item.get("ctime", ""),
                    "来源": "新浪财经",
                })
            if news_list:
                print(f"  获取到 {len(news_list)} 条新闻 (新浪)")
                return news_list
    except Exception as e:
        print(f"  新浪新闻源失败: {e}")

    print("  所有新闻源均失败")
    return []


# ═══════════════════════════════════════════════════════
# HTML 邮件
# ═══════════════════════════════════════════════════════

def build_html(quotes: list[dict], announcements: list[dict], news: list[dict]) -> str:
    """拼装 HTML 邮件正文。"""

    # ── 行情表格 ──
    if quotes:
        quote_rows = ""
        for q in quotes:
            pct = q.get("_涨跌数值", 0)
            color = "#e74c3c" if pct > 0 else "#27ae60" if pct < 0 else "#666"
            quote_rows += f"""
            <tr>
              <td>{q['代码']}</td>
              <td><b>{q['名称']}</b></td>
              <td style="color:#2c3e50;font-weight:bold">{q['最新价']}</td>
              <td style="color:{color}">{q['涨跌幅']}</td>
              <td style="color:{color}">{q['涨跌额']}</td>
              <td>{q.get('今开', '—')}</td>
              <td>{q.get('最高', '—')}</td>
              <td>{q.get('最低', '—')}</td>
              <td>{q.get('成交量', '—')}</td>
              <td>{q.get('成交额', '—')}</td>
            </tr>"""
    else:
        quote_rows = '<tr><td colspan="10" style="text-align:center;color:#999">今日无行情数据（可能非交易日）</td></tr>'

    # ── 公告列表 ──
    if announcements:
        notice_items = ""
        for a in announcements:
            notice_items += f"""
            <li>
              <span class="tag">{a['股票代码']}</span>
              {a['标题']}
              <span style="color:#999;font-size:12px">({a['日期']})</span>
            </li>"""
    else:
        notice_items = '<li style="color:#999">近期无新公告</li>'

    # ── 新闻列表 ──
    if news:
        news_items = ""
        for n in news[:25]:
            summary = n.get('摘要', '')[:150]
            news_items += f"""
            <li>
              <b>{n['标题']}</b>
              <div style="color:#666;font-size:13px;margin-top:2px">{summary}</div>
              <span style="color:#999;font-size:11px">{n.get('时间','')} | {n.get('来源','')}</span>
            </li>"""
    else:
        news_items = '<li style="color:#999">无最新新闻</li>'

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
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
</style></head><body>
<div class="container">
  <div class="header">
    <h1>A股信息日报</h1>
    <p>数据采集时间: {now} &nbsp;|&nbsp; 数据来源: 新浪财经/东方财富 &nbsp;|&nbsp; 仅供参考</p>
  </div>

  <div class="card">
    <div class="card-title">追踪股票行情</div>
    <div style="overflow-x:auto">
    <table>
      <tr><th>代码</th><th>名称</th><th>最新价</th><th>涨跌幅</th><th>涨跌额</th><th>今开</th><th>最高</th><th>最低</th><th>成交量</th><th>成交额</th></tr>
      {quote_rows}
    </table>
    </div>
  </div>

  <div class="card">
    <div class="card-title">最新公告</div>
    <ul>{notice_items}</ul>
  </div>

  <div class="card">
    <div class="card-title">财经新闻</div>
    <ul>{news_items}</ul>
  </div>

  <div class="disclaimer">
    本邮件为自动化数据采集结果，仅收集公开市场信息<br>
    不构成任何投资建议，请独立判断与决策<br>
    数据可能存在延迟，以交易所官方数据为准
  </div>
</div>
</body></html>"""


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
    msg["Subject"] = f"A股信息日报 — {datetime.now().strftime('%Y-%m-%d')}"
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


def start_server(port: int = 8080):
    """启动本地 HTTP 服务器, 打开浏览器即可看报告。"""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                if WEB_HTML.exists():
                    html = WEB_HTML.read_text(encoding="utf-8")
                else:
                    html = "<h2>请先运行一次数据采集</h2>"
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
    print(f"  按 Ctrl+C 停止服务")
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
    news = fetch_news()

    return build_html(quotes, announcements, news)


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
        # 注入自动刷新
        refresh_html = html.replace("</head>",
            '<meta http-equiv="refresh" content="60">\n</head>')
        WEB_HTML.write_text(refresh_html, encoding="utf-8")
        print(f"  每60秒自动刷新")

        print("=" * 50)
        start_server(port)
    else:
        # 非serve模式也保存一份到 web 目录
        WEB_HTML.write_text(html, encoding="utf-8")
        print("=" * 50)
        print("  完成")
        print("=" * 50)


if __name__ == "__main__":
    run()
