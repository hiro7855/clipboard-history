# 📋 Clipboard History — Windows 剪贴板历史管理器

> **100% AI 驱动开发** — 一个不懂代码的小白，通过 [Claude Code](https://claude.ai/code) 从零构建的 Windows 桌面应用。

[![Python](https://img.shields.io/badge/Python-3.14-blue.svg)](https://www.python.org/)
[![PySide6](https://img.shields.io/badge/PySide6-6.11-green.svg)](https://pypi.org/project/PySide6/)
[![Platform](https://img.shields.io/badge/Platform-Windows%2011-lightgrey.svg)]()
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)]()

## 项目简介

Clipboard History 是一款 Windows 11 剪贴板历史管理工具。它会在后台自动记录你所有复制的内容（文字和图片），让你随时搜索、回溯和再次粘贴。

### 为什么特别？

这个项目的**每一行代码**都由 AI（Claude Code）根据自然语言描述生成。项目作者**没有任何编程基础**，全程通过与 AI 对话完成需求描述、技术选型、功能实现、bug 修复和打包发布。

## 功能特性

| 功能 | 说明 |
|------|------|
| 🔍 自动记录 | 后台监控剪贴板，记录所有 Ctrl+C 的文字和图片 |
| 🃏 卡片展示 | 时间降序排列，卡片式预览复制内容 |
| 🔎 实时搜索 | 输入关键词即时过滤历史记录 |
| 📌 置顶功能 | 重要内容置顶，不受过期清理影响 |
| 🗑 删除管理 | 单条删除不需要的记录 |
| ⏱ 保留期限 | 可设置 1天 / 3天 / 5天 自动过期清理 |
| 🔒 隐私保护 | 支持排除指定应用（如密码管理器）的复制记录 |
| ⌨ 全局快捷键 | Alt+V 一键呼出/隐藏窗口 |
| 📷 图片支持 | 支持复制图片的缩略图预览和回贴 |
| 🎨 淡蓝主题 | 简洁直观的淡蓝色 UI 设计 |

## 安装使用

### 方式一：直接运行（推荐）

下载 `dist/ClipboardHistory.exe`，双击运行即可。

### 方式二：源码运行

```bash
# 安装依赖
pip install PySide6

# 运行
python main.py
```

## 使用说明

1. 双击 `ClipboardHistory.exe` 启动（会在系统托盘显示图标）
2. 正常使用 Ctrl+C 复制内容，程序自动记录
3. 按 **Alt+V** 呼出历史窗口
4. 点击任意卡片自动粘贴到当前光标位置
5. 右键系统托盘图标可显示/隐藏或退出

## 开发过程

### AI 协作模式

```
用户（零编程基础）
    │
    │  "我想做一个剪贴板历史软件..."
    │  "它能记录文字和图片..."
    │  "UI用淡蓝色..."
    │
    ▼
Claude Code（AI Agent）
    │
    │  1. 分析需求，提出澄清问题
    │  2. 设计技术方案（Electron → Python/PySide6）
    │  3. 分阶段实现功能
    │  4. 调试修复（Alt+V热键、图片存储等）
    │  5. 打包为 Windows exe
    │
    ▼
ClipboardHistory.exe  ← 交付成果
```

### 关键决策记录

| 时刻 | 决策 | 原因 |
|------|------|------|
| 技术选型 | Electron → **Python + PySide6** | Electron 42/35/28 存在模块解析 bug，Python 更稳定 |
| 数据存储 | SQLite | 轻量嵌入式，无需额外安装 |
| 热键方案 | Windows 原生 `RegisterHotKey` | Qt 无内置全局快捷键，需调用 Win32 API |
| 图片存储 | QBuffer + Base64 | PySide6 不支持 Python BytesIO，需用 Qt 的 QBuffer |

### 踩过的坑

1. **Electron 模块解析失败**（最大坑，耗时 ~1h）
   - `require('electron')` 在 Electron 28/35/42 均无法正确解析 API
   - 最终果断切换 Python + PySide6，所有问题消失

2. **Alt+V 热键不生效**
   - 初版只调用了 `RegisterHotKey` 但未监听 `WM_HOTKEY` 消息
   - 修复：通过 `nativeEvent` 重写直接捕获 Windows 原生消息

3. **图片保存报错**
   - `QPixmap.save()` 不接受 Python `BytesIO`
   - 修复：改用 PySide6 的 `QBuffer`

## 项目结构

```
agent/
├── main.py                     # 完整源码（~870行，单文件）
├── dist/ClipboardHistory.exe   # 打包好的可执行文件
├── docs/                       # 需求、技术、设计文档
├── dev-logs/                   # 开发日志
└── CLAUDE.md                   # AI 协作指引
```

## 技术栈

- **Python 3.14** — 编程语言
- **PySide6 6.11** — Qt6 for Python 桌面 UI
- **SQLite3** — 本地数据存储
- **PyInstaller** — 打包为 Windows exe
- **Win32 API** — 全局热键、模拟粘贴、进程检测

## License

MIT © 2026 — Built with [Claude Code](https://claude.ai/code)
