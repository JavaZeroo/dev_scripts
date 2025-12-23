#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MindSpore nightly/master 构建包下载器（优化&兼容版）

改进点：
- 统一使用 httpx.Client，会话级 verify/follow_redirects；
- 自动兼容 httpx 新旧版本的 Limits 参数（max_keepalive vs max_keepalive_connections）；
- 指数退避重试；
- 断点续传/已完成跳过；
- 进度条：总体+单文件、速率、剩余时间；
- 可过滤 Python 版本（cp39/cp310/cp311 等）；
- 可配置 base_url / arch / variant / build_prefix；
- dry-run 列表模式。
"""

import argparse
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple, Optional

import httpx
import yaml

# 全局中断标志
_shutdown_event = threading.Event()
from bs4 import BeautifulSoup
from rich.logging import RichHandler
from rich.progress import (
    Progress, SpinnerColumn, BarColumn, TextColumn,
    TimeRemainingColumn, DownloadColumn, TransferSpeedColumn,
    TaskProgressColumn
)
import logging

# 仅用于关闭 InsecureRequestWarning（若不需要可删除）
from requests.packages.urllib3.exceptions import InsecureRequestWarning  # type: ignore
import requests  # noqa: F401

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


def _signal_handler(signum, frame):
    """处理 Ctrl+C 信号，设置全局中断标志"""
    logger.warning("\n收到中断信号，正在停止下载...")
    _shutdown_event.set()


# ------------------ 日志 ------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)]
)
logger = logging.getLogger("mindspore_download")

# ------------------ 常量/配置 ------------------
DEFAULT_BASE_URL = "https://repo.mindspore.cn/mindspore/mindspore/version/"
CONFIG_FILE_PATHS = [
    Path.cwd() / ".dev_scripts_config.yml",
    Path.home() / ".dev_scripts_config.yml",
]


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    start_date: str = ""
    end_date: str = ""
    download_dir: str = "downloads"
    max_workers: int = 4
    python_version: Optional[str] = None
    arch: str = "aarch64"
    variant: str = "unified"
    build_prefix: str = "master_"
    dry_run: bool = False
    retries: int = 4
    connect_timeout: float = 10.0
    read_timeout: float = 60.0
    http2: bool = False
    insecure: bool = False


def load_config_from_file() -> dict:
    """从配置文件加载默认值"""
    for config_path in CONFIG_FILE_PATHS:
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    if data and "ms_downloader" in data:
                        logger.info(f"加载配置文件: {config_path}")
                        return data["ms_downloader"]
            except Exception as e:
                logger.warning(f"读取配置文件失败 {config_path}: {e}")
    return {}


def parse_last_argument(last_str: str) -> Tuple[str, str]:
    """
    解析 --last 参数，返回 (start_date, end_date)
    支持: "7days", "2weeks", "3months"
    """
    last_str = last_str.lower().strip()

    # 提取数字和单位
    import re
    match = re.match(r"(\d+)\s*(day|days|week|weeks|month|months)", last_str)
    if not match:
        raise ValueError(f"无法解析 --last 参数: {last_str}")

    count = int(match.group(1))
    unit = match.group(2)

    end_date = datetime.now()
    if unit.startswith("day"):
        start_date = end_date - timedelta(days=count)
    elif unit.startswith("week"):
        start_date = end_date - timedelta(weeks=count)
    elif unit.startswith("month"):
        # 近似计算，每月30天
        start_date = end_date - timedelta(days=count * 30)
    else:
        raise ValueError(f"不支持的时间单位: {unit}")

    return start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")


# ------------------ 工具函数 ------------------
def generate_dates(start_date: str, end_date: str) -> List[str]:
    try:
        start = datetime.strptime(start_date, "%Y%m%d")
        end = datetime.strptime(end_date, "%Y%m%d")
    except ValueError as e:
        logger.error(f"日期格式错误(需要 YYYYMMDD): {e}")
        return []
    if start > end:
        start, end = end, start
    dates = []
    cur = start
    while cur <= end:
        dates.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    if dates:
        logger.info(f"生成 {len(dates)} 个日期，从 {dates[0]} 到 {dates[-1]}")
    return dates


def _sleep_backoff(attempt: int):
    # 2^attempt 的轻量退避，加入抖动
    delay = min(30, (2 ** attempt)) + (0.2 * attempt)
    time.sleep(delay)


def make_limits(max_keepalive: int, max_connections: int) -> httpx.Limits:
    """
    兼容 httpx 新旧版本的 Limits 参数：
      - 新版: max_keepalive_connections
      - 旧版: max_keepalive
    """
    try:
        return httpx.Limits(
            max_keepalive_connections=max_keepalive,
            max_connections=max_connections,
        )
    except TypeError:
        # 老版本回退
        return httpx.Limits(
            max_keepalive=max_keepalive,
            max_connections=max_connections,
        )


def fetch_html(client: httpx.Client, url: str, retries: int) -> Optional[str]:
    for attempt in range(retries):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            if attempt == retries - 1:
                logger.error(f"GET 失败: {url} -> {e}")
                return None
            logger.warning(f"GET 失败: {url} -> {e}，重试中({attempt+1}/{retries})...")
            _sleep_backoff(attempt + 1)
    return None


def head_size(client: httpx.Client, url: str, retries: int) -> Optional[int]:
    """获取远程文件大小，使用 Accept-Encoding: identity 避免压缩干扰"""
    for attempt in range(retries):
        try:
            # 使用 Accept-Encoding: identity 禁用压缩，确保获取真实文件大小
            headers = {"Accept-Encoding": "identity"}
            
            # 首先尝试 HEAD 请求
            r = client.head(url, headers=headers)
            if r.status_code < 400 and "Content-Length" in r.headers:
                return int(r.headers["Content-Length"])
            
            # HEAD 不支持或没有 Content-Length，尝试 Range 请求
            headers["Range"] = "bytes=0-0"
            r = client.get(url, headers=headers)
            # Range 请求返回的 Content-Range 格式: bytes 0-0/total_size
            cr = r.headers.get("Content-Range")
            if cr and "/" in cr:
                total_str = cr.split("/")[-1]
                if total_str != "*":  # * 表示服务器不知道总大小
                    return int(total_str)
            
            # 最后尝试普通 GET 请求读取 Content-Length（有些服务器只在 GET 时返回）
            # 使用 stream=True 避免下载整个文件
            with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as resp:
                if "Content-Length" in resp.headers:
                    return int(resp.headers["Content-Length"])
            
            return None
        except Exception as e:
            if attempt == retries - 1:
                logger.warning(f"获取大小失败(放弃): {url} -> {e}")
                return None
            _sleep_backoff(attempt + 1)
    return None


def parse_dir_links(html: str) -> List[str]:
    """解析目录页中的超链接（适配常见的自动索引页）"""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    table = soup.find("table", id="list") or soup.find("table")
    anchor_iter = table.find_all("a") if table else soup.find_all("a")
    for a in anchor_iter:
        href = a.get("href", "")
        if href and not href.startswith("?") and not href.startswith("#"):
            links.append(href)
    return links


def get_master_builds(cfg: Config, client: httpx.Client, date: str) -> List[str]:
    yyyymm = date[:6]
    url = f"{cfg.base_url.rstrip('/')}/{yyyymm}/{date}/"
    html = fetch_html(client, url, cfg.retries)
    if not html:
        return []
    hrefs = parse_dir_links(html)
    builds = [h for h in hrefs if h.startswith(cfg.build_prefix) and h.endswith("_newest/")]
    logger.info(f"{date} 找到 {len(builds)} 个构建目录")
    return builds


def get_download_links(cfg: Config, client: httpx.Client, date: str, build: str) -> List[str]:
    yyyymm = date[:6]
    build_url = f"{cfg.base_url.rstrip('/')}/{yyyymm}/{date}/{build}{cfg.variant}/{cfg.arch}/"
    html = fetch_html(client, build_url, cfg.retries)
    if not html:
        return []
    hrefs = parse_dir_links(html)
    links = []
    for h in hrefs:
        if not h.endswith(".whl"):
            continue
        if cfg.python_version and f"-{cfg.python_version}-" not in h:
            continue
        links.append(build_url + h)
    logger.info(f"{build.strip('/')} 找到 {len(links)} 个 .whl")
    return links


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def needs_download(local_path: str, remote_size: Optional[int]) -> Tuple[bool, int]:
    """返回 (是否需要下载, 已有大小)"""
    if not os.path.exists(local_path):
        return True, 0
    if remote_size is None:
        # 远端不知大小，保守：不覆盖已存在
        return False, os.path.getsize(local_path)
    local = os.path.getsize(local_path)
    if local >= remote_size:
        return False, local
    return True, local


def download_one(
    client: httpx.Client,
    url: str,
    save_path: str,
    task_id: int,
    progress: Progress,
    total_task_id: Optional[int],
    cfg: Config,
    remote_size: Optional[int],
) -> None:
    # 检查是否已收到中断信号
    if _shutdown_event.is_set():
        return

    # 断点续传
    need, have = needs_download(save_path, remote_size)
    if not need:
        progress.update(task_id, total=remote_size or 0, completed=remote_size or have)
        if total_task_id and remote_size:
            progress.update(total_task_id, advance=0)
        logger.info(f"跳过(已存在): {url}")
        return

    headers = {}
    if have > 0:
        headers["Range"] = f"bytes={have}-"
        # 更新进度条显示已下载的部分
        progress.update(task_id, completed=have)
        if total_task_id:
            progress.update(total_task_id, advance=have)

    attempts = cfg.retries
    for attempt in range(1, attempts + 1):
        try:
            with client.stream(
                "GET",
                url,
                headers=headers,
                timeout=httpx.Timeout(connect=cfg.connect_timeout, read=cfg.read_timeout, write=None, pool=None),
            ) as r:
                r.raise_for_status()
                
                # 检查服务器是否支持断点续传
                # 206 = Partial Content，表示支持 Range
                # 200 = OK，表示不支持 Range，返回的是完整文件
                if have > 0 and r.status_code == 200:
                    # 服务器不支持 Range，需要从头下载
                    logger.warning(f"服务器不支持断点续传，从头下载: {os.path.basename(save_path)}")
                    mode = "wb"
                    # 重置进度条
                    progress.update(task_id, completed=0)
                    if total_task_id:
                        progress.update(total_task_id, advance=-have)
                elif have > 0 and r.status_code == 206:
                    # 服务器支持断点续传
                    mode = "ab"
                    logger.info(f"断点续传: {os.path.basename(save_path)} (已有 {have} 字节)")
                else:
                    mode = "wb"
                    
                with open(save_path, mode) as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 15):  # 32 KiB
                        if not chunk:
                            continue
                        # 检查中断信号
                        if _shutdown_event.is_set():
                            logger.warning(f"下载被中断: {url}")
                            return
                        f.write(chunk)
                        progress.update(task_id, advance=len(chunk))
                        if total_task_id:
                            progress.update(total_task_id, advance=len(chunk))
            logger.info(f"下载完成: {url}")
            return
        except Exception as e:
            if attempt == attempts:
                logger.error(f"下载失败(已达最大重试): {url} -> {e}")
                return
            logger.warning(f"下载失败(第 {attempt}/{attempts} 次): {url} -> {e}，重试中...")
            _sleep_backoff(attempt)


# ------------------ 主流程 ------------------
def main():
    # 加载配置文件中的默认值
    config_defaults = load_config_from_file()

    parser = argparse.ArgumentParser(
        description="下载 MindSpore master/nightly 构建包（优化&兼容版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 使用日期范围
  %(prog)s --start_date 20251201 --end_date 20251215

  # 使用快捷日期（最近7天）
  %(prog)s --last 7days

  # 使用快捷日期（最近2周）
  %(prog)s --last 2weeks --python_version cp310
        """
    )

    # 日期参数（互斥）
    date_group = parser.add_mutually_exclusive_group(required=True)
    date_group.add_argument("--start_date", help="起始日期 YYYYMMDD")
    date_group.add_argument("--end_date", help="结束日期 YYYYMMDD")
    date_group.add_argument("--last", help="日期范围快捷方式，如 '7days', '2weeks', '3months'")

    # 下载参数
    parser.add_argument("--download_dir", default=config_defaults.get("download_dir", "downloads"),
                        help="下载保存目录")
    parser.add_argument("--num_workers", type=int, default=config_defaults.get("max_workers", 4),
                        help="并发下载线程数")
    parser.add_argument("--python_version", default=config_defaults.get("python_version"),
                        help="Python 版本过滤（如 cp39 / cp310 / cp311）")

    # 架构参数
    parser.add_argument("--arch", default=config_defaults.get("arch", "aarch64"),
                        help="架构目录（默认 aarch64，可选 x86_64 等）")
    parser.add_argument("--variant", default=config_defaults.get("variant", "unified"),
                        help="variant 目录（默认 unified）")
    parser.add_argument("--build_prefix", default=config_defaults.get("build_prefix", "master_"),
                        help="构建目录前缀（默认 master_，也可 nightly_ 等）")

    # 网络参数
    parser.add_argument("--base_url", default=config_defaults.get("base_url", DEFAULT_BASE_URL),
                        help="根目录 URL")
    parser.add_argument("--retries", type=int, default=config_defaults.get("retries", 4),
                        help="请求与下载最大重试次数")
    parser.add_argument("--connect_timeout", type=float, default=config_defaults.get("connect_timeout", 10.0),
                        help="连接超时秒数")
    parser.add_argument("--read_timeout", type=float, default=config_defaults.get("read_timeout", 60.0),
                        help="读取超时秒数")
    parser.add_argument("--http2", action="store_true", default=config_defaults.get("http2", False),
                        help="启用 HTTP/2（默认关闭；部分镜像不稳定时可关闭）")
    parser.add_argument("--insecure", action="store_true", default=config_defaults.get("insecure", False),
                        help="跳过 TLS 证书校验（不安全）")
    parser.add_argument("--dry_run", action="store_true", default=config_defaults.get("dry_run", False),
                        help="只列出将要下载的文件，不实际下载")

    args = parser.parse_args()

    # 处理 --last 参数
    if args.last:
        start_date, end_date = parse_last_argument(args.last)
    else:
        start_date = args.start_date
        end_date = args.end_date

    cfg = Config(
        base_url=args.base_url,
        start_date=start_date,
        end_date=end_date,
        download_dir=args.download_dir,
        max_workers=max(1, args.num_workers),
        python_version=args.python_version,
        arch=args.arch,
        variant=args.variant,
        build_prefix=args.build_prefix,
        dry_run=args.dry_run,
        retries=max(1, args.retries),
        connect_timeout=max(1.0, args.connect_timeout),
        read_timeout=max(5.0, args.read_timeout),
        http2=bool(args.http2),
        insecure=bool(args.insecure),
    )

    os.makedirs(cfg.download_dir, exist_ok=True)
    dates = generate_dates(cfg.start_date, cfg.end_date)
    if not dates:
        sys.exit(1)

    limits = make_limits(
        max_keepalive=cfg.max_workers * 2,
        max_connections=cfg.max_workers * 3
    )

    headers = {
        "User-Agent": "ms-download/1.1 (+https://github.com/)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    all_urls: List[Tuple[str, str, str]] = []
    with httpx.Client(
        http2=cfg.http2,
        timeout=httpx.Timeout(connect=cfg.connect_timeout, read=cfg.read_timeout, write=None, pool=None),
        headers=headers,
        limits=limits,
        verify=(not cfg.insecure),          # 会话级 verify
        follow_redirects=True               # 会话级跟随重定向
    ) as client:

        for d in dates:
            builds = get_master_builds(cfg, client, d)
            for b in builds:
                links = get_download_links(cfg, client, d, b)
                all_urls.extend((u, d, b) for u in links)

        if not all_urls:
            logger.warning("未找到任何可下载的 .whl 文件")
            return

        # 预取文件大小
        sizes: List[Optional[int]] = []
        for url, _, _ in all_urls:
            size = head_size(client, url, cfg.retries)
            sizes.append(size)

        total_known = sum(s for s in sizes if s is not None)

        if cfg.dry_run:
            logger.info(f"[dry-run] 将处理 {len(all_urls)} 个文件，总已知大小约 {total_known/1024/1024:.2f} MiB")
            for (url, date, build), s in zip(all_urls, sizes):
                logger.info(f"{date}/{build} -> {os.path.basename(url)} ({s if s is not None else '未知'} bytes)")
            return

        # 进度条 - 添加下载速度显示
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=40),
            TaskProgressColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            transient=False,
        ) as progress:
            total_task_id = progress.add_task("[red]总体进度", total=total_known if total_known > 0 else None)

            # 为每个文件创建任务
            tasks_meta = []
            for (url, date, build), size in zip(all_urls, sizes):
                filename = url.split("/")[-1]
                build_dir = os.path.join(cfg.download_dir, date, build.strip("/"))
                os.makedirs(build_dir, exist_ok=True)
                save_path = os.path.join(build_dir, filename)
                desc = f"下载 {filename}"
                task_id = progress.add_task(desc, total=size if size else None)
                tasks_meta.append((url, save_path, task_id, size, date, build))

            # 使用守护线程，主线程退出时会强制终止
            threads = []
            for url, save_path, task_id, size, _, _ in tasks_meta:
                t = threading.Thread(
                    target=download_one,
                    args=(client, url, save_path, task_id, progress, total_task_id, cfg, size),
                    daemon=True  # 守护线程，主线程退出时自动终止
                )
                threads.append(t)

            # 控制并发数：使用信号量
            semaphore = threading.Semaphore(cfg.max_workers)

            def run_with_semaphore(t):
                with semaphore:
                    if not _shutdown_event.is_set():
                        t.run()

            actual_threads = []
            for t in threads:
                wrapper = threading.Thread(target=run_with_semaphore, args=(t,), daemon=True)
                wrapper.start()
                actual_threads.append(wrapper)

            # 等待所有线程完成，但允许 Ctrl+C 中断
            try:
                while any(t.is_alive() for t in actual_threads):
                    # 短暂等待，让主线程能响应信号
                    for t in actual_threads:
                        t.join(timeout=0.1)
                        if _shutdown_event.is_set():
                            raise KeyboardInterrupt
            except KeyboardInterrupt:
                _shutdown_event.set()
                logger.warning("\n正在强制停止...")

    if _shutdown_event.is_set():
        logger.warning("下载已被用户中断 ⚠️")
        os._exit(1)  # 强制退出，不等待线程
    else:
        logger.info("所有下载任务完成 ✅")


if __name__ == "__main__":
    main()
