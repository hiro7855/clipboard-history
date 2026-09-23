# 技术规格说明

## 1. 技术栈

| 层级 | 技术 | 版本要求 |
|------|------|----------|
| 编程语言 | Python | 3.14.3 |
| UI 框架 | PySide6 (Qt6) | 6.11.0 |
| 数据库 | SQLite3 (内置) | — |
| 剪贴板 | PySide6 QClipboard | — |
| 打包 | PyInstaller | 待定 |

## 2. 架构设计

```
┌─────────────────────────────────────────┐
│              Main Application           │
│  ┌───────────┐ ┌──────────┐ ┌────────┐ │
│  │ Clipboard │ │  Tray &  │ │Storage │ │
│  │  Monitor  │ │  Hotkey  │ │(SQLite)│ │
│  │ (QTimer)  │ │ (Qt GUI) │ │        │ │
│  └─────┬─────┘ └────┬─────┘ └───┬────┘ │
│        │            │           │       │
│  ┌─────┴────────────┴───────────┴────┐  │
│  │         Qt Signals / Slots        │  │
│  └────────────────┬──────────────────┘  │
│  ┌────────────────┴──────────────────┐  │
│  │        Main Window (QWidget)      │  │
│  │  ┌──────┐ ┌──────┐ ┌────────┐    │  │
│  │  │Search│ │ Card │ │Settings│    │  │
│  │  │ Bar  │ │ List │ │ Panel  │    │  │
│  │  └──────┘ └──────┘ └────────┘    │  │
│  └───────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

## 3. 数据库设计

### clips 表
```sql
CREATE TABLE clips (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,          -- 'text' | 'image'
  content TEXT NOT NULL,        -- 文字内容 或 base64 编码图片
  timestamp REAL NOT NULL,      -- Unix 时间戳
  pinned INTEGER DEFAULT 0,     -- 0=未置顶, 1=置顶
  source_app TEXT               -- 来源应用进程名
);
CREATE INDEX idx_timestamp ON clips(timestamp DESC);
CREATE INDEX idx_type ON clips(type);
```

## 4. 模块划分

| 文件 | 职责 |
|------|------|
| `main.py` | 应用入口：剪贴板监控、系统托盘、主窗口、存储、设置 |
