import os
import sys
import tempfile
import sqlite3
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch

TASK_DIR = tempfile.TemporaryDirectory()
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['CLIPBOARD_HISTORY_DATA_DIR'] = TASK_DIR.name
spec = importlib.util.spec_from_file_location('clipboard_app', str(Path(__file__).resolve().parents[1] / 'main.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
app = m.QApplication.instance() or m.QApplication([])
for font_path in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\seguiemj.ttf', r'C:\Windows\Fonts\SegoeIcons.ttf'):
    if Path(font_path).exists(): m.QFontDatabase.addApplicationFont(font_path)
font=m.QFont()
font.setFamilies(m.FONT_FAMILIES)
app.setFont(font)

class TestWindow(m.MainWindow):
    def _register_hotkey(self, text=None):
        return True

class IterationTests(unittest.TestCase):
    def setUp(self):
        self.db = m.Database(':memory:')
        self.settings = m.Settings()
        self.settings.data = dict(m.DEFAULT_SETTINGS, window_pinned=True, max_storage_mb=512)

    def tearDown(self):
        self.db.conn.close()

    def test_lossless_image_capture_and_roundtrip(self):
        image = m.QImage(913, 577, m.QImage.Format_ARGB32)
        image.fill(m.QColor('#38A0D4'))
        image.setPixelColor(456, 288, m.QColor('#FF0000'))
        encoded = m.encode_image(image)
        self.assertEqual(m.decode_image(encoded['content']), image)
        self.assertLessEqual(m.decode_image(encoded['thumbnail']).width(), 200)
        identifier = self.db.insert('image', encoded['content'], 'test.exe', **{k:v for k,v in encoded.items() if k not in ('type','content')})
        window = TestWindow(self.db, self.settings)
        window._use_clip(identifier, 'copy')
        self.assertEqual(app.clipboard().image(), image)
        self.assertEqual(window.clips[0][6:8], (913, 577))
        window.close()

    def test_legacy_migration_preserves_text_and_marks_images(self):
        path = Path(TASK_DIR.name) / 'legacy.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE clips(id INTEGER PRIMARY KEY,type TEXT,content TEXT,timestamp REAL,pinned INTEGER DEFAULT 0,source_app TEXT)')
            db.execute('INSERT INTO clips VALUES(1,?,?,?,?,?)', ('text','legacy content',m.time.time(),1,'test.exe'))
            image = m.QImage(100, 75, m.QImage.Format_RGB32)
            image.fill(m.QColor('blue'))
            db.execute('INSERT INTO clips VALUES(2,?,?,?,?,?)', ('image',m.encode_image(image)['content'],m.time.time(),0,'test.exe'))
        db.close()
        migrated = m.Database(path)
        self.assertEqual(migrated.get_clip(1)[2], 'legacy content')
        self.assertEqual(migrated.get_clip(1)[4], 1)
        self.assertEqual(migrated.get_clip(2)[9], 0)
        migrated.conn.close()

    def test_duplicate_preserves_favorite_and_updates_time(self):
        identifier = self.db.insert('text', 'repeat', 'a.exe')
        self.db.toggle_pin(identifier)
        self.db.conn.execute('UPDATE clips SET timestamp=1 WHERE id=?', (identifier,))
        self.assertEqual(self.db.insert('text','repeat','b.exe'), identifier)
        self.assertEqual(self.db.get_count(), 1)
        self.assertEqual(self.db.get_clip(identifier)[4], 1)
        self.assertGreater(self.db.get_clip(identifier)[3], 1)

    def test_all_records_accessible_and_filters_do_not_delete(self):
        for i in range(621):
            self.db.insert('text',f'record {i}')
        first = self.db.insert('text', 'literal%_needle')
        self.db.toggle_pin(first)
        rows=[]
        for offset in range(0, 700, 60):
            rows.extend(self.db.get_all(limit=60,offset=offset))
        self.assertEqual(len(rows), 622)
        self.assertEqual(self.db.matched_count('%_needle'), 1)
        self.assertEqual(self.db.matched_count(kind='favorites'), 1)
        self.assertEqual(self.db.get_count(), 622)
        window=TestWindow(self.db,self.settings)
        for _ in range(10): window._load_more()
        self.assertEqual(len(window.clips),622)
        window.kind_filter.setCurrentIndex(3)
        self.assertEqual(len(window.clips),1)
        self.assertEqual(self.db.get_count(),622)
        window.close()

    def test_cleanup_keeps_favorites_and_delete_undo(self):
        favorite=self.db.insert('text','favorite')
        other=self.db.insert('text','expired')
        self.db.toggle_pin(favorite)
        self.db.conn.execute('UPDATE clips SET timestamp=1')
        self.db.clean_expired(1)
        self.assertIsNotNone(self.db.get_clip(favorite))
        self.assertIsNone(self.db.get_clip(other))
        window=TestWindow(self.db,self.settings)
        window._on_delete(favorite)
        self.assertEqual(self.db.get_count(),0)
        window._undo_delete()
        self.assertEqual(self.db.get_clip(favorite)[4],1)
        window.close()

    def test_pause_and_exact_application_exclusion(self):
        window=TestWindow(self.db,self.settings)
        self.settings.data['paused']=True
        window.add_clip({'type':'text','content':'paused','source_app':'test.exe'})
        self.assertEqual(self.db.get_count(),0)
        self.settings.data['paused']=False
        self.settings.data['excluded_apps']=['test.exe']
        window.add_clip({'type':'text','content':'blocked','source_app':'test.exe'})
        window.add_clip({'type':'text','content':'allowed','source_app':'mytest.exe'})
        self.assertEqual(self.db.get_count(),1)
        window.close()

    def test_invalid_target_never_sends_input(self):
        window=TestWindow(self.db,self.settings)
        identifier=self.db.insert('text','safe fallback')
        with patch.object(m._user32,'SendInput') as send:
            window._last_external_hwnd=None
            window._use_clip(identifier,'paste')
            window._simulate_paste()
            send.assert_not_called()
        self.assertEqual(app.clipboard().text(),'safe fallback')
        self.assertIn('手动粘贴',window.status_label.text())
        window.close()

    def test_target_or_clipboard_change_cancels_delayed_paste(self):
        window=TestWindow(self.db,self.settings)
        window._pending_paste=(123,456,7)
        with patch.object(window,'_target_valid',return_value=True), patch.object(m._user32,'GetForegroundWindow',return_value=124), patch.object(m._user32,'SendInput') as send:
            window._simulate_paste()
            send.assert_not_called()
        window._pending_paste=(123,456,7)
        with patch.object(window,'_target_valid',return_value=True), patch.object(m._user32,'GetForegroundWindow',return_value=123), patch.object(m._user32,'GetClipboardSequenceNumber',return_value=8), patch.object(m._user32,'SendInput') as send:
            window._simulate_paste()
            send.assert_not_called()
        window.close()

    def test_own_clipboard_write_not_recorded_and_recapture_updates(self):
        monitor=m.ClipboardMonitor(self.settings)
        events=[]
        monitor.new_clip.connect(events.append)
        app.clipboard().setText('self-generated')
        with patch.object(m._user32,'GetClipboardSequenceNumber',return_value=monitor._last_sequence + 1):
            monitor.mark_own_write()
            monitor.check()
        self.assertEqual(events,[])
        app.clipboard().setText('external synthetic record')
        with patch.object(m._user32,'GetClipboardSequenceNumber',return_value=monitor._last_sequence + 1):
            monitor.check()
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['content'],'external synthetic record')
        monitor._fallback.stop()
        monitor._timer.stop()
        monitor.deleteLater()

    def test_settings_atomic_and_dialog_layout(self):
        self.settings.update({'hotkey':'Ctrl+Alt+V','retention_days':7})
        loaded=m.Settings()
        self.assertEqual(loaded.get('retention_days'),7)
        self.assertEqual(m.MainWindow._parse_hotkey('Ctrl+Alt+V'),(0x4003,86))
        with self.assertRaises(ValueError): m.MainWindow._parse_hotkey('V')
        window=TestWindow(self.db,self.settings)
        dialog=m.SettingsDialog(self.settings,window)
        dialog.show()
        app.processEvents()
        self.assertGreater(dialog.width(),400)
        dialog.grab().save(str(Path(TASK_DIR.name)/'clipboard-settings.png'))
        dialog.close()
        identifier=self.db.insert('text','Long text\n' * 200)
        preview=m.PreviewDialog(self.db.get_clip(identifier),window)
        preview.show()
        app.processEvents()
        preview.grab().save(str(Path(TASK_DIR.name)/'clipboard-preview.png'))
        preview.close()
        window.close()

    def test_incremental_updates_preserve_card_and_scroll_anchor(self):
        for i in range(85): self.db.insert('text',f'item {i}')
        window=TestWindow(self.db,self.settings)
        window.show()
        app.processEvents()
        window.scroll.verticalScrollBar().setValue(250)
        app.processEvents()
        value=window.scroll.verticalScrollBar().value()
        card=window._card_by_id[window.clips[5][0]]
        offset=card.y()-value
        window.add_clip({'type':'text','content':'brand new','source_app':'test.exe'})
        app.processEvents()
        self.assertIs(window._card_by_id[card.clip_id],card)
        self.assertEqual(card.y()-window.scroll.verticalScrollBar().value(),offset)
        window.resize(500,720)
        app.processEvents()
        window.grab().save(str(Path(TASK_DIR.name)/'clipboard-main.png'))
        window.close()

    def test_shorten_retention_cancel_keeps_data_and_settings(self):
        self.settings.data['retention_days']=7
        identifier=self.db.insert('text','three days old')
        self.db.conn.execute('UPDATE clips SET timestamp=? WHERE id=?',(m.time.time()-3*86400,identifier))
        window=TestWindow(self.db,self.settings)
        dialog=m.SettingsDialog(self.settings,window)
        dialog.retention_combo.setCurrentIndex(dialog.retention_combo.findData(1))
        with patch.object(m.QMessageBox,'question',return_value=m.QMessageBox.No), patch.object(m,'set_startup') as startup:
            dialog._save()
            startup.assert_not_called()
        self.assertEqual(self.settings.get('retention_days'),7)
        self.assertIsNotNone(self.db.get_clip(identifier))
        dialog.close()
        window.close()

    def test_storage_limit_removes_oldest_only_and_new_record_orders_first(self):
        favorite=self.db.insert('text','f'*400)
        self.db.toggle_pin(favorite)
        oldest=self.db.insert('text','a'*500)
        latest=self.db.insert('text','b'*500)
        self.db.trim_storage(1000/(1024*1024))
        self.assertIsNotNone(self.db.get_clip(favorite))
        self.assertIsNone(self.db.get_clip(oldest))
        self.assertIsNotNone(self.db.get_clip(latest))
        window=TestWindow(self.db,self.settings)
        window.add_clip({'type':'text','content':'new latest','source_app':'test.exe'})
        self.assertEqual(window.card_layout.itemAt(1).widget().clip_id,window.clips[1][0])
        window.close()

    def test_render_minimum_image_and_empty_states(self):
        window=TestWindow(self.db,self.settings)
        window.show()
        window.resize(340,400)
        app.processEvents()
        window.grab().save(str(Path(TASK_DIR.name)/'clipboard-empty-minimum.png'))
        image=m.QImage(1800,900,m.QImage.Format_RGB32)
        image.fill(m.QColor('green'))
        clip=m.encode_image(image)
        window.add_clip(clip)
        dialog=m.PreviewDialog(self.db.get_clip(window.clips[0][0]),window)
        dialog.show()
        app.processEvents()
        for index in (0,1,2):
            dialog.zoom.setCurrentIndex(index)
            app.processEvents()
            self.assertFalse(dialog.image_label.pixmap().isNull())
        dialog.grab().save(str(Path(TASK_DIR.name)/'clipboard-image-preview.png'))
        dialog.close()
        window.close()

    def test_pin_keeps_drag_and_keyboard_selects_without_typing_side_effect(self):
        first=self.db.insert('text','first synthetic')
        second=self.db.insert('text','second synthetic')
        window=TestWindow(self.db,self.settings)
        class FakeHandle:
            def __init__(self): self.moves=0
            def startSystemMove(self): self.moves+=1; return True
        handle=FakeHandle()
        for pinned in (True,False):
            window.pinned=pinned
            event=m.QEvent(m.QEvent.MouseButtonPress)
            event.button=lambda: m.Qt.LeftButton
            with patch.object(window,'windowHandle',return_value=handle):
                self.assertTrue(window.eventFilter(window.title_bar,event))
        self.assertEqual(handle.moves,2)
        self.assertEqual(window._selected_id,second)
        window._select_delta(1)
        self.assertEqual(window._selected_id,first)
        window.close()

    def test_settings_save_failure_reverts_in_memory_data(self):
        original=self.settings.get('paused')
        with patch.object(m.os,'replace',side_effect=OSError('read-only synthetic path')):
            with self.assertRaises(OSError): self.settings.set('paused',not original)
        self.assertEqual(self.settings.get('paused'),original)

if __name__=='__main__':
    result=unittest.main(verbosity=2,exit=False)
    app.clipboard().clear()
    m.logging.shutdown()
    sys.exit(not result.result.wasSuccessful())
