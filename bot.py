import asyncio
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask, render_template, request, redirect,
    send_from_directory, abort, jsonify
)
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# =========================
# НАСТРОЙКИ
# =========================

BASE_DIR = Path(__file__).resolve().parent
TOKEN_FILE = BASE_DIR / "token.txt"
DATABASE_FILE = BASE_DIR / "support.db"
AVATAR_DIR = BASE_DIR / "avatars"
MEDIA_DIR = BASE_DIR / "media"
STICKER_DIR = BASE_DIR / "stickers"   # каждая подпапка = один стикерпак

AVATAR_TTL = 24 * 60 * 60              # обновлять аватарку раз в сутки
MAX_DOWNLOAD_SIZE = 20 * 1024 * 1024   # лимит Bot API на скачивание файлов
MAX_UPLOAD_SIZE = 50 * 1024 * 1024     # лимит Bot API на отправку файлов

WEB_HOST = "127.0.0.1"
WEB_PORT = 5000

# Эти форматы безопасно открывать прямо в браузере,
# всё остальное (html, svg, exe...) отдаётся только как скачивание
INLINE_EXT = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".mp4", ".webm",
    ".ogg", ".oga", ".mp3", ".m4a", ".wav",
}

# Что можно хранить в стикерпаках
STICKER_EXT = {".webp", ".png", ".jpg", ".jpeg", ".gif", ".mp4", ".webm"}

MEDIA_LABELS = {
    "photo": "📷 Фото",
    "animation": "🎞 GIF",
    "video": "🎥 Видео",
    "video_note": "⏺ Кружок",
    "voice": "🎤 Голосовое",
    "audio": "🎵 Аудио",
    "document": "📎 Файл",
    "sticker": "💟 Стикер",
    "sticker_tgs": "💟 Стикер",
}

# Как пытаться отправить файл (по порядку, пока Telegram не примет).
# «Прикрепить» — обычная загрузка файла с компьютера
ATTACH_PLANS = {
    ".jpg": ["photo", "document"],
    ".jpeg": ["photo", "document"],
    ".png": ["photo", "document"],
    ".webp": ["sticker", "document"],
    ".gif": ["animation", "document"],
    ".mp4": ["video", "document"],
    ".mov": ["video", "document"],
    ".webm": ["video", "document"],
    ".mp3": ["audio", "document"],
    ".m4a": ["audio", "document"],
    ".wav": ["audio", "document"],
    ".flac": ["audio", "document"],
    ".ogg": ["voice", "audio", "document"],
    ".oga": ["voice", "audio", "document"],
    ".opus": ["voice", "audio", "document"],
}

# Отправка из стикерпака: webp/webm как стикер, mp4/gif как GIF
LIBRARY_PLANS = {
    ".webp": ["sticker", "photo", "document"],
    ".webm": ["sticker", "video", "document"],
    ".gif": ["animation", "document"],
    ".mp4": ["animation", "video", "document"],
    ".png": ["photo", "document"],
    ".jpg": ["photo", "document"],
    ".jpeg": ["photo", "document"],
}

if not TOKEN_FILE.exists():
    raise FileNotFoundError("Файл token.txt не найден!")

BOT_TOKEN = TOKEN_FILE.read_text(encoding="utf-8").strip()

if not BOT_TOKEN:
    raise ValueError("Файл token.txt пуст!")

# =========================
# ОБЪЕКТЫ
# =========================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE

telegram_loop = None

for folder in (AVATAR_DIR, MEDIA_DIR, STICKER_DIR):
    folder.mkdir(exist_ok=True)


# =========================
# БАЗА ДАННЫХ
# =========================

def get_db():
    db = sqlite3.connect(DATABASE_FILE, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def init_database():
    db = get_db()

    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            sender TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Миграция: добавляем колонки для медиа в уже существующую базу
    columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(messages)")
    }
    for column in ("media_type", "media_file", "media_name"):
        if column not in columns:
            db.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")

    db.commit()
    db.close()
    print("База данных готова.")


def save_user(user_id, name, username):
    db = get_db()
    db.execute("""
        INSERT INTO users (id, name, username)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            username = excluded.username
    """, (user_id, name, username))
    db.commit()
    db.close()


def user_exists(user_id):
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    db.close()
    return row is not None


def save_message(user_id, sender, text,
                 media_type=None, media_file=None, media_name=None):
    db = get_db()
    db.execute("""
        INSERT INTO messages (
            user_id, sender, text, media_type, media_file, media_name
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (user_id, sender, text, media_type, media_file, media_name))
    db.commit()
    db.close()


# =========================
# АВАТАРКИ
# =========================

def avatar_is_fresh(user_id):
    """Есть ли свежий кэш (сама аватарка или отметка «фото нет»)."""
    for name in (f"{user_id}.jpg", f"{user_id}.none"):
        path = AVATAR_DIR / name
        if path.exists() and time.time() - path.stat().st_mtime < AVATAR_TTL:
            return True
    return False


async def update_avatar(user_id):
    jpg = AVATAR_DIR / f"{user_id}.jpg"
    none = AVATAR_DIR / f"{user_id}.none"

    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)

        if photos.total_count == 0:
            # Фото нет или оно скрыто настройками приватности
            jpg.unlink(missing_ok=True)
            none.touch()
            return

        sizes = photos.photos[0]
        size = sizes[1] if len(sizes) > 1 else sizes[0]

        await bot.download(size.file_id, destination=jpg)
        none.unlink(missing_ok=True)

    except Exception as error:
        print("[AVATAR ERROR]", user_id, error)
        # Чтобы не долбить Telegram при каждой загрузке страницы
        none.touch()


# =========================
# ВХОДЯЩИЕ МЕДИА
# =========================

def safe_ext(filename, default):
    """Расширение из имени файла, только если оно безопасное."""
    if filename:
        suffix = Path(filename).suffix.lower()
        if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
            return suffix
    return default


def extract_media(message):
    """
    Возвращает (тип, file_id, расширение, имя, размер)
    или None, если в сообщении нет медиа.
    """
    if message.photo:
        photo = message.photo[-1]  # самое большое разрешение
        return "photo", photo.file_id, ".jpg", None, photo.file_size

    # animation проверяем раньше document: у GIF заполнены оба поля
    if message.animation:
        a = message.animation
        return ("animation", a.file_id,
                safe_ext(a.file_name, ".mp4"), a.file_name, a.file_size)

    if message.video:
        v = message.video
        return ("video", v.file_id,
                safe_ext(v.file_name, ".mp4"), v.file_name, v.file_size)

    if message.video_note:
        v = message.video_note
        return "video_note", v.file_id, ".mp4", None, v.file_size

    if message.voice:
        v = message.voice
        return "voice", v.file_id, ".ogg", None, v.file_size

    if message.audio:
        a = message.audio
        return ("audio", a.file_id,
                safe_ext(a.file_name, ".mp3"),
                a.file_name or a.title, a.file_size)

    if message.sticker:
        s = message.sticker
        if s.is_animated:
            # .tgs (Lottie) в браузере просто так не показать
            return "sticker_tgs", s.file_id, ".tgs", s.emoji, s.file_size
        ext = ".webm" if s.is_video else ".webp"
        return "sticker", s.file_id, ext, s.emoji, s.file_size

    if message.document:
        d = message.document
        return ("document", d.file_id,
                safe_ext(d.file_name, ""), d.file_name, d.file_size)

    return None


async def download_media(message, user_id):
    """Скачивает медиа из сообщения. Возвращает (тип, файл, имя)."""
    media = extract_media(message)

    if media is None:
        return None, None, None

    media_type, file_id, ext, name, size = media

    # Анимированные стикеры не качаем, показываем только эмодзи
    if media_type == "sticker_tgs":
        return media_type, None, name

    if size and size > MAX_DOWNLOAD_SIZE:
        print(f"[MEDIA] файл слишком большой: {size} байт")
        return media_type, None, name

    filename = f"{user_id}_{message.message_id}{ext}"

    try:
        await bot.download(file_id, destination=MEDIA_DIR / filename)
        return media_type, filename, name
    except Exception as error:
        print("[MEDIA ERROR]", error)
        return media_type, None, name


# =========================
# ИСХОДЯЩИЕ МЕДИА
# =========================

async def send_media(chat_id, path, filename, caption, mode):
    """
    Отправляет файл в Telegram. Пробует способы из плана по очереди:
    например, webp -> стикер, если Telegram не принял -> обычный файл.
    Возвращает (сообщение, фактический тип).
    """
    plans = LIBRARY_PLANS if mode == "library" else ATTACH_PLANS
    plan = list(plans.get(path.suffix.lower(), ["document"]))

    # У стикеров не бывает подписи
    if caption and "sticker" in plan:
        plan.remove("sticker")

    file = types.FSInputFile(path, filename=filename)
    caption = caption or None
    last_error = None

    for kind in plan:
        try:
            if kind == "photo":
                message = await bot.send_photo(chat_id, file, caption=caption)
            elif kind == "animation":
                message = await bot.send_animation(chat_id, file, caption=caption)
            elif kind == "video":
                message = await bot.send_video(
                    chat_id, file, caption=caption, supports_streaming=True
                )
            elif kind == "voice":
                message = await bot.send_voice(chat_id, file, caption=caption)
            elif kind == "audio":
                message = await bot.send_audio(chat_id, file, caption=caption)
            elif kind == "sticker":
                message = await bot.send_sticker(chat_id, file)
            else:
                message = await bot.send_document(chat_id, file, caption=caption)

            return message, kind

        except TelegramBadRequest as error:
            print(f"[SEND] {kind} не подошёл: {error.message}")
            last_error = error

    raise last_error


def error_text(error):
    if isinstance(error, TelegramForbiddenError):
        return "Пользователь заблокировал бота"
    if isinstance(error, TelegramBadRequest):
        return f"Telegram отклонил сообщение: {error.message}"
    if error.__class__.__name__ == "TimeoutError":
        return "Telegram не ответил вовремя"
    return str(error) or error.__class__.__name__


def deliver_text(user_id, text):
    """Отправляет текст пользователю и сохраняет в историю."""
    if telegram_loop is None:
        raise RuntimeError("Бот ещё запускается")

    future = asyncio.run_coroutine_threadsafe(
        bot.send_message(chat_id=user_id, text=text),
        telegram_loop
    )
    future.result(timeout=15)

    save_message(user_id, "admin", text)
    print(f"[ADMIN -> {user_id}] {text}")


def deliver_file(user_id, src, display_name, caption, mode):
    """
    Отправляет файл пользователю и сохраняет его в историю чата.
    mode="attach": src — временный файл, он будет перемещён в media/
    mode="library": src — файл из стикерпака, он будет скопирован
    """
    if telegram_loop is None:
        raise RuntimeError("Бот ещё запускается")

    future = asyncio.run_coroutine_threadsafe(
        send_media(user_id, src, display_name, caption, mode),
        telegram_loop
    )
    message, kind = future.result(timeout=180)

    final_name = f"{user_id}_{message.message_id}{src.suffix.lower()}"
    final_path = MEDIA_DIR / final_name

    if mode == "attach":
        src.replace(final_path)
    else:
        shutil.copy2(src, final_path)

    save_message(
        user_id, "admin", caption or "",
        kind, final_name,
        display_name if kind in ("document", "audio") else None
    )
    print(f"[ADMIN -> {user_id}] {kind}: {display_name}")


# =========================
# СТИКЕРПАКИ (папки в stickers/)
# =========================

def clean_name(name, limit=40):
    """Имя, безопасное для файловой системы."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(name or ""))
    return name.strip().strip(".").strip()[:limit].strip()


def pack_dir(name, create=False):
    clean = clean_name(name)
    if not clean:
        raise ValueError("Введите название пака")

    folder = STICKER_DIR / clean

    if create:
        folder.mkdir(exist_ok=True)
    elif not folder.is_dir():
        raise FileNotFoundError("Пак не найден")

    return folder


def unique_path(folder, filename):
    path = folder / filename
    counter = 1
    while path.exists():
        path = folder / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
        counter += 1
    return path


def list_packs():
    packs = []

    for folder in sorted(STICKER_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not folder.is_dir():
            continue

        files = [
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in STICKER_EXT
        ]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        packs.append({
            "name": folder.name,
            "items": [
                {
                    "file": f.name,
                    "video": f.suffix.lower() in (".mp4", ".webm"),
                }
                for f in files
            ],
        })

    return packs


@app.route("/stickers")
def stickers_page():
    return render_template("stickers.html")


@app.route("/api/stickers")
def api_stickers():
    return jsonify(packs=list_packs())


@app.route("/sticker-file/<pack>/<filename>")
def sticker_file(pack, filename):
    try:
        folder = pack_dir(pack)
    except (ValueError, FileNotFoundError):
        abort(404)

    if Path(filename).suffix.lower() not in STICKER_EXT:
        abort(404)

    response = send_from_directory(folder, filename, max_age=3600)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/api/packs", methods=["POST"])
def api_pack_create():
    data = request.get_json(silent=True) or {}
    try:
        pack_dir(data.get("name"), create=True)
    except ValueError as error:
        return jsonify(ok=False, error=str(error)), 400
    return jsonify(ok=True)


@app.route("/api/packs/rename", methods=["POST"])
def api_pack_rename():
    data = request.get_json(silent=True) or {}
    try:
        old = pack_dir(data.get("name"))
        new_name = clean_name(data.get("new_name"))
        if not new_name:
            raise ValueError("Введите новое название")
        new = STICKER_DIR / new_name
        if new.exists():
            raise ValueError("Пак с таким названием уже есть")
        old.rename(new)
    except (ValueError, FileNotFoundError) as error:
        return jsonify(ok=False, error=str(error)), 400
    return jsonify(ok=True)


@app.route("/api/packs/delete", methods=["POST"])
def api_pack_delete():
    data = request.get_json(silent=True) or {}
    try:
        shutil.rmtree(pack_dir(data.get("name")))
    except (ValueError, FileNotFoundError) as error:
        return jsonify(ok=False, error=str(error)), 400
    return jsonify(ok=True)


@app.route("/api/stickers/upload", methods=["POST"])
def api_stickers_upload():
    try:
        folder = pack_dir(request.form.get("pack"), create=True)
    except ValueError as error:
        return jsonify(ok=False, error=str(error)), 400

    saved = 0
    for upload in request.files.getlist("files"):
        ext = Path(upload.filename or "").suffix.lower()
        if ext not in STICKER_EXT:
            continue
        stem = clean_name(Path(upload.filename).stem, 60) or "sticker"
        upload.save(unique_path(folder, stem + ext))
        saved += 1

    if saved == 0:
        return jsonify(
            ok=False,
            error="Подходят только webp, png, jpg, gif, mp4 и webm"
        ), 400

    return jsonify(ok=True, saved=saved)


@app.route("/api/stickers/save", methods=["POST"])
def api_stickers_save():
    """Сохранить медиа из чата в стикерпак."""
    data = request.get_json(silent=True) or {}

    src = MEDIA_DIR / Path(str(data.get("media_file", ""))).name
    if not src.is_file() or src.suffix.lower() not in STICKER_EXT:
        return jsonify(ok=False, error="Этот файл нельзя сохранить"), 400

    try:
        folder = pack_dir(data.get("pack"), create=True)
    except ValueError as error:
        return jsonify(ok=False, error=str(error)), 400

    shutil.copy2(src, unique_path(folder, src.name))
    return jsonify(ok=True, pack=folder.name)


@app.route("/api/stickers/delete", methods=["POST"])
def api_stickers_delete():
    data = request.get_json(silent=True) or {}
    try:
        folder = pack_dir(data.get("pack"))
        target = folder / Path(str(data.get("file", ""))).name
        if target.is_file() and target.suffix.lower() in STICKER_EXT:
            target.unlink()
    except (ValueError, FileNotFoundError) as error:
        return jsonify(ok=False, error=str(error)), 400
    return jsonify(ok=True)


# =========================
# TELEGRAM
# =========================

@dp.message(CommandStart())
async def start(message: types.Message):
    user = message.from_user
    save_user(user.id, user.full_name or "Без имени", user.username)

    await message.answer(
        "Привет! 👋\n\n"
        "Напиши сюда сообщение, и оператор ответит тебе."
    )


@dp.message()
async def receive_message(message: types.Message):
    user = message.from_user
    save_user(user.id, user.full_name or "Без имени", user.username)

    media_type, media_file, media_name = await download_media(
        message, user.id
    )

    text = message.text or message.caption or ""

    if not text and not media_type:
        text = "[Неподдерживаемый тип сообщения]"

    save_message(
        user.id, "user", text,
        media_type, media_file, media_name
    )

    print(f"[MESSAGE] {user.id}: {media_type or 'text'} {text}")

    await message.answer(
        "Сообщение получено ✅\n"
        "Ожидай ответа оператора."
    )


# =========================
# WEB
# =========================

@app.before_request
def block_cross_site_posts():
    """Чужие сайты не должны уметь слать запросы к панели."""
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            abort(403)


@app.errorhandler(413)
def too_large(error):
    return jsonify(ok=False, error="Файл слишком большой (лимит 50 МБ)"), 413


@app.route("/")
def index():
    db = get_db()
    rows = db.execute("""
        SELECT
            users.*,
            (
                SELECT text FROM messages
                WHERE messages.user_id = users.id
                ORDER BY messages.id DESC LIMIT 1
            ) AS last_text,
            (
                SELECT media_type FROM messages
                WHERE messages.user_id = users.id
                ORDER BY messages.id DESC LIMIT 1
            ) AS last_media,
            (
                SELECT MAX(id) FROM messages
                WHERE messages.user_id = users.id
            ) AS last_id
        FROM users
        ORDER BY last_id IS NULL, last_id DESC, users.created_at DESC
    """).fetchall()
    db.close()

    users = []
    for row in rows:
        user = dict(row)

        parts = []
        if user["last_media"]:
            parts.append(MEDIA_LABELS.get(user["last_media"], "📎 Файл"))
        if user["last_text"]:
            parts.append(user["last_text"])
        user["last_message"] = " ".join(parts)

        users.append(user)

    return render_template("index.html", users=users)


@app.route("/avatar/<int:user_id>")
def avatar(user_id):
    if not avatar_is_fresh(user_id) and telegram_loop is not None:
        future = asyncio.run_coroutine_threadsafe(
            update_avatar(user_id), telegram_loop
        )
        try:
            future.result(timeout=10)
        except Exception as error:
            print("[AVATAR ERROR]", error)

    if not (AVATAR_DIR / f"{user_id}.jpg").exists():
        abort(404)

    return send_from_directory(AVATAR_DIR, f"{user_id}.jpg", max_age=3600)


@app.route("/media/<filename>")
def media(filename):
    ext = Path(filename).suffix.lower()

    response = send_from_directory(
        MEDIA_DIR,
        filename,
        as_attachment=ext not in INLINE_EXT,
        max_age=3600
    )
    response.headers["X-Content-Type-Options"] = "nosniff"

    return response


@app.route("/chat/<int:user_id>", methods=["GET", "POST"])
def chat(user_id):
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()

    if user is None:
        db.close()
        return "Пользователь не найден", 404

    # Запасной вариант: отправка текста обычной формой без JS
    if request.method == "POST":
        db.close()
        text = request.form.get("message", "").strip()

        if text:
            try:
                deliver_text(user_id, text)
            except Exception as error:
                print("[TELEGRAM ERROR]", error)

        return redirect(f"/chat/{user_id}")

    messages = db.execute("""
        SELECT id, sender, text, media_type, media_file, media_name, created_at
        FROM messages
        WHERE user_id = ?
        ORDER BY id ASC
    """, (user_id,)).fetchall()
    db.close()

    return render_template("chat.html", user=user, messages=messages)


@app.route("/chat/<int:user_id>/send", methods=["POST"])
def chat_send(user_id):
    """Текст и/или файлы от оператора."""
    if not user_exists(user_id):
        return jsonify(ok=False, error="Пользователь не найден"), 404

    text = request.form.get("message", "").strip()
    uploads = [f for f in request.files.getlist("files") if f.filename]
    errors = []

    if not uploads:
        if text:
            try:
                deliver_text(user_id, text)
            except Exception as error:
                errors.append(error_text(error))
    else:
        for index, upload in enumerate(uploads):
            display_name = Path(upload.filename).name[:100]
            tmp = MEDIA_DIR / f"tmp_{uuid.uuid4().hex}{safe_ext(display_name, '')}"
            upload.save(tmp)

            try:
                # Подпись из поля ввода получает первый файл
                deliver_file(
                    user_id, tmp, display_name,
                    text if index == 0 else "", "attach"
                )
            except Exception as error:
                errors.append(f"{display_name}: {error_text(error)}")
            finally:
                tmp.unlink(missing_ok=True)

    return jsonify(ok=not errors, error="; ".join(errors))


@app.route("/chat/<int:user_id>/sticker", methods=["POST"])
def chat_send_sticker(user_id):
    """Отправить стикер/GIF/картинку из стикерпака."""
    if not user_exists(user_id):
        return jsonify(ok=False, error="Пользователь не найден"), 404

    data = request.get_json(silent=True) or {}

    try:
        folder = pack_dir(data.get("pack"))
        name = Path(str(data.get("file", ""))).name
        src = folder / name

        if not src.is_file() or src.suffix.lower() not in STICKER_EXT:
            raise FileNotFoundError("Стикер не найден")

        deliver_file(user_id, src, name, "", "library")

    except Exception as error:
        return jsonify(ok=False, error=error_text(error))

    return jsonify(ok=True)


# =========================
# ЗАПУСК
# =========================

async def telegram_main():
    global telegram_loop
    telegram_loop = asyncio.get_running_loop()

    print("Telegram бот запускается...")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


def web_main():
    print(f"Веб-панель: http://{WEB_HOST}:{WEB_PORT}")
    app.run(
        host=WEB_HOST,
        port=WEB_PORT,
        debug=False,
        use_reloader=False
    )


async def main():
    init_database()

    threading.Thread(target=web_main, daemon=True).start()

    await telegram_main()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nБот остановлен.")
