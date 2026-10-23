import sys
import os
import re
import json
import time
import ctypes
import logging
import webbrowser
from datetime import datetime
from logging.handlers import RotatingFileHandler

import requests
from PyQt5.QtWidgets import (QApplication, QWidget, QFrame, QLabel, QPushButton,
                             QVBoxLayout, QHBoxLayout, QSystemTrayIcon,
                             QMenu, QAction)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal, QEvent, QLockFile
from PyQt5.QtGui import QPixmap, QIcon, QImageReader

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import winreg

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
APP_NAME = "BingWallpaperClone"
BASE_URL = "https://www.bing.com"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_NAME)
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
CACHE_PATH = os.path.join(DATA_DIR, "cache.json")
LOG_PATH = os.path.join(DATA_DIR, "app.log")
LOCK_PATH = os.path.join(DATA_DIR, "app.lock")

DEFAULT_SETTINGS = {
    "market": "en-US",              # e.g. de-DE, ja-JP, fr-FR (each has its own images)
    "check_interval_minutes": 120,
    "uhd": True,                    # try the 3840x2160 version first
    "keep_last": 30,                # 0 = never delete old wallpapers
    "save_dir": os.path.join(os.path.expanduser("~"), "Pictures", "BingWallpapers"),
}

MARKET_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
# Only files we created are ever eligible for cleanup.
OWN_FILE_RE = re.compile(r"^\d{8}_[a-z]{2}-[A-Z]{2}\.jpg$")

log = logging.getLogger(APP_NAME)


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        if isinstance(user, dict):
            settings.update({k: v for k, v in user.items() if k in DEFAULT_SETTINGS})
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Could not read settings, using defaults: %s", e)

    if not MARKET_RE.match(str(settings["market"])):
        settings["market"] = DEFAULT_SETTINGS["market"]
    try:
        settings["check_interval_minutes"] = max(15, int(settings["check_interval_minutes"]))
        settings["keep_last"] = max(0, int(settings["keep_last"]))
    except (TypeError, ValueError):
        settings["check_interval_minutes"] = DEFAULT_SETTINGS["check_interval_minutes"]
        settings["keep_last"] = DEFAULT_SETTINGS["keep_last"]
    settings["save_dir"] = os.path.expanduser(str(settings["save_dir"]))
    return settings


CONFIG = load_settings()
SAVE_DIR = CONFIG["save_dir"]


def setup_logging():
    os.makedirs(DATA_DIR, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=256_000, backupCount=1,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.setLevel(logging.INFO)
    log.addHandler(handler)

    def excepthook(exc_type, exc, tb):
        log.error("Uncaught exception", exc_info=(exc_type, exc, tb))
    sys.excepthook = excepthook


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(SAVE_DIR, exist_ok=True)
    if not os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_SETTINGS, f, indent=2)
        except OSError as e:
            log.warning("Could not write default settings: %s", e)


def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


# ==========================================
# WINDOWS INTEGRATION (startup, wallpaper)
# ==========================================
def autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def is_autostart_enabled():
    if not IS_WINDOWS:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    if not IS_WINDOWS:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def set_wallpaper_fit_fill():
    """WallpaperStyle 10 = Fill, TileWallpaper 0 = no tiling."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "WallpaperStyle", 0, winreg.REG_SZ, "10")
            winreg.SetValueEx(key, "TileWallpaper", 0, winreg.REG_SZ, "0")
    except OSError as e:
        log.warning("Could not set wallpaper fit: %s", e)


# ==========================================
# WALLPAPER MANAGER (LOGIC LAYER)
# ==========================================
class WallpaperManager:
    """Handles fetching, downloading, caching and setting wallpapers."""

    session = requests.Session()
    session.headers.update({"User-Agent": f"{APP_NAME}/2.0"})

    @staticmethod
    def _api_url():
        return (f"{BASE_URL}/HPImageArchive.aspx?format=js&idx=0&n=8"
                f"&mkt={CONFIG['market']}")

    @staticmethod
    def _full_link(link):
        if not link:
            return ""
        if link.startswith("/"):
            link = BASE_URL + link
        return link if link.startswith(("http://", "https://")) else ""

    @staticmethod
    def _save_cache(items):
        try:
            tmp = CACHE_PATH + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f)
            os.replace(tmp, CACHE_PATH)
        except OSError as e:
            log.warning("Could not save cache: %s", e)

    @staticmethod
    def _load_cache():
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                items = json.load(f)
            market = CONFIG["market"]
            return [i for i in items if i.get("market") == market]
        except Exception:
            return []

    @staticmethod
    def get_wallpaper_data():
        """Fetches recent wallpaper metadata. Falls back to the cache offline."""
        try:
            response = WallpaperManager.session.get(
                WallpaperManager._api_url(), timeout=10)
            response.raise_for_status()
            images = response.json().get("images", [])

            items = []
            for img in images:
                date_str = img["startdate"]
                market = CONFIG["market"]
                path = os.path.join(SAVE_DIR, f"{date_str}_{market}.jpg")

                candidates = []
                urlbase = img.get("urlbase")
                if CONFIG["uhd"] and urlbase:
                    candidates.append(BASE_URL + urlbase + "_UHD.jpg")
                candidates.append(BASE_URL + img["url"])

                copyright_text = img.get("copyright", "")
                title = img.get("title") or copyright_text.split("(")[0].strip() \
                    or "Bing Wallpaper"
                items.append({
                    "date": date_str,
                    "market": market,
                    "path": path,
                    "urls": candidates,
                    "title": title,
                    "copyright": copyright_text,
                    "link": WallpaperManager._full_link(img.get("copyrightlink", "")),
                })
            if items:
                WallpaperManager._save_cache(items)
            return items
        except Exception as e:
            log.warning("Error fetching data, using cache: %s", e)
            return WallpaperManager._load_cache()

    @staticmethod
    def download_image(item):
        """Downloads the image if missing. Atomic write, tries UHD then 1080p."""
        filepath = item["path"]
        if os.path.exists(filepath):
            return True

        for url in item["urls"]:
            tmp = filepath + ".part"
            try:
                r = WallpaperManager.session.get(url, timeout=30)
                r.raise_for_status()
                if not r.headers.get("Content-Type", "").startswith("image/"):
                    raise ValueError(f"not an image: {r.headers.get('Content-Type')}")
                with open(tmp, "wb") as f:
                    f.write(r.content)
                os.replace(tmp, filepath)
                return True
            except Exception as e:
                log.warning("Error downloading %s: %s", url, e)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return False

    @staticmethod
    def cleanup_old(keep_last, protect_paths):
        """Deletes the oldest wallpapers we created, never touching other files."""
        if keep_last <= 0:
            return
        try:
            files = sorted((f for f in os.listdir(SAVE_DIR) if OWN_FILE_RE.match(f)),
                           reverse=True)
        except OSError:
            return
        protected = {os.path.normcase(os.path.abspath(p)) for p in protect_paths}
        for name in files[keep_last:]:
            path = os.path.join(SAVE_DIR, name)
            if os.path.normcase(os.path.abspath(path)) in protected:
                continue
            try:
                os.remove(path)
            except OSError as e:
                log.warning("Could not delete %s: %s", path, e)

    @staticmethod
    def set_wallpaper_to_system(filepath):
        """Sets the desktop wallpaper using Windows API."""
        if not IS_WINDOWS or not os.path.exists(filepath):
            return
        absolute_path = os.path.abspath(filepath)
        set_wallpaper_fit_fill()
        # SPI_SETDESKWALLPAPER = 20, SPIF_UPDATEINIFILE | SPIF_SENDWININICHANGE = 3
        ok = ctypes.windll.user32.SystemParametersInfoW(20, 0, absolute_path, 3)
        if not ok:
            log.warning("SystemParametersInfoW failed for %s", absolute_path)


# ==========================================
# WORKER THREAD
# ==========================================
class UpdateThread(QThread):
    """Fetches metadata and downloads images off the GUI thread."""
    result_ready = pyqtSignal(list)

    def run(self):
        try:
            items = WallpaperManager.get_wallpaper_data()
            for item in items:
                WallpaperManager.download_image(item)
            valid = [i for i in items if os.path.exists(i["path"])]
            WallpaperManager.cleanup_old(CONFIG["keep_last"],
                                         [i["path"] for i in items])
        except Exception:
            log.exception("Update failed")
            valid = []
        self.result_ready.emit(valid)


# ==========================================
# MINI GUI WINDOW (BOTTOM RIGHT)
# ==========================================
class MiniPreviewWindow(QWidget):
    PREVIEW_W, PREVIEW_H = 330, 180

    def __init__(self):
        super().__init__()
        self.wallpapers = []
        self.current_index = 0
        self.hidden_at = 0.0
        self.init_ui()

    def init_ui(self):
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(350, 300)

        # Style only the card, not every QWidget (avoids rounded boxes on children).
        self.setStyleSheet("""
            #card { background-color: #2D2D2D; border-radius: 10px; }
            QLabel { color: white; font-size: 12px; background: transparent; }
            #subtitle { color: #AAAAAA; font-size: 10px; }
            #preview { border: 1px solid #555; }
            QPushButton { background-color: #444; color: white; border: none;
                          padding: 5px; border-radius: 5px; }
            QPushButton:hover { background-color: #666; }
            QPushButton:disabled { color: #777; }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("card")
        outer.addWidget(card)
        layout = QVBoxLayout(card)

        self.lbl_title = QLabel("Bing Wallpaper")
        self.lbl_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_title)

        self.lbl_image = QLabel()
        self.lbl_image.setObjectName("preview")
        self.lbl_image.setFixedSize(self.PREVIEW_W, self.PREVIEW_H)
        self.lbl_image.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_image)

        self.lbl_sub = QLabel("")
        self.lbl_sub.setObjectName("subtitle")
        self.lbl_sub.setAlignment(Qt.AlignCenter)
        self.lbl_sub.setWordWrap(True)
        layout.addWidget(self.lbl_sub)

        btn_layout = QHBoxLayout()
        self.btn_prev = QPushButton("<")
        self.btn_prev.clicked.connect(self.show_prev)
        self.lbl_date = QLabel("")
        self.lbl_date.setAlignment(Qt.AlignCenter)
        self.btn_info = QPushButton("i")
        self.btn_info.setFixedWidth(28)
        self.btn_info.setToolTip("Open on Bing")
        self.btn_info.clicked.connect(self.open_info)
        self.btn_next = QPushButton(">")
        self.btn_next.clicked.connect(self.show_next)

        btn_layout.addWidget(self.btn_prev)
        btn_layout.addWidget(self.lbl_date, 1)
        btn_layout.addWidget(self.btn_info)
        btn_layout.addWidget(self.btn_next)
        layout.addLayout(btn_layout)

    # ---- data ----
    def update_data(self, wallpapers):
        """Applies a fresh list. Only changes the desktop when a new image arrived,
        so a manually chosen wallpaper isn't overwritten by the periodic refresh."""
        if not wallpapers:
            return
        has_new = (not self.wallpapers or
                   wallpapers[0]["path"] != self.wallpapers[0]["path"])
        self.wallpapers = wallpapers
        if has_new:
            self.current_index = 0
            WallpaperManager.set_wallpaper_to_system(wallpapers[0]["path"])
        else:
            self.current_index = min(self.current_index, len(wallpapers) - 1)
        self.refresh_view()

    def _load_preview(self, path):
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if not size.isValid():
            return None
        size.scale(self.PREVIEW_W, self.PREVIEW_H, Qt.KeepAspectRatio)
        reader.setScaledSize(size)
        image = reader.read()
        return None if image.isNull() else QPixmap.fromImage(image)

    def refresh_view(self):
        if not self.wallpapers:
            return
        data = self.wallpapers[self.current_index]

        pixmap = self._load_preview(data["path"])
        if pixmap:
            self.lbl_image.setPixmap(pixmap)
        else:
            self.lbl_image.setText("Preview unavailable")

        title = data["title"]
        if len(title) > 40:
            title = title[:37] + "..."
        self.lbl_title.setText(title)
        self.lbl_sub.setText(data.get("copyright", ""))

        try:
            pretty = datetime.strptime(data["date"], "%Y%m%d").strftime("%b %d, %Y")
        except ValueError:
            pretty = data["date"]
        self.lbl_date.setText(("Latest - " if self.current_index == 0 else "") + pretty)

        self.btn_prev.setEnabled(self.current_index < len(self.wallpapers) - 1)
        self.btn_next.setEnabled(self.current_index > 0)
        self.btn_info.setEnabled(bool(data.get("link")))

    def _apply_current(self):
        self.refresh_view()
        WallpaperManager.set_wallpaper_to_system(
            self.wallpapers[self.current_index]["path"])

    def show_prev(self):
        if self.wallpapers and self.current_index < len(self.wallpapers) - 1:
            self.current_index += 1
            self._apply_current()

    def show_next(self):
        if self.wallpapers and self.current_index > 0:
            self.current_index -= 1
            self._apply_current()

    def open_info(self):
        if self.wallpapers:
            link = self.wallpapers[self.current_index].get("link")
            if link:
                webbrowser.open(link)

    # ---- window behavior ----
    def show_at_corner(self):
        geo = QApplication.primaryScreen().availableGeometry()
        x = geo.right() - self.width() - 20
        y = geo.bottom() - self.height() - 20
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()

    def changeEvent(self, event):
        """Hide when the window loses focus (user clicked outside)."""
        if event.type() == QEvent.ActivationChange and not self.isActiveWindow():
            if self.isVisible():
                self.hidden_at = time.monotonic()
                self.hide()
        super().changeEvent(event)


# ==========================================
# MAIN APP CONTROLLER (SYSTEM TRAY)
# ==========================================
class BingTrayApp:
    def __init__(self):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.mini_window = MiniPreviewWindow()
        self.update_thread = None

        self.tray_icon = QSystemTrayIcon()
        self.tray_icon.setIcon(QIcon(resource_path("icon.ico")))
        self.tray_icon.setVisible(True)
        self.tray_icon.setToolTip("Bing Wallpaper Clone")

        self.menu = QMenu()

        action_update = QAction("Update Now", self.menu)
        action_update.triggered.connect(self.start_update_thread)
        self.menu.addAction(action_update)

        action_folder = QAction("Open wallpaper folder", self.menu)
        action_folder.triggered.connect(self.open_folder)
        self.menu.addAction(action_folder)

        self.action_startup = QAction("Run at startup", self.menu)
        self.action_startup.setCheckable(True)
        self.action_startup.setChecked(is_autostart_enabled())
        self.action_startup.toggled.connect(self.on_toggle_startup)
        self.action_startup.setEnabled(IS_WINDOWS)
        self.menu.addAction(self.action_startup)

        self.menu.addSeparator()

        action_exit = QAction("Exit", self.menu)
        action_exit.triggered.connect(self.app.quit)
        self.menu.addAction(action_exit)

        self.tray_icon.setContextMenu(self.menu)
        self.tray_icon.activated.connect(self.on_tray_click)

        self.timer = QTimer()
        self.timer.timeout.connect(self.start_update_thread)
        self.timer.start(CONFIG["check_interval_minutes"] * 60 * 1000)

        self.start_update_thread()

    def on_tray_click(self, reason):
        if reason != QSystemTrayIcon.Trigger:
            return
        if self.mini_window.isVisible():
            self.mini_window.hide()
        # Clicking the tray icon first steals focus (which hides the window), then
        # the click arrives. Without this guard the window would instantly reopen.
        elif time.monotonic() - self.mini_window.hidden_at > 0.35:
            self.mini_window.show_at_corner()

    def on_toggle_startup(self, enabled):
        try:
            set_autostart(enabled)
        except OSError as e:
            log.error("Could not change startup setting: %s", e)
            self.action_startup.blockSignals(True)
            self.action_startup.setChecked(is_autostart_enabled())
            self.action_startup.blockSignals(False)

    def open_folder(self):
        if IS_WINDOWS and os.path.isdir(SAVE_DIR):
            os.startfile(SAVE_DIR)

    def start_update_thread(self):
        if self.update_thread is not None and self.update_thread.isRunning():
            return  # an update is already in progress
        self.update_thread = UpdateThread()
        self.update_thread.result_ready.connect(self.on_update_finished)
        self.update_thread.start()

    def on_update_finished(self, wallpapers):
        self.mini_window.update_data(wallpapers)

    def run(self):
        sys.exit(self.app.exec_())


# ==========================================
# ENTRY POINT
# ==========================================
def main():
    print(f"{APP_NAME} starting up...")
    setup_logging()
    print("logging initialized")
    ensure_dirs()
    print(f"data dir: {DATA_DIR}")

    # Single instance: a second launch exits quietly instead of adding a 2nd tray icon.
    lock = QLockFile(LOCK_PATH)
    if not lock.tryLock(100):
        sys.exit(0)

    try:
        app_instance = BingTrayApp()
        app_instance.run()
    except KeyboardInterrupt:
        sys.exit()
    finally:
        lock.unlock()


if __name__ == "__main__":
    main()