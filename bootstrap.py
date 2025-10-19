#!/usr/bin/env python3
"""
bootstrap.py
Cross-platform bootstrap helper for Transfer Bot.
It will create a venv, install requirements, ensure ffmpeg is present (best-effort),
copy the provided font if needed, create a `.env` interactively (if not present),
and optionally start the bot.

Usage: python bootstrap.py
"""
import os
import subprocess
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / 'venv'
REQ_FILE = ROOT / 'requirements.txt'
ENV_FILE = ROOT / '.env'
FONT_NAME = 'Impact.ttf'


def check_python():
    if sys.version_info < (3, 8):
        print('Python 3.8+ is required.')
        sys.exit(1)


def create_venv():
    if VENV_DIR.exists():
        print('Virtualenv already exists. Skipping venv creation.')
        return
    print('Creating virtualenv...')
    subprocess.check_call([sys.executable, '-m', 'venv', str(VENV_DIR)])


def get_pip():
    if sys.platform == 'win32':
        return VENV_DIR / 'Scripts' / 'pip.exe'
    return VENV_DIR / 'bin' / 'pip'


def install_requirements():
    if not REQ_FILE.exists():
        print('requirements.txt not found. Skipping pip install.')
        return
    print('Installing python requirements...')
    pip = str(get_pip())
    subprocess.check_call([pip, 'install', '--upgrade', 'pip'])
    subprocess.check_call([pip, 'install', '-r', str(REQ_FILE)])


def ensure_ffmpeg():
    # best-effort check
    try:
        subprocess.check_call(['ffmpeg', '-version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print('ffmpeg found in PATH.')
        return True
    except Exception:
        print('ffmpeg not found in PATH.')
        if sys.platform.startswith('linux'):
            print('Try: sudo apt update && sudo apt install -y ffmpeg')
        elif sys.platform == 'win32':
            print('Please install ffmpeg and add it to PATH: https://ffmpeg.org/download.html')
        return False


def ensure_font():
    # If font file exists in root, good. Otherwise prompt to place it.
    font_path = ROOT / FONT_NAME
    if font_path.exists():
        print(f'Font {FONT_NAME} found in project root.')
        return True
    print(f'Font {FONT_NAME} not found in project root.')
    print('If you want video watermarking to use a particular TTF (Impact.ttf),')
    print('place it next to bot.py or change font_path in bot.py.')
    return False


def create_env_interactive():
    if ENV_FILE.exists():
        print('.env already exists. Skipping creation.')
        return
    print('Creating .env interactively (values will be stored in .env file).')
    api_id = input('API_ID (numeric): ').strip()
    api_hash = input('API_HASH: ').strip()
    bot_token = input('BOT_TOKEN: ').strip()
    admin_id = input('ADMIN_ID (numeric): ').strip()
    with open(ENV_FILE, 'w', encoding='utf-8') as f:
        f.write(f'API_ID={api_id}\n')
        f.write(f'API_HASH={api_hash}\n')
        f.write(f'BOT_TOKEN={bot_token}\n')
        f.write(f'ADMIN_ID={admin_id}\n')
    try:
        os.chmod(ENV_FILE, 0o600)
    except Exception:
        pass
    print('.env created.')


def run_bot():
    print('Starting bot (in current terminal). Use Ctrl+C to stop.')
    python = str(VENV_DIR / ('Scripts' if sys.platform == 'win32' else 'bin') / ('python.exe' if sys.platform == 'win32' else 'python'))
    os.execv(python, [python, str(ROOT / 'bot.py')])


def main():
    check_python()
    create_venv()
    install_requirements()
    ensure_ffmpeg()
    ensure_font()
    if not ENV_FILE.exists():
        create_env_interactive()

    answer = input('Run bot now? [y/N]: ').strip().lower()
    if answer == 'y':
        run_bot()
    else:
        print('Bootstrap finished. To run the bot:')
        if sys.platform == 'win32':
            print('  .\\venv\\Scripts\\Activate.ps1')
            print('  python bot.py')
        else:
            print('  source venv/bin/activate')
            print('  python3 bot.py')


if __name__ == '__main__':
    main()
