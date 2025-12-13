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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Tuple, Optional

import httpx
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


@dataclass
class Config:
    base_url: str
    start_date: str
    end_date: str
    download_dir: str
    max_workers: int
    python_version: Optional[str]
    arch: str
    variant: str
    build_prefix: str
    dry_run: bool
    retries: int
    connect_timeout: float
    read_timeout: float
    http2: bool
    insecure: bool


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
                mode = "ab" if "Range" in headers else "wb"
                with open(save_path, mode) as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 15):  # 32 KiB
                        if not chunk:
                            continue
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
    parser = argparse.ArgumentParser(description="下载 MindSpore master/nightly 构建包（优化&兼容版）")
    parser.add_argument("--start_date", required=True, help="起始日期 YYYYMMDD")
    parser.add_argument("--end_date", required=True, help="结束日期 YYYYMMDD")
    parser.add_argument("--download_dir", default="downloads", help="下载保存目录")
    parser.add_argument("--num_workers", type=int, default=4, help="并发下载线程数")
    parser.add_argument("--python_version", help="Python 版本过滤（如 cp39 / cp310 / cp311）")
    parser.add_argument("--arch", default="aarch64", help="架构目录（默认 aarch64，可选 x86_64 等）")
    parser.add_argument("--variant", default="unified", help="variant 目录（默认 unified）")
    parser.add_argument("--build_prefix", default="master_", help="构建目录前缀（默认 master_，也可 nightly_ 等）")
    parser.add_argument("--base_url", default=DEFAULT_BASE_URL, help="根目录 URL")
    parser.add_argument("--dry_run", action="store_true", help="只列出将要下载的文件，不实际下载")
    parser.add_argument("--retries", type=int, default=4, help="请求与下载最大重试次数")
    parser.add_argument("--connect_timeout", type=float, default=10.0, help="连接超时秒数")
    parser.add_argument("--read_timeout", type=float, default=60.0, help="读取超时秒数")
    parser.add_argument("--http2", action="store_true", help="启用 HTTP/2（默认关闭；部分镜像不稳定时可关闭）")
    parser.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验（不安全）")
    args = parser.parse_args()

    cfg = Config(
        base_url=args.base_url,
        start_date=args.start_date,
        end_date=args.end_date,
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

            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
                futs = []
                for url, save_path, task_id, size, _, _ in tasks_meta:
                    futs.append(
                        ex.submit(
                            download_one, client, url, save_path, task_id, progress, total_task_id, cfg, size
                        )
                    )
                for f in as_completed(futs):
                    _ = f.result()

    logger.info("所有下载任务完成 ✅")


if __name__ == "__main__":
    main()
