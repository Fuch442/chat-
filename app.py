"""
Онлайн-чат: PyWebIO + Redis + Комнаты + Авторизация
Redis-структуры:
  users:{username}     → hash   {password, created}
  rooms                → set    (названия всех комнат)
  room:{r}:password    → string (hash пароля, если закрытая)
  room:{r}:messages    → list   (последние MAX_HISTORY JSON-сообщений)
  room:{r}:online      → set    (никнеймы онлайн)
  channel:{r}          → Pub/Sub канал
"""

import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone

import redis.asyncio as aioredis
from pywebio import start_server
from pywebio.input import actions, input, input_group, radio, PASSWORD
from pywebio.output import (
    clear, output, put_markdown, put_scrollable, toast, put_buttons
)
from pywebio.session import run_async, run_js

REDIS_URL   = os.environ.get("REDIS_URL", "redis://localhost:6379")
MAX_HISTORY = 100


# ── helpers ───────────────────────────────────────────────────────────────────

def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M")

def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def fmt_msg(author: str, text: str, ts: str):
    if author == "📢":
        return put_markdown(f"<small style='color:#888'>{ts}</small> 📢 _{text}_")
    return put_markdown(f"<small style='color:#888'>{ts}</small> **{author}**: {text}")

def pack(author: str, text: str) -> str:
    return json.dumps({"a": author, "t": text, "ts": now_str()})

def unpack(s: str) -> tuple[str, str, str]:
    d = json.loads(s)
    return d["a"], d["t"], d["ts"]


# ── redis helpers ─────────────────────────────────────────────────────────────

async def make_redis() -> aioredis.Redis:
    return await aioredis.from_url(REDIS_URL, decode_responses=True)

async def register(r, username: str, password: str) -> str | None:
    if await r.exists(f"users:{username}"):
        return "Пользователь уже существует!"
    await r.hset(f"users:{username}", mapping={
        "password": hash_pw(password), "created": now_str()
    })
    return None

async def authenticate(r, username: str, password: str) -> bool:
    stored = await r.hget(f"users:{username}", "password")
    return stored == hash_pw(password)

async def get_rooms(r) -> list[str]:
    return sorted(await r.smembers("rooms") or [])

async def create_room(r, name: str, password: str | None):
    await r.sadd("rooms", name)
    if password:
        await r.set(f"room:{name}:password", hash_pw(password))

async def room_is_locked(r, name: str) -> bool:
    return bool(await r.exists(f"room:{name}:password"))

async def check_room_password(r, name: str, pw: str) -> bool:
    stored = await r.get(f"room:{name}:password")
    return stored is None or stored == hash_pw(pw)

async def push_message(r, room: str, author: str, text: str):
    msg = pack(author, text)
    await r.rpush(f"room:{room}:messages", msg)
    await r.ltrim(f"room:{room}:messages", -MAX_HISTORY, -1)
    await r.publish(f"channel:{room}", msg)

async def get_history(r, room: str, n: int = 50):
    return [unpack(m) for m in await r.lrange(f"room:{room}:messages", -n, -1)]

async def join_room(r, room: str, username: str):
    await r.sadd(f"room:{room}:online", username)

async def leave_room(r, room: str, username: str):
    await r.srem(f"room:{room}:online", username)

async def online_count(r, room: str) -> int:
    return await r.scard(f"room:{room}:online")


# ── auth screen ───────────────────────────────────────────────────────────────

async def auth_screen(r) -> str | None:
    put_markdown("## 🧊 Онлайн-чат")
    put_markdown("*Redis · Комнаты · Авторизация*")

    while True:
        action = await actions("Добро пожаловать!", ["Войти", "Зарегистрироваться"])
        if action is None:
            return None

        if action == "Зарегистрироваться":
            data = await input_group("Регистрация", [
                input("Имя пользователя", name="username", required=True,
                      placeholder="nickname"),
                input("Пароль", name="password", type=PASSWORD, required=True,
                      validate=lambda v: "Минимум 4 символа!" if len(v) < 4 else None),
                input("Повторите пароль", name="password2", type=PASSWORD, required=True),
            ], validate=lambda d: ("password2", "Пароли не совпадают!")
               if d["password"] != d["password2"] else None)
            if data is None:
                continue
            err = await register(r, data["username"], data["password"])
            if err:
                toast(err, color="error")
            else:
                toast("✅ Аккаунт создан! Войдите.", color="success")

        else:
            data = await input_group("Вход", [
                input("Имя пользователя", name="username", required=True),
                input("Пароль", name="password", type=PASSWORD, required=True),
            ])
            if data is None:
                continue
            if await authenticate(r, data["username"], data["password"]):
                clear()
                return data["username"]
            toast("❌ Неверный логин или пароль!", color="error")


# ── room screen ───────────────────────────────────────────────────────────────

async def room_screen(r, username: str) -> str | None:
    put_markdown(f"### 👋 Привет, **{username}**!")

    while True:
        rooms  = await get_rooms(r)
        labels = []
        for name in rooms:
            locked = await room_is_locked(r, name)
            count  = await online_count(r, name)
            labels.append(f"{'🔒' if locked else '💬'} {name}  ({count} онлайн)")

        action = await actions("", ["Создать комнату", "Обновить"])
        if action is None:
            return None

        if action == "Создать комнату":
            data = await input_group("Новая комната", [
                input("Название", name="name", required=True),
                input("Пароль (пусто = публичная)", name="password", type=PASSWORD),
            ])
            if data is None:
                continue
            if data["name"] in rooms:
                toast("Комната уже существует!", color="error")
                continue
            await create_room(r, data["name"], data["password"] or None)
            toast(f"✅ Комната «{data['name']}» создана!", color="success")
            clear()
            put_markdown(f"### 👋 Привет, **{username}**!")
            continue

        if not labels:
            toast("Нет доступных комнат", color="warn")
            continue

        chosen_label = await radio("Выберите комнату", options=labels)
        if chosen_label is None:
            continue

        chosen_room = rooms[labels.index(chosen_label)]

        if await room_is_locked(r, chosen_room):
            pw = await input("Пароль комнаты", type=PASSWORD, required=True)
            if pw is None:
                continue
            if not await check_room_password(r, chosen_room, pw):
                toast("❌ Неверный пароль!", color="error")
                continue

        clear()
        return chosen_room


# ── chat screen ───────────────────────────────────────────────────────────────

async def chat_screen(r, username: str, room: str):
    await join_room(r, room, username)
    put_markdown(f"## 💬 **{room}**  <small style='color:#888'>вы: {username}</small>")

    msg_box = output()
    put_scrollable(msg_box, height=380, keep_bottom=True)

    for author, text, ts in await get_history(r, room):
        msg_box.append(fmt_msg(author, text, ts))

    await push_message(r, room, "📢", f"`{username}` присоединился к чату!")

    # отдельный клиент для pub/sub
    r_sub = await make_redis()
    refresh_task = run_async(_subscribe(r_sub, username, room, msg_box))

    while True:
        data = await input_group("", [
            input(placeholder="Сообщение...", name="msg"),
            actions(name="cmd", buttons=[
                "Отправить",
                {"label": "Выйти из комнаты", "type": "cancel"},
            ]),
        ], validate=lambda d: ("msg", "Введите текст!")
           if d["cmd"] == "Отправить" and not d["msg"] else None)

        if data is None:
            break

        await push_message(r, room, username, data["msg"])

    refresh_task.close()
    await leave_room(r, room, username)
    await push_message(r, room, "📢", f"`{username}` покинул комнату.")
    await r_sub.aclose()
    toast("Вы покинули комнату.")


async def _subscribe(r_sub, username: str, room: str, msg_box):
    """Слушает Pub/Sub и выводит чужие сообщения мгновенно."""
    pubsub = r_sub.pubsub()
    await pubsub.subscribe(f"channel:{room}")
    try:
        async for raw in pubsub.listen():
            if raw["type"] != "message":
                continue
            author, text, ts = unpack(raw["data"])
            if author != username:
                msg_box.append(fmt_msg(author, text, ts))
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe(f"channel:{room}")
        await pubsub.aclose()


# ── entry point ───────────────────────────────────────────────────────────────

async def main():
    r = await make_redis()
    # дефолтная публичная комната
    if not await r.sismember("rooms", "Общий"):
        await create_room(r, "Общий", None)

    username = await auth_screen(r)
    if not username:
        return

    while True:
        room = await room_screen(r, username)
        if not room:
            break
        await chat_screen(r, username, room)

    put_markdown("### До свидания! 👋")
    put_buttons(["Войти снова"], onclick=lambda _: run_js("window.location.reload()"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    start_server(main, debug=False, port=port, cdn=False)
