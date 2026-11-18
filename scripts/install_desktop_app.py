#!/usr/bin/python3
"""Install this library's application launcher and icons for the current user.

Autostart is opt-in. Does not modify notification receipts or the AI host's schedule.
"""
from pathlib import Path
import argparse
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent.parent
APP_ID = 'io.github.paperdaily.Control'


def install(autostart=False):
    import gi
    gi.require_version('GdkPixbuf', '2.0')
    from gi.repository import GdkPixbuf

    data_home = Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local/share')
    apps = data_home/'applications'
    icons = data_home/'icons/hicolor'
    entry = apps/f'{APP_ID}.desktop'
    config_home = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')
    startup = config_home/'autostart/paper-daily-ai.desktop'
    script = str(ROOT/'scripts/paper_notify.py')
    for existing in ([entry, startup] if autostart else [entry]):
        if existing.exists() and script not in existing.read_text():
            raise FileExistsError(f'已有另一目录的Paper Daily安装，请先备份并移除对应入口：{existing}')
    apps.mkdir(parents=True, exist_ok=True)
    source = ROOT/'assets/paper-daily-ai.svg'
    vector = icons/'scalable/apps/paper-daily-ai.svg'
    vector.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, vector)
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        target = icons/f'{size}x{size}/apps/paper-daily-ai.png'
        target.parent.mkdir(parents=True, exist_ok=True)
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(source), size, size, True)
        pixbuf.savev(str(target), 'png', [], [])

    # This application's canonical path has no desktop Exec escape characters.
    if any(char in script for char in '\\"`$%\n\r'):
        raise ValueError('The launcher path contains unsupported Exec characters.')
    entry = apps/f'{APP_ID}.desktop'
    entry.write_text(f'''[Desktop Entry]
Type=Application
Version=1.0
Name=论文日报 · 阅读中控
Name[en]=Paper Daily · Reading Center
GenericName=论文阅读助手
Comment=回看最近一期论文的讲解、课件、笔记与每日一句
Exec=/usr/bin/python3 "{script}" --open
TryExec=/usr/bin/python3
Terminal=false
Icon=paper-daily-ai
Categories=Education;
Keywords=论文;日报;阅读;中控;paper;daily;research;Obsidian;
StartupNotify=true
StartupWMClass=paper-daily-ai
X-PaperDaily-Managed=true
''')
    entry.chmod(0o644)
    if autostart:
        startup.parent.mkdir(parents=True, exist_ok=True)
        startup.write_text(f'''[Desktop Entry]
Type=Application
Name=Paper Daily reminders
Comment=Observe completed local paper reports after desktop login
Exec=/usr/bin/python3 "{script}" --watch
TryExec=/usr/bin/python3
Terminal=false
Icon=paper-daily-ai
StartupNotify=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=8
''')
    if shutil.which('desktop-file-validate'):
        subprocess.run(['desktop-file-validate', str(entry)], check=True)
    for command in (['update-desktop-database', str(apps)],
                    ['gtk-update-icon-cache', '-f', '-t', str(icons)]):
        if shutil.which(command[0]):
            subprocess.run(command, check=True)
    print(entry)
    return entry


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--autostart', action='store_true', help='Also enable the local reminder watcher at desktop login')
    install(parser.parse_args().autostart)
