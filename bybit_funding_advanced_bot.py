import asyncio
import aiohttp
import json
import logging
from telegram import Bot
from telegram.ext import Application, CommandHandler

# ----------- CONFIG -----------
CONFIG_FILE = "config.json"
BYBIT_REST_SYMBOLS = "https://api.bybit.com/v5/market/instruments-info"
BYBIT_REST_FUNDING = "https://api.bybit.com/v5/market/funding/prev-funding-rate"
FUNDING_THRESHOLD = 0.005  # 0.5%
CHECK_INTERVAL = 60  # секунд
# -------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Завантаження токену та chat_id
with open(CONFIG_FILE, "r") as f:
    CONFIG = json.load(f)

TELEGRAM_TOKEN = CONFIG.get("telegram_token")
CHAT_ID = CONFIG.get("chat_id")  # заповниться після /start

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
    CONFIG["chat_id"] = CHAT_ID
    with open(CONFIG_FILE, "w") as f:
        json.dump(CONFIG, f)
    await update.message.reply_text("Бот активований!")

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
            next_time = funding.get("nextFundingTime", "")
            prev_rate = funding_state.get(sym, 0)

            # Перевірка на поріг ±0.5%
            if abs(rate) >= FUNDING_THRESHOLD and abs(prev_rate - rate) >= FUNDING_THRESHOLD:
                msg = f"Symbol: {sym}\nFunding: {rate*100:.3f}%\nNext funding: {next_time}"
                if CHAT_ID:
                    try:
                        await bot.send_message(chat_id=CHAT_ID, text=msg)
                    except Exception as e:
                        logger.error(f"Error sending message: {e}")
                logger.info(msg)

            funding_state[sym] = rate

        await asyncio.sleep(CHECK_INTERVAL)

# ------------ Run Bot ------------

async def funding_monitor_task():
    await funding_monitor()

if __name__ == "__main__":
    async def main():
        monitor_task = asyncio.create_task(funding_monitor_task())
        await app.run_polling()  # запускає Telegram бота і автоматично ініціалізує Application
        await monitor_task

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
