"""
Clipboard History — Windows 剪贴板历史管理工具
基于 PySide6 + SQLite
"""
import sys
import os
import json
import time
import sqlite3
import base64
import ctypes
from ctypes import wintypes
import re
import hashlib
import logging
import tempfile
from datetime import datetime, timedelta

# Windows MSG structure for nativeEvent
class WinMSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt_x", wintypes.LONG),
        ("pt_y", wintypes.LONG),
    ]

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
    QPushButton, QScrollArea, QFrame, QLabel, QSystemTrayIcon,
    QMenu, QComboBox, QDialog, QListWidget, QListWidgetItem,
    QMessageBox, QSizePolicy, QButtonGroup, QTextEdit, QCheckBox, QSpinBox
)
from PySide6.QtCore import (
    Qt, QTimer, Signal, Slot, QSize, QEvent, QPoint, QObject,
    QBuffer, QByteArray, QIODevice, QRect, QRectF
)
from PySide6.QtNetwork import QLocalSocket, QLocalServer
from PySide6.QtGui import (
    QAction, QIcon, QPixmap, QImage, QPainter, QColor, QFont,
    QPalette, QShortcut, QKeySequence, QCursor, QPainterPath, QFontDatabase,
    QTextLayout, QTextOption, QTextCharFormat
)

# ─── Configuration ───────────────────────────────────────────
APP_NAME = "Clipboard History"
APP_VERSION = "1.2.0"
APP_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
DATA_DIR = os.environ.get("CLIPBOARD_HISTORY_DATA_DIR", os.path.join(APP_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "clips.db")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
DEFAULT_SETTINGS = {
    "retention_days": 3,
    "excluded_apps": [],
    "max_clips": 2000,
    "poll_interval_ms": 500,
    "window_pinned": False,      # 固定在桌面：不自动隐藏，仍可拖动
    "window_geometry": None,     # [x, y, w, h]，记住上次的位置和大小
    "default_action": "preview",
    "paused": False,
    "hotkey": "Alt+V",
    "max_storage_mb": 512,
    "onboarding_seen": False,
}

# ─── Color Theme（Apple 风格：克制的中性灰 + 单一强调蓝）─────────
COLORS = {
    "accent": "#0066CC",
    "accent_hover": "#0077ED",
    "accent_tint": "rgba(0, 102, 204, 0.10)",
    "accent_ring": "rgba(0, 102, 204, 0.45)",
    "bg": "#F5F5F7",
    "card_bg": "#FFFFFF",
    "fill": "rgba(118, 118, 128, 0.12)",        # 系统填充色：搜索框、分段控件
    "fill_strong": "rgba(118, 118, 128, 0.20)",
    "hairline": "rgba(0, 0, 0, 0.08)",
    "hairline_strong": "rgba(0, 0, 0, 0.14)",
    "text": "#1D1D1F",
    "text_secondary": "#6E6E73",
    "text_tertiary": "#86868B",
    "pin": "#E85D04",
    "pin_tint": "rgba(232, 93, 4, 0.12)",
    "delete": "#E30000",
    "delete_tint": "rgba(227, 0, 0, 0.10)",
}

# PingFang 优先（装了就用），否则拉丁字走 Segoe UI、中文走微软雅黑 UI
FONT_FAMILIES = ["PingFang SC", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei"]

# Win11 自带的 Segoe Fluent Icons 线性图标（Win10 回退到 MDL2，码位相同）
ICONS = {
    "pin": "\uE718",
    "pinned": "\uE841",
    "delete": "\uE74D",
    "settings": "\uE713",
    "close": "\uE711",
    "search": "\uE721",
    "image": "\uE91B",
}

_user32 = ctypes.windll.user32
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetClipboardOwner.restype = wintypes.HWND

os.makedirs(DATA_DIR, exist_ok=True)
logging.basicConfig(filename=os.path.join(DATA_DIR, "errors.log"), level=logging.ERROR,
                    format="%(asctime)s %(levelname)s %(message)s")


def encode_image(image):
    """原图以无损PNG保存，缩略图单独生成。"""
    def png_bytes(value):
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        if not value.save(buf, "PNG"):
            raise ValueError("图片编码失败")
        return bytes(buf.data())
    raw = png_bytes(image)
    thumb = png_bytes(image.scaled(200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    return {"type": "image", "content": base64.b64encode(raw).decode("ascii"),
            "thumbnail": base64.b64encode(thumb).decode("ascii"),
            "width": image.width(), "height": image.height(), "byte_size": len(raw),
            "image_original": 1, "content_hash": hashlib.sha256(raw).hexdigest()}


# ─── Database ─────────────────────────────────────────────────
class Database:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        if self.conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            self.conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            self.conn.execute("VACUUM")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS clips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                pinned INTEGER DEFAULT 0,
                source_app TEXT
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON clips(timestamp DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_type ON clips(type)")
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(clips)")}
        for name, kind in (("thumbnail", "TEXT"), ("width", "INTEGER DEFAULT 0"),
                           ("height", "INTEGER DEFAULT 0"), ("byte_size", "INTEGER DEFAULT 0"),
                           ("image_original", "INTEGER DEFAULT 0"), ("content_hash", "TEXT")):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE clips ADD COLUMN {name} {kind}")
        # 老图片只有缩略图，保留数据并明确标记，不假装能够恢复原图。
        for clip_id, clip_type, content in self.conn.execute(
                "SELECT id,type,content FROM clips WHERE content_hash IS NULL").fetchall():
            try:
                raw = content.encode("utf-8") if clip_type == "text" else base64.b64decode(content)
            except (ValueError, TypeError):
                raw = content.encode("utf-8")
            image = decode_image(content) if clip_type == "image" else None
            self.conn.execute("UPDATE clips SET content_hash=?,byte_size=?,thumbnail=?,width=?,height=? WHERE id=?",
                              (hashlib.sha256(raw).hexdigest(), len(raw),
                               content if image is not None else None,
                               image.width() if image is not None else 0,
                               image.height() if image is not None else 0, clip_id))
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON clips(type,content_hash)")
        self.conn.commit()

    def insert(self, clip_type, content, source_app="", **metadata):
        digest = metadata.get("content_hash") or hashlib.sha256(
            content.encode("utf-8") if clip_type == "text" else base64.b64decode(content)).hexdigest()
        existing = self.conn.execute("SELECT id FROM clips WHERE type=? AND content_hash=? ORDER BY pinned DESC,timestamp DESC LIMIT 1",
                                     (clip_type, digest)).fetchone()
        if existing:
            self.conn.execute("UPDATE clips SET timestamp=?,source_app=? WHERE id=?",
                              (time.time(), source_app, existing[0]))
            if clip_type == "image" and metadata.get("image_original"):
                self.conn.execute("UPDATE clips SET content=?,thumbnail=?,width=?,height=?,byte_size=?,image_original=1 WHERE id=?",
                                  (content, metadata.get("thumbnail"), metadata.get("width", 0), metadata.get("height", 0), metadata.get("byte_size", 0), existing[0]))
            self.conn.commit()
            return existing[0]
        self.conn.execute("INSERT INTO clips (type,content,timestamp,source_app,thumbnail,width,height,byte_size,image_original,content_hash) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (clip_type, content, time.time(), source_app, metadata.get("thumbnail"),
                           metadata.get("width", 0), metadata.get("height", 0),
                           metadata.get("byte_size", len(content.encode("utf-8"))),
                           metadata.get("image_original", 0), digest))
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def _where(self, search="", kind="all", days=0):
        conditions, params = [], []
        if search:
            like = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            conditions.append("((type='text' AND content LIKE ? ESCAPE '\\') OR COALESCE(source_app,'') LIKE ? ESCAPE '\\')")
            params.extend((like, like))
        if kind in ("text", "image"):
            conditions.append("type=?")
            params.append(kind)
        elif kind == "favorites":
            conditions.append("pinned=1")
        if days:
            conditions.append("timestamp>=?")
            params.append(time.time() - days * 86400)
        return (" WHERE " + " AND ".join(conditions) if conditions else ""), params

    def get_all(self, search="", kind="all", days=0, limit=60, offset=0):
        where, params = self._where(search, kind, days)
        return self.conn.execute(
            "SELECT id,type,CASE WHEN type='image' THEN COALESCE(thumbnail,content) ELSE substr(content,1,1000) END,"
            "timestamp,pinned,source_app,width,height,byte_size,image_original FROM clips" + where +
            " ORDER BY pinned DESC,timestamp DESC,id DESC LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()

    def get_clip(self, clip_id):
        return self.conn.execute("SELECT id,type,content,timestamp,pinned,source_app,width,height,byte_size,image_original "
                                 "FROM clips WHERE id=?", (clip_id,)).fetchone()

    def matched_count(self, search="", kind="all", days=0):
        where, params = self._where(search, kind, days)
        return self.conn.execute("SELECT COUNT(*) FROM clips" + where, params).fetchone()[0]

    def expired_count(self, retention_days):
        return self.conn.execute("SELECT COUNT(*) FROM clips WHERE pinned=0 AND timestamp<?",
                                 (time.time() - retention_days * 86400,)).fetchone()[0]

    def storage_bytes(self):
        return self.conn.execute("SELECT COALESCE(SUM(length(CAST(content AS BLOB))+COALESCE(length(thumbnail),0)),0) FROM clips").fetchone()[0]

    def trim_storage(self, max_mb):
        removed = 0
        total = self.storage_bytes()
        for clip_id, size in self.conn.execute("SELECT id,length(CAST(content AS BLOB))+COALESCE(length(thumbnail),0) FROM clips WHERE pinned=0 ORDER BY timestamp,id").fetchall():
            if total <= max_mb * 1024 * 1024:
                break
            self.conn.execute("DELETE FROM clips WHERE id=?", (clip_id,))
            total -= size
            removed += 1
        self.conn.commit()
        return removed

    def delete(self, clip_id):
        row = self.conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        self.conn.execute("DELETE FROM clips WHERE id=?", (clip_id,))
        self.conn.commit()
        return row

    def restore(self, row):
        self.conn.execute("INSERT OR REPLACE INTO clips VALUES (" + ",".join("?" for _ in row) + ")", row)
        self.conn.commit()

    def clear(self, include_favorites=False):
        self.conn.execute("DELETE FROM clips" + ("" if include_favorites else " WHERE pinned=0"))
        self.conn.commit()
        self.conn.execute("PRAGMA incremental_vacuum(2048)")
        self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def toggle_pin(self, clip_id):
        row = self.conn.execute("SELECT pinned FROM clips WHERE id=?", (clip_id,)).fetchone()
        if row:
            new_val = 0 if row[0] else 1
            self.conn.execute("UPDATE clips SET pinned=? WHERE id=?", (new_val, clip_id))
            self.conn.commit()
            return new_val
        return 0

    def clean_expired(self, retention_days):
        cutoff = time.time() - (retention_days * 86400)
        self.conn.execute("DELETE FROM clips WHERE pinned=0 AND timestamp < ?", (cutoff,))
        self.conn.commit()

    def trim_to(self, max_clips):
        """删除最旧的非置顶记录，使总数不超过 max_clips。"""
        if not max_clips or max_clips <= 0:
            return
        count = self.get_count()
        if count <= max_clips:
            return
        excess = count - max_clips
        self.conn.execute(
            "DELETE FROM clips WHERE id IN ("
            "SELECT id FROM clips WHERE pinned=0 ORDER BY timestamp ASC LIMIT ?"
            ")", (excess,)
        )
        self.conn.commit()

    def get_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0]


# ─── Settings ─────────────────────────────────────────────────
class Settings:
    def __init__(self):
        self.data = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            self.save()

    def save(self):
        fd, path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(path, SETTINGS_PATH)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def update(self, values):
        before = dict(self.data)
        self.data.update(values)
        try:
            self.save()
        except OSError:
            self.data = before
            raise

    def get(self, key, default=None):
        if default is None:
            return self.data.get(key, DEFAULT_SETTINGS.get(key))
        return self.data.get(key, default)

    def set(self, key, value):
        self.update({key: value})


# ─── Image decode helper ──────────────────────────────────────
def decode_image(base64_str):
    """把 base64 图片内容解码成 QImage。失败返回 null QImage。"""
    try:
        image = QImage.fromData(base64.b64decode(base64_str), "PNG")
        return image
    except Exception:
        return QImage()


# 图片缩略图内存缓存：clip_id -> QPixmap(44x44 圆角)。
# 避免每次 rebuild 卡片列表都重复做 base64 解码 + PNG 解压 + 缩放。
_THUMB_CACHE = {}
THUMB_SIZE = 44


def _screen_dpr():
    return QApplication.primaryScreen().devicePixelRatio()


def get_thumb_pixmap(clip_id, base64_str):
    cached = _THUMB_CACHE.get(clip_id)
    if cached is not None:
        return cached
    image = decode_image(base64_str)
    if image.isNull():
        return None
    # 按屏幕倍率渲染、居中裁成正方形再切圆角，高分屏下不糊
    dpr = _screen_dpr()
    side = int(THUMB_SIZE * dpr)
    scaled = QPixmap.fromImage(image).scaled(
        side, side, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    thumb = QPixmap(side, side)
    thumb.fill(Qt.transparent)
    painter = QPainter(thumb)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, side, side), 8 * dpr, 8 * dpr)
    painter.setClipPath(path)
    painter.drawPixmap((side - scaled.width()) // 2, (side - scaled.height()) // 2, scaled)
    painter.end()
    thumb.setDevicePixelRatio(dpr)
    if len(_THUMB_CACHE) < 2200:
        _THUMB_CACHE[clip_id] = thumb
    return thumb


_ICON_FONT = None


def icon_font_family():
    global _ICON_FONT
    if _ICON_FONT is None:
        families = QFontDatabase.families()
        _ICON_FONT = next(
            (f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families),
            "Segoe UI Symbol")
    return _ICON_FONT


def glyph_pixmap(glyph, color, size):
    """把图标字体里的一个字形画成透明底 QPixmap（给 QLineEdit 等只收 QIcon 的地方用）。"""
    dpr = _screen_dpr()
    pixmap = QPixmap(int(size * dpr), int(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.TextAntialiasing)
    font = QFont(icon_font_family())
    font.setPixelSize(int(size * 0.85))
    painter.setFont(font)
    painter.setPen(QColor(color))
    painter.drawText(QRect(0, 0, size, size), Qt.AlignCenter, ICONS[glyph])
    painter.end()
    return pixmap


class IconButton(QPushButton):
    """图标字体按钮（替代 emoji）。样式由主窗口样式表按 objectName 统一下发，
    卡片多时不用每个按钮各解析一遍样式表。"""

    def __init__(self, glyph, name, tooltip="", size=28):
        super().__init__(ICONS[glyph])
        self.setObjectName(name)
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip or name)

    def set_state(self, glyph, active):
        self.setText(ICONS[glyph])
        self.setProperty("active", bool(active))
        # 动态属性变了要重新 polish，[active="true"] 选择器才会生效
        self.style().unpolish(self)
        self.style().polish(self)


class ElidedLabel(QLabel):
    """最多显示 max_lines 行，放不下时最后一行末尾加省略号；窗口缩放时按新宽度重排。
    中文无空格也能按字折行（WrapAtWordBoundaryOrAnywhere），不会把右侧按钮挤出去。"""

    def __init__(self, text, max_lines=2):
        super().__init__()
        self._full_text = text
        self._max_lines = max_lines
        self._lines = []
        self.search_term = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def _relayout(self):
        width = max(1, self.width())
        layout = QTextLayout(self._full_text, self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        if self.search_term:
            fmt = QTextCharFormat()
            fmt.setBackground(QColor("#FFF0AD"))
            ranges = []
            for match in re.finditer(re.escape(self.search_term), self._full_text, re.IGNORECASE):
                item = QTextLayout.FormatRange()
                item.start, item.length, item.format = match.start(), len(match.group()), fmt
                ranges.append(item)
            layout.setFormats(ranges)
        layout.beginLayout()
        spans = []
        while len(spans) < self._max_lines:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            spans.append((line.textStart(), line.textLength()))
        layout.endLayout()

        metrics = self.fontMetrics()
        self._lines = []
        for i, (start, length) in enumerate(spans):
            if i == len(spans) - 1 and start + length < len(self._full_text):
                text = metrics.elidedText(self._full_text[start:], Qt.ElideRight, width)
            else:
                text = self._full_text[start:start + length]
            self._lines.append(text)
        # 只占实际行数的高度，一行的短文本和时间行能挨在一起
        self.setFixedHeight(max(1, len(self._lines)) * metrics.lineSpacing())

    def resizeEvent(self, event):
        self._relayout()
        super().resizeEvent(event)

    def changeEvent(self, event):
        if event.type() == QEvent.FontChange:
            self._relayout()
        super().changeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        metrics = self.fontMetrics()
        y = metrics.ascent()
        for text in self._lines:
            if self.search_term:
                for match in re.finditer(re.escape(self.search_term), text, re.IGNORECASE):
                    x = metrics.horizontalAdvance(text[:match.start()])
                    width = metrics.horizontalAdvance(match.group())
                    painter.fillRect(QRect(x, y - metrics.ascent(), width, metrics.height()), QColor("#FFF0AD"))
            painter.drawText(0, y, text)
            y += metrics.lineSpacing()
        painter.end()


def create_app_icon():
    """加载应用图标(assets/icon.png)；缺失时回退到 emoji 绘制。"""
    icon_path = os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "assets", "icon.ico")
    if os.path.exists(icon_path):
        return QIcon(icon_path)
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor(COLORS["accent"]))
    painter = QPainter(pixmap)
    painter.setPen(QColor("#FFFFFF"))
    painter.setFont(QFont("Microsoft YaHei", 16))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "📋")
    painter.end()
    return QIcon(pixmap)


# ─── Clipboard Card Widget ────────────────────────────────────
class ClipCard(QFrame):
    clicked = Signal(int)
    delete_requested = Signal(int)
    pin_requested = Signal(int)
    selected = Signal(int)

    def __init__(self, clip_data):
        super().__init__()
        self.clip_id = clip_data[0]
        self.clip_type = clip_data[1]
        self.content = clip_data[2]
        self.timestamp = clip_data[3]
        self.pinned = bool(clip_data[4])
        self.source_app = clip_data[5] if len(clip_data) > 5 else ""
        self.image_width = clip_data[6] if len(clip_data) > 6 else 0
        self.image_height = clip_data[7] if len(clip_data) > 7 else 0
        self.image_size = clip_data[8] if len(clip_data) > 8 else 0
        self.image_original = bool(clip_data[9]) if len(clip_data) > 9 else False

        self.setObjectName("clipCard")
        self.setCursor(Qt.PointingHandCursor)
        self._setup_ui()

    def _setup_ui(self):
        # 样式全部写在主窗口样式表里（QFrame#clipCard / QLabel#clipText ...）
        self.setFixedHeight(76)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 8, 10)
        layout.setSpacing(12)

        # Content area
        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(3)
        content_layout.addStretch()

        if self.clip_type == "text":
            # 换行、缩进等连续空白压成一个空格，预览更紧凑
            raw = re.sub(r"\s+", " ", self.content).strip()
            self.text_label = ElidedLabel(raw[:400], max_lines=2)
            self.text_label.setObjectName("clipText")
            content_layout.addWidget(self.text_label)
        else:
            # Image thumbnail（走内存缓存，避免重复解码），缩略图放左侧
            thumb = get_thumb_pixmap(self.clip_id, self.content)
            if thumb is not None:
                self.thumb_label = QLabel()
                self.thumb_label.setObjectName("clipThumb")
                self.thumb_label.setPixmap(thumb)
                self.thumb_label.setFixedSize(THUMB_SIZE, THUMB_SIZE)
                layout.addWidget(self.thumb_label)
            self.text_label = QLabel(f"图片 {self.image_width}×{self.image_height}" +
                                    ("" if self.image_original else " · 历史缩略图"))
            self.text_label.setObjectName("clipText")
            content_layout.addWidget(self.text_label)

        # Timestamp · 来源应用
        ts = datetime.fromtimestamp(self.timestamp)
        meta = self._format_time(ts)
        app = self._format_app(self.source_app)
        if app:
            meta = f"{meta}  ·  {app}"
        self.time_label = QLabel(meta)
        self.time_label.setObjectName("clipMeta")
        content_layout.addWidget(self.time_label)
        content_layout.addStretch()

        layout.addWidget(content_widget, 1)

        # Action buttons：平时隐藏，悬停才出现；已置顶的卡片常显橙色图钉
        self.pin_btn = IconButton("pin", "cardPin", "收藏 / 取消收藏（收藏不会自动过期）", size=26)
        self.pin_btn.clicked.connect(lambda e: self.pin_requested.emit(self.clip_id))

        self.del_btn = IconButton("delete", "cardDelete", "删除", size=26)
        self.del_btn.clicked.connect(lambda e: self.delete_requested.emit(self.clip_id))

        for btn in (self.pin_btn, self.del_btn):
            policy = btn.sizePolicy()
            policy.setRetainSizeWhenHidden(True)   # 显隐时不让文字区跟着跳
            btn.setSizePolicy(policy)
            layout.addWidget(btn, 0, Qt.AlignVCenter)

        self.update_pinned(self.pinned)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # 点在置顶/删除按钮上时不触发粘贴
            child = self.childAt(event.position().toPoint())
            if not isinstance(child, QPushButton):
                self.selected.emit(self.clip_id)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            if not isinstance(self.childAt(event.position().toPoint()), QPushButton):
                self.clicked.emit(self.clip_id)
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        window = self.window()
        menu = QMenu(self)
        for label, action in (("完整预览", lambda: window._preview_clip(self.clip_id)),
                              ("复制", lambda: window._use_clip(self.clip_id, "copy")),
                              ("粘贴到目标窗口", lambda: window._use_clip(self.clip_id, "paste")),
                              ("取消收藏" if self.pinned else "收藏（不会自动过期）", lambda: self.pin_requested.emit(self.clip_id)),
                              ("删除", lambda: self.delete_requested.emit(self.clip_id))):
            menu.addAction(label, action)
        menu.exec(event.globalPos())

    def enterEvent(self, event):
        self.pin_btn.setVisible(True)
        self.del_btn.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.pin_btn.setVisible(self.pinned)
        self.del_btn.setVisible(False)
        super().leaveEvent(event)

    def update_pinned(self, pinned):
        """原地更新置顶状态（不重建卡片，避免卡顿）。"""
        self.pinned = bool(pinned)
        self.pin_btn.set_state("pinned" if self.pinned else "pin", self.pinned)
        hovered = self.underMouse()
        self.pin_btn.setVisible(self.pinned or hovered)
        self.del_btn.setVisible(hovered)

    @staticmethod
    def _format_app(name):
        if not name:
            return ""
        if name.lower().endswith(".exe"):
            name = name[:-4]
        return name[:1].upper() + name[1:]

    def _format_time(self, ts):
        now = datetime.now()
        diff = now - ts
        if diff < timedelta(minutes=1):
            return "刚刚"
        elif diff < timedelta(hours=1):
            return f"{int(diff.total_seconds() / 60)}分钟前"
        elif diff < timedelta(days=1):
            return ts.strftime("%H:%M")
        elif diff < timedelta(days=7):
            return f"{diff.days}天前"
        else:
            return ts.strftime("%m/%d %H:%M")


# ─── Settings Dialog ──────────────────────────────────────────
class PreviewDialog(QDialog):
    def __init__(self, clip, window):
        super().__init__(window)
        self.setWindowTitle("完整预览")
        self.resize(660, 580)
        self.setMinimumSize(420, 360)
        layout = QVBoxLayout(self)
        meta = f"{datetime.fromtimestamp(clip[3]):%Y-%m-%d %H:%M} · {ClipCard._format_app(clip[5])}"
        if clip[1] == "image":
            meta += f" · {clip[6]}×{clip[7]} · {clip[8] / 1024:.0f} KB"
            if not clip[9]:
                meta += " · 历史缩略图，原始像素已无法恢复"
        label = QLabel(meta)
        label.setWordWrap(True)
        layout.addWidget(label)
        if clip[1] == "text":
            editor = QTextEdit()
            editor.setReadOnly(True)
            editor.setPlainText(clip[2])
            layout.addWidget(editor, 1)
        else:
            self.image = QPixmap.fromImage(decode_image(clip[2]))
            self.zoom = QComboBox()
            for name, value in (("适应窗口", 0), ("原始大小 100%", 1), ("放大 200%", 2)):
                self.zoom.addItem(name, value)
            layout.addWidget(self.zoom)
            self.scroll = QScrollArea()
            self.image_label = QLabel()
            self.image_label.setAlignment(Qt.AlignCenter)
            self.scroll.setWidget(self.image_label)
            layout.addWidget(self.scroll, 1)
            self.zoom.currentIndexChanged.connect(self._render_image)
            QTimer.singleShot(0, self._render_image)
        buttons = QHBoxLayout()
        for text, action in (("复制", "copy"), ("粘贴到目标窗口", "paste")):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, a=action: self._use(window, clip[0], a))
            buttons.addWidget(button)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def _use(self, window, clip_id, action):
        self.accept()
        QTimer.singleShot(0, lambda: window._use_clip(clip_id, action))

    def _render_image(self):
        scale = self.zoom.currentData()
        pix = self.image
        if not scale:
            size = self.scroll.viewport().size() - QSize(16, 16)
            pix = pix.scaled(QSize(max(1, size.width()), max(1, size.height())), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        elif scale != 1:
            pix = pix.scaled(self.image.size() * scale, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setPixmap(pix)
        self.image_label.resize(pix.size())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "zoom"):
            self._render_image()


def running_apps():
    names = set()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    @callback_type
    def collect(hwnd, parameter):
        if _user32.IsWindowVisible(hwnd):
            name = process_name(hwnd)
            if name and name.lower() not in ("python.exe", "pythonw.exe", "clipboardhistory.exe"):
                names.add(name.lower())
        return True
    _user32.EnumWindows(collect, 0)
    return sorted(names)


def process_name(hwnd):
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buf))
        return os.path.basename(buf.value) if kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(length)) else ""
    finally:
        kernel.CloseHandle(handle)


def startup_path():
    return os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "ClipboardHistory.vbs")


def set_startup(enabled):
    path = startup_path()
    if not enabled:
        if os.path.isfile(path):
            os.remove(path)
        return
    command = f'"{sys.executable}"' if getattr(sys, "frozen", False) else f'"{os.path.join(os.path.dirname(sys.executable), "pythonw.exe")}" "{os.path.abspath(__file__)}"'
    script = 'Set shell = CreateObject("WScript.Shell")\n' + 'shell.Run "' + command.replace('"', '""') + '", 0, False\n'
    with open(path, "w", encoding="utf-16") as file:
        file.write(script)


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(f"设置 · v{APP_VERSION}")
        self.resize(470, 700)
        self.setMinimumSize(430, 480)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        self.layout = QVBoxLayout(body)
        self.layout.setSpacing(10)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        def label(text):
            item = QLabel(text)
            item.setWordWrap(True)
            self.layout.addWidget(item)
        label("保留期限（仅影响未收藏记录）")
        self.retention_combo = QComboBox()
        for name, days in (("1 天", 1), ("3 天", 3), ("5 天", 5), ("7 天", 7), ("30 天", 30)):
            self.retention_combo.addItem(name, days)
        self.retention_combo.setCurrentIndex(max(0, self.retention_combo.findData(settings.get("retention_days"))))
        self.layout.addWidget(self.retention_combo)
        label("点击记录时")
        self.action_combo = QComboBox()
        for name, value in (("完整预览（推荐）", "preview"), ("只复制", "copy"), ("直接粘贴", "paste")):
            self.action_combo.addItem(name, value)
        self.action_combo.setCurrentIndex(max(0, self.action_combo.findData(settings.get("default_action", "preview"))))
        self.layout.addWidget(self.action_combo)
        label("呼出快捷键（Alt / Ctrl / Shift + 字母或数字）")
        self.hotkey_input = QLineEdit(settings.get("hotkey", "Alt+V"))
        self.layout.addWidget(self.hotkey_input)
        self.startup = QCheckBox("开机自启")
        self.startup.setChecked(os.path.isfile(startup_path()))
        self.layout.addWidget(self.startup)
        label(f"历史内容占用：{parent.db.storage_bytes() / 1024 / 1024:.1f} MB；上限（MB）")
        self.capacity = QSpinBox()
        self.capacity.setRange(64, 4096)
        self.capacity.setValue(settings.get("max_storage_mb", 512))
        self.layout.addWidget(self.capacity)
        label("达到容量或条数上限后清理最旧未收藏记录；收藏超限时暂停新记录并提示。")
        label("排除应用：从运行中的应用选择，也可输入完整进程名。浏览器排除会影响整个浏览器。")
        self.running_combo = QComboBox()
        self.running_combo.addItem("选择正在运行的应用…", "")
        for name in running_apps():
            self.running_combo.addItem(ClipCard._format_app(name), name)
        self.running_combo.currentIndexChanged.connect(lambda: self.app_input.setText(self.running_combo.currentData() or ""))
        self.layout.addWidget(self.running_combo)
        row = QHBoxLayout()
        self.app_input = QLineEdit()
        self.app_input.setPlaceholderText("例如 notepad.exe")
        row.addWidget(self.app_input)
        add = QPushButton("添加")
        add.clicked.connect(self._add_app)
        self.app_input.returnPressed.connect(self._add_app)
        row.addWidget(add)
        self.layout.addLayout(row)
        self.app_list = QListWidget()
        self.app_list.setMaximumHeight(110)
        self.app_list.addItems(settings.get("excluded_apps", []))
        self.layout.addWidget(self.app_list)
        remove = QPushButton("移除选中应用")
        remove.clicked.connect(self._remove_app)
        self.layout.addWidget(remove)
        label("历史仅存储于本机，未加密。暂停记录可用于临时处理敏感内容。")
        self.clear_favorites = QCheckBox("清空时包含收藏")
        self.layout.addWidget(self.clear_favorites)
        clear = QPushButton("清空历史…")
        clear.clicked.connect(self._clear)
        self.layout.addWidget(clear)
        label("用法：Alt+V 呼出；上下键选择；Enter 粘贴；Ctrl+Shift+C 复制；Space 预览；Esc 隐藏。右键记录可选择操作。更新时退出应用并替换程序文件，保留 data 文件夹。")
        actions = QHBoxLayout()
        save = QPushButton("保存设置")
        save.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        actions.addWidget(save)
        actions.addWidget(cancel)
        outer.addLayout(actions)

    def _add_app(self):
        name = self.app_input.text().strip().lower()
        if not re.fullmatch(r"[^\\/:*?\"<>|]+\.exe", name):
            QMessageBox.information(self, "排除应用", "请输入完整进程名，例如 notepad.exe。")
            return
        if not self.app_list.findItems(name, Qt.MatchExactly):
            self.app_list.addItem(name)
        self.app_input.clear()

    def _remove_app(self):
        for item in self.app_list.selectedItems():
            self.app_list.takeItem(self.app_list.row(item))

    def _clear(self):
        include = self.clear_favorites.isChecked()
        message = "删除所有历史，包括收藏？此操作无法撤销。" if include else "删除所有未收藏历史？收藏会保留，此操作无法撤销。"
        if QMessageBox.question(self, "清空历史", message, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self.parent().db.clear(include)
            _THUMB_CACHE.clear()
            self.parent()._undo_row = None
            self.parent().load_clips(self.parent().search_input.text())
            self.parent()._status("历史已清空" + ("" if include else "，收藏已保留"))

    def _save(self):
        window = self.parent()
        days = self.retention_combo.currentData()
        hotkey = self.hotkey_input.text().strip()
        try:
            window._parse_hotkey(hotkey)
        except ValueError as error:
            QMessageBox.information(self, "快捷键", str(error))
            return
        count = window.db.expired_count(days)
        if days < self.settings.get("retention_days") and count:
            if QMessageBox.question(self, "缩短保留期限", f"改为 {days} 天将删除 {count} 条未收藏历史。是否继续？",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        capacity = self.capacity.value()
        if capacity < self.settings.get("max_storage_mb", 512) and window.db.storage_bytes() > capacity * 1024 * 1024:
            if QMessageBox.question(self, "缩小容量", "将清理最旧未收藏历史至容量上限，是否继续？",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        previous_hotkey = self.settings.get("hotkey")
        previous_startup = os.path.isfile(startup_path())
        if hotkey != previous_hotkey and not window._register_hotkey(hotkey):
            QMessageBox.information(self, "快捷键被占用", "无法注册该快捷键，原快捷键仍保留，请换一个组合。")
            return
        try:
            set_startup(self.startup.isChecked())
            self.settings.update({"retention_days": days, "default_action": self.action_combo.currentData(),
                                  "hotkey": hotkey, "max_storage_mb": capacity,
                                  "excluded_apps": [self.app_list.item(i).text() for i in range(self.app_list.count())]})
            window.db.clean_expired(days)
            window.db.trim_storage(capacity)
        except OSError:
            if hotkey != previous_hotkey:
                window._register_hotkey(previous_hotkey)
            try:
                set_startup(previous_startup)
            except OSError:
                pass
            QMessageBox.warning(self, "保存失败", "设置未能完整保存，请检查数据目录是否可写。")
            return
        self.accept()


# ─── Main Window ──────────────────────────────────────────────
class MainWindow(QWidget):
    WM_HOTKEY = 0x0312
    HOTKEY_ID = 1
    SHADOW = 10                  # 外圈阴影宽度，同时也是边缘拖拽缩放的热区
    RADIUS = 14
    CORNER_GRIP = 20             # 四角热区放宽一点，斜向缩放更好抓
    DEFAULT_SIZE = (400, 560)    # 窗体本身（不含阴影）的默认大小
    MIN_SIZE = (320, 380)

    def __init__(self, db, settings):
        super().__init__()
        self.db = db
        self.settings = settings
        self.clips = []
        self.pinned = bool(settings.get("window_pinned", False))
        self._geometry_restored = False
        self._last_external_hwnd = None
        self._last_external_pid = None
        self._selected_id = None
        self._undo_row = None
        self._loading = False
        self._pending_paste = None
        self._hotkey_id = self.HOTKEY_ID

        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        # 必须在原生窗口创建（winId）之前设置，圆角和阴影才是透明的
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        s = self.SHADOW * 2
        self.setMinimumSize(self.MIN_SIZE[0] + s, self.MIN_SIZE[1] + s)
        self.resize(self.DEFAULT_SIZE[0] + s, self.DEFAULT_SIZE[1] + s)

        # 拖动/缩放结束后再落盘，避免拖动过程中狂写 settings.json
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(400)
        self._geometry_timer.timeout.connect(self._save_geometry)

        # 固定模式下点卡片要把内容贴回“之前那个窗口”，所以持续记住它
        self._foreground_timer = QTimer(self)
        self._foreground_timer.setInterval(250)
        self._foreground_timer.timeout.connect(self._track_foreground)

        self._setup_ui()
        self._apply_theme()
        self._update_pin_button()
        self.load_clips()

        # Shortcut: Escape to hide
        QShortcut(QKeySequence(Qt.Key_Escape), self, self.hide)
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self.search_input.setFocus())
        self.search_input.installEventFilter(self)
        for sequence, callback in (("Down", lambda: self._select_delta(1)),
                                   ("Up", lambda: self._select_delta(-1)),
                                   ("Return", lambda: self._use_selected("paste")),
                                   ("Ctrl+Shift+C", lambda: self._use_selected("copy")),
                                   ("Space", lambda: self._use_selected("preview"))):
            shortcut = QShortcut(QKeySequence(sequence), self, callback)
            if sequence == "Space":
                shortcut.setEnabled(False)
                self._preview_shortcut = shortcut

        # Register Alt+V global hotkey on this window
        self._register_hotkey()
        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.setInterval(60000)
        self._cleanup_timer.timeout.connect(self._periodic_cleanup)
        self._cleanup_timer.start()
        self._undo_timer = QTimer(self)
        self._undo_timer.setSingleShot(True)
        self._undo_timer.setInterval(10000)
        self._undo_timer.timeout.connect(self._expire_undo)
        self._periodic_cleanup()

    @staticmethod
    def _parse_hotkey(text):
        parts = [part.strip().upper() for part in text.split("+")]
        modifiers = {"ALT": 1, "CTRL": 2, "CONTROL": 2, "SHIFT": 4}
        if len(parts) < 2 or len(parts[-1]) != 1 or not parts[-1].isascii() or not parts[-1].isalnum():
            raise ValueError("请输入 Alt/Ctrl/Shift 与一个字母或数字的组合，例如 Alt+V。")
        flags = 0x4000  # MOD_NOREPEAT
        for part in parts[:-1]:
            if part not in modifiers:
                raise ValueError("仅支持 Alt、Ctrl、Shift 与字母或数字组合。")
            flags |= modifiers[part]
        return flags, ord(parts[-1])

    def _register_hotkey(self, text=None):
        text = text or self.settings.get("hotkey", "Alt+V")
        try:
            mods, key = self._parse_hotkey(text)
            new_id = 2 if self._hotkey_id == 1 else 1
            hwnd = int(self.winId())
            if not _user32.RegisterHotKey(hwnd, new_id, mods, key):
                self._status(f"快捷键 {text} 被占用，请到设置修改")
                return False
            _user32.UnregisterHotKey(hwnd, self._hotkey_id)
            self._hotkey_id = new_id
            return True
        except ValueError as error:
            self._status(str(error))
            return False

    def nativeEvent(self, eventType, message):
        msg = WinMSG.from_address(int(message))
        if msg.message == self.WM_HOTKEY and msg.wParam == self._hotkey_id:
            self._toggle_show()
            return True, 0
        return False, 0

    def _toggle_show(self):
        if self.isVisible():
            self.hide()
        else:
            # 呼出前先记下当前前台窗口，固定模式粘贴时要还给它
            self._track_foreground()
            if not self._geometry_restored:
                self._restore_geometry()
                self._geometry_restored = True
            self.show()
            self.activateWindow()
            self.raise_()
            self.search_input.setFocus()
            self.search_input.clear()
            self.load_clips("", reset=True)

    # ── 位置 / 大小记忆 ──
    def _restore_geometry(self):
        geo = self.settings.get("window_geometry")
        if geo and len(geo) == 4:
            rect = QRect(*geo)
            # 上次所在的显示器可能已拔掉，中心点不在任何屏幕上就回退默认位置
            if QApplication.screenAt(rect.center()) is not None:
                self.setGeometry(rect)
                return
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        avail = screen.availableGeometry()
        s = self.SHADOW * 2
        w, h = self.DEFAULT_SIZE[0] + s, self.DEFAULT_SIZE[1] + s
        self.setGeometry(avail.right() - w - 10, avail.top() + 10, w, h)

    def _save_geometry(self):
        g = self.geometry()
        self.settings.set("window_geometry", [g.x(), g.y(), g.width(), g.height()])

    def moveEvent(self, event):
        if self._geometry_restored:
            self._geometry_timer.start()
        super().moveEvent(event)

    def resizeEvent(self, event):
        if self._geometry_restored:
            self._geometry_timer.start()
        super().resizeEvent(event)

    # ── 固定在桌面（类似微信置顶）──
    def _toggle_pin(self):
        self.pinned = not self.pinned
        self.settings.set("window_pinned", self.pinned)
        self._update_pin_button()

    def _update_pin_button(self):
        self.pin_btn.set_state("pinned" if self.pinned else "pin", self.pinned)
        if self.pinned:
            self.pin_btn.setToolTip("保持显示：置顶且不自动隐藏，可拖动标题栏\n点击恢复自动隐藏")
        else:
            self.pin_btn.setToolTip("保持显示（置顶且不自动隐藏）")
        self.title_bar.setCursor(Qt.SizeAllCursor)

    def _track_foreground(self):
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != os.getpid():
            self._last_external_hwnd = hwnd
            self._last_external_pid = pid.value

    def showEvent(self, event):
        self._foreground_timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._foreground_timer.stop()
        super().hideEvent(event)

    def changeEvent(self, event):
        # 未固定时：窗口失去激活（点了别处）就自动隐藏；
        # 有模态对话框（如设置）打开时不隐藏
        if (event.type() == QEvent.ActivationChange and not self.isActiveWindow()
                and not self.pinned and self.isVisible()
                and QApplication.activeModalWidget() is None):
            self.hide()
        super().changeEvent(event)

    # ── 无边框窗口的拖动与缩放 ──
    def eventFilter(self, obj, event):
        if obj is getattr(self, "search_input", None) and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Down, Qt.Key_Up):
                self._select_delta(1 if event.key() == Qt.Key_Down else -1)
                self.body.setFocus()
                return True
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self._use_selected("paste")
                return True
        if obj is getattr(self, "search_input", None) and event.type() in (QEvent.FocusIn, QEvent.FocusOut):
            if hasattr(self, "_preview_shortcut"):
                self._preview_shortcut.setEnabled(event.type() == QEvent.FocusOut)
        # 按住标题栏拖动窗口，固定状态也允许移动
        if (obj is self.title_bar and event.type() == QEvent.MouseButtonPress
                and event.button() == Qt.LeftButton):
            self.windowHandle().startSystemMove()
            return True
        return super().eventFilter(obj, event)

    def _edges_at(self, pos):
        """鼠标落在外圈阴影区时，返回要拖拽的边/角。"""
        w, h = self.width(), self.height()
        x, y = pos.x(), pos.y()
        g, c = self.SHADOW, self.CORNER_GRIP
        left, right, top, bottom = x <= g, x >= w - g, y <= g, y >= h - g
        if left or right:
            top, bottom = top or y <= c, bottom or y >= h - c
        if top or bottom:
            left, right = left or x <= c, right or x >= w - c
        edges = Qt.Edge(0)
        if left:
            edges |= Qt.LeftEdge
        if right:
            edges |= Qt.RightEdge
        if top:
            edges |= Qt.TopEdge
        if bottom:
            edges |= Qt.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges):
        horizontal = bool(edges & (Qt.LeftEdge | Qt.RightEdge))
        vertical = bool(edges & (Qt.TopEdge | Qt.BottomEdge))
        if horizontal and vertical:
            main_diagonal = bool(edges & Qt.LeftEdge) == bool(edges & Qt.TopEdge)
            return Qt.SizeFDiagCursor if main_diagonal else Qt.SizeBDiagCursor
        if horizontal:
            return Qt.SizeHorCursor
        if vertical:
            return Qt.SizeVerCursor
        return Qt.ArrowCursor

    def mouseMoveEvent(self, event):
        if not event.buttons():
            self.setCursor(self._cursor_for(self._edges_at(event.position().toPoint())))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            edges = self._edges_at(event.position().toPoint())
            if edges:
                self.windowHandle().startSystemResize(edges)
                return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # 完全透明的像素在 Windows 分层窗口上会被鼠标穿透，
        # 先铺一层肉眼不可见（alpha=1）的底，外圈缩放热区才接得到鼠标
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))
        # 柔和阴影：一圈圈半透明圆角矩形叠加，越靠近窗体越深，整体略往下偏 1px
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 3))
        rect = QRectF(self.rect())
        for i in range(self.SHADOW):
            radius = self.RADIUS + self.SHADOW - i
            painter.drawRoundedRect(rect.adjusted(i, i + 1, -i, -i + 1), radius, radius)
        painter.end()

    # ── UI ──
    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(self.SHADOW, self.SHADOW, self.SHADOW, self.SHADOW)

        # 圆角窗体；显式设箭头光标，免得子控件继承外圈的缩放光标
        self.body = QFrame()
        self.body.setObjectName("body")
        self.body.setCursor(Qt.ArrowCursor)
        self.body.setFocusPolicy(Qt.StrongFocus)
        outer.addWidget(self.body)

        layout = QVBoxLayout(self.body)
        layout.setContentsMargins(14, 10, 14, 14)
        layout.setSpacing(10)

        # Title bar (drag handle)
        self.title_bar = QWidget()
        self.title_bar.installEventFilter(self)
        title_layout = QHBoxLayout(self.title_bar)
        title_layout.setContentsMargins(2, 0, 0, 0)
        title_layout.setSpacing(2)

        title_label = QLabel("剪贴板")
        title_label.setObjectName("title")
        title_layout.addWidget(title_label)
        title_layout.addStretch()

        self.pin_btn = IconButton("pin", "titleBtn")
        self.pin_btn.clicked.connect(self._toggle_pin)
        title_layout.addWidget(self.pin_btn)

        settings_btn = IconButton("settings", "titleBtn", "设置")
        settings_btn.clicked.connect(self._open_settings)
        title_layout.addWidget(settings_btn)

        close_btn = IconButton("close", "titleBtn", "隐藏（Esc / Alt+V）")
        close_btn.clicked.connect(self._toggle_show)
        title_layout.addWidget(close_btn)

        layout.addWidget(self.title_bar)

        # Search bar
        self.search_input = QLineEdit()
        self.search_input.setObjectName("search")
        self.search_input.setPlaceholderText("搜索")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.addAction(
            QIcon(glyph_pixmap("search", COLORS["text_tertiary"], 14)),
            QLineEdit.LeadingPosition)
        self.search_input.textChanged.connect(self._on_search_text)
        layout.addWidget(self.search_input)

        # 筛选只影响展示，不改变保留期限或数据。
        toolbar = QHBoxLayout()
        self.kind_filter = QComboBox()
        for name, value in (("全部", "all"), ("文字", "text"), ("图片", "image"), ("收藏", "favorites")):
            self.kind_filter.addItem(name, value)
        self.kind_filter.currentIndexChanged.connect(lambda: self.load_clips(self.search_input.text(), reset=True))
        toolbar.addWidget(self.kind_filter)
        self.days_filter = QComboBox()
        for name, value in (("所有时间", 0), ("最近1天", 1), ("最近3天", 3), ("最近7天", 7)):
            self.days_filter.addItem(name, value)
        self.days_filter.currentIndexChanged.connect(lambda: self.load_clips(self.search_input.text(), reset=True))
        toolbar.addWidget(self.days_filter)
        self.pause_btn = QPushButton()
        self.pause_btn.clicked.connect(self._toggle_pause)
        toolbar.addWidget(self.pause_btn)
        layout.addLayout(toolbar)
        self.count_label = QLabel()
        self.count_label.setObjectName("count")
        layout.addWidget(self.count_label)

        # Scroll area for cards
        self.scroll = QScrollArea()
        self.scroll.setObjectName("scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)

        self.card_container = QWidget()
        self.card_container.setObjectName("cardContainer")
        self.card_layout = QVBoxLayout(self.card_container)
        self.card_layout.setContentsMargins(0, 2, 4, 2)
        self.card_layout.setSpacing(8)
        self.card_layout.addStretch()

        self.scroll.setWidget(self.card_container)
        self.scroll.verticalScrollBar().valueChanged.connect(self._scroll_load)
        layout.addWidget(self.scroll, 1)

        # 空状态
        self.empty_label = QLabel()
        self.empty_label.setObjectName("empty")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.hide()
        layout.addWidget(self.empty_label, 1)
        self.load_more_btn = QPushButton("加载更多")
        self.load_more_btn.clicked.connect(self._load_more)
        layout.addWidget(self.load_more_btn)
        actions = QHBoxLayout()
        for text, action in (("预览", "preview"), ("复制", "copy"), ("粘贴", "paste")):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, a=action: self._use_selected(a))
            actions.addWidget(button)
        layout.addLayout(actions)
        status_row = QHBoxLayout()
        self.status_label = QLabel("上下键选择 · Enter 粘贴 · Ctrl+Shift+C 复制")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("count")
        status_row.addWidget(self.status_label, 1)
        self.undo_btn = QPushButton("撤销删除")
        self.undo_btn.clicked.connect(self._undo_delete)
        self.undo_btn.hide()
        status_row.addWidget(self.undo_btn)
        layout.addLayout(status_row)
        self._update_pause()

    def _apply_theme(self):
        c = COLORS
        icon_font = icon_font_family()
        self.setStyleSheet(f"""
            QFrame#body {{
                background: {c['bg']};
                border: 1px solid {c['hairline']};
                border-radius: {self.RADIUS}px;
            }}
            QLabel#title {{
                color: {c['text']}; font-size: 16px; font-weight: 700;
                letter-spacing: 1px;
            }}
            QPushButton#titleBtn {{
                font-family: "{icon_font}"; font-size: 13px;
                color: {c['text_secondary']}; background: transparent;
                border: none; border-radius: 14px;
            }}
            QPushButton#titleBtn:hover {{ background: {c['fill']}; color: {c['text']}; }}
            QPushButton#titleBtn:pressed {{ background: {c['fill_strong']}; }}
            QPushButton#titleBtn[active="true"] {{
                color: {c['accent']}; background: {c['accent_tint']};
            }}

            QLineEdit#search {{
                background: {c['fill']}; color: {c['text']};
                border: 2px solid transparent; border-radius: 10px;
                padding: 5px 6px; font-size: 13px;
                selection-background-color: {c['accent']};
            }}
            QLineEdit#search:focus {{
                background: {c['card_bg']}; border-color: {c['accent_ring']};
            }}

            QFrame#segmented {{ background: {c['fill']}; border-radius: 8px; }}
            QFrame#segmented QPushButton {{
                background: transparent; color: {c['text']};
                border: 1px solid transparent; border-radius: 6px;
                padding: 3px 12px; font-size: 12px;
            }}
            QFrame#segmented QPushButton:hover:!checked {{ color: {c['accent']}; }}
            QFrame#segmented QPushButton:checked {{
                background: {c['card_bg']}; border-color: {c['hairline']};
                font-weight: 600;
            }}
            QPushButton#link {{
                background: transparent; border: none; padding: 4px 2px;
                color: {c['accent']}; font-size: 12px;
            }}
            QPushButton#link:hover {{ color: {c['accent_hover']}; text-decoration: underline; }}
            QLabel#count {{ color: {c['text_tertiary']}; font-size: 12px; }}

            QScrollArea#scroll, QWidget#cardContainer {{ background: transparent; }}
            QScrollBar:vertical {{
                width: 8px; background: transparent; margin: 2px 0 2px 2px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(0, 0, 0, 0.18); border-radius: 3px; min-height: 32px;
            }}
            QScrollBar::handle:vertical:hover {{ background: rgba(0, 0, 0, 0.32); }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

            QFrame#clipCard {{
                background: {c['card_bg']};
                border: 1px solid {c['hairline']};
                border-radius: 12px;
            }}
            QFrame#clipCard:hover {{ border-color: {c['accent_ring']}; }}
            QFrame#clipCard[selected="true"] {{ border: 2px solid {c['accent']}; background: {c['accent_tint']}; }}
            QComboBox {{ background: white; border: 1px solid {c['hairline']}; border-radius: 6px; padding: 4px; }}
            QPushButton {{ background: white; color: {c['text']}; border: 1px solid {c['hairline_strong']}; border-radius: 7px; padding: 5px; }}
            QPushButton:hover {{ background: {c['accent_tint']}; border-color: {c['accent']}; }}
            QLabel#clipText {{ color: {c['text']}; font-size: 13px; }}
            QLabel#clipMeta {{ color: {c['text_tertiary']}; font-size: 11px; }}
            QPushButton#cardPin, QPushButton#cardDelete {{
                font-family: "{icon_font}"; font-size: 12px;
                color: {c['text_tertiary']}; background: transparent;
                border: none; border-radius: 13px;
            }}
            QPushButton#cardPin:hover {{ background: {c['pin_tint']}; color: {c['pin']}; }}
            QPushButton#cardPin[active="true"] {{ color: {c['pin']}; }}
            QPushButton#cardDelete:hover {{ background: {c['delete_tint']}; color: {c['delete']}; }}

            QLabel#empty {{ color: {c['text_tertiary']}; font-size: 13px; }}
        """)

    def _filters(self):
        return self.kind_filter.currentData(), self.days_filter.currentData()

    def load_clips(self, search="", reset=False):
        if self._loading:
            return
        self._loading = True
        bar = self.scroll.verticalScrollBar()
        old_scroll = bar.value()
        anchor = None
        if not reset and old_scroll:
            for row in self.clips:
                card = getattr(self, "_card_by_id", {}).get(row[0])
                if card is not None and card.geometry().bottom() >= old_scroll:
                    anchor = (row[0], card.y() - old_scroll)
                    break
        kind, days = self._filters()
        rows = self.db.get_all(search.strip(), kind, days, max(60, len(self.clips) if not reset else 60))
        old_cards = getattr(self, "_card_by_id", {})
        new_ids = {row[0] for row in rows}
        old_rows = {row[0]: row for row in self.clips}
        new_rows = {row[0]: row for row in rows}
        for clip_id in list(old_cards):
            if clip_id not in new_ids or old_rows.get(clip_id) != new_rows.get(clip_id):
                card = old_cards.pop(clip_id)
                self.card_layout.removeWidget(card)
                card.deleteLater()
        self._card_by_id = old_cards
        self._visible_ids = [row[0] for row in rows]
        self.clips = rows
        for index, row in enumerate(rows):
            card = self._card_by_id.get(row[0])
            if card is None:
                card = self._append_card(row)
            self.card_layout.removeWidget(card)
            self.card_layout.insertWidget(index, card)
            if isinstance(card.text_label, ElidedLabel):
                card.text_label.search_term = search.strip()
                card.text_label._relayout()
                card.text_label.update()
        if self._selected_id not in new_ids:
            self._selected_id = rows[0][0] if rows else None
        self._paint_selection()
        self._update_count()
        self.card_layout.activate()
        saved_anchor = anchor
        def restore_scroll():
            if saved_anchor and saved_anchor[0] in self._card_by_id:
                bar.setValue(self._card_by_id[saved_anchor[0]].y() - saved_anchor[1])
            else:
                bar.setValue(0 if reset else old_scroll)
        if reset:
            bar.setValue(0)
        elif anchor and anchor[0] in self._card_by_id:
            card = self._card_by_id[anchor[0]]
            bar.setValue(card.y() - anchor[1])
        else:
            bar.setValue(old_scroll)
        QTimer.singleShot(0, restore_scroll)
        self._loading = False

    def _update_count(self):
        kind, days = self._filters()
        matched = self.db.matched_count(self.search_input.text().strip(), kind, days)
        count = len(self.clips)
        total = self.db.get_count()
        self.count_label.setText(f"共 {total} 条 · 匹配 {matched} 条 · 已加载 {count} 条")
        empty = count == 0
        self.empty_label.setText("没有找到相关内容" if self.search_input.text() or kind != "all" or days else
                                 "还没有复制记录\n复制的文字和图片会出现在这里")
        self.empty_label.setVisible(empty)
        self.scroll.setVisible(not empty)
        self.load_more_btn.setVisible(count < matched)

    def _append_card(self, clip):
        card = ClipCard(clip)
        self._card_by_id[clip[0]] = card
        card.clicked.connect(self._on_clip_click)
        card.selected.connect(self._select_id)
        card.delete_requested.connect(self._on_delete)
        card.pin_requested.connect(self._on_pin)
        self.card_layout.insertWidget(self.card_layout.count() - 1, card)
        return card

    def _load_more(self):
        if self._loading:
            return
        kind, days = self._filters()
        rows = self.db.get_all(self.search_input.text().strip(), kind, days, 60, len(self.clips))
        self._loading = True
        for row in rows:
            self.clips.append(row)
            self._visible_ids.append(row[0])
            card = self._append_card(row)
            if isinstance(card.text_label, ElidedLabel):
                card.text_label.search_term = self.search_input.text().strip()
                card.text_label._relayout()
                card.text_label.update()
        self._paint_selection()
        self._update_count()
        self._loading = False

    def _scroll_load(self, value):
        bar = self.scroll.verticalScrollBar()
        if not self._loading and bar.maximum() > 0 and value >= bar.maximum() - 80 and self.load_more_btn.isVisible():
            self._load_more()

    def _paint_selection(self):
        for clip_id, card in self._card_by_id.items():
            selected = clip_id == self._selected_id
            if card.property("selected") != selected:
                card.setProperty("selected", selected)
                card.style().unpolish(card)
                card.style().polish(card)
                card.update()

    def _select_id(self, clip_id):
        self._selected_id = clip_id
        self._paint_selection()

    def _select_delta(self, delta):
        if not self.clips:
            return
        ids = [row[0] for row in self.clips]
        index = ids.index(self._selected_id) if self._selected_id in ids else -1
        if delta > 0 and index == len(ids) - 1:
            self._load_more()
            ids = [row[0] for row in self.clips]
        index = max(0, min(len(ids) - 1, index + delta))
        self._select_id(ids[index])
        self.scroll.ensureWidgetVisible(self._card_by_id[ids[index]], 0, 20)
        self._preview_shortcut.setEnabled(True)

    def _use_selected(self, action):
        if self._selected_id is None:
            self._status("请先选择一条记录")
            return
        self._preview_clip(self._selected_id) if action == "preview" else self._use_clip(self._selected_id, action)

    @Slot(str)
    def _on_search_text(self, text):
        self._search_text = text
        if not hasattr(self, "_search_timer"):
            self._search_timer = QTimer(self)
            self._search_timer.setSingleShot(True)
            self._search_timer.setInterval(80)
            self._search_timer.timeout.connect(self._flush_search)
        self._search_timer.start()

    def _flush_search(self):
        self.load_clips(getattr(self, "_search_text", ""), reset=True)

    @Slot(int)
    def _on_clip_click(self, clip_id):
        self._select_id(clip_id)
        self.body.setFocus()
        action = self.settings.get("default_action", "preview")
        self._preview_clip(clip_id) if action == "preview" else self._use_clip(clip_id, action)

    def _preview_clip(self, clip_id):
        clip = self.db.get_clip(clip_id)
        if clip:
            PreviewDialog(clip, self).exec()

    def _status(self, message, notify=False):
        self.status_label.setText(message)
        if notify and hasattr(self, "tray"):
            self.tray.showMessage(APP_NAME, message, QSystemTrayIcon.Information, 2500)

    def _target_valid(self, hwnd, pid):
        if not hwnd or not pid or pid == os.getpid() or not _user32.IsWindow(hwnd):
            return False
        current = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(current))
        return current.value == pid

    def _use_clip(self, clip_id, action):
        clip = self.db.get_clip(clip_id)
        if not clip:
            self._status("记录已不存在")
            return
        clipboard = QApplication.clipboard()
        monitor = getattr(self, "monitor", None)
        # 定时检查按序列号识别自身写入，避免回贴产生重复记录。
        try:
            if clip[1] == "text":
                clipboard.setText(clip[2])
            else:
                image = decode_image(clip[2])
                if image.isNull():
                    self._status("图片无法读取，未改变剪贴板")
                    return
                clipboard.setImage(image)
            if monitor:
                monitor.mark_own_write()
        except Exception:
            self._status("复制失败，请稍后重试")
            return
        self._status("已复制" + ("原图" if clip[1] == "image" and clip[9] else ""))
        if action == "copy":
            return
        hwnd, pid = self._last_external_hwnd, self._last_external_pid
        if not self._target_valid(hwnd, pid):
            self._status("已复制；目标窗口不可用，请手动粘贴", notify=True)
            return
        if not _user32.SetForegroundWindow(hwnd) or _user32.GetForegroundWindow() != hwnd:
            self._status("已复制；无法切回目标，请手动粘贴", notify=True)
            return
        sequence = _user32.GetClipboardSequenceNumber()
        self._pending_paste = (hwnd, pid, sequence)
        if not self.pinned:
            self.hide()
        QTimer.singleShot(80, self._simulate_paste)

    def _simulate_paste(self):
        target = self._pending_paste
        self._pending_paste = None
        if not target:
            return
        hwnd, pid, sequence = target
        if (not self._target_valid(hwnd, pid) or _user32.GetForegroundWindow() != hwnd
                or _user32.GetClipboardSequenceNumber() != sequence):
            self._status("粘贴已取消：目标或剪贴板发生变化，请手动粘贴", notify=True)
            return
        if _user32.GetAsyncKeyState(0x12) & 0x8000 or _user32.GetAsyncKeyState(0x10) & 0x8000:
            self._status("已复制；请松开 Alt/Shift 后手动粘贴", notify=True)
            return
        # SendInput一次提交完整按键组，不向已经失效的目标发送按键。
        class KeyboardInput(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
        class MouseInput(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
        class InputUnion(ctypes.Union):
            _fields_ = [("ki", KeyboardInput), ("mi", MouseInput)]
        class Input(ctypes.Structure):
            _anonymous_ = ("data",)
            _fields_ = [("type", wintypes.DWORD), ("data", InputUnion)]
        ctrl_held = bool(_user32.GetAsyncKeyState(0x11) & 0x8000)
        keys = [(0x56, 0), (0x56, 2)] if ctrl_held else [(0x11, 0), (0x56, 0), (0x56, 2), (0x11, 2)]
        events = (Input * len(keys))()
        for event, (key, flags) in zip(events, keys):
            event.type = 1
            event.ki.wVk, event.ki.dwFlags = key, flags
        _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        sent = _user32.SendInput(len(events), events, ctypes.sizeof(Input))
        if sent != len(events):
            if not ctrl_held:
                _user32.keybd_event(0x11, 0, 2, 0)
            _user32.keybd_event(0x56, 0, 2, 0)
            self._status("已复制；目标权限可能阻止粘贴，请手动粘贴", notify=True)
        else:
            self._status("已发送粘贴操作；若目标未接收，可手动 Ctrl+V")

    @Slot(int)
    def _on_delete(self, clip_id):
        self._undo_row = self.db.delete(clip_id)
        _THUMB_CACHE.pop(clip_id, None)
        self.load_clips(self.search_input.text())
        self.undo_btn.setVisible(self._undo_row is not None)
        self._undo_timer.start()
        self._status("已删除，10秒内可撤销")

    def _expire_undo(self):
        self._undo_row = None
        self.undo_btn.hide()

    def _undo_delete(self):
        if self._undo_row:
            self.db.restore(self._undo_row)
            self._expire_undo()
            self.load_clips(self.search_input.text())
            self._status("已恢复记录")

    @Slot(int)
    def _on_pin(self, clip_id):
        state = self.db.toggle_pin(clip_id)
        self.load_clips(self.search_input.text())
        self._status("已收藏，不会自动过期" if state else "已取消收藏，重新遵循保留期限")

    def _open_settings(self):
        if SettingsDialog(self.settings, self).exec() == QDialog.Accepted:
            self.load_clips(self.search_input.text())
            self._update_pause()
            self._status("设置已保存")

    def _update_pause(self):
        paused = self.settings.get("paused", False)
        self.pause_btn.setText("恢复记录" if paused else "暂停记录")
        self.pause_btn.setToolTip("当前已暂停，复制内容不会保存" if paused else "暂停保存新的剪贴板内容")
        if paused:
            self._status("记录已暂停，点击恢复记录继续")
        if hasattr(self, "tray"):
            self.tray.setToolTip(APP_NAME + (" · 已暂停" if paused else " · 正在记录"))
            self.tray._pause_action.setText("恢复记录" if paused else "暂停记录")

    def _toggle_pause(self):
        if hasattr(self, "monitor"):
            self.monitor.reset_sequence()
        self.settings.set("paused", not self.settings.get("paused", False))
        self._update_pause()
        self._status("记录已暂停" if self.settings.get("paused") else "已恢复记录")

    def _periodic_cleanup(self):
        count = self.db.get_count()
        self.db.clean_expired(self.settings.get("retention_days"))
        self.db.trim_to(self.settings.get("max_clips", 2000))
        self.db.trim_storage(self.settings.get("max_storage_mb", 512))
        self.db.conn.execute("PRAGMA incremental_vacuum(256)")
        self.db.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        if count != self.db.get_count():
            _THUMB_CACHE.clear()
            self.load_clips(self.search_input.text())

    def add_clip(self, clip_data):
        if self.settings.get("paused", False):
            return
        source = clip_data.get("source_app", "").lower()
        if source in [name.lower() for name in self.settings.get("excluded_apps", [])]:
            return
        maximum = self.settings.get("max_storage_mb", 512) * 1024 * 1024
        size = len(clip_data["content"].encode("utf-8")) + len(clip_data.get("thumbnail", "").encode("utf-8"))
        if size > maximum:
            self._status("本次内容超过存储上限，未记录", notify=True)
            return
        favorite_size, favorite_count = self.db.conn.execute("SELECT COALESCE(SUM(length(CAST(content AS BLOB))+COALESCE(length(thumbnail),0)),0),COUNT(*) FROM clips WHERE pinned=1").fetchone()
        if favorite_size + size > maximum or favorite_count >= self.settings.get("max_clips", 2000):
            self.settings.set("paused", True)
            self._update_pause()
            self._status("收藏已占满容量，请整理收藏或增大上限后恢复记录", notify=True)
            return
        metadata = {key: value for key, value in clip_data.items() if key not in ("type", "content", "source_app")}
        self.db.insert(clip_data["type"], clip_data["content"], source, **metadata)
        self.db.trim_to(self.settings.get("max_clips", 2000))
        removed = self.db.trim_storage(self.settings.get("max_storage_mb", 512))
        if removed:
            self._status("达到容量上限，已清理最旧未收藏记录")
        if self.isVisible():
            self.load_clips(self.search_input.text())


# ─── Clipboard Monitor ───────────────────────────────────────
class ClipboardMonitor(QObject):
    new_clip = Signal(dict)
    issue = Signal(str)

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self._last_sequence = _user32.GetClipboardSequenceNumber()
        self._own_sequences = set()
        self._retry_sequence = None
        self._retry_count = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.check)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)
        # 系统通知为主，低频校验覆盖通知遗漏和剪贴板短暂被占用。
        self._fallback = QTimer(self)
        self._fallback.setInterval(1000)
        self._fallback.timeout.connect(self.check)
        self._fallback.start()

    def reset_sequence(self):
        self._last_sequence = _user32.GetClipboardSequenceNumber()

    def mark_own_write(self):
        self._own_sequences.add(_user32.GetClipboardSequenceNumber())
        self._timer.start(0)

    def _on_clipboard_changed(self):
        if not self._timer.isActive():
            self._timer.start(0)

    def check(self):
        sequence = _user32.GetClipboardSequenceNumber()
        if sequence == self._last_sequence:
            return
        if sequence in self._own_sequences:
            self._own_sequences.discard(sequence)
            self._last_sequence = sequence
            return
        if self.settings.get("paused", False):
            self._last_sequence = sequence
            return
        source = process_name(_user32.GetClipboardOwner() or _user32.GetForegroundWindow())
        if source.lower() in [name.lower() for name in self.settings.get("excluded_apps", [])]:
            self._last_sequence = sequence
            return
        try:
            clipboard = QApplication.clipboard()
            mime = clipboard.mimeData()
            if mime is None:
                self._retry(sequence)
                return
            # 图片格式优先，避免同时含文本的截图被误保存为文本。
            clip = None
            if mime.hasImage():
                image = clipboard.image()
                if image.isNull():
                    self._retry(sequence)
                    return
                clip = encode_image(image)
            elif mime.hasText() and mime.text():
                text = mime.text()
                clip = {"type": "text", "content": text, "byte_size": len(text.encode("utf-8"))}
            # 读取期间发生了新复制则放弃不完整快照，再读取新版本。
            if _user32.GetClipboardSequenceNumber() != sequence:
                self._timer.start(0)
                return
            self._last_sequence = sequence
            self._own_sequences.clear()
            if clip:
                clip["source_app"] = source
                self.new_clip.emit(clip)
        except Exception:
            logging.exception("Clipboard capture failed")
            self.issue.emit("本次内容未能记录，请重新复制；可到数据目录查看错误日志")
            self._last_sequence = sequence

    def _retry(self, sequence):
        if self._retry_sequence != sequence:
            self._retry_sequence, self._retry_count = sequence, 0
        self._retry_count += 1
        if self._retry_count <= 10:
            self._timer.start(50)
        else:
            self._last_sequence = sequence
            self.issue.emit("剪贴板被占用或格式无法读取，请重新复制")


# ─── System Tray ──────────────────────────────────────────────
def create_tray_icon(app, window):
    icon = create_app_icon()

    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip(APP_NAME)

    menu = QMenu()
    # 透明底 + 无系统阴影，圆角外面才不会露出直角
    menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
    menu.setAttribute(Qt.WA_TranslucentBackground)
    menu.setStyleSheet(f"""
        QMenu {{
            background: white; border: 1px solid {COLORS['hairline_strong']};
            border-radius: 10px; padding: 5px;
        }}
        QMenu::item {{
            padding: 7px 28px 7px 16px; border-radius: 6px;
            font-size: 13px; color: {COLORS['text']};
        }}
        QMenu::item:selected {{ background: {COLORS['accent']}; color: white; }}
        QMenu::separator {{ height: 1px; background: {COLORS['hairline']}; margin: 4px 8px; }}
    """)

    # QAction 必须挂父对象、菜单必须有人持有引用：
    # 否则函数一返回就被 Python 回收，右键菜单里只剩分隔线
    show_action = QAction("显示/隐藏", menu)
    show_action.triggered.connect(window._toggle_show)
    menu.addAction(show_action)
    pause_action = QAction("暂停记录", menu)
    pause_action.triggered.connect(window._toggle_pause)
    menu.addAction(pause_action)
    tray._pause_action = pause_action
    settings_action = QAction("设置 / 使用说明", menu)
    settings_action.triggered.connect(window._open_settings)
    menu.addAction(settings_action)

    menu.addSeparator()

    quit_action = QAction("退出", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray._menu = menu
    tray.activated.connect(lambda reason: window._toggle_show() if reason == QSystemTrayIcon.DoubleClick else None)

    tray.show()
    return tray


# ─── Main ─────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    app.setFont(font)
    app.setStyleSheet(f"""
        QToolTip {{
            background: white; color: {COLORS['text']};
            border: 1px solid {COLORS['hairline_strong']};
            border-radius: 6px; padding: 4px 8px; font-size: 12px;
        }}
    """)

    # ── 单实例：已存在则唤起已运行实例后退出 ──
    socket = QLocalSocket()
    socket.connectToServer(APP_NAME)
    if socket.waitForConnected(500):
        socket.write(b"show")
        socket.flush()
        socket.disconnectFromServer()
        print("Another instance is already running.")
        sys.exit(0)

    server = QLocalServer()
    QLocalServer.removeServer(APP_NAME)
    if not server.listen(APP_NAME):
        print("Failed to start single-instance server.")

    def _on_second_instance():
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.disconnectFromServer()
        # 唤起已运行实例的窗口
        if not window.isVisible():
            window._toggle_show()

    server.newConnection.connect(_on_second_instance)

    db = Database()
    settings = Settings()

    # Clean expired on startup
    db.clean_expired(settings.get("retention_days"))
    db.trim_to(settings.get("max_clips", 2000))

    # Create main window
    window = MainWindow(db, settings)

    # System tray
    tray = create_tray_icon(app, window)
    window.tray = tray
    window._update_pause()
    app.setWindowIcon(create_app_icon())

    # Clipboard monitor（只靠 dataChanged 门铃即时触发，不再轮询）
    monitor = ClipboardMonitor(settings)
    window.monitor = monitor
    monitor.new_clip.connect(window.add_clip)
    monitor.issue.connect(lambda text: window._status(text, notify=True))

    def report_error(exc_type, error, tb):
        logging.error("Unhandled application error", exc_info=(exc_type, error, tb))
        window._status("操作未能完成，请重试；详情已保存到本地错误日志", notify=True)
    sys.excepthook = report_error

    # Show window at start
    window._toggle_show()

    # Show welcome message
    if not settings.get("onboarding_seen", False):
        window._status(f"{settings.get('hotkey')} 呼出 · 上下键选择 · Enter粘贴 · 点击记录预览")
        tray.showMessage(APP_NAME, "点击记录可完整预览，复制与粘贴可分别选择。\n设置中可修改快捷键、保留期限和开机自启。", QSystemTrayIcon.Information, 5000)
        settings.set("onboarding_seen", True)

    def close_resources():
        window._save_geometry()
        _user32.UnregisterHotKey(int(window.winId()), window._hotkey_id)
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.conn.close()
    app.aboutToQuit.connect(close_resources)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
