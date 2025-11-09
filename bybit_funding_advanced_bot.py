import asyncio
import json
import logging
import math
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import websockets
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

CONFIG_PATH = Path("config.json")
STATE_PATH = Path("state.json")

if not CONFIG_PATH.exists():
    raise SystemExit("Створіть config.json з telegram_token та (опціонально) chat_id")

with open(CONFIG_PATH, "r") as f:
    CONFIG = json.load(f)

TELEGRAM_TOKEN = CONFIG["telegram_token"]
CHAT_ID = CONFIG.get("chat_id")

BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"
BYBIT_REST = "https://api.bybit.com/v5/market/instruments-info"

DEFAULT_THRESHOLD = 0.5  # % для старту і кроку

# --- STATE ---
state = {}
if STATE_PATH.exists():
    with open(STATE_PATH, "r") as f:
        state = json.load(f)


def save_state():
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


async def fetch_usdt_symbols():
    """Отримує список всіх perpetual USDT контрактів."""
    async with aiohttp.ClientSession() as session:
        async with session.get(BYBIT_REST, params={"category": "linear"}) as r:
            data = await r.json()
            symbols = [
                i["symbol"] for i in data["result"]["list"]
                if i.get("quoteCoin") == "USDT" and i.get("contractType") == "LinearPerpetual"
            ]
            return symbols


def get_alert_level(fr_pct, threshold):
    """Повертає найближчу кратну сходинку (±threshold)."""
    if abs(fr_pct) < threshold:
        return 0
    sign = 1 if fr_pct > 0 else -1
    level = math.floor(abs(fr_pct) / threshold) * threshold
    return sign * level


async def handle_ticker(item, app):
    symbol = item.get("symbol")
    if not symbol:
        return

    fr_raw = item.get("fundingRate")
    if fr_raw is None:
        return
    try:
        fr = float(fr_raw)
    except:
        return

    fr_pct = fr * 100
    nft_raw = item.get("nextFundingTime")
    if not nft_raw:
        return

    try:
        nft = datetime.fromtimestamp(int(nft_raw) / 1000, tz=timezone.utc)
    except:
        return

    now = datetime.now(tz=timezone.utc)
    delta = nft - now
    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)

    st = state.setdefault(symbol, {
        "last_alert": 0,
        "last_hours": None,
        "threshold": DEFAULT_THRESHOLD,
        "history": []
    })

    threshold = st["threshold"]
    alert_level = get_alert_level(fr_pct, threshold)

    # зберігаємо історію для звіту (до 24 год)
    st["history"].append({
        "time": now.isoformat(),
        "fr": fr_pct,
        "nft": int(nft_raw)
    })
    # тримаємо історію за останні 24 год
    st["history"] = [
        h for h in st["history"]
        if now - datetime.fromisoformat(h["time"]) < timedelta(hours=24)
    ]

    # якщо повернулося в норму
    if alert_level == 0:
        if st["last_alert"] != 0:
            st["last_alert"] = 0
            save_state()
        return

    # чи перетнув наступну сходинку
    if alert_level != st["last_alert"]:
        st["last_alert"] = alert_level
        save_state()
        msg = (
            f"⚡ Funding Alert: {symbol}\n"
            f"Funding Rate: {fr_pct:.4f}%\n"
            f"Порог: ±{threshold}%\n"
            f"Рівень: {alert_level:+.2f}%\n"
            f"До списання: {hours} год {minutes} хв"
        )
        await send_msg(app, msg)

    # чи змінився інтервал списання на ≥ 1 год
    if st["last_hours"] is None or abs(st["last_hours"] - hours) >= 1:
        st["last_hours"] = hours
        save_state()
        if abs(fr_pct) >= threshold:
            msg = (
                f"🕒 Зміна інтервалу списання funding для {symbol}\n"
                f"Funding Rate: {fr_pct:.4f}%\n"
                f"Новий інтервал: {hours} год {minutes} хв"
            )
            await send_msg(app, msg)


async def send_msg(app, text):
    if not CONFIG.get("chat_id"):
        logging.warning("Chat ID не встановлено — повідомлення не відправлене.")
        return
    try:
        await app.bot.send_message(chat_id=CONFIG["chat_id"], text=text)
    except Exception as e:
        logging.error(f"Помилка відправлення: {e}")


async def ws_loop(app):
    """Основний WebSocket цикл."""
    while True:
        try:
            symbols = await fetch_usdt_symbols()
            topics = [f"tickers.{s}" for s in symbols]

            async with websockets.connect(BYBIT_WS, ping_interval=15) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                logging.info(f"Підписано на {len(topics)} символів")

                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        if isinstance(data.get("data"), list):
                            for item in data["data"]:
                                await handle_ticker(item, app)
                        elif isinstance(data.get("data"), dict):
                            await handle_ticker(data["data"], app)
                    except Exception as e:
                        logging.warning(f"Помилка обробки: {e}")
        except Exception as e:
            logging.error(f"WebSocket error: {e}")
            await asyncio.sleep(5)


# --- Telegram Commands ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CONFIG["chat_id"] = update.effective_chat.id
    with open(CONFIG_PATH, "w") as f:
        json.dump(CONFIG, f, indent=2)
    await update.message.reply_text("✅ Бот активовано! Відслідковує всі USDT perpetual контракти.")


async def set_threshold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        threshold = float(context.args[0])
    except:
        await update.message.reply_text("Вкажіть поріг у %. Приклад: /setthreshold 0.7")
        return

    for s in state.values():
        s["threshold"] = threshold
    save_state()
    await update.message.reply_text(f"✅ Новий глобальний поріг: ±{threshold}%.")


async def get_threshold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not state:
        await update.message.reply_text(f"Поточний поріг: ±{DEFAULT_THRESHOLD}% (за замовчуванням)")
        return

    t = list(state.values())[0].get("threshold", DEFAULT_THRESHOLD)
    await update.message.reply_text(f"Поточний поріг: ±{t}%")


async def daily_report(app):
    """Надсилає щоденний звіт раз на добу."""
    while True:
        now = datetime.now(tz=timezone.utc)
        next_run = (now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
        delay = (next_run - now).total_seconds()
        await asyncio.sleep(delay)

        lines = ["📊 Щоденний звіт Funding Rate (ост. 24 год)\n"]
        for sym, s in state.items():
            history = s.get("history", [])
            if not history:
                continue
            max_fr = max(abs(h["fr"]) for h in history)
            if max_fr >= s.get("threshold", DEFAULT_THRESHOLD):
                last = history[-1]
                dt = datetime.fromisoformat(last["time"]).strftime("%H:%M")
                lines.append(f"{sym}: {last['fr']:+.4f}% о {dt} UTC")

        if len(lines) == 1:
            lines.append("За останні 24 год жодна монета не перевищила поріг.")

        await send_msg(app, "\n".join(lines))


# --- MAIN ---
async def main():
    logging.basicConfig(level=logging.INFO)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("setthreshold", set_threshold_cmd))
    app.add_handler(CommandHandler("getthreshold", get_threshold_cmd))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    ws_task = asyncio.create_task(ws_loop(app))
    report_task = asyncio.create_task(daily_report(app))

    stop_event = asyncio.Event()

    def stop_handler(*_):
        stop_event.set()

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_event_loop().add_signal_handler(sig, stop_handler)
    except NotImplementedError:
        # Windows does not support add_signal_handler
        pass

    await stop_event.wait()
    ws_task.cancel()
    report_task.cancel()
    await app.updater.stop()
    await app.stop()
    await app.shutdown()



if __name__ == "__main__":
    asyncio.run(main())
