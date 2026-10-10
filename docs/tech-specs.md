# 技术规格说明

> v1.2.0：剪贴板以dataChanged通知触发，序列号校验且有1秒补偿检查；图片content保存原图PNG的base64，thumbnail独立保存200px预览。clips追加thumbnail/width/height/byte_size/image_original/content_hash列，迁移保留既有内容。列表每页60条，只查询缩略图和有限文本，完整内容按需读取。SQLite增量回收与WAL检查点定时执行。设置以临时文件原子替换。应用仍集中main.py，tests/test_iteration.py使用临时数据回归。

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
