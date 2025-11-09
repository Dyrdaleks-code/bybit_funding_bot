import asyncio
import aiohttp
import logging
import os
from telegram import Bot
from telegram.ext import Application, CommandHandler

# ----------- CONFIG -----------
BYBIT_REST_SYMBOLS = "https://api.bybit.com/v5/market/instruments-info"
BYBIT_REST_FUNDING = "https://api.bybit.com/v5/market/funding/prev-funding-rate"
FUNDING_THRESHOLD = 0.005  # 0.5%
CHECK_INTERVAL = 60  # секунд
# -------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Завантажуємо токен та chat_id з Environment Variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # залишаємо None, бот запише при /start

bot = Bot(token=TELEGRAM_TOKEN)
app = Application.builder().token(TELEGRAM_TOKEN).build()

# Зберігаємо попередні значення фандингу
funding_state = {}

# ------------ Bybit REST Functions ------------

async def fetch_usdt_symbols():
    """Отримує список всіх perpetual USDT контрактів."""
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    params = {"category": "linear"}
    async with aiohttp.ClientSession() as session:
        async with session.get(BYBIT_REST_SYMBOLS, headers=headers, params=params) as r:
            if r.status != 200:
                logger.error(f"Error {r.status} fetching symbols: {await r.text()}")
                return []
            data = await r.json()
            symbols = [
                i["symbol"] for i in data["result"]["list"]
                if i.get("quoteCoin") == "USDT" and i.get("contractType") == "LinearPerpetual"
            ]
            return symbols

async def fetch_funding(symbol: str):
    """Отримує останній фандинг для символу."""
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    params = {"symbol": symbol}
    async with aiohttp.ClientSession() as session:
        async with session.get(BYBIT_REST_FUNDING, headers=headers, params=params) as r:
            if r.status != 200:
                logger.error(f"Error {r.status} fetching funding for {symbol}: {await r.text()}")
                return None
            data = await r.json()
            return data.get("result", {})

# ------------ Telegram Commands ------------

async def start(update, context):
    global CHAT_ID
    CHAT_ID = update.effective_chat.id
    await update.message.reply_text("Бот активований! Ви будете отримувати повідомлення про фандинг.")

app.add_handler(CommandHandler("start", start))

# ------------ Main Loop ------------

async def funding_monitor():
    symbols = await fetch_usdt_symbols()
    if not symbols:
        logger.error("No symbols found, stopping monitor")
        return

    logger.info(f"Monitoring {len(symbols)} USDT perpetual symbols")

    while True:
        for sym in symbols:
            funding = await fetch_funding(sym)
            if not funding:
                continue
            rate = float(funding.get("fundingRate", 0))
            interval_sec = funding.get("fundingInterval", 3600)
            hours = interval_sec // 3600
            minutes = (interval_sec % 3600) // 60

            prev_rate = funding_state.get(sym, 0)
            if abs(rate) >= FUNDING_THRESHOLD and abs(rate - prev_rate) >= FUNDING_THRESHOLD:
                if CHAT_ID:
                    msg = f"{sym}: Funding rate {rate*100:.2f}%\nInterval: {hours}h {minutes}m"
                    try:
                        await bot.send_message(chat_id=CHAT_ID, text=msg)
                    except Exception as e:
                        logger.error(f"Failed to send message: {e}")
                funding_state[sym] = rate
            elif abs(rate) < FUNDING_THRESHOLD:
                funding_state[sym] = 0  # обнуляємо стан, щоб повідомлення знову надсилались при підйомі

        await asyncio.sleep(CHECK_INTERVAL)

# ------------ Entry Point ------------

async def main():
    # Запускаємо моніторинг у фоновому режимі
    monitor_task = asyncio.create_task(funding_monitor())

    # Запускаємо Telegram бота
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Очікуємо завершення моніторингу
    await monitor_task

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually")
