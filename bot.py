import os
import logging
import threading
import time
import asyncio
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

# Конфигурационные константы
API_URL = "https://api.sorare.com/graphql"
CACHE_TIME = 600  # 10 минут кэширования игроков
CACHE_MAX_SIZE = 1000  # Максимальное количество элементов в кэше
PING_INTERVAL = 300  # 5 минут в секундах
PING_URL = "https://google.com"  # URL для проверки соединения

# Инициализация кэшей
players_cache = TTLCache(maxsize=CACHE_MAX_SIZE, ttl=CACHE_TIME)
prices_cache = TTLCache(maxsize=CACHE_MAX_SIZE, ttl=300)  # 5 минут для цен

# Состояния диалога
SELECT_PLAYER = 1

def self_ping():
    """Фоновая задача для поддержания активности инстанса"""
    while True:
        try:
            response = requests.get(PING_URL, timeout=10)
            if response.status_code == 200:
                logger.info("Self-ping выполнен успешно")
            else:
                logger.warning(f"Неожиданный статус при self-ping: {response.status_code}")
        except Exception as e:
            logger.error(f"Ошибка при self-ping: {str(e)}")
        time.sleep(PING_INTERVAL)

def search_players(name: str) -> list:
    """Поиск игроков через API Sorare"""
    cached = players_cache.get(name)
    if cached:
        return cached

    query = """
    query SearchPlayers($name: String!) {
        football {
            allPlayers(search: $name) {
                nodes {
                    slug
                    displayName
                }
            }
        }
    }
    """
    
    try:
        response = requests.post(
            API_URL,
            json={"query": query, "variables": {"name": name}},
            timeout=10
        )
        response.raise_for_status()
        
        data = response.json()
        players = data.get("data", {}).get("football", {}).get("allPlayers", {}).get("nodes", [])
        players_cache[name] = players
        return players
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка API при поиске игроков: {e}")
        return []
    except KeyError as e:
        logger.error(f"Ошибка парсинга ответа API: {e}")
        return []

def get_min_price(slug: str) -> float:
    """Получение минимальной цены карточек игрока"""
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
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка API при получении цен: {e}")
        return None
    except (KeyError, ValueError) as e:
        logger.error(f"Ошибка обработки данных: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    await update.message.reply_text(
        '🏟️ Добро пожаловать в Sorare Price Bot!\n'
        'Введите имя футболиста (например, "Messi"):'
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка текстового ввода пользователя"""
    player_name = update.message.text.strip()
    
    if not player_name:
        await update.message.reply_text("❌ Пожалуйста, введите имя игрока")
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
            "🔢 Найдено несколько игроков. Выберите нужного:",
            reply_markup=reply_markup
        )
        context.user_data["players"] = players
        return SELECT_PLAYER
        
    return await handle_player_selection(update, context, players[0])

async def handle_player_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, player: dict = None) -> int:
    """Обработка выбора игрока"""
    if not player:
        selected_name = update.message.text
        players = context.user_data.get("players", [])
        player = next((p for p in players if p["displayName"] == selected_name), None)
    
    if not player:
        await update.message.reply_text("❌ Ошибка выбора игрока")
        return ConversationHandler.END
        
    try:
        min_price = get_min_price(player["slug"])
        
        if min_price is None:
            response_text = f"ℹ️ У {player['displayName']} нет карточек в продаже"
        else:
            response_text = (
                f"✅ {player['displayName']}\n"
                f"🏷 Минимальная цена: {min_price:.2f} ETH\n"
                f"🔄 Данные обновлены: {time.strftime('%H:%M:%S')}"
            )
            
        await update.message.reply_text(response_text)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке игрока: {e}")
        await update.message.reply_text("⚠️ Произошла ошибка при получении данных")
        
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена диалога"""
    await update.message.reply_text("❌ Операция отменена")
    return ConversationHandler.END

def main() -> None:
    """Основная функция запуска бота"""
    # Запуск фонового потока для self-ping
    ping_thread = threading.Thread(target=self_ping, daemon=True)
    ping_thread.start()

    # Инициализация бота
    application = ApplicationBuilder().token(os.getenv("TELEGRAM_TOKEN")).build()

    # Настройка обработчиков диалогов
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)],
        states={
            SELECT_PLAYER: [MessageHandler(filters.TEXT, handle_player_selection)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True
    )

    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start))
    application.add_handler(conv_handler)

    # Запуск бота
    application.run_polling()

if __name__ == '__main__':
    main()