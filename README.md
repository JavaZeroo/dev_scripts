# Dev Scripts

个人开发脚本集合，包含各种实用工具和自动化脚本。

## 📁 目录结构

```text
dev_scripts/
├── scripts/                           # 脚本目录
│   ├── cli.py                        # 统一 CLI 入口
│   ├── config_wizard.py              # 交互式配置向导
│   ├── download/                     # 下载相关工具
│   │   └── ms_downloader.py          # MindSpore 包下载器
│   ├── automation/                   # 自动化脚本 (待添加)
│   ├── data/                         # 数据处理工具 (待添加)
│   └── utils/                        # 通用工具函数 (待添加)
├── .dev_scripts_config.yml.example   # 配置文件示例
├── pyproject.toml                    # 项目配置
└── README.md
```

## 🚀 快速开始

### 环境配置

```bash
# 使用 uv 创建虚拟环境并安装依赖
uv sync

# 安装为全局命令（可选）
uv pip install -e .
```

### 激活环境

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

## 📜 使用指南

### 统一 CLI 入口

安装后可以使用 `dev-scripts` 命令访问所有功能：

```bash
# 查看帮助
dev-scripts --help

# 运行配置向导
dev-scripts config-wizard

# 下载 MindSpore 包
dev-scripts ms-download --last 7days
```

### 配置文件

使用交互式配置向导生成配置文件：

```bash
dev-scripts config-wizard
```

或手动复制配置文件模板：

```bash
# 复制示例配置
cp .dev_scripts_config.yml.example .dev_scripts_config.yml

# 或复制到用户目录（全局生效）
cp .dev_scripts_config.yml.example ~/.dev_scripts_config.yml
```

配置文件支持设置默认参数（如下载目录、架构、并发数等），避免每次输入。

### Download 下载工具

| 命令 | 说明 |
|------|------|
| `ms-download` | MindSpore nightly/master 构建包下载器，支持断点续传、进度显示 |

#### 使用示例

```bash
# 使用快捷日期（最近7天）
dev-scripts ms-download --last 7days

# 使用日期范围
dev-scripts ms-download --start_date 20251201 --end_date 20251215

# 使用快捷日期（最近2周，指定Python版本）
dev-scripts ms-download --last 2weeks --python_version cp310

# 预览将要下载的文件
dev-scripts ms-download --last 1day --dry_run

# 也可直接使用 ms-download 命令
ms-download --last 7days
```

## 🛠️ 开发指南

### 添加新脚本

1. 根据脚本功能选择或创建对应的分类目录
2. 在脚本开头添加文档字符串说明用途
3. 在 `scripts/cli.py` 中添加新的子命令
4. 在 `pyproject.toml` 的 `[project.scripts]` 中更新（如需独立命令）
5. 更新本 README 的脚本列表

### 分类建议

- `download/` - 下载、爬虫相关
- `automation/` - 自动化、批处理任务
- `data/` - 数据处理、转换工具
- `utils/` - 通用工具函数
- `dev/` - 开发辅助工具
- `system/` - 系统管理脚本

## 📋 依赖

- Python >= 3.13
- beautifulsoup4
- httpx[http2]
- requests
- rich
- pyyaml

## 📄 License

MIT
