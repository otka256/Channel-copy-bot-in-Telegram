# Transfer Bot — quick setup

This repository contains a Telegram transfer bot (`bot.py`) plus helper scripts to bootstrap and install it on a server.

Files:
- `bot.py` — main bot implementation (Pyrogram).
- `requirements.txt` — Python dependencies.
- `setup.sh` — opinionated installer for Debian/Ubuntu (creates venv, installs packages, creates systemd service).
- `bootstrap.py` — cross-platform helper that creates venv, installs requirements and can start the bot interactively.
- `Impact.ttf` — (optional) put this font next to `bot.py` if you want the video watermark to use it.

Quick steps (recommended on Debian/Ubuntu VPS):

1. Upload repository to server (SCP, git clone, or SFTP).
2. Make `setup.sh` executable: `chmod +x setup.sh`.
3. Run installer (may require sudo):

```bash
./setup.sh
```

The script will create a Python virtualenv, install Python requirements and optionally create a `.env` file. It can also run the bot once interactively to allow Pyrogram to prompt for the user login/session creation. At the end it creates a systemd service `transferbot` and starts it.

If you prefer a manual or cross-platform path use `bootstrap.py`:

```bash
# Linux / macOS
python3 bootstrap.py

# Windows (PowerShell)
python bootstrap.py
```

`bootstrap.py` will create `venv`, install `requirements.txt`, check for `ffmpeg`, and optionally run the bot.

Important notes and troubleshooting
- ffmpeg: required for video watermarking. On Debian/Ubuntu: `sudo apt install -y ffmpeg`. On Windows: download FFmpeg and add `ffmpeg.exe` to PATH.
- Font for `add_text_watermark_to_video`: place `Impact.ttf` next to `bot.py` or modify `bot.py` to point to a system-installed TTF path.
- Pyrogram user login: when the script runs the first time, Pyrogram may ask for phone number / login code for the `user` client; follow prompts in the terminal to create the session file.
- `.env` contains sensitive credentials; keep it chmod 600 and do not commit to git.
- Service logs: `sudo journalctl -u transferbot -f`.

If you'd like, I can:
- update `bot.py` to fall back to a system font path when `Impact.ttf` is missing,
- generate a Windows Service / NSSM example for running the bot on Windows,
- or try to run some quick static checks on `bot.py` for missing imports.
