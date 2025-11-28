# Настройка Python Telegram Bot: Конфигурация, Логирование и Управление Доступом

*Критически важные компоненты инициализации Python Telegram Bot: загрузка конфигурации, настройка логирования, инициализация API (Gemini), а также система контроля доступа с использованием декораторов для ограничения функционала.*

## Конфигурация и Безопасность

### Загрузка Переменных Окружения (`.env`)

Ключевые параметры, такие как токены API и идентификаторы администраторов, загружаются из файла `.env` для обеспечения безопасности и гибкости.

```python
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
try:
    MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID"))
except (TypeError, ValueError):
    print("КРИТИЧЕСКАЯ ОШИБКА: MAIN_ADMIN_ID не установлен в .env файле. Бот не может запуститься.")
    exit()
```

> [!WARNING]
> Если `MAIN_ADMIN_ID` не установлен, бот не запустится, так как это критически важный параметр для управления доступом.

### Загрузка Основной Конфигурации (`config.json`)

Дополнительные настройки бота, такие как пути к файлам, дни рождения, временные зоны и лимиты запросов, хранятся в `config.json`.

```json
{
    "FILE_LIST_PATH": "all_files_utf8.txt",
    "BASE_FOLDER_NAME": "Dok_2.5",
    "BIRTHDAYS": {
        "Иванов И.И.": "20.01",
        "Петров П.П.": "15.03"
    },
    "TIMEZONE_MSK": "Europe/Moscow",
    "BROADCAST_HOUR": 9,
    "BROADCAST_MINUTE": 0,
    "MAX_GEMINI_REQUESTS_PER_MINUTE": 5,
    "MAX_PDF_SIZE_MB_AI": 15,
    "MAX_PDF_TO_SEND_COUNT": 10,
    "MAX_PDF_TOTAL_SIZE_MB_SEND": 50,
    "WHITELISTED_USER_IDS": [12345678, 87654321]
}
```

```python
try:
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
except FileNotFoundError:
    print("КРИТИЧЕСКАЯ ОШИБКА: config.json не найден. Бот не может запуститься.")
    exit()
except json.JSONDecodeError:
    print("КРИТИЧЕСКАЯ ОШИБКА: Не удалось прочитать config.json. Проверьте синтаксис.")
    exit()

# Пример использования констант из config.json
FILE_LIST_PATH = config.get("FILE_LIST_PATH", "all_files_utf8.txt")
BASE_FOLDER_NAME = config.get("BASE_FOLDER_NAME", "Dok_2.5")
# ... другие константы
```

> [!INFO]
> `WHITELISTED_USER_IDS` из `config.json` используется для инициализации белого списка, который затем будет храниться в `context.bot_data` для персистентности.

## Настройка Логирования и Путей

### Путь к Лог-файлу

Логирование настроено для записи в файл и вывода в консоль, что критически важно для отладки и мониторинга работы бота.

```python
try:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) # Исправлено: __file__
except NameError:
    SCRIPT_DIR = os.getcwd()

LOG_FILE_PATH = os.path.join(SCRIPT_DIR, "bot_log.txt")
```

### Конфигурация Loggers

```python
log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
root_logger = logging.getLogger()
root_logger.setLevel(logging.WARNING) # Понижаем уровень логгирования

# Отключение INFO-логов от httpx для уменьшения шума
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Очистка старых обработчиков (для перезапуска)
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

logger = logging.getLogger(__name__) # Исправлено: __name__
logger.info(f"--- Логгирование запущено. Логи сохраняются в: {LOG_FILE_PATH} ---")
logger.info(f"Путь к скрипту (SCRIPT_DIR): {SCRIPT_DIR}")
```

> [!TIP]
> Установка `root_logger.setLevel(logging.WARNING)` и отключение логов `httpx`/`httpcore` помогает значительно уменьшить объем логов, оставляя только критически важные сообщения.

## Инициализация Gemini AI

Интеграция с Google Gemini для функционала AI. Проверяется наличие API-ключа и библиотеки.

```python
try:
    from google import genai
    from google.genai.errors import APIError

    if not GEMINI_API_KEY:
        raise ValueError("API-ключ Gemini не установлен в .env.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    MODEL = 'gemini-1.5-flash' # Использование полной и актуальной модели
    GEMINI_ENABLED = True
    logger.info("Клиент Gemini успешно инициализирован.")
except ImportError:
    logger.warning("Библиотека 'google.genai' не найдена. AI будет отключен.")
    GEMINI_ENABLED = False
except Exception as e:
    logger.warning(f"Инициализация Gemini не удалась. AI будет отключен. Ошибка: {e}")
    GEMINI_ENABLED = False
```

> [!WARNING]
> Если `GEMINI_API_KEY` отсутствует или библиотека `google.genai` не установлена, функционал AI будет отключен, и бот продолжит работу без него.

## Декораторы для Управления Доступом

Декораторы обеспечивают централизованное управление правами доступа к командам и функциям бота.

### `restricted_access`

Ограничивает доступ к командам на основе белого списка пользователей, хранящегося в `context.bot_data`.

```python
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
```

> [!TIP]
> Использование `context.bot_data` позволяет сделать белый список персистентным между перезапусками бота, если используется `PicklePersistence`.

### `is_main_admin`

Ограничивает доступ только для главного администратора, определенного как `MAIN_ADMIN_ID`.

```python
def is_main_admin(func: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """
    Декоратор, ограничивающий доступ к функциям только для MAIN_ADMIN_ID.
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs) -> None:
        user = update.effective_user
        if not user or user.id != MAIN_ADMIN_ID:
            logger.warning(f"🚫 ОТКАЗ АДМИНУ: Пользователь ID {user.id if user else 'Unknown'} попытался использовать команду для админа.")
            if update.message:
                await update.message.reply_text("🚫 У вас нет прав на выполнение этой команды.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper
```

> [!INFO]
> Оба декоратора используют `@wraps(func)` из модуля `functools`, что сохраняет метаданные оригинальной функции, улучшая отладку.

