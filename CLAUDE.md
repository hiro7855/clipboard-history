# Clipboard History — 项目工作指引

## 项目简介
Windows 11 历史剪贴板管理工具，基于 Python + PySide6 构建。后台监控剪贴板，记录文字和图片，支持搜索、置顶、粘贴。

## 技术栈
- Python 3.14.3
- PySide6 6.11.0 (Qt6 for Python)
- SQLite3 (内置)
- pyperclip (备用)

## 标准文档路径

| 文档 | 路径 | 说明 |
|------|------|------|
| 需求规格 | `./docs/requirements.md` | 功能与非功能需求 |
| 技术规格 | `./docs/tech-specs.md` | 架构、数据库设计 |
| UI 设计规范 | `./docs/design-specs.md` | 色彩、布局、交互规范 |
| 开发步骤 | `./docs/development-guide.md` | 分阶段执行清单 |
| 变更记录 | `./docs/changelog.md` | 版本变更日志 |

## 开发日志
- 每日日志存放：`./dev-logs/YYYY-MM-DD/log.md`
- 每次开发前创建当日文件夹和日志
- 日志记录：完成事项 + 待办事项 + 遇到的问题

## 项目结构
```
agent/
├── main.py          # 应用主文件（监控、UI、托盘、存储）
├── data/            # 运行时数据（自动生成）
│   ├── clips.db     # SQLite 数据库
│   └── settings.json # 用户设置
├── docs/            # 标准文档
├── dev-logs/        # 开发日志
└── assets/          # 图标等静态资源（预留）
```

## 工作约定
1. 所有功能集中在 main.py，保持简单
2. 数据本地存储，不联网
3. 编辑前先阅读文件

## 启动方式
```bash
python main.py
```
