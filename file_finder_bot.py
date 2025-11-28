import telegram
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes, Job, PicklePersistence
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode, ChatAction
from telegram.helpers import escape_markdown

import logging
import os
import re
import json
import asyncio
import time
import pytz
import traceback
import html
from dotenv import load_dotenv
from typing import List, Optional, Set, Dict, Callable, Awaitable, Tuple
from functools import wraps
from datetime import time as time_of_day, datetime

# --- 1. ЗАГРУЗКА КОНФИГУРАЦИИ И БЕЗОПАСНОСТЬ ---

# Загружаем переменные из .env (TOKEN, GEMINI_KEY, ADMIN_ID)
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
try:
    MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID"))
except (TypeError, ValueError):
    print("КРИТИЧЕСКАЯ ОШИБКА: MAIN_ADMIN_ID не установлен в .env файле. Бот не может запуститься.")
    exit()

# Загружаем настройки из config.json
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
except FileNotFoundError:
    print("КРИТИЧЕСКАЯ ОШИБКА: config.json не найден. Бот не может запуститься.")
    exit()
except json.JSONDecodeError:
    print("КРИТИЧЕСКАЯ ОШИБКА: Не удалось прочитать config.json. Проверьте синтаксис.")
    exit()

# Константы из config.json
FILE_LIST_PATH = config.get("FILE_LIST_PATH", "all_files_utf8.txt")
BASE_FOLDER_NAME = config.get("BASE_FOLDER_NAME", "Dok_2.5")
BIRTHDAYS: Dict[str, str] = config.get("BIRTHDAYS", {})
TIMEZONE_MSK = pytz.timezone(config.get("TIMEZONE_MSK", 'Europe/Moscow'))
BROADCAST_TIME = time_of_day(
    hour=config.get("BROADCAST_HOUR", 0),
    minute=config.get("BROADCAST_MINUTE", 0),
    tzinfo=TIMEZONE_MSK
)

# Лимиты
MAX_GEMINI_REQUESTS_PER_MINUTE = config.get("MAX_GEMINI_REQUESTS_PER_MINUTE", 5)
MAX_PDF_SIZE_MB_AI = config.get("MAX_PDF_SIZE_MB_AI", 15)
MAX_PDF_TO_SEND_COUNT = config.get("MAX_PDF_TO_SEND_COUNT", 10)
MAX_PDF_TOTAL_SIZE_MB_SEND = config.get("MAX_PDF_TOTAL_SIZE_MB_SEND", 50) # Новый лимит

# Белый список (будет храниться в bot_data для персистентности)
INITIAL_WHITELISTED_USER_IDS: Set[int] = set(config.get("WHITELISTED_USER_IDS", []))


# --- 2. НАСТРОЙКА ЛОГГИРОВАНИЯ И ПУТЕЙ ---

try:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    SCRIPT_DIR = os.getcwd()

LOG_FILE_PATH = os.path.join(SCRIPT_DIR, "bot_log.txt")

# Настройка логгирования
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
root_logger = logging.getLogger()
root_logger.setLevel(logging.WARNING) # Понижаем уровень логгирования

# Отключение INFO-логов от httpx
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Очистка старых обработчиков
for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)

# Файловый обработчик
try:
    file_handler = logging.FileHandler(LOG_FILE_PATH, mode='a', encoding='utf-8')
    file_handler.setFormatter(log_formatter)
    root_logger.addHandler(file_handler)
except IOError as e:
    print(f"Критическая ошибка: Не удалось создать файл лога по пути {LOG_FILE_PATH}. Ошибка: {e}")

# Консольный обработчик
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)

logger = logging.getLogger(__name__)
logger.info(f"--- Логгирование запущено. Логи сохраняются в: {LOG_FILE_PATH} ---")
logger.info(f"Путь к скрипту (SCRIPT_DIR): {SCRIPT_DIR}")


# --- 3. ИНИЦИАЛИЗАЦИЯ GEMINI ---

try:
    from google import genai
    from google.genai.errors import APIError

    if not GEMINI_API_KEY:
        raise ValueError("API-ключ Gemini не установлен в .env.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    MODEL = 'gemini-2.5-flash' # ⭐️ FIX: Используем полный путь к модели
    GEMINI_ENABLED = True
    logger.info("Клиент Gemini успешно инициализирован.")
except ImportError:
    logger.warning("Библиотека 'google.genai' не найдена. AI будет отключен.")
    GEMINI_ENABLED = False
except Exception as e:
    logger.warning(f"Инициализация Gemini не удалась. AI будет отключен. Ошибка: {e}")
    GEMINI_ENABLED = False


# --- 4. ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ И КОНСТАНТЫ ---

# Глобальные списки для кэширования данных из файла
RELATIVE_FILE_PATHS: List[str] = []
PERSON_FOLDERS: List[str] = []
UNIQUE_SURNAMES: Set[str] = set()
GROUPED_SURNAMES: Dict[str, List[str]] = {}

# Константы для CallbackQuery
CALLBACK_SHOW_CATEGORIES = 'show_cats'
CALLBACK_SELECT_CATEGORY = 'select_cat:'
CALLBACK_SELECT_FOLDER = 'select_folder:'
CALLBACK_SELECT_SURNAME = 'select_surname:'
CALLBACK_BACK_TO_CATEGORIES = 'back_to_cats'
CALLBACK_SETTINGS = "settings"
CALLBACK_TOGGLE_BROADCAST = "toggle_broadcast"
CALLBACK_FULL_REPORT = "full_report"
CALLBACK_BACK_TO_START = "back_to_start"
CALLBACK_PERSON_CARD = 'card:'
BACK_PERSON_CARD = 'card:'

# Команды
COMMAND_BIRTHDAY = "birthdays"
COMMAND_SHOW_ALL = "show_all_folders"
COMMAND_CANCEL = "cancel"
COMMAND_SEARCH = "search"

# Ключи для context.user_data (теперь персистентные)
CONTEXT_KEY = 'pdf_context_path'
CONTEXT_MATCHES_KEY = 'current_matches'
CONTEXT_MATCH_INDEX_KEY = 'current_match_index'
USER_BROADCAST_KEY = 'send_birthday_broadcast'

# Режимы работы бота
MODE_SEARCH = 0
MODE_AI = 1

# Регулярное выражение для очистки
SHERLOCK_PREFIX_PATTERN = re.compile(r'^\s*Sherlock\s*[—_-]?\s*', re.IGNORECASE)

# --- 5. ДЕКОРАТОРЫ (УПРАВЛЕНИЕ ДОСТУПОМ) ---

def restricted_access(func: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """
    Декоратор, ограничивающий доступ к боту на основе белого списка в context.bot_data.
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs) -> None:
        user = update.effective_user
        if not user:
            logger.warning("Попытка доступа без 'effective_user'.")
            return

        user_id = user.id
        
        # Получаем белый список из персистентного bot_data
        # context.bot_data инициализируется в main()
        whitelist = context.bot_data.get('WHITELISTED_USER_IDS', set())

        if not whitelist: # Если список пуст, разрешаем всем
            return await func(update, context, *args, **kwargs)

        if user_id not in whitelist:
            logger.warning(f"🚫 ОТКАЗ В ДОСТУПЕ: Пользователь ID {user_id} ({user.username}) не в 'whitelist'.")
            
            message_text = (
                "🚫 **ОТКАЗ В ДОСТУПЕ**\n\n"
                "Ваш ID (`{}`) не найден в белом списке администратора бота. "
                "Пожалуйста, свяжитесь с владельцем для получения доступа."
            ).format(user_id)

            if update.callback_query:
                await update.callback_query.answer(message_text, show_alert=True)
            elif update.message:
                await update.message.reply_text(message_text, parse_mode=ParseMode.MARKDOWN)
                
            return
            
        return await func(update, context, *args, **kwargs)
    return wrapper

def is_main_admin(func: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """
    Декоратор, ограничивающий доступ к функциям только для MAIN_ADMIN_ID.
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs) -> None:
        user = update.effective_user
        if not user or user.id != MAIN_ADMIN_ID:
            logger.warning(f"🚫 ОТКАЗ АДМИНУ: Пользователь ID {user.id if user else 'Unknown'} попытался использовать /add_admin.")
            if update.message:
                await update.message.reply_text("🚫 У вас нет прав на выполнение этой команды.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# --- 6. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (Работа с данными) ---

def get_user_broadcast_status(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Возвращает статус рассылки, по умолчанию True (Вкл). (Из персистентного user_data)"""
    return context.user_data.get(USER_BROADCAST_KEY, True)

def find_folder_by_fio_dr(fio_dr: str, folders: List[str]) -> Optional[str]:
    """Ищет относительный путь к папке по полному ФИО ДР (ключу из BIRTHDAYS)."""
    clean_fio_dr = SHERLOCK_PREFIX_PATTERN.sub('', fio_dr).lower().replace('_', ' ').strip()
    
    for folder_path in folders:
        folder_name = os.path.basename(folder_path)
        clean_folder_name = SHERLOCK_PREFIX_PATTERN.sub('', folder_name).lower().replace('_', ' ').strip()
        
        if clean_folder_name == clean_fio_dr:
            return folder_path
            
    return None

def get_today_birthdays(birthday_data: Dict[str, str]) -> List[Tuple[str, str]]:
    """Возвращает список (ФИО ДР, ДД.ММ) именинников на сегодня по МСК."""
    now_msk = datetime.now(TIMEZONE_MSK)
    today_date_str = now_msk.strftime("%d.%m")
    
    birthdays_today: List[Tuple[str, str]] = []
    
    for fio_dr, dd_mm in birthday_data.items():
        if dd_mm == today_date_str:
            birthdays_today.append((fio_dr, dd_mm))
            
    return birthdays_today

def get_summary_from_folder(folder_path: str) -> str:
    """
    Ищет TXT файл, имя которого соответствует имени папки,
    используя список RELATIVE_FILE_PATHS.
    """
    global RELATIVE_FILE_PATHS
    
    person_folder_name = os.path.basename(folder_path)
    base_file_name_lower = SHERLOCK_PREFIX_PATTERN.sub('', person_folder_name).lower().replace('_', ' ')
    
    txt_file_path = None
    
    for file_path_relative in RELATIVE_FILE_PATHS:
        if not file_path_relative.startswith(folder_path + os.sep):
            continue
            
        file_name = os.path.basename(file_path_relative)
        file_name_no_ext = os.path.splitext(file_name)[0]
        
        clean_file_name_no_ext = SHERLOCK_PREFIX_PATTERN.sub('', file_name_no_ext).lower().replace('_', ' ')
        
        names_match = (
            clean_file_name_no_ext.startswith(base_file_name_lower) or
            base_file_name_lower.startswith(clean_file_name_no_ext)
        )
        
        file_ext = file_path_relative.lower()
        if names_match and file_ext.endswith(('.txt', '.txt_')):
            txt_file_path = os.path.join(SCRIPT_DIR, file_path_relative)
            break
            
    if txt_file_path and os.path.exists(txt_file_path):
        try:
            with open(txt_file_path, 'r', encoding='utf-8') as f:
                summary = f.read(500).strip() # Читаем только 500 символов для КРАТКОЙ сводки
                if len(summary) == 500:
                    summary += "..."
                return summary
        except Exception as e:
            logger.error(f"Ошибка чтения TXT файла сводки в {folder_path}: {e}")
            return "Не удалось прочитать краткую сводку."
            
    return "Краткая сводка (ФИО.txt) не найдена."

def split_text_into_chunks(text: str, max_length: int = 4000) -> List[str]:
    """Разделяет длинный текст на части, не превышающие лимит Telegram."""
    chunks = []
    current_chunk = ""
    for paragraph in text.split('\n\n'):
        if len(current_chunk) + len(paragraph) + 2 > max_length and current_chunk:
            chunks.append(current_chunk.strip())
            current_chunk = ""
        
        while len(paragraph) > max_length:
            chunks.append(paragraph[:max_length])
            paragraph = paragraph[max_length:]
        
        current_chunk += paragraph + '\n\n'
        
    if current_chunk:
        chunks.append(current_chunk.strip())
        
    return chunks

def load_file_paths(file_path: str) -> List[str]:
    """Загружает список путей, делает их относительными от BASE_FOLDER_NAME."""
    try:
        full_file_path = os.path.join(SCRIPT_DIR, file_path)
        
        with open(full_file_path, 'r', encoding='utf-8') as f:
            paths = [line.strip() for line in f if line.strip()]
            
        relative_paths = []
        lower_base_folder = BASE_FOLDER_NAME.lower()
        
        for path in paths:
            normalized_path = os.path.normpath(path)
            try:
                lower_normalized_path = normalized_path.lower()
                base_index = lower_normalized_path.index(lower_base_folder)
                relative_path = normalized_path[base_index:]
                relative_paths.append(relative_path)
            except ValueError:
                continue
                
        logger.info(f"Загружено и очищено {len(relative_paths)} относительных путей к файлам из {full_file_path}.")
        return relative_paths
        
    except FileNotFoundError:
        logger.error(f"Файл {full_file_path} не найден.")
        return []
    except Exception as e:
        logger.error(f"Ошибка при чтении файла {full_file_path}: {e}")
        return []

def load_and_extract_person_folders(file_path: str) -> List[str]:
    """
    Загружает пути, извлекает папки, фамилии И группирует фамилии по категории.
    """
    global RELATIVE_FILE_PATHS, UNIQUE_SURNAMES, GROUPED_SURNAMES 
    
    RELATIVE_FILE_PATHS = load_file_paths(file_path)

    unique_person_folders: Set[str] = set()
    base_folder_lower = BASE_FOLDER_NAME.lower()
    temp_grouped_surnames: Dict[str, Set[str]] = {}
    
    for path in RELATIVE_FILE_PATHS:
        normalized_path = path.replace('/', '\\')
        parts = normalized_path.split('\\')
        
        category = "Прочее"
        base_index_list = [i for i, part in enumerate(parts) if part.lower() == base_folder_lower]
        
        if base_index_list and base_index_list[0] + 1 < len(parts):
            category_candidate = parts[base_index_list[0] + 1]
            if '.' not in category_candidate:
                category = category_candidate
        
        person_folder_path = os.path.dirname(path)
        
        if person_folder_path.lower() != base_folder_lower and os.path.basename(person_folder_path):
            unique_person_folders.add(person_folder_path)
            
            folder_name = os.path.basename(person_folder_path)
            clean_name = SHERLOCK_PREFIX_PATTERN.sub('', folder_name).replace('_', ' ').strip()
            
            surname = clean_name.split()[0] if clean_name else None
            if surname:
                final_surname = surname.capitalize()
                UNIQUE_SURNAMES.add(final_surname)

                category_key = category.capitalize()
                if category_key not in temp_grouped_surnames:
                    temp_grouped_surnames[category_key] = set()
                temp_grouped_surnames[category_key].add(final_surname)

    GROUPED_SURNAMES.clear()
    for k, v in temp_grouped_surnames.items():
        GROUPED_SURNAMES[k] = sorted(list(v)) 

    logger.info(f"Найдено {len(unique_person_folders)} папок, {len(UNIQUE_SURNAMES)} фамилий и {len(GROUPED_SURNAMES)} категорий.")
    return sorted(list(unique_person_folders))

def find_folder_matches(query: str, folders: List[str]) -> List[str]:
    """Ищем пути к папкам, содержащие ВСЕ слова из запроса."""
    clean_query = re.sub(r'[\s_]+', ' ', query.lower()).strip()
    query_words = clean_query.split()
    
    if not query_words:
        return []

    matches = []
    for folder_path in folders:
        folder_name = os.path.basename(folder_path)
        folder_name_lower_cleaned = re.sub(r'[\s_]+', ' ', SHERLOCK_PREFIX_PATTERN.sub('', folder_name).lower()).strip()
        
        all_words_in_name = all(word in folder_name_lower_cleaned for word in query_words)
        
        if all_words_in_name:
            matches.append(folder_path)
            
    return matches

# --- 7. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (Интерфейс) ---

def generate_person_card_markup(matches: List[str], current_index: int) -> InlineKeyboardMarkup:
    """Создает разметку с кнопками для карточки человека: Назад, Вперед, Найти."""
    total_matches = len(matches)
    pagination_buttons = []
    
    if current_index > 0:
        pagination_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"card:prev:{current_index - 1}"))
    else:
        pagination_buttons.append(InlineKeyboardButton(" ", callback_data='_'))
        
    pagination_buttons.append(InlineKeyboardButton(f"{current_index + 1} / {total_matches}", callback_data='_'))
    
    if current_index < total_matches - 1:
        pagination_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"card:next:{current_index + 1}"))
    else:
        pagination_buttons.append(InlineKeyboardButton(" ", callback_data='_'))

    # Отправляем индекс в callback_data
    select_button = [InlineKeyboardButton(
        "📂 Найти", 
        callback_data=f"{CALLBACK_SELECT_FOLDER}{current_index}" # <-- Исправлено
    )]
    
    return InlineKeyboardMarkup([pagination_buttons, select_button])

async def send_person_card(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                           matches: List[str], index: int, query_text: str, is_edit: bool = False):
    """Отображает или редактирует карточку человека с пагинацией."""
    
    chat_id = update.effective_chat.id
    
    if not (0 <= index < len(matches)):
        if is_edit and update.callback_query:
            await update.callback_query.answer("Ошибка: неверный индекс карточки.", show_alert=True)
        return

    safe_query = escape_markdown(query_text, version=2)
    folder_path = matches[index]
    
    folder_name_raw = os.path.basename(folder_path)
    fio_dr = escape_markdown(SHERLOCK_PREFIX_PATTERN.sub('', folder_name_raw).replace('_', ' ').strip(), version=2)
    
    summary = get_summary_from_folder(folder_path)
    safe_summary = summary # Экранируем в блоке кода
    
    markup = generate_person_card_markup(matches, index)
    
    text = (
        f"🔎 По запросу '{safe_query}' найдено несколько записей\n\n"
        f"**Вот ФИО ДР:** `{fio_dr}`\n\n"
        f"**Краткая сводка:**\n"
        f"```\n{safe_summary}\n```" 
    )
    
    context.user_data[CONTEXT_MATCHES_KEY] = matches
    context.user_data[CONTEXT_MATCH_INDEX_KEY] = index
    context.user_data['last_query'] = query_text
    
    if is_edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN_V2
            )
            await update.callback_query.answer()
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                await update.callback_query.answer("Карточка не изменилась.")
            else:
                logger.warning(f"Ошибка редактирования карточки: {e}")
                await update.callback_query.answer("Произошла ошибка при обновлении карточки.", show_alert=True)
            
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )

async def handle_multiple_matches(update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                  matches: List[str], query: str):
    """Инициализирует пагинацию для нескольких совпадений."""
    await send_person_card(update, context, matches, 0, query)

async def send_birthday_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, fio_dr: str, person_folder_path: str):
    """Отправляет сообщение о дне рождения (для рассылки)."""
    
    # ⭐️ НОВОЕ: Индикация процесса
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    
    safe_fio_dr = escape_markdown(fio_dr, version=2)
    safe_folder_path = escape_markdown(person_folder_path, version=2) 
    
    message_text = (
        rf"🎉 **Сегодня Днем Рождения у {safe_fio_dr}\!** 🎉" "\n\n")
    bot_data = context.application.bot_data
    
    # Инициализация маппинга и счетчика (для персистентности)
    if 'fio_id_counter' not in bot_data:
        bot_data['fio_id_counter'] = 0
    if 'fio_map' not in bot_data or not isinstance(bot_data.get('fio_map'), dict):
        bot_data['fio_map'] = {}
        
    fio_id = bot_data['fio_id_counter']
    bot_data['fio_map'][fio_id] = fio_dr # Сохраняем длинный fio_dr по короткому ID
    bot_data['fio_id_counter'] = fio_id + 1
    
    callback_data = f"{CALLBACK_FULL_REPORT}:{fio_id}"
    # ⭐️ НОВОЕ: Кнопка для быстрого получения полного отчета
    # Из файла file_finder_bot.py:
    # ...
    keyboard = [
        [InlineKeyboardButton(
            "📂 Показать полный отчет", 
            # 💥 БЫЛО: callback_data=f"{CALLBACK_FULL_REPORT}:{fio_dr}" 
            callback_data=f"{CALLBACK_FULL_REPORT}:{fio_id}" # ✅ ИСПРАВЛЕНИЕ: Используем короткий fio_id
        )]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=message_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup # Добавлена кнопка
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения о ДР пользователю {chat_id}: {e}")

async def send_folder_contents(chat_id: int, context: ContextTypes.DEFAULT_TYPE, person_folder_relative_path: str, update: Update = None):
    """
    (⭐️ НОВАЯ РЕФАКТОРИНГ-ФУНКЦИЯ)
    Находит все TXT/PDF в папке и отправляет их пользователю
    с соблюдением лимитов по количеству и ОБЩЕМУ РАЗМЕРУ.
    """
    
    user_id = chat_id # Для логгирования
    logger.info(f"ID {user_id}: Запуск 'send_folder_contents' для: '{person_folder_relative_path}'")
    
    person_folder_name = os.path.basename(person_folder_relative_path)
    
    pdf_files_to_send: List[str] = []
    txt_file_path = None
    base_file_name_lower = SHERLOCK_PREFIX_PATTERN.sub('', person_folder_name).lower().replace('_', ' ')
    
    # 1. Сбор всех файлов в папке
    for file_path_relative in RELATIVE_FILE_PATHS:
        if not file_path_relative.startswith(person_folder_relative_path + os.sep):
            continue
        
        full_file_path = os.path.join(SCRIPT_DIR, file_path_relative)
        file_ext = file_path_relative.lower()
        
        if file_ext.endswith(('.txt', '.txt_')):
            file_name_no_ext = os.path.splitext(os.path.basename(file_path_relative))[0]
            clean_file_name_no_ext = SHERLOCK_PREFIX_PATTERN.sub('', file_name_no_ext).lower().replace('_', ' ')
            names_match = (
                clean_file_name_no_ext.startswith(base_file_name_lower) or
                base_file_name_lower.startswith(clean_file_name_no_ext)
            )
            if names_match:
                txt_file_path = full_file_path
        
        elif file_ext.endswith(('.pdf', '.pdf_')):
            if os.path.exists(full_file_path):
                pdf_files_to_send.append(full_file_path)

    # 2. Отправка сводки (TXT)
    if not (update and getattr(update, 'callback_query', update)):
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    
    if txt_file_path and os.path.exists(txt_file_path):
        try:
            with open(txt_file_path, 'r', encoding='utf-8') as f:
                summary_content = f.read().strip()
            
            display_name = escape_markdown(person_folder_name.replace('_', ' '), version=2)
            
            if summary_content:
                # Формируем текст
                title = f"📝 **Краткая сводка для {display_name}:**"
                safe_summary_content = escape_markdown(summary_content, version=2)
                body = f"```\n{safe_summary_content}\n```"
                full_text = f"{title}\n{body}"

                # ⭐️ FIX: (Запрос 3)
                if update and update.callback_query:
                    try:
                        # Пытаемся отредактировать сообщение (карточку)
                        await update.callback_query.edit_message_text(
                            text=full_text,
                            parse_mode=ParseMode.MARKDOWN_V2,
                            reply_markup=None # Убираем кнопки
                        )
                        logger.info(f"ID {user_id}: Сводка (TXT) отправлена (как EDIT) для: {person_folder_name}")
                    
                    except telegram.error.BadRequest as e:
                        if "Message is not modified" in str(e):
                            pass
                        elif "message to edit not found" in str(e).lower() or len(full_text) > 4096:
                             # Если не вышло (сообщение старое или слишком длинное), отправляем новым
                            logger.warning(f"ID {user_id}: Ошибка EDIT сводки (длина {len(full_text)}), отправка новым: {e}")
                            await context.bot.send_message(chat_id=chat_id, text=title, parse_mode=ParseMode.MARKDOWN_V2)
                            await context.bot.send_message(chat_id=chat_id, text=body, parse_mode=ParseMode.MARKDOWN_V2)
                        else:
                            raise
                else:
                    # Старое поведение (отправка новым сообщением)
                    await context.bot.send_message(chat_id=chat_id, text=title, parse_mode=ParseMode.MARKDOWN_V2)
                    await context.bot.send_message(chat_id=chat_id, text=body, parse_mode=ParseMode.MARKDOWN_V2)
                    logger.info(f"ID {user_id}: Отправлена сводка (TXT) (как NEW) для: {person_folder_name}")

            else:
                logger.warning(f"ID {user_id}: Файл сводки {txt_file_path} пуст.")
        
        except Exception as e:
            logger.error(f"ID {user_id}: Ошибка при чтении файла сводки {txt_file_path}: {e}")
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка чтения TXT-файла сводки: {e}")

    # 3. Отправка PDF файлов (с новыми лимитами)
    if pdf_files_to_send:
        
        # ⭐️ НОВОЕ: Индикация процесса
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        
        files_to_send_final: List[str] = []
        total_size_mb = 0.0
        
        # ⭐️ НОВЫЙ БЛОК: Проверка лимитов по размеру и количеству
        for pdf_path in pdf_files_to_send:
            try:
                file_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
                
                # Проверка лимита на КОЛИЧЕСТВО
                if len(files_to_send_final) >= MAX_PDF_TO_SEND_COUNT:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"⚠️ Достигнут лимит на отправку ({MAX_PDF_TO_SEND_COUNT} файлов). Отправка остальных PDF отменена."
                    )
                    break
                
                # Проверка лимита на ОБЩИЙ РАЗМЕР
                if (total_size_mb + file_size_mb) > MAX_PDF_TOTAL_SIZE_MB_SEND:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"⚠️ Достигнут лимит на общий размер файлов ({MAX_PDF_TOTAL_SIZE_MB_SEND} МБ). Отправка остальных PDF отменена."
                    )
                    break
                    
                files_to_send_final.append(pdf_path)
                total_size_mb += file_size_mb
                
            except OSError as e:
                 logger.error(f"ID {user_id}: Не удалось получить размер файла {pdf_path}: {e}")

        # Отправка отфильтрованного списка
        for pdf_file_path in files_to_send_final:
            try:
                with open(pdf_file_path, 'rb') as doc_file:
                    filename = os.path.basename(pdf_file_path)
                    display_filename = SHERLOCK_PREFIX_PATTERN.sub('', filename.replace('_', ' '))
                    
                    # Используем bot_data для ID (персистентный)
                    bot_data = context.application.bot_data
                    current_id = bot_data.get('id_counter', 0)
                    
                    # ⭐️ FIX: Гарантируем, что pdf_map существует и является словарем
                    if 'pdf_map' not in bot_data or not isinstance(bot_data.get('pdf_map'), dict):
                        bot_data['pdf_map'] = {}
                        logger.warning("Восстановлен 'pdf_map' в bot_data, т.к. он отсутствовал или был поврежден.")

                    bot_data['pdf_map'][current_id] = pdf_file_path
                    bot_data['id_counter'] = current_id + 1
                    
                    callback_data = f'ask_ai|{current_id}'
                    keyboard = [
                        [InlineKeyboardButton("🧠 Спросить ИИ по этому документу", callback_data=callback_data)]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=doc_file,
                        caption=f"✅ Файл документа: {display_filename}",
                        reply_markup=reply_markup
                    )
                    logger.info(f"ID {user_id}: Файл {filename} (ID {current_id}) успешно отправлен с кнопкой ИИ.")

            except Exception as e:
                logger.error(f"ID {user_id}: Ошибка при отправке PDF файла {pdf_file_path}: {e}")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Произошла ошибка при отправке PDF файла '{os.path.basename(pdf_file_path)}': {e}"
                )

    
    if not pdf_files_to_send and not (txt_file_path and os.path.exists(txt_file_path)):
        logger.warning(f"ID {user_id}: Найдена папка '{person_folder_name}', но не найдены PDF/TXT.")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Найдена папка '{person_folder_name.replace('_', ' ')}', но не найдены соответствующие файлы PDF/TXT внутри нее."
        )


# --- 8. ОБРАБОТЧИК GEMINI (AI) ---

async def gemini_query(query: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Отправляет запрос к Gemini, используя контекст PDF, если он доступен.
    Включает проверку лимита запросов и размера файла.
    """
    
    user_id = update.effective_user.id
    current_time = time.time()
    
    # ⭐️ НОВОЕ: Индикация процесса
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    if not GEMINI_ENABLED:
        logger.warning(f"ID {user_id}: Попытка вызова Gemini, но он отключен.")
        await update.message.reply_text("❌ AI-ассистент отключен.")
        return
    
    # 1. ПРОВЕРКА ОГРАНИЧЕНИЯ СКОРОСТИ (RATE LIMITING)
    if 'gemini_requests' not in context.user_data:
        context.user_data['gemini_requests'] = []

    context.user_data['gemini_requests'] = [
        t for t in context.user_data['gemini_requests'] if current_time - t < 60
    ]
    
    if len(context.user_data['gemini_requests']) >= MAX_GEMINI_REQUESTS_PER_MINUTE:
        logger.warning(f"ID {user_id}: Превышен лимит {MAX_GEMINI_REQUESTS_PER_MINUTE} запросов к AI в минуту.")
        context.user_data[CONTEXT_KEY] = None 
        context.user_data['mode'] = MODE_SEARCH
        await update.message.reply_text(
            f"⚠️ **Превышен лимит AI-запросов!** (Не более {MAX_GEMINI_REQUESTS_PER_MINUTE} в минуту).\n"
            "**Режим сброшен на Поиск.** Попробуйте снова через минуту.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
        
    context.user_data['gemini_requests'].append(current_time)
        
    pdf_context_path = context.user_data.get(CONTEXT_KEY)
    uploaded_file = None
    response = None

    if pdf_context_path and os.path.exists(pdf_context_path):
        # 1. Режим ИИ с контекстом PDF
        logger.info(f"ID {user_id}: Режим ИИ (с файлом). Файл: {pdf_context_path}. Запрос: '{query}'")
        
        # 2. ПРОВЕРКА РАЗМЕРА ФАЙЛА (AI)
        file_size_bytes = os.path.getsize(pdf_context_path)
        file_size_mb = file_size_bytes / (1024 * 1024)
        
        if file_size_mb > MAX_PDF_SIZE_MB_AI:
            logger.warning(f"ID {user_id}: Файл {pdf_context_path} слишком большой ({file_size_mb:.2f} МБ). Лимит AI: {MAX_PDF_SIZE_MB_AI} МБ.")
            context.user_data[CONTEXT_KEY] = None 
            context.user_data['mode'] = MODE_SEARCH
            context.user_data['gemini_requests'].pop() 
            await update.message.reply_text(
                f"❌ **Сбой AI-анализа.**\n"
                f"Размер файла **{file_size_mb:.2f} МБ** превышает AI-лимит в **{MAX_PDF_SIZE_MB_AI} МБ**.\n"
                "**Режим сброшен на Поиск.**",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        
        system_instruction = "Ты полезный ассистент, который отвечает на вопросы пользователя, используя предоставленный документ. Будь точен и говори на русском языке."
        full_query_text = (
            f"Тебе предоставлен документ ({os.path.basename(pdf_context_path)}). "
            f"Ответь на вопрос, основываясь на содержимом документа. Вопрос: {query}"
        )
        
        await update.message.reply_text("💡 Загружаю файл и отправляю запрос к Gemini...")
        
        try:
            logger.info(f"ID {user_id}: Загрузка файла в Gemini...")
            uploaded_file = client.files.upload(file=pdf_context_path)
            logger.info(f"ID {user_id}: Файл загружен в Gemini: {uploaded_file.name}")
            
            prompt_parts = [ system_instruction, uploaded_file, full_query_text ]
            
            response = client.models.generate_content(
                model=MODEL,
                contents=prompt_parts,
            )

        except Exception as e:
            logger.error(f"ID {user_id}: Ошибка Gemini с файлом: {e}")
            await update.message.reply_text(f"❌ Произошла ошибка при обращении к AI-ассистенту с файлом. Ошибка: {e}")
            # Не сбрасываем режим, даем пользователю попробовать еще раз
            return
        finally:
            if uploaded_file:
                try:
                    client.files.delete(name=uploaded_file.name)
                    logger.info(f"ID {user_id}: Удален файл Gemini: {uploaded_file.name}")
                except Exception as e:
                    logger.error(f"ID {user_id}: Ошибка при удалении файла Gemini: {e}")
        
        if response and response.text:
            logger.info(f"ID {user_id}: Gemini (с файлом) дал ответ.")
            response_text_chunks = split_text_into_chunks(response.text)
            
            await update.message.reply_text(
                f"🤖 **Ответ ИИ по документу:**",
                parse_mode=ParseMode.MARKDOWN
            )

            for chunk in response_text_chunks:
                safe_chunk = escape_markdown(chunk, version=2) # Экранируем V2
                try:
                    await update.message.reply_text(safe_chunk, parse_mode=ParseMode.MARKDOWN_V2)
                except telegram.error.BadRequest as e:
                    logger.warning(f"ID {user_id}: Ошибка Markdown V2. Отправка как чистый текст: {e}")
                    await update.message.reply_text(chunk) # Отправка как есть
        else:
            logger.warning(f"ID {user_id}: Gemini (с файлом) не вернул текст.")
            await update.message.reply_text("❌ ИИ не смог сгенерировать ответ по документу. Режим сброшен на Поиск.")

        
    else:
        # 1. Режим ИИ без контекста (общий вопрос)
        logger.info(f"ID {user_id}: Режим ИИ (общий). Запрос: '{query}'")
        await update.message.reply_text("💡 Отправляю общий запрос к Gemini...")
        
        system_instruction = "Ты полезный ассистент, который также знает, что основная функция — это поиск файлов. Отвечай на общие вопросы дружелюбно и точно. Говори на русском языке."
        full_query = f"{system_instruction}\n\n{query}"
        
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=full_query,
            )
        except Exception as e:
            logger.error(f"ID {user_id}: Ошибка Gemini (общий): {e}")
            await update.message.reply_text("❌ Произошла ошибка при обращении к AI-ассистенту.")
            return
        
        if response and response.text:
            logger.info(f"ID {user_id}: Gemini (общий) дал ответ.")
            response_text_chunks_general = split_text_into_chunks(response.text)
            
            await update.message.reply_text(
                f"🤖 **Ответ ИИ:**",
                parse_mode=ParseMode.MARKDOWN
            )
            
            for chunk in response_text_chunks_general:
                safe_chunk = escape_markdown(chunk, version=2)
                try:
                    await update.message.reply_text(safe_chunk, parse_mode=ParseMode.MARKDOWN_V2)
                except telegram.error.BadRequest:
                    logger.warning(f"ID {user_id}: Ошибка Markdown V2 (общий). Отправка как чистый текст.")
                    await update.message.reply_text(chunk)
        else:
            logger.warning(f"ID {user_id}: Gemini (общий) не вернул текст.")
            await update.message.reply_text("❌ ИИ не смог сгенерировать ответ. Режим сброшен на Поиск.")


# --- 9. ОСНОВНЫЕ ОБРАБОТЧИКИ TELEGRAM ---

async def send_start_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет или редактирует начальное сообщение с кнопками."""
    
    is_on = get_user_broadcast_status(context)
    status_text = "Вкл" if is_on else "Выкл"
    
    keyboard = [
        [InlineKeyboardButton("📜 Доступные Фамилии", callback_data='show_surnames')],
        [InlineKeyboardButton("🎂 У кого сегодня день рождения?", callback_data=COMMAND_BIRTHDAY)], 
        [InlineKeyboardButton(f"⚙️ Настройки рассылки (Текущий: {status_text})", callback_data=CALLBACK_SETTINGS)],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = (
        r"👋 **Привет\!** Я бот для поиска документов и AI\-ассистент\." "\n\n"
        r"**Режим:** 🔍 **Поиск\.** \(Используйте /cancel для сброса AI\)" "\n\n"
        r"Введите **ФИО/Дату рождения** для поиска папки или выберите действие ниже\."
    )
    
    # Редактируем, если это колбэк
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN_V2
            )
            await update.callback_query.answer()
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                await update.callback_query.answer("Меню не изменилось.")
            else:
                logger.warning(f"Ошибка редактирования сообщения в start: {e}")
    
    # Отправляем новое, если это команда
    elif update.effective_message:
        await update.effective_message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    else:
        logger.warning("send_start_message вызвана без callback_query и без effective_message")
            
@restricted_access
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает /start. Сбрасывает режим и показывает главное меню."""
    user_id = update.effective_user.id
    logger.info(f"ID {user_id}: Вызвана команда /start. Сброс контекста.")
    
    # Сбрасываем только контекст поиска/AI, но НЕ настройки (рассылка)
    context.user_data.pop(CONTEXT_KEY, None)
    context.user_data.pop(CONTEXT_MATCHES_KEY, None)
    context.user_data.pop(CONTEXT_MATCH_INDEX_KEY, None)
    context.user_data['mode'] = MODE_SEARCH 
    
    await send_start_message(update, context)

@restricted_access
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """( /search ) Явно переводит бот в режим поиска."""
    user_id = update.effective_user.id
    
    context.user_data.pop(CONTEXT_KEY, None) 
    context.user_data['mode'] = MODE_SEARCH
    
    logger.info(f"ID {user_id}: Включен режим ПОИСКА (через /search).")
    
    await update.effective_message.reply_text(
        r"🔍 **Включен режим Поиска\./**" "\n\n"
        r"Теперь вы можете вводить ФИО для поиска папок\. "
        r"Используйте /start для вызова главного меню\.",
        parse_mode=ParseMode.MARKDOWN_V2
    )

@restricted_access
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(⭐️ НОВАЯ /cancel) Отменяет режим AI и сбрасывает контекст."""
    user_id = update.effective_user.id
    
    context.user_data.pop(CONTEXT_KEY, None) 
    context.user_data.pop(CONTEXT_MATCHES_KEY, None)
    context.user_data.pop(CONTEXT_MATCH_INDEX_KEY, None)
    context.user_data['mode'] = MODE_SEARCH
    
    logger.info(f"ID {user_id}: Режим ИИ отменен (через /cancel). Включен режим ПОИСКА.")
    
    await update.effective_message.reply_text(
        r"🤖❌ **Режим ИИ отменен\./**" "\n\n"
        r"🔍 **Включен режим Поиска\./**",
        parse_mode=ParseMode.MARKDOWN_V2
    )

@restricted_access
async def settings_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает меню настроек (вызывается из button_handler)."""
    is_on = get_user_broadcast_status(context)
    
    toggle_text = "🔴 Выключить рассылку" if is_on else "🟢 Включить рассылку"
    
    text = (
        "⚙️ **Настройки Ежедневной Рассылки Дней Рождения**" "\n\n"
        f"Текущий статус: {'**Включена**' if is_on else '**Выключена**'}\n"
        f"Время рассылки: {BROADCAST_TIME.strftime('%H:%M')} МСК"
    )

    keyboard = [
        [InlineKeyboardButton(toggle_text, callback_data=CALLBACK_TOGGLE_BROADCAST)],
        [InlineKeyboardButton("⬅️ Назад", callback_data=CALLBACK_BACK_TO_START)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN_V2
            )
            await query.answer()
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                await query.answer("Настройки не изменились.")
            else:
                raise
    else:
        # Резервный вариант, если вызвано не кнопкой
        await update.effective_message.reply_text(
            text=text, 
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )

@restricted_access
async def birthday_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает команду 'У кого ДР' (кнопка или /birthdays)."""
    
    chat_id = update.effective_chat.id
    query = update.callback_query
    
    birthdays_today = get_today_birthdays(BIRTHDAYS)
    
    if not birthdays_today:
        message_text = r"🎂 Сегодня нет именинников\."
    else:
        message_text = r"🎉 **Сегодняшние Именинники\!** 🎉" + "\n\n"
        for fio_dr, dob in birthdays_today:
            safe_fio_dr = escape_markdown(fio_dr, version=2)
            message_text += rf"**{safe_fio_dr}**" + "\n"
            
    keyboard = [
        [InlineKeyboardButton(r"↩️ Назад в Главное меню", callback_data=CALLBACK_BACK_TO_START)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
            
    # Если это колбэк, редактируем
    if query:
        try:
            await query.edit_message_text(
                text=message_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN_V2
            )
            await query.answer()
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                await query.answer()
            else:
                logger.error(f"Ошибка в birthday_today_command (edit): {e}")
    # Если это команда /birthdays, отправляем новое
    elif update.effective_message:
         await update.effective_message.reply_text(
            text=message_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )

@restricted_access
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовые сообщения (Поиск или AI)."""
    query = update.message.text.strip()
    current_mode = context.user_data.get('mode', MODE_SEARCH)
    user_id = update.effective_user.id
    
    if current_mode == MODE_AI:
        await gemini_query(query, update, context)
        return
    
    # --- Режим Поиска ---
    logger.info(f"ID {user_id}: Режим ПОИСКА. Запрос: '{query}'")

    if not PERSON_FOLDERS:
        logger.error(f"ID {user_id}: Ошибка: Список PERSON_FOLDERS пуст.")
        await update.message.reply_text("❌ Ошибка: База данных папок пуста. Проверьте {FILE_LIST_PATH}.")
        return
        
    found_folders = find_folder_matches(query, PERSON_FOLDERS)
    num_matches = len(found_folders)

    if num_matches == 1:
        # --- Сценарий 1: Найдена ОДНА папка ---
        logger.info(f"ID {user_id}: Найдена 1 папка: '{found_folders[0]}'")
        
        # ⭐️ НОВЫЙ РЕФАКТОРИНГ: Вызываем единую функцию отправки
        await send_folder_contents(update.message.chat_id, context, found_folders[0])

    elif num_matches > 1:
        # --- Сценарий 2: Найдено НЕСКОЛЬКО папок (карточка) ---
        logger.info(f"ID {user_id}: Найдено {num_matches} папок. Запуск карточки.")
        await handle_multiple_matches(update, context, found_folders, query)

    else:
        # --- Сценарий 3: Найдено 0 совпадений ---
        logger.info(f"ID {user_id}: 0 совпадений по запросу: '{query}'")
        safe_query = escape_markdown(query, version=2)
        await update.message.reply_text(
            rf"❌ Папка по запросу \`{safe_query}\` не найдена\. Попробуйте уточнить запрос \(например, 'Иванов'\)\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )


@restricted_access
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия на inline-кнопки."""
    query = update.callback_query
    await query.answer() # Немедленно отвечаем, чтобы убрать "часики"
        
    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    # 1. Показ списка фамилий
    if data == 'show_surnames':
        if not GROUPED_SURNAMES:
            await query.edit_message_text(text="❌ Список фамилий и категорий пуст.")
            return

        response_parts = ["📚 **Доступные Фамилии (Отсортировано по категории):**\n"]
        sorted_categories = sorted(GROUPED_SURNAMES.keys())
        
        for category in sorted_categories:
            surnames = GROUPED_SURNAMES[category]
            response_parts.append(f"\n**{category.replace('_', ' ')}**") # ⭐️ FIX: Убираем _ из категорий
            
            surname_lines = []
            row = []
            for surname in surnames:
                row.append(surname)
                if len(row) == 2:
                    surname_lines.append(" | ".join(row))
                    row = []
            if row:
                surname_lines.append(" | ".join(row))
                
            response_parts.extend(surname_lines)


        response_text = "\n".join(response_parts)
        
        # ⭐️ FIX: Добавляем кнопку "Назад"
        keyboard = [
            [InlineKeyboardButton("⬅️ Назад", callback_data=CALLBACK_BACK_TO_START)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        messages = split_text_into_chunks(response_text)
        
        try:
            # ⭐️ FIX: Редактируем текущее сообщение (Запрос 1)
            await query.edit_message_text(
                text=messages[0], 
                reply_markup=reply_markup, # ⭐️ FIX: (Запрос 2)
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Если есть еще части, отправляем их отдельно
            if len(messages) > 1:
                for msg in messages[1:]:
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
        
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                await query.answer("Список уже показан.")
            else:
                logger.error(f"Ошибка редактирования списка фамилий: {e}")
                await query.answer("Ошибка при обновлении списка.", show_alert=True)
        return

    # 2. Навигация по меню
    elif data == CALLBACK_SETTINGS:
        await settings_menu_handler(update, context)
        return
        
    elif data == CALLBACK_TOGGLE_BROADCAST:
        current_status = get_user_broadcast_status(context)
        new_status = not current_status
        context.user_data[USER_BROADCAST_KEY] = new_status
        logger.info(f"ID {user_id}: Рассылка переключена на {'ВКЛ' if new_status else 'ВЫКЛ'}.")
        await settings_menu_handler(update, context) # Обновляем меню
        return

    elif data == CALLBACK_BACK_TO_START:
        await send_start_message(update, context)
        return
        
    elif data == COMMAND_BIRTHDAY:
        await birthday_today_command(update, context)
        return
        
    # 3. Запрос полного отчета (из сообщения о ДР)
    # file_finder_bot.py (внутри button_handler, добавьте/измените блок)

    # 7. Обработка полного отчета о ДР (рассылка)
    elif data.startswith(CALLBACK_FULL_REPORT):
        try:
            fio_id_str = data.split(':', 1)[1]
            fio_id = int(fio_id_str)

            bot_data = context.application.bot_data
            fio_map: Dict[int, str] = bot_data.get('fio_map', {})
            fio_dr = fio_map.get(fio_id)
            
            if not fio_dr:
                await query.answer("Срок действия ссылки истек или данные не найдены.", show_alert=True)
                return

            person_folder_path = find_folder_by_fio_dr(fio_dr, PERSON_FOLDERS)
            
            if not person_folder_path:
                await query.answer(f"Не удалось найти папку для: {fio_dr}.", show_alert=True)
                return
            
            await query.answer(f"Загружаю отчет для: {fio_dr}...", show_alert=False)
            
            # ⭐️ Важно: Передайте update для редактирования сообщения
            await send_folder_contents(query.message.chat_id, context, person_folder_path, update=update) 
            
        except Exception as e:
            logger.error(f"Ошибка в CALLBACK_FULL_REPORT: {data}. Ошибка: {e}")
            await query.answer("Ошибка данных отчета.", show_alert=True)
            return

    # 4. Включение режима AI
    elif data.startswith('ask_ai|'):
        _, id_str = data.split('|', 1)
        try:
            pdf_id = int(id_str)
        except ValueError:
            logger.error(f"ID {user_id}: Ошибка: Неверный формат ID файла в callback: {id_str}")
            await query.message.reply_text("❌ Ошибка: Неверный формат ID файла.")
            return

        pdf_path = context.application.bot_data.get('pdf_map', {}).get(pdf_id)
        
        if not pdf_path or not os.path.exists(pdf_path):
            logger.error(f"ID {user_id}: Ошибка: Контекст файла (ID {pdf_id}) не найден или файл удален. Путь: {pdf_path}")
            await query.message.reply_text("❌ Ошибка: Контекст файла не найден или файл удален. (Возможно, бот перезапускался).")
            return
        
        context.user_data[CONTEXT_KEY] = pdf_path
        context.user_data['mode'] = MODE_AI
        
        display_name = escape_markdown(os.path.basename(pdf_path).split('.')[0].replace('_', ' '), version=2)
        
        logger.info(f"ID {user_id}: Включен режим ИИ для файла (ID {pdf_id}): {pdf_path}")
        await query.message.reply_text(
            rf"🧠 **Включен Режим ИИ\.**"
            rf"Вы выбрали документ: **{display_name}**\." "\n\n"
            r"**Теперь введите ваш вопрос** по этому документу\. "
            r"Используйте /cancel для отмены\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    # 5. Пагинация (карточка)
    elif data.startswith(CALLBACK_PERSON_CARD):
        try:
            parts = data.split(':')
            new_index = int(parts[2])
            
            matches = context.user_data.get(CONTEXT_MATCHES_KEY)
            last_query = context.user_data.get('last_query', '')
            
            if matches:
                await send_person_card(update, context, matches, new_index, last_query, is_edit=True)
            else:
                await query.answer("Сессия поиска истекла. Начните новый поиск.", show_alert=True)
                
        except (IndexError, ValueError, TypeError) as e:
            logger.error(f"Ошибка пагинации: {data}. Ошибка: {e}")
            await query.answer("Ошибка данных.", show_alert=True)
        return
        
    # 6. Выбор "Найти" из карточки
    elif data.startswith(CALLBACK_SELECT_FOLDER):
        try:
            index_str = data.split(':', 1)[1]
            selected_index = int(index_str)
            
            matches = context.user_data.get(CONTEXT_MATCHES_KEY)

            if not matches or not (0 <= selected_index < len(matches)):
                await query.answer("Сессия поиска истекла или неверный индекс.", show_alert=True)
                try:
                    await query.edit_message_reply_markup(reply_markup=None) 
                except:
                    pass # Сообщение могло быть удалено
                return

            folder_path = matches[selected_index] 
            
            # Очистка контекста пагинации
            context.user_data.pop(CONTEXT_MATCHES_KEY, None)
            context.user_data.pop(CONTEXT_MATCH_INDEX_KEY, None)
            context.user_data.pop('last_query', None)
            
            # ⭐️ FIX: (Запрос 3) НЕ удаляем кнопки, а передаем query
            # в send_folder_contents, чтобы ОНА отредактировала сообщение
            await send_folder_contents(query.message.chat_id, context, folder_path, update=update)
            
        except (IndexError, ValueError, TypeError) as e:
            logger.error(f"Ошибка выбора папки: {e}")
            await query.answer("Ошибка выбора папки.", show_alert=True)
        return

# --- 10. ФУНКЦИИ АДМИНИСТРАТОРА (⭐️ НОВЫЕ) ---

@is_main_admin 
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """( /add_admin <ID> ) Добавляет ID в 'whitelist'."""
    try:
        # ⭐️ FIX: Инициализируем 'whitelist' как set, если он не существует
        if 'whitelist' not in context.bot_data or not isinstance(context.bot_data.get('whitelist'), set):
            context.bot_data['whitelist'] = set()
            
        user_id_to_add = int(context.args[0])
        context.bot_data['whitelist'].add(user_id_to_add)
        logger.info(f"АДМИН: {update.effective_user.id} добавил {user_id_to_add} в 'whitelist'.")
        await update.message.reply_text(f"✅ Пользователь {user_id_to_add} добавлен в белый список.")
    except (IndexError, ValueError):
        await update.message.reply_text("Ошибка. Используйте: /add_admin <ID пользователя>")
    except Exception as e:
        logger.error(f"Ошибка в add_admin: {e}")
        await update.message.reply_text(f"❌ Произошла ошибка: {e}")

@is_main_admin
async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """( /remove_admin <ID> ) Удаляет ID из 'whitelist'."""
    try:
        user_id_to_remove = int(context.args[0])
        if user_id_to_remove == MAIN_ADMIN_ID:
            await update.message.reply_text("❌ Нельзя удалить главного администратора.")
            return
            
        context.bot_data['whitelist'].discard(user_id_to_remove)
        logger.info(f"АДМИН: {update.effective_user.id} удалил {user_id_to_remove} из 'whitelist'.")
        await update.message.reply_text(f"✅ Пользователь {user_id_to_remove} удален из белого списка.")
    except (IndexError, ValueError):
        await update.message.reply_text("Ошибка. Используйте: /remove_admin <ID пользователя>")
    except Exception as e:
        logger.error(f"Ошибка в remove_admin: {e}")
        await update.message.reply_text(f"❌ Произошла ошибка: {e}")

@is_main_admin
async def list_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ...
    
    whitelist: Set[int] = context.application.bot_data.get('whitelist', set())
    admin_list_text = []
    
    for user_id in whitelist:
        # ⭐️ FIX: Экранируем ID, если он вставляется в строку с форматированием
        escaped_user_id = escape_markdown(str(user_id), version=2) 
        
        if user_id == MAIN_ADMIN_ID:
            admin_list_text.append(f"*👑 Главный Админ (ID: {escaped_user_id})*")
        else:
            admin_list_text.append(f"• ID: `{escaped_user_id}`")
    
    if not admin_list_text:
        message = escape_markdown("Список администраторов пуст (кроме Главного Админа).", version=2)
    else:
        # ⭐️ FIX: Экранирование не требуется, если вы используете только базовое форматирование
        # (как * и `), и если все данные уже экранированы.
        message = "*Список Администраторов:*\n\n" + "\n".join(admin_list_text)

    await update.message.reply_text(
        text=message,
        parse_mode=ParseMode.MARKDOWN_V2, # ⭐️ Убедитесь, что этот режим установлен
        disable_web_page_preview=True
    )

# --- 11. ФОНОВЫЕ ЗАДАЧИ (Рассылка) ---

async def birthday_broadcast(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневная рассылка о днях рождения (JobQueue)."""
    logger.info("Запуск ежедневной рассылки о днях рождения.")
    
    birthdays_today = get_today_birthdays(BIRTHDAYS) 
    
    if not birthdays_today:
        logger.info("Рассылка: Дней рождения сегодня нет.")
        return

    # ⭐️ ИЗМЕНЕНИЕ: Отправляем всем, кто в 'whitelist' (из bot_data)
    # и у кого включена рассылка (из user_data)
    
    whitelist = context.application.bot_data.get('whitelist', set())
    if not whitelist:
        logger.warning("Рассылка: 'whitelist' в bot_data пуст. Некому отправлять.")
        return

    for user_id in whitelist:
        try:
            # Получаем персистентные user_data
            user_data = context.application.user_data.get(user_id, {})
            
            if not user_data.get(USER_BROADCAST_KEY, True):
                logger.info(f"Рассылка для пользователя ID {user_id} отключена в настройках.")
                continue

            # Отправляем по одному сообщению на каждого именинника
            for fio_dr, _ in birthdays_today:
                person_folder_path = find_folder_by_fio_dr(fio_dr, PERSON_FOLDERS)
                
                if person_folder_path:
                    await send_birthday_message(context, user_id, fio_dr, person_folder_path)
                else:
                    logger.warning(f"Рассылка: Не удалось найти папку для именинника: {fio_dr}")
                    
        except Exception as e:
            logger.error(f"Ошибка при обработке рассылки для пользователя ID {user_id}: {e}")

@is_main_admin
async def test_broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """( /test_broadcast ) Запускает тестовую рассылку для MAIN_ADMIN_ID."""
    
    chat_id = update.effective_chat.id
    await update.effective_message.reply_text("⏳ Запускаю тестовую рассылку (только для вас)...")

    # Используем run_once для немедленного запуска
    context.job_queue.run_once(
        callback=birthday_broadcast_test, # ⭐️ Отдельная функция для теста
        when=0, 
        name="test_broadcast_job",
        chat_id=chat_id,
        data=chat_id
    )

async def birthday_broadcast_test(context: ContextTypes.DEFAULT_TYPE) -> None:
    """(⭐️ НОВАЯ) Функция только для /test_broadcast. Отправляет только админу."""
    user_id = context.job.chat_id # Получаем ID админа из chat_id
    logger.info(f"Запуск ТЕСТОВОЙ рассылки для ID {user_id}.")
    
    birthdays_today = get_today_birthdays(BIRTHDAYS) 
    
    if not birthdays_today:
        logger.info("Тест рассылки: Дней рождения сегодня нет.")
        await context.bot.send_message(chat_id=user_id, text="✅ Тест завершен. Сегодня нет именинников.")
        return

    try:
        await context.bot.send_message(chat_id=user_id, text=f"✅ Тест: Найдено {len(birthdays_today)} именинников. Отправляю...")
        for fio_dr, _ in birthdays_today:
            person_folder_path = find_folder_by_fio_dr(fio_dr, PERSON_FOLDERS)
            
            if person_folder_path:
                await send_birthday_message(context, user_id, fio_dr, person_folder_path)
            else:
                logger.warning(f"Тест рассылки: Не удалось найти папку для: {fio_dr}")
                await context.bot.send_message(chat_id=user_id, text=f"⚠️ Тест: Не найдена папка для {fio_dr}")
    except Exception as e:
        logger.error(f"Ошибка при тестовой рассылке для ID {user_id}: {e}")
        await context.bot.send_message(chat_id=user_id, text=f"❌ Ошибка тестовой рассылки: {e}")


# --- 12. ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК (⭐️ НОВЫЙ) ---

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует ошибки и отправляет уведомление главному админу."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    # Форматируем трейсбек
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    # Форматируем сообщение для админа
    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    try:
        # 1. Отправляем заголовок
        await context.bot.send_message(
            chat_id=MAIN_ADMIN_ID,
            text="‼️ **Критическая ошибка в боте** ‼️",
            parse_mode=ParseMode.HTML
        )
        
        # 2. Отправляем update (разбивая, если нужно)
        await context.bot.send_message(chat_id=MAIN_ADMIN_ID, text="<b>Update:</b>", parse_mode=ParseMode.HTML)
        update_content = json.dumps(update_str, indent=2, ensure_ascii=False)
        # Используем меньший лимит для <pre> тегов
        for chunk in split_text_into_chunks(update_content, 3500): 
             await context.bot.send_message(
                chat_id=MAIN_ADMIN_ID,
                text=f"<pre>{html.escape(chunk)}</pre>",
                parse_mode=ParseMode.HTML
            )
            
        # 3. Отправляем context data (обычно короткие)
        context_info = (
            f"<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n"
            f"<pre>context.user_data = {html.escape(str(context.user_data))}</pre>"
        )
        await context.bot.send_message(chat_id=MAIN_ADMIN_ID, text=context_info, parse_mode=ParseMode.HTML)

        # 4. Отправляем traceback (разбивая, если нужно)
        await context.bot.send_message(chat_id=MAIN_ADMIN_ID, text="<b>Traceback:</b>", parse_mode=ParseMode.HTML)
        for chunk in split_text_into_chunks(tb_string, 3500): # Используем меньший лимит
             await context.bot.send_message(
                chat_id=MAIN_ADMIN_ID,
                text=f"<pre>{html.escape(chunk)}</pre>",
                parse_mode=ParseMode.HTML
            )
            
    except Exception as e:
        logger.error(f"Провал! Не удалось отправить сообщение об ошибке админу: {e}")

# --- 13. ЗАПУСК БОТА (main) ---

async def set_bot_commands(application: Application):
    """Устанавливает команды в меню Telegram."""
    
    # Команды для всех пользователей
    user_commands = [
        telegram.BotCommand(COMMAND_SEARCH, "🔍 Включить режим Поиска"),
        telegram.BotCommand(COMMAND_CANCEL, "❌ Отменить режим ИИ / Сбросить"),
        telegram.BotCommand(COMMAND_BIRTHDAY, "🎂 У кого сегодня ДР?"),
        telegram.BotCommand("start", "🚀 Начать / Главное меню"),
    ]
    
    # Команды только для Админа
    admin_commands = user_commands + [
        telegram.BotCommand("test_broadcast", "🧪 Тест рассылки ДР"),
        telegram.BotCommand("add_admin", "➕ Добавить админа (ID)"),
        telegram.BotCommand("remove_admin", "➖ Удалить админа (ID)"),
        telegram.BotCommand("list_admins", "👥 Показать админов"),
    ]
    
    try:
        # Устанавливаем общие команды для всех
        await application.bot.set_my_commands(user_commands)
        
        # Устанавливаем расширенные команды для Главного Админа
        await application.bot.set_my_commands(
            admin_commands, 
            scope=telegram.BotCommandScopeChat(chat_id=MAIN_ADMIN_ID)
        )
        
        logger.info("Команды Telegram успешно установлены (общие и для админа).")
    except Exception as e:
        logger.error(f"Не удалось установить команды Telegram: {e}")

def main():
    """Запуск бота с персистентностью и всеми обработчиками."""
    global PERSON_FOLDERS
    
    if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
        logger.critical("ОШИБКА: TELEGRAM_BOT_TOKEN или GEMINI_API_KEY не найдены в .env. Бот не может запуститься.")
        return

    logger.info("--- Запуск бота ---")
    
    # 1. Загрузка папок
    logger.info(f"Загрузка списка файлов из: {FILE_LIST_PATH}")
    PERSON_FOLDERS = load_and_extract_person_folders(FILE_LIST_PATH)
    
    if not PERSON_FOLDERS:
        logger.critical(f"Бот не может найти базу папок (PERSON_FOLDERS пуст). Проверьте {FILE_LIST_PATH} и BASE_FOLDER_NAME.")
    else:
        logger.info(f"Успешно загружено {len(PERSON_FOLDERS)} папок.")

    # 2. ⭐️ НОВОЕ: Настройка персистентности
    # Данные будут сохраняться в файл 'bot_persistence.pkl'
    persistence = PicklePersistence(filepath="bot_persistence.pkl")
    
    # 3. Создание приложения
    try:
        application = (
            Application.builder()
            .token(TELEGRAM_BOT_TOKEN)
            .persistence(persistence) # Включаем сохранение
            .build()
        )
        
        # 4. ⭐️ НОВОЕ: Инициализация bot_data при первом запуске
        # (данные восстановятся из .pkl, если он существует)
        if 'whitelist' not in application.bot_data:
            # Если whitelist не найден в персистентности, инициализируем его
            application.bot_data['whitelist'] = INITIAL_WHITELISTED_USER_IDS
            logger.info(f"💾 Инициализирован новый 'whitelist' с {len(INITIAL_WHITELISTED_USER_IDS)} ID.")
        elif isinstance(application.bot_data['whitelist'], set):
            # Если whitelist уже существует, убедимся, что начальные ID там есть
            application.bot_data['whitelist'].update(INITIAL_WHITELISTED_USER_IDS)
            logger.info("💾 Существующий 'whitelist' обновлен начальными ID.")
        else:
            # На случай, если в persistence сохранилось что-то некорректное
            application.bot_data['whitelist'] = INITIAL_WHITELISTED_USER_IDS
            logger.warning("⚠️ 'whitelist' в persistence был некорректным, перезаписан.")
        if 'pdf_map' not in application.bot_data:
            application.bot_data['pdf_map'] = {}
        if 'id_counter' not in application.bot_data:
            application.bot_data['id_counter'] = 0
            
        # Гарантируем, что главный админ всегда в списке
        application.bot_data['whitelist'].add(MAIN_ADMIN_ID)


        # 5. Добавление фоновых задач (Рассылка)
        application.job_queue.run_daily(
            birthday_broadcast,
            time=BROADCAST_TIME, 
            name="daily_birthday_broadcast"
        )
        logger.info(f"Ежедневная рассылка запланирована на {BROADCAST_TIME.strftime('%H:%M')} МСК.")
        
        # 6. Регистрация обработчиков
        
        # Команды
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler(COMMAND_SEARCH, search_command))
        application.add_handler(CommandHandler(COMMAND_CANCEL, cancel_command))
        application.add_handler(CommandHandler(COMMAND_BIRTHDAY, birthday_today_command))
        application.add_handler(CommandHandler("test_broadcast", test_broadcast_command))
        
        # Команды Админа
        application.add_handler(CommandHandler("add_admin", add_admin_command))
        application.add_handler(CommandHandler("remove_admin", remove_admin_command))
        application.add_handler(CommandHandler("list_admins", list_admins_command))

        # Сообщения
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
        
        # Кнопки
        application.add_handler(CallbackQueryHandler(button_handler))
        
        # ⭐️ НОВОЕ: Глобальный обработчик ошибок (должен быть последним)
        application.add_error_handler(error_handler)

        logger.info("🤖 Бот запущен. Ожидание сообщений...")
        
        # 7. ⭐️ NEW: Установка команд через post_init (вместо сложного asyncio)
        application.post_init = set_bot_commands
            
        # 8. Запуск polling
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except telegram.error.InvalidToken:
        logger.critical("ОШИБКА: Неверный токен Telegram. Бот не может запуститься.")
    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске бота: {e}", exc_info=True)

if __name__ == '__main__':
    main()