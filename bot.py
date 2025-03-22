import os
import logging
import threading
import time
from cachetools import TTLCache
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes
)
import requests

# Настройка логгирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
API_URL = "https://api.sorare.com/graphql"
CACHE_TIME = 600
CACHE_MAX_SIZE = 1000
PING_INTERVAL = 300
PING_URL = "https://google.com"

# Кэши
players_cache = TTLCache(maxsize=CACHE_MAX_SIZE, ttl=CACHE_TIME)
prices_cache = TTLCache(maxsize=CACHE_MAX_SIZE, ttl=300)

# Состояния
SELECT_PLAYER = 1

def self_ping():
    while True:
        try:
            response = requests.get(PING_URL, timeout=10)
            logger.info("Self-ping: Status %s", response.status_code)
        except Exception as e:
            logger.error("Self-ping error: %s", str(e))
        time.sleep(PING_INTERVAL)

def search_players(name: str) -> list:
    cached = players_cache.get(name)
    if cached:
        return cached

    query = """
    query SearchPlayers($search: String!) {
        allFootballPlayers(search: $search) {
            nodes {
                slug
                displayName
            }
        }
    }
    """
    
    try:
        response = requests.post(
            API_URL,
            json={"query": query, "variables": {"search": name}},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            timeout=10
        )
        
        logger.debug("API Response: %s", response.text)
        response.raise_for_status()
        
        data = response.json()
        players = data.get("data", {}).get("allFootballPlayers", {}).get("nodes", [])
        players_cache[name] = players
        return players
        
    except Exception as e:
        logger.error("Search error: %s", str(e))
        return []

def get_min_price(slug: str) -> float:
    cached = prices_cache.get(slug)
    if cached is not None:
        return cached

    query = """
    query GetPlayerCards($slug: String!) {
        football {
            player(slug: $slug) {
                cards(rarities: [limited, rare, super_rare, unique], auctionType: [buyNow]) {
                    nodes {
                        price
                    }
                }
            }
        }
    }
    """
    
    try:
        response = requests.post(
            API_URL,
            json={"query": query, "variables": {"slug": slug}},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            timeout=15
        )
        
        response.raise_for_status()
        data = response.json()
        player_data = data.get("data", {}).get("football", {}).get("player", {})
        
        if not player_data:
            return None
            
        cards = player_data.get("cards", {}).get("nodes", [])
        prices = [float(card["price"]) for card in cards if card.get("price")]
        
        if not prices:
            return None
            
        min_price = min(prices)
        prices_cache[slug] = min_price
        return min_price
        
    except Exception as e:
        logger.error("Price error: %s", str(e))
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔍 Введите имя футболиста (например, "Messi"):')

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    player_name = update.message.text.strip()
    
    if not player_name:
        await update.message.reply_text("❌ Введите имя игрока")
        return ConversationHandler.END
    
    players = search_players(player_name)
    
    if not players:
        await update.message.reply_text("🔍 Игрок не найден")
        return ConversationHandler.END
        
    if len(players) > 1:
        keyboard = [[p["displayName"]] for p in players[:5]]
        reply_markup = ReplyKeyboardMarkup(
            keyboard, 
            one_time_keyboard=True,
            resize_keyboard=True
        )
        await update.message.reply_text(
            "🔢 Выберите игрока:",
            reply_markup=reply_markup
        )
        context.user_data["players"] = players
        return SELECT_PLAYER
        
    return await handle_player_selection(update, context, players[0])

async def handle_player_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, player: dict = None):
    if not player:
        selected_name = update.message.text
        players = context.user_data.get("players", [])
        player = next((p for p in players if p["displayName"] == selected_name), None)
    
    if not player:
        await update.message.reply_text("❌ Ошибка выбора")
        return ConversationHandler.END
        
    try:
        min_price = get_min_price(player["slug"])
        
        if min_price is None:
            response_text = f"ℹ️ {player['displayName']}: Нет карточек"
        else:
            response_text = f"✅ {player['displayName']}\nМинимальная цена: {min_price:.2f} ETH"
            
        await update.message.reply_text(response_text)
        
    except Exception as e:
        logger.error("Ошибка: %s", str(e))
        await update.message.reply_text("⚠️ Ошибка получения данных")
        
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Отменено")
    return ConversationHandler.END

def main():
    ping_thread = threading.Thread(target=self_ping, daemon=True)
    ping_thread.start()

    application = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)],
        states={SELECT_PLAYER: [MessageHandler(filters.TEXT, handle_player_selection)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)

    application.run_polling()

if __name__ == '__main__':
    main()