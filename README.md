# Bongo cat app for Linux X11

![Screenshot](screenshot.png)

I vibecoded my own version of bongocat app which works for me on X11. This version is based on [https://github.com/kuroni/bongocat-osu](https://github.com/kuroni/bongocat-osu), which is based on the original version by [HamishDuncanson](https://github.com/HamishDuncanson).

This version moves the mouse with the user and hits on the keyboard. It has a tray icon to show/hide the window which lives in the bottom right corner of the screen. When user moves his mouse above the bongocat, the window will become semi-transparent.


## Requirements

You will need to install requirements from `requirements.txt` either using `pip` (or whatever is your python package manager) or, on Ubuntu, you can install `sudo apt install python3-pyqt5 python3-xlib`

## Running

`python bongocat.py`

## License

MIT