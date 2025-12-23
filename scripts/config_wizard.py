#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Dev Scripts 配置向导

交互式生成配置文件的工具。
"""

import os
import sys
from pathlib import Path


def get_input(prompt: str, default: str = "") -> str:
    """获取用户输入，支持默认值"""
    if default:
        full_prompt = f"{prompt} [{default}]: "
    else:
        full_prompt = f"{prompt}: "
    result = input(full_prompt).strip()
    return result if result else default


def get_yes_no(prompt: str, default: bool = False) -> bool:
    """获取是/否输入"""
    default_str = "Y/n" if default else "y/N"
    while True:
        result = input(f"{prompt} [{default_str}]: ").strip().lower()
        if not result:
            return default
        if result in ("y", "yes", "是", "1"):
            return True
        if result in ("n", "no", "否", "0"):
            return False
        print("请输入 y/yes 或 n/no")


def get_int_input(prompt: str, default: int, min_val: int = None, max_val: int = None) -> int:
    """获取整数输入"""
    while True:
        result = get_input(prompt, str(default))
        try:
            val = int(result)
            if min_val is not None and val < min_val:
                print(f"值必须大于等于 {min_val}")
                continue
            if max_val is not None and val > max_val:
                print(f"值必须小于等于 {max_val}")
                continue
            return val
        except ValueError:
            print("请输入有效的整数")


def get_choice(prompt: str, choices: list, default_index: int = 0) -> str:
    """获取选项输入"""
    print(f"\n{prompt}")
    for i, choice in enumerate(choices):
        mark = "*" if i == default_index else " "
        print(f"  {mark} {i + 1}. {choice}")

    while True:
        result = get_input("请选择", str(default_index + 1))
        try:
            index = int(result) - 1
            if 0 <= index < len(choices):
                return choices[index]
            print(f"请输入 1-{len(choices)} 之间的数字")
        except ValueError:
            print("请输入有效的数字")


def wizard():
    """运行配置向导"""
    print("=" * 60)
    print("  Dev Scripts 配置向导")
    print("=" * 60)
    print()
    print("此向导将帮助您创建配置文件。")
    print("您可以随时按 Ctrl+C 退出。")
    print()

    # 选择配置文件位置
    print("\n配置文件位置:")
    print("  1. 当前项目目录 (.dev_scripts_config.yml)")
    print("     - 仅在当前项目中生效")
    print("     - 可以提交到版本控制")
    print()
    print("  2. 用户主目录 (~/.dev_scripts_config.yml)")
    print("     - 所有项目共享配置")
    print("     - 不会影响其他开发者")
    print()

    choice = get_input("选择配置文件位置 (1/2)", "1")
    if choice == "1":
        config_path = Path.cwd() / ".dev_scripts_config.yml"
    else:
        config_path = Path.home() / ".dev_scripts_config.yml"

    print(f"\n配置文件将保存到: {config_path}")
    print()

    # 检查文件是否已存在
    if config_path.exists():
        print("⚠️  配置文件已存在！")
        if not get_yes_no("是否覆盖？", False):
            print("已取消。")
            return

    print("\n" + "-" * 60)
    print("配置 MindSpore 下载器")
    print("-" * 60)

    # 收集配置
    download_dir = get_input("下载保存目录", "downloads")

    max_workers = get_int_input("并发下载线程数", 4, min_val=1, max_val=16)

    python_version_choice = get_choice(
        "Python 版本过滤 (可选)",
        ["不过滤", "cp39", "cp310", "cp311", "cp312"],
        0
    )
    python_version = None if python_version_choice == "不过滤" else python_version_choice

    arch_choice = get_choice(
        "架构",
        ["aarch64", "x86_64"],
        0
    )
    arch = arch_choice

    variant = get_input("Variant 目录", "unified")
    build_prefix = get_input("构建目录前缀", "master_")

    print("\n" + "-" * 60)
    print("网络设置")
    print("-" * 60)

    base_url = get_input("根目录 URL", "https://repo.mindspore.cn/mindspore/mindspore/version/")

    retries = get_int_input("最大重试次数", 4, min_val=1, max_val=10)
    connect_timeout = get_int_input("连接超时秒数", 10, min_val=1)
    read_timeout = get_int_input("读取超时秒数", 60, min_val=5)

    http2 = get_yes_no("启用 HTTP/2", False)
    insecure = get_yes_no("跳过 TLS 证书校验（不安全）", False)

    print("\n" + "=" * 60)
    print("配置预览")
    print("=" * 60)
    print()
    print("ms_downloader:")
    print(f"  download_dir: {download_dir!r}")
    print(f"  max_workers: {max_workers}")
    print(f"  python_version: {python_version!r}")
    print(f"  arch: {arch!r}")
    print(f"  variant: {variant!r}")
    print(f"  build_prefix: {build_prefix!r}")
    print(f"  base_url: {base_url!r}")
    print(f"  retries: {retries}")
    print(f"  connect_timeout: {connect_timeout}")
    print(f"  read_timeout: {read_timeout}")
    print(f"  http2: {http2}")
    print(f"  insecure: {insecure}")
    print(f"  dry_run: false")
    print()

    if not get_yes_no("是否保存配置？", True):
        print("已取消。")
        return

    # 写入配置文件
    config_content = f"""# dev-scripts 配置文件
# 由配置向导生成

ms_downloader:
  # 下载保存目录
  download_dir: {download_dir}

  # 并发下载线程数
  max_workers: {max_workers}

  # Python 版本过滤（可选：cp39, cp310, cp311, cp312 等）
  python_version: {python_version if python_version else 'null'}

  # 架构目录（aarch64, x86_64 等）
  arch: {arch}

  # variant 目录
  variant: {variant}

  # 构建目录前缀（master_, nightly_ 等）
  build_prefix: {build_prefix}

  # 根目录 URL
  base_url: {base_url}

  # 请求与下载最大重试次数
  retries: {retries}

  # 连接超时秒数
  connect_timeout: {connect_timeout}

  # 读取超时秒数
  read_timeout: {read_timeout}

  # 启用 HTTP/2
  http2: {str(http2).lower()}

  # 跳过 TLS 证书校验（不安全，仅用于测试）
  insecure: {str(insecure).lower()}

  # 只列出将要下载的文件，不实际下载
  dry_run: false
"""

    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(config_content, encoding="utf-8")
        print(f"\n✅ 配置已保存到: {config_path}")
        print()
        print("您现在可以使用以下命令:")
        print(f"  dev-scripts ms-download --last 7days")
        print()
        print("配置文件中的默认值将被自动使用。")
    except Exception as e:
        print(f"\n❌ 保存配置失败: {e}")
        return 1

    return 0


def main():
    """主入口"""
    try:
        return wizard()
    except KeyboardInterrupt:
        print("\n\n已取消。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
