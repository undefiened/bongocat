import sys
import os
import math
import signal
import argparse
import threading
from abc import ABC, abstractmethod
from PyQt5.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu, QAction
from PyQt5.QtGui import QPainter, QPixmap, QColor, QPen, QPolygonF, QBrush, QIcon
from PyQt5.QtCore import Qt, QTimer, QPoint, QPointF


APP_DIR = os.path.dirname(os.path.abspath(__file__))


class InputBackendError(RuntimeError):
    """Raised when a global input backend cannot be started."""


def is_wayland_session():
    return (
        os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland'
        or bool(os.environ.get('WAYLAND_DISPLAY'))
    )


def configure_qt_platform():
    """Use XWayland for the overlay unless the user selected a Qt platform.

    Standard Wayland xdg-shell surfaces cannot choose an absolute position or
    request always-on-top behavior.  XWayland provides those desktop-overlay
    semantics on GNOME, KDE, and wlroots compositors without tying the app to a
    compositor-specific protocol.
    """
    if (
        is_wayland_session()
        and os.environ.get('DISPLAY')
        and not os.environ.get('QT_QPA_PLATFORM')
    ):
        os.environ['QT_QPA_PLATFORM'] = 'xcb'
        print("Wayland session detected; using XWayland for overlay placement")


def make_x11_window_sticky(widget, xdisplay=None):
    from Xlib import display, X

    owns_display = xdisplay is None
    xdisplay = xdisplay or display.Display()
    try:
        window_id = int(widget.winId())
        x11_window = xdisplay.create_resource_object('window', window_id)

        desktop_atom = xdisplay.get_atom('_NET_WM_DESKTOP')
        cardinal_atom = xdisplay.get_atom('CARDINAL')
        x11_window.change_property(
            desktop_atom, cardinal_atom, 32, [0xFFFFFFFF]
        )

        state_atom = xdisplay.get_atom('_NET_WM_STATE')
        sticky_atom = xdisplay.get_atom('_NET_WM_STATE_STICKY')
        atom_atom = xdisplay.get_atom('ATOM')
        x11_window.change_property(
            state_atom, atom_atom, 32, [sticky_atom], X.PropModeAppend
        )
        xdisplay.flush()
    finally:
        if owns_display:
            xdisplay.close()


def get_x11_pointer_position():
    from Xlib import display

    xdisplay = display.Display()
    try:
        data = xdisplay.screen().root.query_pointer()._data
        return data['root_x'], data['root_y']
    finally:
        xdisplay.close()


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
        from Xlib import display

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
            make_x11_window_sticky(widget, self.display)
        except Exception as e:
            print(f"Warning: Failed to make window sticky: {e}")

    def cleanup(self):
        self.display.close()


class EvdevInputBackend(InputBackend):
    def __init__(self):
        import evdev

        self._pressed_keys = set()
        self._lock = threading.Lock()
        self._running = True
        self._threads = []
        self._devices = []

        screen_bounds = QApplication.primaryScreen().geometry()
        for screen in QApplication.screens()[1:]:
            screen_bounds = screen_bounds.united(screen.geometry())
        self._screen_bounds = screen_bounds
        self._mouse_x = screen_bounds.center().x()
        self._mouse_y = screen_bounds.center().y()
        if QApplication.platformName() == 'xcb':
            try:
                self._mouse_x, self._mouse_y = get_x11_pointer_position()
            except Exception as exc:
                print(
                    f"Warning: Cannot read initial pointer position: {exc}",
                    file=sys.stderr,
                )

        denied_paths = []
        keyboard_count = 0
        pointer_count = 0
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                caps = dev.capabilities()
            except PermissionError:
                denied_paths.append(path)
                continue
            except OSError as exc:
                print(f"Warning: Cannot inspect {path}: {exc}", file=sys.stderr)
                continue

            key_caps = caps.get(evdev.ecodes.EV_KEY, [])
            rel_caps = caps.get(evdev.ecodes.EV_REL, [])
            abs_caps = {
                item[0] if isinstance(item, tuple) else item
                for item in caps.get(evdev.ecodes.EV_ABS, [])
            }
            is_keyboard = evdev.ecodes.KEY_A in key_caps
            is_relative_pointer = (
                evdev.ecodes.REL_X in rel_caps
                and evdev.ecodes.REL_Y in rel_caps
            )
            is_touchpad = (
                evdev.ecodes.INPUT_PROP_POINTER in dev.input_props()
                and evdev.ecodes.ABS_X in abs_caps
                and evdev.ecodes.ABS_Y in abs_caps
            )
            pointer_mode = (
                'relative' if is_relative_pointer
                else 'touchpad' if is_touchpad
                else None
            )
            if not is_keyboard and pointer_mode is None:
                dev.close()
                continue

            keyboard_count += int(is_keyboard)
            pointer_count += int(pointer_mode is not None)
            self._devices.append(dev)
            t = threading.Thread(
                target=self._read_device,
                args=(dev, is_keyboard, pointer_mode),
                daemon=True,
                name=f"evdev-{os.path.basename(path)}",
            )
            t.start()
            self._threads.append(t)

        if keyboard_count == 0:
            self.cleanup()
            if denied_paths:
                raise InputBackendError(
                    "Cannot read keyboard input devices. Add your user to the "
                    "'input' group, then log out and back in. See README.md."
                )
            raise InputBackendError("No keyboard input device found in /dev/input")
        if pointer_count == 0:
            print(
                "Warning: No pointer device found; mouse arm will stay still",
                file=sys.stderr,
            )

    def _move_pointer(self, delta_x, delta_y):
        self._mouse_x = max(
            self._screen_bounds.left(),
            min(self._screen_bounds.right(), self._mouse_x + delta_x),
        )
        self._mouse_y = max(
            self._screen_bounds.top(),
            min(self._screen_bounds.bottom(), self._mouse_y + delta_y),
        )

    def _read_device(self, dev, is_keyboard, pointer_mode):
        import evdev

        device_id = dev.path
        touching = False
        absolute_x = None
        absolute_y = None
        last_absolute_x = None
        last_absolute_y = None
        remainder_x = 0.0
        remainder_y = 0.0

        if pointer_mode == 'touchpad':
            x_info = dev.absinfo(evdev.ecodes.ABS_X)
            y_info = dev.absinfo(evdev.ecodes.ABS_Y)
            x_range = max(1, x_info.max - x_info.min)
            y_range = max(1, y_info.max - y_info.min)
            x_scale = self._screen_bounds.width() / x_range
            y_scale = self._screen_bounds.height() / y_range

        try:
            for event in dev.read_loop():
                if not self._running:
                    break
                if is_keyboard and event.type == evdev.ecodes.EV_KEY:
                    with self._lock:
                        key = (device_id, event.code)
                        if event.value == 1:
                            self._pressed_keys.add(key)
                        elif event.value == 0:
                            self._pressed_keys.discard(key)
                if pointer_mode == 'relative' and event.type == evdev.ecodes.EV_REL:
                    with self._lock:
                        delta_x = 0
                        delta_y = 0
                        if event.code == evdev.ecodes.REL_X:
                            delta_x = event.value
                        elif event.code == evdev.ecodes.REL_Y:
                            delta_y = event.value
                        self._move_pointer(delta_x, delta_y)
                elif pointer_mode == 'touchpad':
                    if (
                        event.type == evdev.ecodes.EV_KEY
                        and event.code == evdev.ecodes.BTN_TOUCH
                    ):
                        touching = bool(event.value)
                        if not touching:
                            last_absolute_x = None
                            last_absolute_y = None
                    elif event.type == evdev.ecodes.EV_ABS:
                        if event.code == evdev.ecodes.ABS_X:
                            absolute_x = event.value
                        elif event.code == evdev.ecodes.ABS_Y:
                            absolute_y = event.value
                    elif (
                        event.type == evdev.ecodes.EV_SYN
                        and event.code == evdev.ecodes.SYN_REPORT
                        and touching
                        and absolute_x is not None
                        and absolute_y is not None
                    ):
                        if (
                            last_absolute_x is not None
                            and last_absolute_y is not None
                        ):
                            scaled_x = (
                                (absolute_x - last_absolute_x) * x_scale
                                + remainder_x
                            )
                            scaled_y = (
                                (absolute_y - last_absolute_y) * y_scale
                                + remainder_y
                            )
                            delta_x = int(scaled_x)
                            delta_y = int(scaled_y)
                            remainder_x = scaled_x - delta_x
                            remainder_y = scaled_y - delta_y
                            with self._lock:
                                self._move_pointer(delta_x, delta_y)
                        last_absolute_x = absolute_x
                        last_absolute_y = absolute_y
        except OSError:
            pass
        finally:
            with self._lock:
                self._pressed_keys = {
                    key for key in self._pressed_keys if key[0] != device_id
                }

    def get_pressed_keys(self):
        with self._lock:
            return {key_code for _, key_code in self._pressed_keys}

    def get_mouse_position(self):
        with self._lock:
            return self._mouse_x, self._mouse_y

    def make_sticky(self, widget):
        if QApplication.platformName() == 'xcb':
            make_x11_window_sticky(widget)

    def cleanup(self):
        self._running = False
        for dev in self._devices:
            try:
                dev.close()
            except Exception:
                pass
        self._devices = []


def detect_backend(override=None):
    if override == 'x11':
        return X11InputBackend()
    if override == 'evdev':
        return EvdevInputBackend()

    if is_wayland_session():
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
        self.img_dir = os.path.join(APP_DIR, "img")
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

        screen_geo = QApplication.primaryScreen().availableGeometry()
        self.move(screen_geo.right() - self.w - 49, screen_geo.bottom() - self.h - 49)

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
        self.pressed_keys = set()
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
        pressed_keys = set(self.backend.get_pressed_keys())

        if not pressed_keys:
            self.key_state = 0
        else:
            if pressed_keys - self.pressed_keys:
                self.key_state = 2 if self.key_state == 1 else 1
        self.pressed_keys = pressed_keys

        root_x, root_y = self.backend.get_mouse_position()

        # Transparency logic — only update when changed to avoid
        # triggering compositor attention animations (GNOME orange border)
        over_window = self.frameGeometry().contains(QPoint(root_x, root_y))
        new_opacity = 0.2 if over_window else 1.0
        if self.windowOpacity() != new_opacity:
            self.setWindowOpacity(new_opacity)

        screen = QApplication.screenAt(QPoint(root_x, root_y))
        screen_geo = (screen or QApplication.primaryScreen()).geometry()
        fx = max(0.0, min(1.0, (root_x - screen_geo.left()) / max(1, screen_geo.width() - 1)))
        fy = max(0.0, min(1.0, (root_y - screen_geo.top()) / max(1, screen_geo.height() - 1)))

        self.target_x = -97 * fx + 44 * fy + 184
        self.target_y = -76 * fx - 40 * fy + 324

        self.calculate_arm()
        self.update()

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

    configure_qt_platform()
    app = QApplication(sys.argv)
    try:
        backend = detect_backend(args.backend)
    except (InputBackendError, ImportError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    cat = BongoCat(backend)
    cat.show()

    ret = app.exec_()
    backend.cleanup()
    sys.exit(ret)
