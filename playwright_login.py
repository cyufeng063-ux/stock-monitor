#!/usr/bin/env python3
"""
同花顺问财 Cookie 刷新工具 — Playwright 持久化浏览器

通过持久化浏览器 profile，用户只需登录一次，后续自动提取 Cookie，
无需手动 F12 复制。

用法:
    python playwright_login.py             # 打开浏览器，手动登录
    python playwright_login.py --headless  # 无头模式，从已有 profile 提取
    python playwright_login.py --check     # 仅检查登录状态
    python playwright_login.py --fresh     # 清除旧 profile，重新开始
"""

import argparse
import logging
import os
import shutil
import sys
import time

logger = logging.getLogger("playwright_login")

# 同花顺问财 目标域名
TARGET_URL = "https://www.iwencai.com"
COOKIE_DOMAIN = "https://www.iwencai.com"


def setup_logging():
    """极简日志配置 (独立脚本，不依赖 logger_config)。"""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(message)s", datefmt="%H:%M:%S"
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ---------- 登录状态检测 ----------

def check_login_status(context, page) -> bool:
    """检测当前浏览器是否已登录同花顺问财。

    双重检查:
    1. Cookie 中是否有 v 字段 (服务端 session 标识)
    2. 页面中是否有「登录」入口 (未登录会有)

    Args:
        context: Playwright BrowserContext
        page: Playwright Page (已导航到 iwencai.com)

    Returns:
        True 表示已登录
    """
    # 检查 1: Cookie
    cookies = context.cookies(COOKIE_DOMAIN)
    has_v = any(c["name"] == "v" for c in cookies)

    # 检查 2: 页面登录入口
    page_has_login = False
    try:
        # 同花顺未登录时导航栏有 "登录" 链接/按钮
        login_element = page.locator('a:has-text("登录"), button:has-text("登录")')
        # 排除 footer 等位置
        visible_login = login_element.locator("visible=true")
        page_has_login = visible_login.count() > 0
    except Exception:
        # 页面可能还在加载，保守判定为未登录
        page_has_login = True

    return has_v and not page_has_login


# ---------- Cookie 提取与保存 ----------

def extract_cookies(context) -> str:
    """从浏览器上下文中提取 iwencai.com 域的所有 Cookie。

    Args:
        context: Playwright BrowserContext

    Returns:
        Cookie 字符串，格式: key1=value1; key2=value2; ...
    """
    cookies = context.cookies(COOKIE_DOMAIN)
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    field_count = len(cookies)
    has_v = any(c["name"] == "v" for c in cookies)
    has_uid = any(c["name"] == "other_uid" for c in cookies)

    logger.info("提取到 %d 个 Cookie 字段 (v=%s, other_uid=%s)",
                field_count, "✓" if has_v else "✗", "✓" if has_uid else "✗")
    return cookie_str


def save_cookie(cookie_str: str, file_path: str) -> bool:
    """保存 Cookie 到文件。

    Args:
        cookie_str: Cookie 字符串
        file_path: 保存路径

    Returns:
        是否成功
    """
    if "v=" not in cookie_str:
        logger.error("Cookie 无效: 缺少 v 字段，无法保存")
        return False

    os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(cookie_str)

    logger.info("Cookie 已保存到: %s (%d 字符)", file_path, len(cookie_str))
    return True


# ---------- 主流程 ----------

def login_via_browser(
    profile_dir: str = "cookies/browser_profile",
    headless: bool = False,
    timeout: int = 300,
) -> str | None:
    """通过 Playwright 持久化浏览器获取/刷新 Cookie。

    核心流程:
    1. 启动持久化浏览器 (profile 可复用)
    2. 访问 iwencai.com
    3. 检测登录状态
       - 已登录 → 直接提取 Cookie
       - 未登录 → 等待用户手动登录 (可见模式)
    4. 提取并返回 Cookie 字符串

    Args:
        profile_dir: 浏览器持久化 profile 目录
        headless: True=无头模式仅尝试已有 session, 不等待登录
        timeout: 等待用户登录的超时秒数

    Returns:
        Cookie 字符串，失败返回 None
    """
    from playwright.sync_api import sync_playwright

    os.makedirs(profile_dir, exist_ok=True)

    logger.info("启动 Playwright Chromium...")
    logger.info("Profile 目录: %s", os.path.abspath(profile_dir))

    pw = sync_playwright().start()

    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
            ignore_default_args=["--enable-automation"],
        )

        page = context.new_page()
        logger.info("正在访问 %s ...", TARGET_URL)

        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            logger.warning("页面加载超时，继续尝试检测登录状态...")

        page.wait_for_timeout(2000)  # 等待 Cookie 和 JS 完全初始化

        # --- 情况 1: 已有有效 session ---
        if check_login_status(context, page):
            logger.info("✓ 检测到已登录状态 (浏览器 profile 有效)")
            cookie = extract_cookies(context)
            context.close()
            pw.stop()
            return cookie

        # --- 情况 2: 无头模式但 session 已过期 ---
        if headless:
            logger.error(
                "无头模式下未检测到登录状态。\n"
                "请先运行 python playwright_login.py (不带 --headless) 手动登录一次。"
            )
            context.close()
            pw.stop()
            return None

        # --- 情况 3: 可见模式，等待用户手动登录 ---
        print()
        print("=" * 60)
        print("  请在打开的浏览器窗口中登录同花顺")
        print(f"  网址: {TARGET_URL}")
        print(f"  等待超时: {timeout} 秒")
        print("=" * 60)
        print()

        start = time.time()
        last_notify = start

        while time.time() - start < timeout:
            try:
                # 每次检查前先刷新页面，确保Cookie被正确设置
                if check_login_status(context, page):
                    print()
                    logger.info("✓ 检测到登录成功！等待 Cookie 完全写入...")
                    page.wait_for_timeout(3000)  # 3秒安全边际
                    cookie = extract_cookies(context)
                    context.close()
                    pw.stop()
                    return cookie
            except Exception as e:
                logger.debug("登录检测轮询异常: %s", e)

            # 每 30 秒通知一次进度
            elapsed = time.time() - last_notify
            if elapsed > 30:
                remaining = int(timeout - (time.time() - start))
                print(f"  ... 等待登录中 (剩余 {remaining} 秒)")
                last_notify = time.time()

            time.sleep(2)

        # 超时
        logger.error("等待登录超时 (%d 秒)", timeout)
        context.close()
        pw.stop()
        return None

    except Exception as e:
        logger.error("Playwright 异常: %s", e)
        # 尝试清理
        try:
            pw.stop()
        except Exception:
            pass
        return None


# ---------- CLI ----------

def main():
    setup_logging()

    parser = argparse.ArgumentParser(
        description="同花顺问财 Cookie 刷新工具 (Playwright)"
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="无头模式: 仅尝试从已有浏览器 profile 提取 Cookie (不打开窗口)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="仅检查登录状态 (不保存 Cookie)",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="清除旧 profile，重新开始",
    )
    parser.add_argument(
        "--profile-dir", default="cookies/browser_profile",
        help="浏览器持久化 profile 目录 (默认 cookies/browser_profile)",
    )
    parser.add_argument(
        "--output", default="cookies/cookie.txt",
        help="Cookie 输出文件路径 (默认 cookies/cookie.txt)",
    )
    parser.add_argument(
        "--timeout", type=int, default=300,
        help="等待用户登录的超时秒数 (默认 300)",
    )
    args = parser.parse_args()

    # --fresh: 清除旧 profile
    if args.fresh:
        profile_abs = os.path.abspath(args.profile_dir)
        if os.path.exists(profile_abs):
            logger.info("清除旧 profile: %s", profile_abs)
            shutil.rmtree(profile_abs, ignore_errors=True)
        # 同时清除旧 cookie
        output_abs = os.path.abspath(args.output)
        if os.path.exists(output_abs):
            os.remove(output_abs)
            logger.info("清除旧 Cookie: %s", output_abs)

    # --check: 仅检查登录状态
    if args.check:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=args.profile_dir,
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
            page = context.new_page()
            try:
                page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(2000)

            if check_login_status(context, page):
                print("已登录 ✓")
            else:
                print("未登录 ✗")

            context.close()
        finally:
            pw.stop()
        return

    # --- 正常流程: 获取/刷新 Cookie ---
    output_abs = os.path.abspath(args.output)

    if args.headless:
        logger.info("=== 无头模式: 尝试从已有 profile 提取 Cookie ===")
    else:
        logger.info("=== 交互模式: 将打开浏览器等待登录 ===")

    cookie = login_via_browser(
        profile_dir=args.profile_dir,
        headless=args.headless,
        timeout=args.timeout,
    )

    if cookie:
        save_cookie(cookie, output_abs)
        print()
        print("=" * 60)
        print("  ✓ Cookie 刷新成功！")
        print(f"  文件: {output_abs}")
        print(f"  可以运行 python run_daily.py 开始抓取数据")
        print("=" * 60)
        sys.exit(0)
    else:
        print()
        print("=" * 60)
        if args.headless:
            print("  ✗ 无头模式未能提取 Cookie")
            print("  请运行 python playwright_login.py 手动登录")
        else:
            print("  ✗ 登录未完成或超时")
            print("  请重新运行 python playwright_login.py 重试")
        print("=" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
