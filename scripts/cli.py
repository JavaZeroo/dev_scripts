#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Dev Scripts - 统一 CLI 入口

提供所有脚本的统一入口和交互式菜单。
"""

import argparse
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def main():
    # 检查是否需要直接委托给 ms-download
    if len(sys.argv) > 1 and sys.argv[1] == "ms-download":
        # 移除 "ms-download" 参数，直接调用原始脚本
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from scripts.download.ms_downloader import main as ms_download_main
        return ms_download_main()

    parser = argparse.ArgumentParser(
        description="Dev Scripts - 个人开发脚本集合",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
可用命令:
  config-wizard  交互式配置向导
  ms-download    MindSpore 包下载器

使用 'dev-scripts <command> --help' 查看具体命令的帮助。

示例:
  dev-scripts config-wizard           # 运行配置向导
  dev-scripts ms-download --last 7days
  dev-scripts ms-download --start_date 20251201 --end_date 20251215
        """
    )

    parser.add_argument(
        "--version",
        action="version",
        version="dev-scripts 0.1.0"
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # config-wizard 子命令
    config_wizard_parser = subparsers.add_parser(
        "config-wizard",
        help="交互式配置向导",
        description="通过交互式问答生成配置文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  dev-scripts config-wizard

配置向导会询问您一系列问题，并自动生成配置文件。
        """
    )

    # ms-download 子命令（仅用于帮助信息）
    ms_download_parser = subparsers.add_parser(
        "ms-download",
        help="MindSpore master/nightly 构建包下载器",
        description="下载 MindSpore master/nightly 构建包，支持断点续传、进度显示",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 使用快捷日期（最近7天）
  dev-scripts ms-download --last 7days

  # 使用日期范围
  dev-scripts ms-download --start_date 20251201 --end_date 20251215

  # 指定 Python 版本和架构
  dev-scripts ms-download --last 2weeks --python_version cp310 --arch x86_64

  # 预览将要下载的文件
  dev-scripts ms-download --last 1day --dry_run
        """
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "config-wizard":
        # 运行配置向导
        from scripts.config_wizard import wizard
        return wizard()

    if args.command == "ms-download":
        # 如果 ms-download 命令没有参数，显示帮助
        # 实际执行在函数开头已经处理
        from scripts.download.ms_downloader import main as ms_download_main
        return ms_download_main()

    return 0


if __name__ == "__main__":
    sys.exit(main())
