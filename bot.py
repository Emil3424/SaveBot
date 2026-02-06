#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Простой Telegram-бот для скачивания видео из Instagram Reels, YouTube (shorts/видео) и Twitter (gif/mp4).
Использует yt-dlp + ffmpeg.
"""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from glob import glob
from pathlib import Path

import telebot

# === Настройки ===
BOT_TOKEN = "8251189398:AAEYtFnqPNfmMG0-5529y47GjZhKPY3bRHk"


# Папка для временных скачиваний
TMP_ROOT = Path("/tmp/tg_downloader")
TMP_ROOT.mkdir(parents=True, exist_ok=True)

# yt-dlp опции по умолчанию
YT_DLP_FORMAT = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
# если нужен всегда mp4: --merge-output-format mp4
YTDLP_BASE_ARGS = [
    "--no-warnings",
    "--no-mtime",
    "--no-playlist",   # по умолчанию не скачиваем плейлисты
    "--merge-output-format", "mp4",
]

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# простые регулярки для определения сервиса
RE_URL = re.compile(r"(https?://[^\s]+)")
RE_YT = re.compile(r"(youtube\.com|youtu\.be)")
RE_INSTA = re.compile(r"(instagram\.com|instagr\.am)")
RE_TW = re.compile(r"(twitter\.com|x\.com|t\.co)")

# ---- утилиты ----
def run_ytdlp(url: str, out_dir: Path, extra_args=None, cookies_file: str = None):
    """
    Запускает yt-dlp в указанную папку и возвращает путь к скачанному файлу или None.
    """
    if extra_args is None:
        extra_args = []
    # шаблон имени файла
    out_template = str(out_dir / "%(title).200s-%(id)s.%(ext)s")
    cmd = ["yt-dlp"] + YTDLP_BASE_ARGS + ["-f", YT_DLP_FORMAT, "-o", out_template] + extra_args + [url]
    if cookies_file:
        cmd = ["yt-dlp", "--cookies", cookies_file] + YTDLP_BASE_ARGS + ["-f", YT_DLP_FORMAT, "-o", out_template] + extra_args + [url]

    # Запускаем и ждем завершения
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "stdout": "", "stderr": "yt-dlp timed out."}

    if proc.returncode != 0:
        return {"error": "yt-dlp-failed", "stdout": proc.stdout, "stderr": proc.stderr}

    # Найдем файл в папке
    files = list(out_dir.glob("*"))
    if not files:
        return {"error": "no-file", "stdout": proc.stdout, "stderr": proc.stderr}
    # выберем самый большой файл (вдруг скачалось несколько)
    files_sorted = sorted(files, key=lambda p: p.stat().st_size, reverse=True)
    return {"path": str(files_sorted[0]), "stdout": proc.stdout, "stderr": proc.stderr}

def convert_mp4_to_gif(mp4_path: str, gif_path: str, fps: int = 12, scale: str = "640:-1"):
    """
    Конвертация mp4 -> gif (может дать большой файл). Возвращает True/False.
    """
    cmd = [
        "ffmpeg", "-y", "-i", mp4_path,
        "-vf", f"fps={fps},scale={scale}:flags=lanczos",
        "-gifflags", "-transdiff",
        "-f", "gif", gif_path
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode == 0, proc.stdout + proc.stderr

def send_file(chat_id, filepath, caption=""):
    size = os.path.getsize(filepath)
    try:
        with open(filepath, "rb") as f:
            bot.send_document(chat_id, f, caption=caption)
    except Exception as e:
        bot.send_message(chat_id, f"Ошибка при отправке: {e}")


# ---- работник скачивания в отдельном потоке ----
def worker_download_and_send(chat_id, url, cookies_file=None, convert_to_gif=False):
    msg = bot.send_message(chat_id, f"Начинаю загрузку: {url}")
    tmpdir = Path(tempfile.mkdtemp(prefix="tgdl_", dir=str(TMP_ROOT)))
    try:
        bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text="Запрос к yt-dlp...")
        result = run_ytdlp(url, tmpdir, extra_args=[], cookies_file=cookies_file)
        if result.get("error"):
            bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id,
                                  text=f"Ошибка yt-dlp: {result['error']}\n\nstderr:\n{result.get('stderr','')[:1000]}")
            return

        filepath = result["path"]
        bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text=f"Скачано: {os.path.basename(filepath)}\nРазмер: {os.path.getsize(filepath)} bytes\nОтправляю...")

        if convert_to_gif:
            gif_path = str(tmpdir / (Path(filepath).stem + ".gif"))
            ok, log = convert_mp4_to_gif(filepath, gif_path)
            if not ok:
                bot.send_message(chat_id, f"Ошибка конвертации в GIF:\n{log[:1000]}")
                # отправим mp4 как fallback
                send_file(chat_id, filepath, caption="Не удалось конвертировать в gif, отправляю mp4.")
            else:
                send_file(chat_id, gif_path, caption="GIF")
        else:
            send_file(chat_id, filepath, caption="Готово")

    except Exception as e:
        bot.send_message(chat_id, f"Внутренняя ошибка: {e}")
    finally:
        try:
            shutil.rmtree(tmpdir)
        except Exception:
            pass

# ---- обработчики сообщений ----
@bot.message_handler(commands=["start", "help"])
def start_help(msg):
    text = (
        "Привет! Отправь мне ссылку на Instagram Reels, YouTube (включая Shorts) или Twitter (X).\n"
        "Бот попытается скачать медиа и прислать файл.\n\n"
        "Примеры команд:\n"
        "- просто пришлите ссылку\n"
        "- /gif + ссылка — попытаться конвертировать в GIF (для твиттера/gif)\n\n"
        "Внимание: для некоторых материалов может потребоваться авторизация (cookies)."
    )
    bot.reply_to(msg, text)

@bot.message_handler(commands=["gif"])
def cmd_gif(msg):
    text = msg.text or ""
    urls = RE_URL.findall(text)
    if not urls:
        bot.reply_to(msg, "Пожалуйста, пришлите команду /gif вместе со ссылкой.")
        return
    url = urls[0]
    threading.Thread(target=worker_download_and_send, args=(msg.chat.id, url, None, True), daemon=True).start()
    bot.reply_to(msg, "Запустил задачу: конвертация в GIF (может занять время).")

@bot.message_handler(func=lambda m: True, content_types=['text'])
def catch_all(msg):
    text = msg.text or ""
    urls = RE_URL.findall(text)
    if not urls:
        bot.reply_to(msg, "Я ожидаю ссылку на видео (Instagram / YouTube / Twitter).")
        return
    url = urls[0]

    # Детект сервиса (можно расширить)
    if RE_INSTA.search(url):
        # Instagram: yt-dlp обычно скачивает Reels; для приватных нужен --cookies cookies.txt
        note = "Instagram detected."
    elif RE_YT.search(url):
        note = "YouTube detected."
    elif RE_TW.search(url):
        note = "Twitter/X detected."
    else:
        note = "URL обнаружен; пробую yt-dlp."

    bot.reply_to(msg, f"{note} Запускаю скачивание в фоне...")
    threading.Thread(target=worker_download_and_send, args=(msg.chat.id, url, None, False), daemon=True).start()

if __name__ == "__main__":
    print("Bot is polling...")
    bot.remove_webhook()
    bot.infinity_polling()


