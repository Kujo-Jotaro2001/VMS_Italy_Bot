# Скопируй этот файл в config.py и заполни своими данными
# cp config_example.py config.py

TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"       # получишь от @BotFather
TELEGRAM_CHAT_ID   = ["YOUR_CHAT_ID", "SECOND_CHAT_ID"]  # один или список; второй узнаёт запустив setup_telegram.py

# Прямая ссылка на страницу с капчей (после капчи — информация о слотах)
TARGET_URL = "http://italyvms.com/autoform/?t=YOUR_TOKEN&lang=ru"

NO_SLOTS_TEXT = "На ближайшие 2 недели записи нет"
HEADLESS = False  # True — браузер без окна
