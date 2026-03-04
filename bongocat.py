import sys
import os
import math
import signal
import argparse
import threading
from abc import ABC, abstractmethod
from PyQt5.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu, QAction
from PyQt5.QtGui import QPainter, QPixmap, QColor, QPen, QPainterPath, QPolygonF, QBrush, QIcon
from PyQt5.QtCore import Qt, QTimer, QPointF, QRect


class InputBackend(ABC):
    @abstractmethod
    def get_pressed_keys(self):
        """Return list of currently pressed keycodes."""
        pass

    @abstractmethod
    def get_mouse_position(self):
        """Return (x, y) absolute cursor position."""
        pass

    @abstractmethod
    def make_sticky(self, widget):
        """Make window visible on all workspaces."""
        pass

    def cleanup(self):
        """Release resources."""
        pass


class X11InputBackend(InputBackend):
    def __init__(self):
        from Xlib import display, X
        self._X = X
        self.display = display.Display()
        self.root = self.display.screen().root

    def get_pressed_keys(self):
        key_map = self.display.query_keymap()
        pressed_keys = []
        for i, byte in enumerate(key_map[1:]):
            if byte:
                for bit in range(8):
                    if byte & (1 << bit):
                        pressed_keys.append(i * 8 + bit + 8)
        return pressed_keys

    def get_mouse_position(self):
        data = self.root.query_pointer()._data
        return data['root_x'], data['root_y']

    def make_sticky(self, widget):
        try:
            window_id = int(widget.winId())
            x11_window = self.display.create_resource_object('window', window_id)

            desktop_atom = self.display.get_atom('_NET_WM_DESKTOP')
            cardinal_atom = self.display.get_atom('CARDINAL')
            x11_window.change_property(desktop_atom, cardinal_atom, 32, [0xFFFFFFFF])

            state_atom = self.display.get_atom('_NET_WM_STATE')
            sticky_atom = self.display.get_atom('_NET_WM_STATE_STICKY')
            atom_atom = self.display.get_atom('ATOM')
            x11_window.change_property(state_atom, atom_atom, 32, [sticky_atom], self._X.PropModeAppend)

            self.display.flush()
        except Exception as e:
            print(f"Warning: Failed to make window sticky: {e}")

    def cleanup(self):
        self.display.close()


class EvdevInputBackend(InputBackend):
    def __init__(self):
        import evdev
        self._pressed_keys = set()
        self._mouse_x = 0
        self._mouse_y = 0
        self._lock = threading.Lock()
        self._running = True
        self._threads = []
        self._keyboard_dev = None
        self._mouse_dev = None

        # Get screen bounds for mouse clamping
        screen = QApplication.primaryScreen().geometry()
        self._screen_w = screen.width()
        self._screen_h = screen.height()
        self._mouse_x = self._screen_w // 2
        self._mouse_y = self._screen_h // 2

        # Find devices
        devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
        for dev in devices:
            caps = dev.capabilities()
            if self._keyboard_dev is None and evdev.ecodes.EV_KEY in caps:
                # Check it has actual keyboard keys (not just mouse buttons)
                key_caps = caps[evdev.ecodes.EV_KEY]
                if evdev.ecodes.KEY_A in key_caps:
                    self._keyboard_dev = dev
                    continue
            if self._mouse_dev is None and evdev.ecodes.EV_REL in caps:
                self._mouse_dev = dev
                continue

        if self._keyboard_dev is None:
            print("Warning: No keyboard device found via evdev")
        else:
            t = threading.Thread(target=self._read_keyboard, daemon=True)
            t.start()
            self._threads.append(t)

        if self._mouse_dev is None:
            print("Warning: No mouse device found via evdev")
        else:
            t = threading.Thread(target=self._read_mouse, daemon=True)
            t.start()
            self._threads.append(t)

    def _read_keyboard(self):
        import evdev
        try:
            for event in self._keyboard_dev.read_loop():
                if not self._running:
                    break
                if event.type == evdev.ecodes.EV_KEY:
                    with self._lock:
                        if event.value == 1:  # key down
                            self._pressed_keys.add(event.code)
                        elif event.value == 0:  # key up
                            self._pressed_keys.discard(event.code)
        except OSError:
            pass

    def _read_mouse(self):
        import evdev
        try:
            for event in self._mouse_dev.read_loop():
                if not self._running:
                    break
                if event.type == evdev.ecodes.EV_REL:
                    with self._lock:
                        if event.code == evdev.ecodes.REL_X:
                            self._mouse_x = max(0, min(self._screen_w, self._mouse_x + event.value))
                        elif event.code == evdev.ecodes.REL_Y:
                            self._mouse_y = max(0, min(self._screen_h, self._mouse_y + event.value))
        except OSError:
            pass

    def get_pressed_keys(self):
        with self._lock:
            return list(self._pressed_keys)

    def get_mouse_position(self):
        with self._lock:
            return self._mouse_x, self._mouse_y

    def make_sticky(self, widget):
        # No reliable way to do this on Wayland; no-op
        pass

    def cleanup(self):
        self._running = False
        if self._keyboard_dev:
            try:
                self._keyboard_dev.close()
            except Exception:
                pass
        if self._mouse_dev:
            try:
                self._mouse_dev.close()
            except Exception:
                pass


def detect_backend(override=None):
    if override == 'x11':
        return X11InputBackend()
    if override == 'evdev':
        return EvdevInputBackend()

    session_type = os.environ.get('XDG_SESSION_TYPE', '').lower()
    if session_type == 'wayland' or os.environ.get('WAYLAND_DISPLAY'):
        print("Detected Wayland session, using evdev backend")
        return EvdevInputBackend()
    else:
        print("Using X11 backend")
        return X11InputBackend()


class BongoCat(QWidget):
    def __init__(self, backend):
        super().__init__()

        # Constants from bongocat-osu
        self.PAW_START = (211, 159)
        self.PAW_END = (258, 228)
        self.WINDOW_W = 612
        self.WINDOW_H = 354

        # Global offsets from osu.cpp
        self.OFFSET_X = -38
        self.OFFSET_Y = -50

        # Load images
        self.img_dir = "img"
        self.bg = QPixmap(os.path.join(self.img_dir, "mousebg.png"))
        self.paws_up = QPixmap(os.path.join(self.img_dir, "up.png"))
        self.left_down = QPixmap(os.path.join(self.img_dir, "left.png"))
        self.right_down = QPixmap(os.path.join(self.img_dir, "right.png"))
        self.mouse_img = QPixmap(os.path.join(self.img_dir, "mouse.png"))
        # Mouse size
        self.mouse_img = self.mouse_img.scaled(int(104 * 1.3), int(68 * 1.3), Qt.KeepAspectRatio, Qt.SmoothTransformation)

        # Scale for the window
        self.scale = 0.5
        self.w = int(self.WINDOW_W * self.scale)
        self.h = int(self.WINDOW_H * self.scale)

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setGeometry(100, 100, self.w, self.h)

        screen_geo = QApplication.primaryScreen().geometry()
        self.move(screen_geo.width() - self.w - 50, screen_geo.height() - self.h - 50)

        # Tray Icon Setup
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon(os.path.join(self.img_dir, "mouse.png")))

        tray_menu = QMenu()
        show_action = QAction("Show", self)
        hide_action = QAction("Hide", self)
        quit_action = QAction("Exit", self)

        show_action.triggered.connect(self.show)
        hide_action.triggered.connect(self.hide)
        quit_action.triggered.connect(QApplication.quit)

        tray_menu.addAction(show_action)
        tray_menu.addAction(hide_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self.on_tray_icon_activated)
        self.tray_icon.show()

        # State
        self.key_state = 0
        self.last_key_pressed = None
        self.target_x = 258
        self.target_y = 228
        self.wrist_x = 258
        self.wrist_y = 228
        self.arm_points = []

        self.backend = backend
        try:
            self.backend.make_sticky(self)
        except Exception as e:
            print(f"Warning: Failed to make window sticky: {e}")

        self.timer = QTimer()
        self.timer.timeout.connect(self.update_state)
        self.timer.start(16)

        self.sig_timer = QTimer()
        self.sig_timer.start(500)
        self.sig_timer.timeout.connect(lambda: None)

    def on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            if self.isVisible():
                self.hide()
            else:
                self.show()

    def update_state(self):
        pressed_keys = self.backend.get_pressed_keys()

        if not pressed_keys:
            self.key_state = 0
            self.last_key_pressed = None
        else:
            current_key = pressed_keys[0]
            if current_key != self.last_key_pressed:
                self.key_state = 2 if self.key_state == 1 else 1
                self.last_key_pressed = current_key

        root_x, root_y = self.backend.get_mouse_position()

        # Transparency logic — only update when changed to avoid
        # triggering compositor attention animations (GNOME orange border)
        over_window = self.geometry().contains(root_x, root_y)
        new_opacity = 0.2 if over_window else 1.0
        if self.windowOpacity() != new_opacity:
            self.setWindowOpacity(new_opacity)

        screen_geo = QApplication.primaryScreen().geometry()
        fx = max(0.0, min(1.0, root_x / screen_geo.width()))
        fy = max(0.0, min(1.0, root_y / screen_geo.height()))

        self.target_x = -97 * fx + 44 * fy + 184
        self.target_y = -76 * fx - 40 * fy + 324

        self.calculate_arm()
        self.update()
        self.raise_()

    def bezier(self, t, points):
        n = (len(points) // 2) - 1
        x, y = 0, 0
        def fact(n):
            if n <= 1: return 1
            res = 1
            for i in range(2, n + 1): res *= i
            return res

        for i in range(n + 1):
            coeff = fact(n) / (fact(i) * fact(n - i))
            term = coeff * (t ** i) * ((1 - t) ** (n - i))
            x += points[2 * i] * term
            y += points[2 * i + 1] * term
        return x, y

    def calculate_arm(self):
        x_start, y_start = self.PAW_START
        x_end, y_end = self.PAW_END
        tx, ty = self.target_x, self.target_y

        dist1 = math.hypot(x_start - tx, y_start - ty)
        c1x = x_start - 0.7237 * dist1 / 2
        c1y = y_start + 0.69 * dist1 / 2

        pss = [x_start, y_start]
        oof = 6
        for i in range(1, oof):
            p = self.bezier(i/oof, [x_start, y_start, c1x, c1y, tx, ty])
            pss.extend(p)
        pss.extend([tx, ty])

        a_perp = ty - c1y
        b_perp = c1x - tx
        le_perp = math.hypot(a_perp, b_perp)
        ax = tx + a_perp / le_perp * 60
        ay = ty + b_perp / le_perp * 60
        self.wrist_x, self.wrist_y = ax, ay

        dist2 = math.hypot(x_end - ax, y_end - ay)
        c2x = x_end - 0.6 * dist2 / 2
        c2y = y_end + 0.8 * dist2 / 2

        push = 20
        s, t = tx - c1x, ty - c1y
        le1 = math.hypot(s, t)
        s, t = s * push / le1, t * push / le1

        s2, t2 = ax - c2x, ay - c2y
        le2 = math.hypot(s2, t2)
        s2, t2 = s2 * push / le2, t2 * push / le2

        for i in range(1, oof):
            p = self.bezier(i/oof, [tx, ty, tx + s, ty + t, ax + s2, ay + t2, ax, ay])
            pss.extend(p)
        pss.extend([ax, ay])

        for i in range(oof - 1, 0, -1):
            p = self.bezier(i/oof, [x_end, y_end, c2x, c2y, ax, ay])
            pss.extend(p)
        pss.extend([x_end, y_end])

        iter_val = 25
        self.arm_points = []
        for i in range(iter_val + 1):
            p = self.bezier(i / float(iter_val), pss)
            self.arm_points.append(QPointF(p[0] + self.OFFSET_X, p[1] + self.OFFSET_Y))

    def draw_arm(self, painter):
        if not self.arm_points:
            return

        poly = QPolygonF()
        for p in self.arm_points:
            poly.append(p)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(255, 255, 255)))
        painter.drawPolygon(poly)

        # Slightly thinner and slower taper
        self.draw_tapered_edge(painter, self.arm_points, QColor(0, 0, 0, 100), 8)
        self.draw_tapered_edge(painter, self.arm_points, QColor(0, 0, 0), 6)

    def draw_tapered_edge(self, painter, points, color, start_width):
        width = start_width
        for i in range(len(points) - 1):
            pen = QPen(color, width)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(points[i], points[i+1])
            width -= 0.1
            if width < 2: width = 2

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(self.scale, self.scale)

        # Layer 1: Background
        painter.drawPixmap(0, 0, self.bg)

        # Layer 2: Mouse device (Drawn behind the arm)
        mpos0 = (self.wrist_x + self.target_x) / 2 - 52 - 15
        mpos1 = (self.wrist_y + self.target_y) / 2 - 34 + 5
        mx = mpos0 + self.OFFSET_X
        my = mpos1 + self.OFFSET_Y
        painter.drawPixmap(int(mx), int(my), self.mouse_img)

        # Layer 3: Dynamic Arm (Drawn on top of the mouse)
        self.draw_arm(painter)

        # Layer 4: Keyboard Paws
        if self.key_state == 1:
            painter.drawPixmap(0, 0, self.left_down)
        elif self.key_state == 2:
            painter.drawPixmap(0, 0, self.right_down)
        else:
            painter.drawPixmap(0, 0, self.paws_up)


def sigint_handler(*args):
    QApplication.quit()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, sigint_handler)

    parser = argparse.ArgumentParser(description="Bongo Cat desktop overlay")
    parser.add_argument('--backend', choices=['x11', 'evdev'], default=None,
                        help="Force input backend (default: auto-detect)")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    backend = detect_backend(args.backend)
    cat = BongoCat(backend)
    cat.show()

    ret = app.exec_()
    backend.cleanup()
    sys.exit(ret)
