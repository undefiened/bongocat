# Bongo Cat desktop overlay for Linux

![Screenshot](screenshot.png)

This version works on both X11 and Wayland sessions. It is based on [bongocat-osu](https://github.com/kuroni/bongocat-osu), which is based on the original version by [HamishDuncanson](https://github.com/HamishDuncanson).

The cat follows pointer movement and hits the keyboard when you type. A tray icon shows or hides the bottom-right overlay. The overlay becomes semi-transparent when the pointer passes over it.


## Requirements

Install Python dependencies with:

```sh
python3 -m pip install -r requirements.txt
```

On Ubuntu/Debian, distro packages can be used instead:

```sh
sudo apt install python3-pyqt5 python3-xlib python3-evdev xwayland
```

### Wayland input permission

Wayland does not expose global keyboard state to ordinary applications. Bongo Cat therefore reads Linux evdev devices. Your login user must be allowed to read `/dev/input/event*`.

First try running the app. If it reports `Cannot read keyboard input devices`, add your user to the `input` group, then fully log out and back in:

```sh
sudo usermod -aG input "$USER"
```

Security note: membership in `input` allows programs running as your user to read all keyboard input, including passwords. Remove the membership with `sudo gpasswd -d "$USER" input` if you no longer use it.

The window uses XWayland during a Wayland session because the standard Wayland window protocol does not provide portable absolute placement, always-on-top, or all-workspace controls. Input still comes directly from evdev, so keyboard animation works while native Wayland applications have focus.

## Running

```sh
python3 bongocat.py
```

Backend selection is automatic. For troubleshooting, force one with `--backend x11` or `--backend evdev`.

## License

MIT
