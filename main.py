import asyncio
import json
import os
import secrets
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command
from aiogram.types import Message
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
import uvicorn

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
BASE_URL = os.environ["BASE_URL"].rstrip("/")
API_SERVER = os.getenv("TELEGRAM_API_SERVER", "").strip()

# Telegram's local Bot API server is needed for files larger than 20 MB.
# When configured, it must be reachable from this Python process.
if API_SERVER:
    api = TelegramAPIServer.from_base(API_SERVER, is_local=True)
    session = AiohttpSession(api=api)
    bot = Bot(BOT_TOKEN, session=session)
else:
    bot = Bot(BOT_TOKEN)

dp = Dispatcher()
app = FastAPI(title="IPA download server")

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
UPLOAD_DIR = DATA_DIR / "ipa"
STATE_FILE = DATA_DIR / "current.json"
MAX_FILE_SIZE = 2_000_000_000  # 2 GB decimal; Telegram local Bot API limit

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def read_state():
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def write_state(data):
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Привет! Я раздаю готовые подписанные IPA-файлы.\\n\\n"
        "Администратор может отправить мне IPA документом. "
        "После загрузки я выдам новую ссылку для скачивания."
    )


@dp.message(Command("link"))
async def current_link(message: Message):
    if not is_admin(message):
        return
    state = read_state()
    if not state:
        await message.answer("Пока IPA-файл не загружен.")
        return
    await message.answer(f"Текущая ссылка:\\n{BASE_URL}/d/{state['token']}")


@dp.message(F.document)
async def receive_ipa(message: Message):
    if not is_admin(message):
        await message.answer("Загрузка доступна только администратору.")
        return

    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".ipa"):
        await message.answer("Пришли именно файл с расширением .ipa.")
        return

    if doc.file_size is None or doc.file_size > MAX_FILE_SIZE:
        await message.answer("Файл больше допустимого лимита 2 ГБ.")
        return

    status = await message.answer("Получил файл. Загружаю на сервер…")
    temp_path = UPLOAD_DIR / "uploading.tmp"

    try:
        tg_file = await bot.get_file(doc.file_id)
        # Downloads to disk; the file is not loaded into RAM.
        await bot.download_file(tg_file.file_path, destination=temp_path)

        actual_size = temp_path.stat().st_size
        if actual_size > MAX_FILE_SIZE:
            temp_path.unlink(missing_ok=True)
            await status.edit_text("Файл превышает лимит 2 ГБ.")
            return

        safe_name = Path(doc.file_name).name
        token = secrets.token_urlsafe(24)
        final_path = UPLOAD_DIR / f"{token}.ipa"
        temp_path.replace(final_path)

        old = read_state()
        write_state({
            "token": token,
            "filename": safe_name,
            "path": str(final_path),
            "size": actual_size,
        })

        # Each update gets a new link. Remove the previous version.
        if old and old.get("path"):
            old_path = Path(old["path"])
            if old_path.exists() and old_path != final_path:
                old_path.unlink()

        await status.edit_text(
            "Готово! IPA загружен.\\n\\n"
            f"Файл: {safe_name}\\n"
            f"Размер: {actual_size / (1024 ** 2):.1f} МБ\\n\\n"
            f"Новая ссылка:\\n{BASE_URL}/d/{token}"
        )
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        await status.edit_text(
            "Не удалось загрузить файл. Проверь настройки Telegram Bot API "
            "и свободное место на сервере.\\n"
            f"Техническая ошибка: {type(exc).__name__}"
        )


@app.get("/d/{token}")
async def download_ipa(token: str):
    state = read_state()
    if not state or not secrets.compare_digest(token, state.get("token", "")):
        raise HTTPException(status_code=404, detail="Ссылка недействительна или устарела")

    path = Path(state["path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")

    return FileResponse(
        path=path,
        filename=state["filename"],
        media_type="application/octet-stream",
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


async def run_bot():
    await dp.start_polling(bot)


async def run_web():
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    await asyncio.gather(run_bot(), run_web())


if __name__ == "__main__":
    asyncio.run(main())
