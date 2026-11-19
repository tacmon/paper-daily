#!/usr/bin/python3
"""Local GTK notification: observes finished papers; never runs an AI task."""
from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parent.parent
APP_ID = 'io.github.paperdaily.Control'
sys.path.insert(0, str(ROOT))
import paper_library as library


def pending_papers(root=ROOT, date=None, include_acknowledged=False):
    papers, days, errors = library.load_library(root)
    if errors:
        print('Skipping invalid library records: ' + '; '.join(errors), file=sys.stderr)
    result = []
    for day, record in sorted(days.items(), reverse=True):
        if day > library.today() and not include_acknowledged:
            continue
        if date and day != date:
            continue
        data = papers[record['paper_id']]
        base = root / 'papers' / data['id']
        if library.effective_status(root, data) != 'ready':
            continue
        try:
            background = library.inside(base, data['artifacts'].get('background', 'background.md'))
            if not background.is_file() or not background.stat().st_size:
                continue
        except (OSError, ValueError):
            continue
        key = f"{day}--{data['id']}"
        state_path = root / '.notifications' / (key + '.json')
        try:
            state = library.read_json(state_path) if state_path.exists() else {}
        except (OSError, ValueError):
            print('Unreadable notification state; treating as pending: ' + str(state_path), file=sys.stderr)
            state = {}
        if not include_acknowledged and state.get('acknowledged_at'):
            continue
        result.append({'date': day, 'data': data, 'base': base, 'state_path': state_path, 'state': state})
    return result


def save_state(item, **updates):
    state = dict(item['state'])
    state.update(schema_version=1, date=item['date'], paper_id=item['data']['id'], **updates)
    library.atomic_json(item['state_path'], state)
    item['state'] = state


def available_papers(root=ROOT):
    """Manual browsing includes dismissed reports, but never future publications."""
    return [item for item in pending_papers(root, include_acknowledged=True)
            if item['date'] <= library.today()]


def pending_delivery_notice(root=ROOT, now=None, include_acknowledged=False):
    """Observe a missed delivery target, never infer agent/process state or run AI."""
    config_path = root / 'config.json'
    try:
        config = library.read_json(config_path) if config_path.exists() else {}
        setting = config.get('desktop_notification', {}).get('late_report_alert', {})
        if not setting.get('enabled', False):
            return None
        zone = ZoneInfo(config.get('timezone', 'Asia/Shanghai'))
        now = datetime.now(zone) if now is None else now.astimezone(zone)
        after = setting.get('after', '09:00')
        deadline = datetime.combine(now.date(), datetime.strptime(after, '%H:%M').time(), zone)
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as exc:
        print('Delivery notice configuration unavailable: ' + str(exc), file=sys.stderr)
        return None
    if now < deadline:
        return None
    day = now.date().isoformat()
    # Reuse the exact completion gate, even when its normal reminder was dismissed.
    if pending_papers(root, date=day, include_acknowledged=True):
        return None
    path = root / '.notifications' / ('delivery-' + day + '.json')
    try:
        state = library.read_json(path) if path.exists() else {}
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    if state.get('acknowledged_at') and not include_acknowledged:
        return None
    return {'kind': 'delivery_delay', 'date': day, 'after': after,
            'timezone': str(zone), 'state_path': path, 'state': state}


def save_delivery_state(item, **updates):
    state = dict(item['state'])
    state.update(schema_version=1, kind='delivery_delay', date=item['date'], **updates)
    library.atomic_json(item['state_path'], state)
    item['state'] = state


def open_research_action(item, kind):
    """Launch locally, without acknowledging the reminder or opening a personal profile."""
    if kind == 'obsidian':
        from scripts.obsidian_bridge import open_note
        return open_note(item, ROOT)
    command = ['/usr/bin/python3', str(ROOT / 'prototype_v2/open_workbench.py')]
    if kind == 'notes':
        relative = item['data']['artifacts'].get('notes_html')
        if not relative:
            raise FileNotFoundError('这一篇还没有独立笔记。')
        target = library.inside(item['base'], relative)
        command += ['--note', str(target)]
    elif kind == 'notes_search':
        command += ['--scope', 'notes']
    elif kind == 'journal':
        command += ['--journal', item['data']['id']]
    elif kind != 'workbench':
        raise ValueError('未知的阅读入口。')
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:240] or '工作台暂时无法打开。')


def start_background():
    directory = ROOT / '.notifications'
    directory.mkdir(exist_ok=True)
    # The daemon also acquires this lock, which resolves simultaneous launch races.
    with (directory / 'daemon.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'already_running'}
        fcntl.flock(lock, fcntl.LOCK_UN)
    with (directory / 'daemon.log').open('ab') as log:
        proc = subprocess.Popen(['/usr/bin/python3', str(Path(__file__).resolve()), '--watch'],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, close_fds=True)
    return {'status': 'started', 'pid': proc.pid}


CSS = b'''
window { background: #f7f8fc; color: #1b2640; font-family: "Noto Sans CJK SC", sans-serif; }
headerbar { background: #f7f8fc; border: 0; box-shadow: none; min-height: 32px; }
.eyebrow { color: #67718c; font-size: 11px; font-weight: 700; letter-spacing: 2px; }
.badge { color: #237358; background: #e4f2eb; border-radius: 20px; padding: 6px 13px; font-size: 12px; }
.headline { font-size: 30px; font-weight: 800; color: #192641; }
.greeting { color: #68738a; font-size: 13px; }
.paper { background: white; border: 1px solid #e5e9f1; border-radius: 16px; padding: 22px; }
.paper-title { color: #23314c; font-size: 21px; font-weight: 700; }
.paper-title-original { color: #68738a; font-size: 14px; }
.meta { color: #788298; font-size: 12px; }
.asset { color: #4c5d7d; background: #eef2f9; border-radius: 8px; padding: 8px 12px; font-size: 12px; }
.location { font-size: 12px; color: #68738a; }
button { border-radius: 10px; padding: 12px 18px; font-size: 14px; font-weight: 600; box-shadow: none; background-image: none; }
button.primary { color: white; background: #4a61db; border: 1px solid #4a61db; }
button.primary:hover { background: #3b50c4; }
button.secondary { color: #35476b; background: white; border: 1px solid #dce2ee; }
button.secondary:hover { background: #edf0f8; }
button.quiet { color: #7a8497; background: transparent; border-color: transparent; font-size: 12px; padding: 7px 10px; }
.footer { color: #939bad; font-size: 11px; }
.error { color: #b33e50; font-size: 12px; }
'''


def run_gui(args):
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk, Gdk, Gio, GLib, Pango

    ok, _ = Gtk.init_check([])
    if not ok:
        print('No desktop display; reminders remain pending.', flush=True)
        return 2
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    session_dismissed = set()
    read_only = args.preview or args.open
    application = None

    def locked():
        try:
            reply = Gio.bus_get_sync(Gio.BusType.SESSION, None).call_sync(
                'org.gnome.ScreenSaver', '/org/gnome/ScreenSaver',
                'org.gnome.ScreenSaver', 'GetActive', None, GLib.VariantType.new('(b)'),
                Gio.DBusCallFlags.NONE, 800, None)
            return reply.unpack()[0]
        except GLib.Error:
            return None  # Unknown lock state is not evidence of an unlocked session.

    class Reminder:
        def __init__(self, item, on_close):
            self.item, self.on_close = item, on_close
            self.handled = False
            self.was_shown = False
            self.win = Gtk.Window(title='论文日报 · 阅读中控' if args.open else 'Paper Daily by AI · 论文已就绪')
            self.win.set_wmclass('paper-daily-ai', 'paper-daily-ai')
            self.win.set_icon_name('paper-daily-ai')
            self.win.set_resizable(False)
            self.win.set_default_size(700, -1)
            self.win.set_position(Gtk.WindowPosition.CENTER)
            self.win.set_keep_above(not args.open)
            self.win.set_skip_taskbar_hint(False)
            header = Gtk.HeaderBar(show_close_button=True)
            header.set_title('论文日报 · 阅读中控' if args.open else 'Paper Daily by AI')
            header.pack_start(Gtk.Image.new_from_icon_name('paper-daily-ai', Gtk.IconSize.LARGE_TOOLBAR))
            self.win.set_titlebar(header)
            self.win.connect('delete-event', lambda *_: self.close('dismissed'))
            self.win.connect('key-press-event', self.keypress)
            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            for side in ['top', 'bottom', 'start', 'end']:
                getattr(outer, 'set_margin_' + side)(32 if side != 'top' else 4)
            self.win.add(outer)
            top = Gtk.Box(spacing=12)
            top.pack_start(self.label('PAPER DAILY  /  每日一篇', 'eyebrow'), True, True, 0)
            top.pack_end(self.label('已验收  ·  ' + item['date'], 'badge'), False, False, 0)
            outer.pack_start(top, False, False, 0)
            headline = '今天的论文，已备好' if item['date'] == library.today() else '这一期论文，已备好'
            if args.open:
                headline = '最近一期，随时回看'
            title = self.label(headline, 'headline'); title.set_margin_top(24)
            outer.pack_start(title, False, False, 0)
            greeting = self.label('尊敬的主人，今日本 AI 为您推荐的论文已准备就绪。' if item['date'] == library.today() else '尊敬的主人，本 AI 为您推荐的这一期论文已准备就绪。', 'greeting')
            greeting.set_margin_top(9); greeting.set_margin_bottom(23)
            outer.pack_start(greeting, False, False, 0)
            if args.open:
                notice = pending_delivery_notice(include_acknowledged=True)
                if notice:
                    status = self.label(f"{notice['date']} 的完整汇报尚未就绪；下面是最近一期。", 'error', wrap=True)
                    status.set_margin_bottom(12)
                    outer.pack_start(status, False, False, 0)
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
            card.get_style_context().add_class('paper')
            chinese_title = str(item['data'].get('title_zh') or '').strip()
            original_title = item['data']['title']
            paper_title = self.label(chinese_title or original_title, 'paper-title', wrap=True)
            paper_title.set_max_width_chars(44)
            card.pack_start(paper_title, False, False, 0)
            if chinese_title and chinese_title != original_title:
                original = self.label(original_title, 'paper-title-original', wrap=True)
                original.set_max_width_chars(58)
                card.pack_start(original, False, False, 0)
            authors = [library.author_name(x) for x in item['data'].get('authors', [])]
            author_text = '、'.join(authors[:2]) + (' 等' if len(authors) > 2 else '')
            card.pack_start(self.label(f"{author_text}  ·  {item['data'].get('year', '')}", 'meta', wrap=True), False, False, 0)
            assets = Gtk.Box(spacing=8)
            secs = round(item['data']['verification']['video_duration_seconds'])
            for text in [f'中文讲解  {secs // 60:02d}:{secs % 60:02d}', '可编辑 PPT', 'PDF · 精读 · 背景']:
                assets.pack_start(self.label(text, 'asset'), False, False, 0)
            card.pack_start(assets, False, False, 0)
            outer.pack_start(card, False, False, 0)
            location = self.label('请打开论文目录查看，或直接播放讲解。', 'location')
            location.set_margin_top(19)
            outer.pack_start(location, False, False, 0)
            path = self.label(str(item['base']), 'meta', wrap=True)
            path.set_max_width_chars(68); path.set_selectable(True); path.set_margin_top(5)
            path.set_can_focus(False)
            outer.pack_start(path, False, False, 0)
            actions = Gtk.Box(spacing=12); actions.set_margin_top(24)
            for text, kind, css in [('▶  播放讲解', 'video', 'primary'), ('打开论文目录', 'folder', 'secondary'), ('查看 PPT', 'slides', 'secondary')]:
                button = Gtk.Button(label=text); button.get_style_context().add_class(css)
                button.connect('clicked', lambda _button, action=kind: self.open(action))
                actions.pack_start(button, kind == 'video', kind == 'video', 0)
            outer.pack_start(actions, False, False, 0)
            research = Gtk.Box(spacing=12); research.set_margin_top(10)
            self.research_buttons = []
            for text, kind in [('阅读本篇笔记', 'obsidian'), ('浏览器版笔记', 'notes'), ('选文工作台', 'workbench'), ('检索已读笔记', 'notes_search')]:
                button = Gtk.Button(label=text); button.get_style_context().add_class('secondary')
                button.connect('clicked', lambda _button, action=kind: self.open_research(action))
                if kind == 'obsidian':
                    button.set_tooltip_text('在 Obsidian 的论文日报知识库中打开本篇笔记')
                if kind == 'notes' and not item['data']['artifacts'].get('notes_html'):
                    button.set_sensitive(False); button.set_tooltip_text('这一篇还没有独立笔记')
                if kind == 'obsidian' and not item['data']['artifacts'].get('notes'):
                    button.set_sensitive(False); button.set_tooltip_text('这一篇还没有Markdown笔记')
                self.research_buttons.append(button)
                research.pack_start(button, True, True, 0)
            outer.pack_start(research, False, False, 0)
            journal = Gtk.Button(label='写一句总结 · 可直接保存')
            journal.get_style_context().add_class('secondary'); journal.set_margin_top(10)
            journal.connect('clicked', lambda *_: self.open_research('journal'))
            self.research_buttons.append(journal)
            outer.pack_start(journal, False, False, 0)
            self.error = self.label('', 'error', wrap=True); self.error.set_margin_top(8)
            outer.pack_start(self.error, False, False, 0)
            foot = Gtk.Box(); foot.set_margin_top(10)
            foot.pack_start(self.label('一次一篇，慢慢读懂。', 'footer'), True, True, 0)
            close = Gtk.Button(label='关闭窗口' if args.open else '我知道了，关闭提醒'); close.get_style_context().add_class('quiet')
            close.connect('clicked', lambda *_: self.close('dismissed'))
            foot.pack_end(close, False, False, 0); outer.pack_start(foot, False, False, 0)

        @staticmethod
        def label(text, css, wrap=False):
            label = Gtk.Label(label=text, xalign=0)
            label.get_style_context().add_class(css)
            label.set_line_wrap(wrap); label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            return label

        def keypress(self, _win, event):
            if event.keyval == Gdk.KEY_Escape:
                return self.close('dismissed')
            return False

        def show(self):
            if self.win.get_visible():
                return
            self.win.show_all()
            self.win.present()
            if not read_only:
                save_state(self.item, status='displayed', displayed_at=library.timestamp())
            if not self.was_shown:
                self.was_shown = True
                print(json.dumps({'status': 'displayed', 'date': self.item['date'], 'paper_id': self.item['data']['id'], 'preview': args.preview, 'manual': args.open}), flush=True)
                if args.screenshot:
                    GLib.timeout_add(700, self.capture)
                if args.auto_close:
                    GLib.timeout_add_seconds(args.auto_close, lambda: self.close('preview_timeout'))

        def capture(self):
            surface = self.win.get_window()
            pixbuf = Gdk.pixbuf_get_from_window(surface, 0, 0, self.win.get_allocated_width(), self.win.get_allocated_height())
            Path(args.screenshot).parent.mkdir(parents=True, exist_ok=True)
            pixbuf.savev(str(Path(args.screenshot).resolve()), 'png', [], [])
            monitor = self.win.get_display().get_monitor_at_window(surface)
            area = monitor.get_workarea()
            x, y = self.win.get_position(); w, h = self.win.get_size()
            print(json.dumps({'screenshot': args.screenshot, 'window': [x, y, w, h], 'workarea': [area.x, area.y, area.width, area.height]}), flush=True)
            return False

        def open(self, kind):
            try:
                target = self.item['base'] if kind == 'folder' else library.inside(self.item['base'], self.item['data']['artifacts'][kind])
                if not target.exists():
                    raise FileNotFoundError('文件已移动，请检查论文目录。')
                # Gio reports immediate launch errors and uses the desktop's registered handler.
                Gio.AppInfo.launch_default_for_uri(target.as_uri(), self.win.get_display().get_app_launch_context())
            except Exception as exc:
                self.error.set_text('暂时无法打开：' + str(exc))
                return
            # Content actions can be used in sequence; only an explicit close acknowledges.
            self.error.set_text('')

        def open_research(self, kind):
            # Starting the local service must not freeze GTK or stop the user closing it.
            enabled = [button.get_sensitive() for button in self.research_buttons]
            for button in self.research_buttons:
                button.set_sensitive(False)
            self.error.set_text('')

            def finished(error):
                if not self.handled:
                    for button, sensitive in zip(self.research_buttons, enabled):
                        button.set_sensitive(sensitive)
                    self.error.set_text('暂时无法打开：' + error if error else '')
                return False

            def launch():
                try:
                    open_research_action(self.item, kind)
                    error = ''
                except Exception as exc:
                    error = str(exc)
                GLib.idle_add(finished, error)

            threading.Thread(target=launch, daemon=True).start()

        def close(self, action):
            if self.handled:
                return True
            if not read_only:
                try:
                    save_state(self.item, status='acknowledged', acknowledged_at=library.timestamp(), action=action)
                except OSError as exc:
                    # Never trap the user in an unclosable topmost window.
                    print('Could not persist dismissal; retry after restart: ' + str(exc), flush=True)
                session_dismissed.add((self.item['date'], self.item['data']['id']))
            self.handled = True
            self.win.destroy(); self.on_close()
            return True

    class DeliveryNotice:
        def __init__(self, item, on_close):
            self.item, self.on_close = item, on_close
            self.handled = False
            self.was_shown = False
            self.win = Gtk.Window(title='论文日报 · 今日进度提醒')
            self.win.set_icon_name('paper-daily-ai')
            self.win.set_default_size(620, -1)
            self.win.set_resizable(False)
            self.win.set_position(Gtk.WindowPosition.CENTER)
            self.win.set_keep_above(not args.open)
            header = Gtk.HeaderBar(show_close_button=True)
            header.set_title('论文日报 · 今日进度')
            self.win.set_titlebar(header)
            self.win.connect('delete-event', lambda *_: self.close())
            self.win.connect('key-press-event', self.keypress)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
            for side in ('top', 'bottom', 'start', 'end'):
                getattr(body, 'set_margin_' + side)(28)
            self.win.add(body)
            for text, css in [
                ('PAPER DAILY  /  ' + item['date'], 'eyebrow'),
                ('今日汇报尚未就绪', 'headline'),
                (f"已到 {item['after']}（{item['timezone']}），还未发现今天通过完整验收的论文汇报。", 'greeting'),
                ('可能仍在制作，也可能尚未启动或执行中断。此提醒只检查本地成果，不能判断具体原因。', 'location'),
                ('你可以继续阅读已有材料。今天的汇报完成后，仍会单独提醒。', 'location'),
            ]:
                label = Reminder.label(text, css, wrap=True)
                label.set_max_width_chars(46)
                body.pack_start(label, False, False, 0)
            actions = Gtk.Box(spacing=12)
            for text, kind in [('打开阅读中控', 'control'), ('打开论文库', 'folder')]:
                button = Gtk.Button(label=text)
                button.get_style_context().add_class('secondary')
                button.connect('clicked', lambda _b, action=kind: self.open(action))
                actions.pack_start(button, True, True, 0)
            body.pack_start(actions, False, False, 0)
            self.error = Reminder.label('', 'error', wrap=True)
            body.pack_start(self.error, False, False, 0)
            close = Gtk.Button(label='关闭窗口' if args.open else '我知道了，关闭提醒')
            close.get_style_context().add_class('quiet')
            close.connect('clicked', lambda *_: self.close())
            body.pack_start(close, False, False, 0)

        def keypress(self, _win, event):
            if event.keyval == Gdk.KEY_Escape:
                return self.close()
            return False

        def open(self, kind):
            try:
                if kind == 'control':
                    subprocess.Popen(['/usr/bin/python3', str(ROOT / 'scripts/paper_notify.py'), '--open'],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
                else:
                    Gio.AppInfo.launch_default_for_uri(ROOT.as_uri(), self.win.get_display().get_app_launch_context())
                self.error.set_text('')
            except Exception as exc:
                self.error.set_text('暂时无法打开：' + str(exc))

        def show(self):
            if self.win.get_visible():
                return
            self.win.show_all()
            self.win.present()
            if not read_only:
                save_delivery_state(self.item, status='displayed', displayed_at=library.timestamp())
            if not self.was_shown:
                self.was_shown = True
                print(json.dumps({'status': 'delivery_notice_displayed', 'date': self.item['date'],
                                  'manual': args.open}), flush=True)

        def retire(self):
            """Expired/resolved notices never acknowledge or hide a completed paper."""
            self.handled = True
            self.win.destroy()

        def close(self):
            if self.handled:
                return True
            if not read_only:
                try:
                    save_delivery_state(self.item, status='acknowledged', acknowledged_at=library.timestamp())
                except OSError as exc:
                    print('Could not persist delivery notice dismissal: ' + str(exc), flush=True)
                session_dismissed.add(('delivery_delay', self.item['date']))
            self.retire()
            self.on_close()
            return True

    window = [None]
    def on_close():
        window[0] = None
        if args.open:
            application.quit()
        elif args.preview:
            Gtk.main_quit()
        else:
            GLib.timeout_add_seconds(2, lambda: (scan(), False)[1])

    def scan():
        try:
            is_locked = locked()
            if is_locked is not False:
                if window[0]:
                    window[0].win.hide()
                return True
            if isinstance(window[0], DeliveryNotice):
                current = pending_delivery_notice(include_acknowledged=True)
                if current is None or current['date'] != window[0].item['date']:
                    window[0].retire()
                    window[0] = None
            if window[0]:
                window[0].show()
                return True
            pending = pending_papers(date=args.date if args.preview else None,
                                     include_acknowledged=args.preview)
            pending = [i for i in pending if (i['date'], i['data']['id']) not in session_dismissed]
            if pending:
                window[0] = Reminder(pending[0], on_close)
                window[0].show()
            elif args.preview:
                print('No complete paper available for preview.', flush=True)
                Gtk.main_quit()
            else:
                notice = pending_delivery_notice()
                if notice and ('delivery_delay', notice['date']) not in session_dismissed:
                    window[0] = DeliveryNotice(notice, on_close)
                    window[0].show()
        except Exception as exc:
            print('Notification check failed: ' + str(exc), flush=True)
        return True

    if args.open:
        # A separate, single-instance viewer. It never owns the watcher lock,
        # changes notification receipts, or stops the background reminder.
        application = Gtk.Application(application_id=APP_ID)

        def activate(app):
            existing = app.get_windows()
            if existing:
                existing[0].present()
                return
            papers = available_papers()
            if papers:
                window[0] = Reminder(papers[0], on_close)
                app.add_window(window[0].win)
                window[0].show()
            elif (notice := pending_delivery_notice(include_acknowledged=True)):
                window[0] = DeliveryNotice(notice, on_close)
                app.add_window(window[0].win)
                window[0].show()
            else:
                empty = Gtk.MessageDialog(
                    message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.CLOSE,
                    text='暂无已就绪的论文')
                empty.set_title('论文日报 · 阅读中控')
                empty.set_icon_name('paper-daily-ai')
                empty.set_position(Gtk.WindowPosition.CENTER)
                empty.format_secondary_text('论文完成并通过验收后，会自动弹窗提醒。你也可以稍后从应用列表再次打开这里。')
                empty.connect('response', lambda *_: app.quit())
                app.add_window(empty)
                empty.show_all()

        application.connect('activate', activate)
        return application.run([sys.argv[0]])

    # Periodic check recovers missed filesystem events; it never calls a model.
    GLib.timeout_add_seconds(30, scan)
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    connection.signal_subscribe('org.gnome.ScreenSaver', 'org.gnome.ScreenSaver',
                                'ActiveChanged', '/org/gnome/ScreenSaver', None,
                                Gio.DBusSignalFlags.NONE, lambda *_: scan())
    GLib.idle_add(lambda: (scan(), False)[1])
    Gtk.main()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--watch', action='store_true')
    mode.add_argument('--preview', action='store_true')
    mode.add_argument('--open', action='store_true', help='Open the latest report without changing reminder state')
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--stop', action='store_true')
    parser.add_argument('--date', type=library.valid_day)
    parser.add_argument('--screenshot')
    parser.add_argument('--auto-close', type=int, default=0)
    args = parser.parse_args()
    if args.stop:
        p = ROOT / '.notifications' / 'daemon.json'
        if p.exists():
            pid = library.read_json(p)['pid']
            proc = Path(f'/proc/{pid}/cmdline')
            command = proc.read_bytes().split(b'\0') if proc.exists() else []
            if str(Path(__file__).resolve()).encode() in command and b'--watch' in command:
                os.kill(pid, signal.SIGTERM)
                print('Notifier stopped; pending reminders preserved.'); return 0
        print('Notifier is not running.'); return 0
    if (args.screenshot or args.auto_close) and not args.preview:
        parser.error('screenshot/auto-close are preview-only; real reminders stay visible')
    if args.check:
        rows = pending_papers(date=args.date)
        print(json.dumps([{'date': i['date'], 'paper_id': i['data']['id']} for i in rows], ensure_ascii=False))
        return 0
    if not args.watch and not args.preview and not args.open:
        result = start_background()
        print(json.dumps(result))
        return 0
    directory = ROOT / '.notifications'; directory.mkdir(exist_ok=True)
    lock = None
    if args.watch:
        lock = (directory / 'daemon.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Notifier already running.'); return 0
        library.atomic_json(directory / 'daemon.json', {'pid': os.getpid(), 'started_at': library.timestamp()})
    return run_gui(args)


if __name__ == '__main__':
    raise SystemExit(main())
