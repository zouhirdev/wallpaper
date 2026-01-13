import sys
import os
import requests
import ctypes
import threading
from PyQt5.QtWidgets import (QApplication, QWidget, QLabel, QPushButton, 
                             QVBoxLayout, QHBoxLayout, QSystemTrayIcon, 
                             QMenu, QAction, QDesktopWidget)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QEvent
from PyQt5.QtGui import QPixmap, QIcon

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
BING_API_URL = "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=7&mkt=en-US"
BASE_URL = "https://www.bing.com"
SAVE_DIR = os.path.join(os.path.expanduser("~"), "Pictures", "BingWallpapers")
CHECK_INTERVAL_MS = 2 * 60 * 60 * 1000  # 2 Hours

if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)


def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)



# ==========================================
# WALLPAPER MANAGER (LOGIC LAYER)
# ==========================================
class WallpaperManager:
    """Handles fetching, downloading, and setting wallpapers."""
    
    @staticmethod
    def get_wallpaper_data():
        """Fetches the last 7 days of wallpaper data from Bing."""
        try:
            response = requests.get(BING_API_URL, timeout=10)
            response.raise_for_status()
            data = response.json()
            images = data.get('images', [])
            
            processed_list = []
            for img in images:
                url = BASE_URL + img['url']
                date_str = img['startdate']
                filename = f"{date_str}.jpg"
                filepath = os.path.join(SAVE_DIR, filename)
                
                processed_list.append({
                    'url': url,
                    'path': filepath,
                    'title': img.get('copyright', 'Bing Wallpaper')
                })
            return processed_list
        except Exception as e:
            print(f"Error fetching data: {e}")
            return []

    @staticmethod
    def download_image(url, filepath):
        """Downloads image if it doesn't exist."""
        if not os.path.exists(filepath):
            try:
                img_data = requests.get(url, timeout=20).content
                with open(filepath, 'wb') as handler:
                    handler.write(img_data)
                return True
            except Exception as e:
                print(f"Error downloading {url}: {e}")
                return False
        return True

    @staticmethod
    def set_wallpaper_to_system(filepath):
        """Sets the desktop wallpaper using Windows API."""
        if os.path.exists(filepath):
            absolute_path = os.path.abspath(filepath)
            # SPI_SETDESKWALLPAPER = 20, SPIF_UPDATEINIFILE = 0x01, SPIF_SENDWININICHANGE = 0x02
            ctypes.windll.user32.SystemParametersInfoW(20, 0, absolute_path, 3)

# ==========================================
# WORKER THREAD (MULTITHREADING)
# ==========================================
class UpdateWorker(QObject):
    """Background worker to download images without freezing GUI."""
    finished = pyqtSignal(list) # Emits list of wallpaper dicts

    def run(self):
        wallpapers = WallpaperManager.get_wallpaper_data()
        # Download all images in the list
        for item in wallpapers:
            WallpaperManager.download_image(item['url'], item['path'])
        
        # Determine valid downloaded wallpapers to send back
        valid_wallpapers = [w for w in wallpapers if os.path.exists(w['path'])]
        self.finished.emit(valid_wallpapers)

# ==========================================
# MINI GUI WINDOW (BOTTOM RIGHT)
# ==========================================
class MiniPreviewWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.wallpapers = []
        self.current_index = 0
        self.init_ui()

    def init_ui(self):
        # Qt.Tool makes it act like a palette window (doesn't show in taskbar)
        # Frameless removes the title bar
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(350, 250)
        
        self.setStyleSheet("""
            QWidget { background-color: #2D2D2D; color: white; border-radius: 10px; }
            QPushButton { background-color: #444; border: none; padding: 5px; border-radius: 5px; }
            QPushButton:hover { background-color: #666; }
            QLabel { font-size: 12px; }
        """)

        layout = QVBoxLayout()
        
        # Title
        self.lbl_title = QLabel("Bing Wallpaper")
        self.lbl_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_title)

        # Image Preview
        self.lbl_image = QLabel()
        self.lbl_image.setFixedSize(330, 180)
        self.lbl_image.setScaledContents(True)
        self.lbl_image.setStyleSheet("border: 1px solid #555;")
        layout.addWidget(self.lbl_image)

        # Controls
        btn_layout = QHBoxLayout()
        
        self.btn_prev = QPushButton("<")
        self.btn_prev.clicked.connect(self.show_prev)
        
        self.lbl_date = QLabel("Today")
        self.lbl_date.setAlignment(Qt.AlignCenter)
        
        self.btn_next = QPushButton(">")
        self.btn_next.clicked.connect(self.show_next)

        btn_layout.addWidget(self.btn_prev)
        btn_layout.addWidget(self.lbl_date)
        btn_layout.addWidget(self.btn_next)
        
        layout.addLayout(btn_layout)
        self.setLayout(layout)

    def update_data(self, wallpapers):
        self.wallpapers = wallpapers
        if self.wallpapers:
            self.current_index = 0
            self.refresh_view()
            WallpaperManager.set_wallpaper_to_system(self.wallpapers[0]['path'])

    def refresh_view(self):
        if not self.wallpapers:
            return

        data = self.wallpapers[self.current_index]
        
        pixmap = QPixmap(data['path'])
        self.lbl_image.setPixmap(pixmap)
        
        title = data['title'].split('(')[0].strip()
        if len(title) > 40: title = title[:37] + "..."
        self.lbl_title.setText(title)
        
        if self.current_index == 0:
            self.lbl_date.setText("Today")
        else:
            self.lbl_date.setText(f"{self.current_index} days ago")

    def show_prev(self):
        if self.wallpapers and self.current_index < len(self.wallpapers) - 1:
            self.current_index += 1
            self.refresh_view()
            WallpaperManager.set_wallpaper_to_system(self.wallpapers[self.current_index]['path'])

    def show_next(self):
        if self.wallpapers and self.current_index > 0:
            self.current_index -= 1
            self.refresh_view()
            WallpaperManager.set_wallpaper_to_system(self.wallpapers[self.current_index]['path'])

    def show_at_corner(self):
        screen_geo = QDesktopWidget().availableGeometry()
        x = screen_geo.width() - self.width() - 20
        y = screen_geo.height() - self.height() - 20
        self.move(x, y)
        self.show()
        self.raise_()           # Bring to front
        self.activateWindow()   # Give it focus immediately

    # ==========================================================
    # THIS IS THE NEW CODE TO HANDLE CLICKING OUTSIDE
    # ==========================================================
    def changeEvent(self, event):
        """Detects if the window loses focus (user clicked outside)."""
        if event.type() == QEvent.ActivationChange:
            if not self.isActiveWindow():
                self.hide()
        super().changeEvent(event)

# ==========================================
# MAIN APP CONTROLLER (SYSTEM TRAY)
# ==========================================
class BingTrayApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        # Init GUI Window
        self.mini_window = MiniPreviewWindow()

        # Init Tray Icon
        self.tray_icon = QSystemTrayIcon()
        
        # LOAD CUSTOM ICON HERE using resource_path
        icon_path = resource_path("icon.ico")
        self.tray_icon.setIcon(QIcon(icon_path)) 
        
        self.tray_icon.setVisible(True)
        self.tray_icon.setToolTip("Bing Wallpaper Clone")

        # Context Menu (Right Click)
        self.menu = QMenu()
        
        action_update = QAction("Update Now", self.menu)
        action_update.triggered.connect(self.start_update_thread)
        self.menu.addAction(action_update)
        
        self.menu.addSeparator()
        
        action_exit = QAction("Exit", self.menu)
        action_exit.triggered.connect(self.app.quit)
        self.menu.addAction(action_exit)
        
        self.tray_icon.setContextMenu(self.menu)

        # Handle Tray Click
        self.tray_icon.activated.connect(self.on_tray_click)

        # Timer
        self.timer = QTimer()
        self.timer.timeout.connect(self.start_update_thread)
        self.timer.start(CHECK_INTERVAL_MS)

        # Threading
        self.thread = threading.Thread() 
        
        # Initial Update
        self.start_update_thread()

    def on_tray_click(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            if self.mini_window.isVisible():
                self.mini_window.hide()
            else:
                self.mini_window.show_at_corner()
                self.mini_window.activateWindow()

    def start_update_thread(self):
        self.worker = UpdateWorker()
        self.worker.finished.connect(self.on_update_finished)
        self.thread_obj = threading.Thread(target=self.worker.run)
        self.thread_obj.start()

    def on_update_finished(self, wallpapers):
        self.mini_window.update_data(wallpapers)

    def run(self):
        sys.exit(self.app.exec_())
# ==========================================
# ENTRY POINT
# ==========================================
if __name__ == "__main__":
    try:
        app_instance = BingTrayApp()
        app_instance.run()
    except KeyboardInterrupt:
        sys.exit()