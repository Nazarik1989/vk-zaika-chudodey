import os
import re
import random
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

VK_GROUP_TOKEN = os.getenv("VK_GROUP_TOKEN", "").strip()
VK_CONFIRMATION_TOKEN = os.getenv("VK_CONFIRMATION_TOKEN", "").strip()
VK_SECRET_KEY = os.getenv("VK_SECRET_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip()
VK_API_VERSION = os.getenv("VK_API_VERSION", "5.199").strip()
BOT_NAME = os.getenv("BOT_NAME", "Зайка-чудодей").strip()
VK_GROUP_ID = int(os.getenv("VK_GROUP_ID", "232950079") or "232950079")
VK_MEMBERS_ONLY = os.getenv("VK_MEMBERS_ONLY", "true").strip().lower() in {"1", "true", "yes", "да", "on"}
DB_PATH = os.getenv("DB_PATH", "zaika_memory.db").strip()
MAX_MEMORY_MESSAGES = int(os.getenv("MAX_MEMORY_MESSAGES", "12") or "12")


# -----------------------------
# Health
# -----------------------------
@app.route("/health", methods=["GET"])
def health():
    return "ok", 200


# -----------------------------
# Persistent memory
# -----------------------------
def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def db_connect():
    return sqlite3.connect(DB_PATH, timeout=10)


def init_db() -> None:
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                user_name TEXT,
                greeted INTEGER DEFAULT 0,
                last_intent TEXT,
                last_topic TEXT,
                last_format TEXT,
                updated_at TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def get_user_state(user_id: int) -> Dict:
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            return {
                "user_id": user_id,
                "user_name": "",
                "greeted": 0,
                "last_intent": "",
                "last_topic": "",
                "last_format": "",
            }
        return dict(row)


def update_user_state(user_id: int, **kwargs) -> None:
    state = get_user_state(user_id)
    state.update(kwargs)
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, user_name, greeted, last_intent, last_topic, last_format, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                user_name=excluded.user_name,
                greeted=excluded.greeted,
                last_intent=excluded.last_intent,
                last_topic=excluded.last_topic,
                last_format=excluded.last_format,
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                state.get("user_name") or "",
                int(state.get("greeted") or 0),
                state.get("last_intent") or "",
                state.get("last_topic") or "",
                state.get("last_format") or "",
                now_iso(),
            ),
        )
        conn.commit()


def save_message(user_id: int, role: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, text, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, text[:4000], now_iso()),
        )
        # Keep the table small per user.
        conn.execute(
            """
            DELETE FROM messages
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT 40
            )
            """,
            (user_id, user_id),
        )
        conn.commit()


def get_recent_messages(user_id: int, limit: int = MAX_MEMORY_MESSAGES) -> List[Dict[str, str]]:
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT role, text FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        rows = [dict(row) for row in cur.fetchall()]
    rows.reverse()
    return rows


init_db()


# -----------------------------
# Text helpers
# -----------------------------
def clean_vk_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"```.*?```", lambda m: m.group(0).replace("```", ""), text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    text = text.replace("###", "").replace("##", "").replace("**", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е").strip()


def first_name_part(name: Optional[str]) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    return name.split()[0]


def maybe_name(user_name: Optional[str]) -> str:
    # Имя используем в явном приветствии.
    # В обычных ответах не подставляем имя, чтобы бот не звучал навязчиво.
    return ""


def remove_leading_name_vocative(text: str, user_name: Optional[str]) -> str:
    """Убирает навязчивое обращение по имени в начале AI-ответа.
    Например: «Понимаю, Варвара. ...» -> «Понимаю. ...»
    """
    name = first_name_part(user_name)
    if not name or not text:
        return text
    escaped = re.escape(name)
    cleaned = text.strip()
    cleaned = re.sub(rf"^({escaped})\s*[,!—-]+\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        rf"^(Понимаю|Хорошо|Конечно|Да|Смотри|Знаешь|Похоже|Я рядом)\s*,\s*{escaped}\s*([.!?])?\s*",
        lambda m: f"{m.group(1)}. ",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def is_short_greeting(text: str) -> bool:
    t = norm(text)
    return t in {"привет", "здравствуй", "здравствуйте", "добрый день", "доброе утро", "добрый вечер", "хай", "ку", "hello", "hi"}


def count_requested(text: str, default: int = 5, min_count: int = 1, max_count: int = 20) -> int:
    m = re.search(r"\b(\d{1,2})\b", text or "")
    if not m:
        return default
    return max(min_count, min(max_count, int(m.group(1))))


def extract_topic_after_markers(text: str) -> str:
    raw = (text or "").strip()
    t = norm(raw)

    patterns = [
        r"(?:расклад|подсказк[ауи]?|совет|карту|карта|карты|картам|таро)\s+(?:на|по|про|о|об|для|насчет|по поводу)\s+(.+)",
        r"(?:сделай|дай|посмотри|вытащи|вытяни|хочу|нужен|нужна)\s+(?:мне\s+)?(?:расклад|подсказк[ауи]?|совет|карту|карта|карты|таро)\s*(?:на|по|про|о|об|для|насчет|по поводу)?\s*(.+)",
        r"(?:что\s+(?:меня|мне)\s+ждет|что\s+будет)\s+(.+)",
        r"(?:стоит\s+ли|нужно\s+ли|надо\s+ли|можно\s+ли)\s+(.+)",
        r"(?:по поводу)\s+(.+)",
    ]
    for pat in patterns:
        m = re.search(pat, t, flags=re.I)
        if m:
            topic = cleanup_topic(m.group(1))
            if is_meaningful_topic(topic):
                return topic[:180]

    cleaned = re.sub(
        r"\b(сделай|дай|посмотри|вытяни|вытащи|хочу|нужен|нужна|мне|пожалуйста|плиз|расклад|таро|карту|карта|карты|картам|картами|совет|подсказку|подсказка|на|по|про|о|об|для|давай)\b",
        " ",
        t,
        flags=re.I,
    )
    cleaned = cleanup_topic(cleaned)
    if is_meaningful_topic(cleaned):
        return cleaned[:180]
    return ""


def cleanup_topic(topic: str) -> str:
    topic = (topic or "").strip().lower().replace("ё", "е")
    topic = topic.strip(" .?!,:;—-")
    topic = re.sub(r"^(сейчас|сегодня|пожалуйста|плиз|мне|я|давай|ну|а)\s+", "", topic).strip()
    topic = re.sub(r"\s+", " ", topic).strip(" .?!,:;—-")
    return topic


def is_meaningful_topic(topic: str) -> bool:
    topic = cleanup_topic(topic)
    if len(topic) < 4:
        return False
    trash = {
        "расклад", "таро", "карту", "карта", "карты", "картам", "картами",
        "совет", "подсказку", "подсказка", "по картам", "давай картам",
        "давай по картам", "посмотри по картам", "через карты", "карт",
    }
    if topic in trash:
        return False
    if re.fullmatch(r"(?:давай|посмотри|сделай|хочу|можно)?\s*(?:по|через)?\s*(?:карт|карты|картам|картами|таро|расклад)\s*", topic):
        return False
    return True


def is_soft_decision_question(text: str) -> bool:
    t = norm(text)
    markers = (
        "стоит ли", "нужно ли", "надо ли", "можно ли", "как быть", "что делать",
        "не знаю", "сомнева", "выбор", "выбрать", "решиться",
        "менять", "смен", "уходить", "уволь", "переезд", "ехать", "поезд",
        "отнош", "работ", "деньг", "покуп", "продаж", "дом", "квартир",
    )
    return any(m in t for m in markers)


def extract_life_topic(text: str) -> str:
    t = norm(text)
    patterns = [
        r"(?:стоит\s+ли|нужно\s+ли|надо\s+ли|можно\s+ли)\s+(.+)",
        r"(?:я\s+переживаю\s+за|переживаю\s+за|тревожусь\s+за|боюсь\s+за)\s+(.+)",
        r"(?:сомневаюсь\s+насчет|сомневаюсь\s+по поводу)\s+(.+)",
        r"(?:что\s+делать\s+с|как\s+быть\s+с)\s+(.+)",
    ]
    for pat in patterns:
        m = re.search(pat, t, flags=re.I)
        if m:
            topic = cleanup_topic(m.group(1))
            if is_meaningful_topic(topic):
                return topic[:180]

    topic = extract_topic_after_markers(t)
    if is_meaningful_topic(topic):
        return topic
    return ""


def is_tarot_continuation_to_previous_topic(text: str) -> bool:
    t = norm(text)
    compact = re.sub(r"\s+", " ", t).strip()
    phrases = {
        "да", "давай", "хочу", "можно", "конечно", "ага", "ок", "окей",
        "давай по картам", "по картам", "посмотри по картам", "смотрим по картам",
        "давай через карты", "через карты", "хочу по картам", "можно по картам",
        "давай расклад", "сделай расклад", "сделай по картам", "давай таро", "хочу таро",
    }
    if compact in phrases:
        return True
    if re.fullmatch(r"(?:давай|хочу|можно|посмотри|сделай)?\s*(?:по|через)?\s*(?:карты|картам|картами|таро|расклад)\s*", compact):
        return True
    return False


def is_single_card_request(text: str) -> bool:
    t = norm(text)
    if "расклад" in t or "3 карт" in t or "три карт" in t:
        return False
    return (
        "карта дня" in t
        or "одну карту" in t
        or "1 карту" in t
        or "вытяни карту" in t
        or "вытащи карту" in t
        or "достань карту" in t
        or re.search(r"\bкарту\b", t) is not None
    )


# -----------------------------
# Full Tarot deck
# -----------------------------
MAJOR_ARCANA: Dict[str, str] = {
    "Шут": "новое начало, свобода, доверие пути, свежий взгляд",
    "Маг": "воля, действие, личная сила, умение пользоваться ресурсами",
    "Жрица": "интуиция, тишина, скрытая информация, внутреннее знание",
    "Императрица": "рост, забота, плодородие, телесность, красота",
    "Император": "структура, границы, ответственность, порядок",
    "Иерофант": "традиции, обучение, наставник, правила, вера",
    "Влюблённые": "выбор, отношения, ценности, притяжение",
    "Колесница": "движение, контроль, победа, решимость",
    "Сила": "мягкая мощь, терпение, самообладание, смелость",
    "Отшельник": "пауза, внутренний поиск, мудрость, дистанция",
    "Колесо Фортуны": "поворот, цикл, шанс, перемены",
    "Справедливость": "честность, баланс, последствия, ясное решение",
    "Повешенный": "переоценка, пауза, другой взгляд, отпускание контроля",
    "Смерть": "завершение, трансформация, обновление, переход",
    "Умеренность": "гармония, исцеление, спокойный ритм, настройка баланса",
    "Дьявол": "привязанности, искушения, зависимость, тени желания",
    "Башня": "резкое очищение, разрушение старого, правда, освобождение",
    "Звезда": "надежда, вдохновение, мягкое восстановление, вера в путь",
    "Луна": "сомнения, тревоги, тайное, интуиция, неопределённость",
    "Солнце": "радость, ясность, успех, энергия, открытость",
    "Суд": "пробуждение, важный вывод, зов, новый этап",
    "Мир": "завершение, цельность, результат, переход на новый уровень",
}

SUIT_BASE = {
    "Жезлов": "действие, энергия, инициативу, амбиции, движение",
    "Кубков": "чувства, отношения, эмоциональный фон, близость",
    "Мечей": "мысли, решения, конфликты, честность, анализ",
    "Пентаклей": "деньги, работа, тело, быт, стабильность, практику",
}

RANK_MEANINGS = {
    "Туз": "новый импульс и зарождение возможности",
    "Двойка": "выбор, баланс и необходимость сверить направление",
    "Тройка": "рост, первые результаты и расширение",
    "Четвёрка": "опора, пауза, стабильность и фиксация",
    "Пятёрка": "напряжение, вызов и точка пересборки",
    "Шестёрка": "движение к облегчению, поддержка и восстановление",
    "Семёрка": "проверка, защита позиции и внутренняя стойкость",
    "Восьмёрка": "динамика, работа процесса и быстрые изменения",
    "Девятка": "личный итог, зрелость и приближение результата",
    "Десятка": "завершение цикла, нагрузка или полнота опыта",
    "Паж": "новость, проба, ученичество и первый шаг",
    "Рыцарь": "активное движение, напор и развитие события",
    "Королева": "зрелое принятие, забота и внутренняя устойчивость",
    "Король": "управление, ответственность и уверенное владение темой",
}


def build_minor_arcana() -> Dict[str, str]:
    deck = {}
    ranks = [
        "Туз", "Двойка", "Тройка", "Четвёрка", "Пятёрка", "Шестёрка", "Семёрка",
        "Восьмёрка", "Девятка", "Десятка", "Паж", "Рыцарь", "Королева", "Король",
    ]
    for suit, base in SUIT_BASE.items():
        for rank in ranks:
            deck[f"{rank} {suit}"] = f"{RANK_MEANINGS[rank]}; сфера карты — {base}"
    return deck


TAROT_MEANINGS: Dict[str, str] = {**MAJOR_ARCANA, **build_minor_arcana()}
TAROT_CARDS: List[str] = list(TAROT_MEANINGS.keys())


def draw_cards(count: int) -> List[str]:
    return random.sample(TAROT_CARDS, min(count, len(TAROT_CARDS)))


def tarot_card_description(card: str) -> str:
    meaning = TAROT_MEANINGS.get(card, "интуитивная подсказка, которую важно прочитать мягко и честно")
    return f"{card} — {meaning}."


# -----------------------------
# VK API helpers
# -----------------------------
def vk_api(method: str, params: Dict) -> Dict:
    if not VK_GROUP_TOKEN:
        return {"error": {"error_msg": "VK_GROUP_TOKEN is empty"}}
    payload = dict(params)
    payload["access_token"] = VK_GROUP_TOKEN
    payload["v"] = VK_API_VERSION
    try:
        r = requests.post(f"https://api.vk.com/method/{method}", data=payload, timeout=15)
        return r.json()
    except Exception as e:
        return {"error": {"error_msg": str(e)}}


def vk_send_message(user_id: int, text: str) -> None:
    text = clean_vk_text(text)
    if not text:
        text = "Я рядом. Напиши мне ещё раз чуть подробнее."
    data = vk_api(
        "messages.send",
        {
            "user_id": user_id,
            "message": text[:3900],
            "random_id": random.randint(1, 2_000_000_000),
        },
    )
    if "error" in data:
        print(f"VK_SEND_ERROR user_id={user_id} error={data['error']}", flush=True)


def vk_get_user_name(user_id: int) -> str:
    data = vk_api("users.get", {"user_ids": user_id, "fields": "first_name"})
    try:
        user = data.get("response", [{}])[0]
        return (user.get("first_name") or "").strip()
    except Exception:
        return ""


def vk_is_member(user_id: int) -> bool:
    if not VK_MEMBERS_ONLY:
        return True
    data = vk_api("groups.isMember", {"group_id": VK_GROUP_ID, "user_id": user_id})
    if "error" in data:
        print(f"VK_MEMBER_ERROR user_id={user_id} error={data['error']}", flush=True)
        # If VK check fails, do not block a real user by accident.
        return True
    return str(data.get("response")) in {"1", "true", "True"}


# -----------------------------
# Intent detection
# -----------------------------
def wants_capabilities(text: str) -> bool:
    t = norm(text)
    exact = {
        "что ты умеешь", "помощь", "команды", "меню", "start", "/start", "начать", "возможности",
        "как с тобой работать", "что можешь", "что ты можешь",
    }
    return t in exact


def wants_affirmations(text: str) -> bool:
    t = norm(text)
    return any(x in t for x in ["аффирмац", "утверждени", "позитивные фразы"])


def wants_motivation(text: str) -> bool:
    t = norm(text)
    return any(x in t for x in ["мотивац", "мотивируй", "настрой на день", "настрой на неделю", "вдохнови", "сил на"])


def wants_support(text: str) -> bool:
    t = norm(text)
    support_words = [
        "тяжело", "плохо", "грустно", "страшно", "тревожно", "устала", "устал", "выгорел",
        "выгорела", "не могу", "не получается", "нет сил", "поддержи", "хочу поговорить",
        "сомневаюсь", "переживаю", "паника", "одиноко", "не знаю что делать", "опустились руки",
    ]
    return any(x in t for x in support_words)


def wants_tarot(text: str, state: Optional[Dict] = None) -> bool:
    t = norm(text)

    # Важно: не уводим в Таро по обычным жизненным вопросам вроде
    # "стоит ли менять работу", "что будет с отпуском", "я переживаю за переезд".
    # Таро включается только когда человек явно просит карты/расклад/гадание
    # или продолжает уже начатый Таро-сценарий.
    explicit_tarot_markers = [
        "таро",
        "расклад",
        "карт",      # карта, карты, картам, картами
        "аркан",
        "колод",
        "погада",
        "гадани",
        "вытяни",
        "вытащи",
        "достань карту",
        "узнать у карт",
        "посмотри по картам",
        "совет карт",
        "подсказка карт",
        "энергия недели",
        "энергия месяца",
    ]
    if any(x in t for x in explicit_tarot_markers):
        return True

    if state and (state.get("last_intent") or "").startswith("tarot"):
        # Короткое подтверждение после предложения Таро: "да", "давай", "сделай".
        if t in {"да", "давай", "хочу", "можно", "конечно", "ага", "ок", "окей", "подскажи", "сделай"}:
            return True

    return False


def is_plain_tarot_menu_request(text: str) -> bool:
    t = norm(text)
    return t in {"таро", "расклад", "карты", "карту", "хочу таро", "давай таро", "сделай расклад"}


# -----------------------------
# Bot answers
# -----------------------------
def greeting_answer(user_name: Optional[str] = None, compact: bool = False) -> str:
    name = first_name_part(user_name)
    hello = f"Привет, {name}!" if name else "Привет!"
    if compact:
        return (
            f"{hello} Я здесь. Можешь написать обычными словами, что нужно: "
            "поддержка, аффирмации, мотивация, карта дня или расклад."
        )
    return (
        f"{hello} Я на связи и могу помочь в нескольких форматах:\n\n"
        "🃏 карта дня;\n"
        "🌙 подсказка на неделю;\n"
        "🔮 расклад на ситуацию из 3 карт;\n"
        "❓ расклад на вопрос;\n"
        "💬 мягкая поддержка;\n"
        "✨ аффирмации;\n"
        "🔥 мотивация и настрой.\n\n"
        "Напиши живыми словами, что сейчас нужно. Например: “стоит ли менять работу”, “давай по картам” или “дай 7 аффирмаций на любовь”."
    )


def capabilities_answer(user_name: Optional[str] = None, compact: bool = False) -> str:
    prefix = maybe_name(user_name)
    if compact:
        return (
            f"{prefix}я на связи. Можешь написать живыми словами: нужна карта дня, расклад на ситуацию, "
            "аффирмации, мотивация или мягкая поддержка."
        )
    return (
        f"{prefix}я могу помочь в нескольких форматах:\n\n"
        "🃏 карта дня;\n"
        "🌙 подсказка на неделю;\n"
        "🔮 расклад на ситуацию из 3 карт;\n"
        "❓ расклад на вопрос;\n"
        "💬 мягкая поддержка;\n"
        "✨ аффирмации;\n"
        "🔥 мотивация и настрой.\n\n"
        "Напиши обычными словами, например: “сделай расклад на смену работы” или “дай 7 аффирмаций на любовь”."
    )


def tarot_menu(user_name: Optional[str] = None) -> str:
    prefix = maybe_name(user_name)
    return (
        f"{prefix}могу сделать Таро-подсказку в нескольких форматах:\n\n"
        "🃏 карта дня;\n"
        "🌙 подсказка на неделю;\n"
        "🔮 расклад на ситуацию из 3 карт;\n"
        "❓ расклад на вопрос;\n"
        "📅 энергия месяца;\n"
        "💡 совет карт.\n\n"
        "Напиши формат и тему. Например: “расклад на смену работы” или “что меня ждёт в отношениях”."
    )


def tarot_single_answer(format_name: str, user_text: str, intro: str = "") -> str:
    card = draw_cards(1)[0]
    topic = extract_topic_after_markers(user_text)
    topic_line = f"Тема: {topic}.\n\n" if topic else ""
    return (
        f"{intro}\n" if intro else ""
    ) + (
        f"{format_name}\n"
        f"{topic_line}"
        f"Вытянутая карта: {card}.\n\n"
        f"{tarot_card_description(card)}\n\n"
        "Бережный совет: прислушайся к тому, где внутри появляется спокойное “да”, а где тело сжимается. "
        "Карта не приговор, а символическая подсказка — решение всё равно остаётся в твоих руках."
    )


def tarot_three_cards_answer(format_name: str, user_text: str, positions: Optional[List[str]] = None) -> str:
    topic = extract_topic_after_markers(user_text)
    if not topic:
        topic = "текущая ситуация"
    cards = draw_cards(3)
    positions = positions or ["что влияет на ситуацию", "что может открыться дальше", "совет карты"]
    lines = [
        f"{format_name}",
        f"Тема: {topic}.",
        "",
    ]
    for i, (pos, card) in enumerate(zip(positions, cards), start=1):
        lines.append(f"{i}. {pos.capitalize()} — {card}.")
        lines.append(tarot_card_description(card))
        lines.append(interpret_card_for_topic(card, topic, pos))
        lines.append("")
    lines.append(
        "Итог: воспринимай расклад как мягкую навигацию, а не как приговор. "
        "Он помогает увидеть настроение ситуации и возможные точки внимания."
    )
    return "\n".join(lines).strip()


def interpret_card_for_topic(card: str, topic: str, position: str) -> str:
    meaning = TAROT_MEANINGS.get(card, "символическая подсказка")
    prompt = (
        f"Карта: {card}. Значение: {meaning}. Тема пользователя: {topic}. "
        f"Позиция в раскладе: {position}. Дай 2-3 предложения мягкой интерпретации на русском. "
        "Без Markdown, без заголовков, без категоричных предсказаний."
    )
    answer = openrouter_simple(prompt, max_tokens=240)
    if answer:
        return clean_vk_text(answer)
    return f"В этой позиции карта мягко указывает на тему: {meaning}. Важно не спешить и свериться с реальными обстоятельствами."


def handle_tarot(user_id: int, user_text: str, user_name: Optional[str] = None) -> str:
    state = get_user_state(user_id)
    t = norm(user_text)

    # Если человек после обычного совета пишет “давай по картам”,
    # берём прошлую тему, а не делаем тему “картам”.
    if is_tarot_continuation_to_previous_topic(user_text):
        last_topic = state.get("last_topic") or "текущая ситуация"
        update_user_state(user_id, last_intent="tarot_spread", last_topic=last_topic, last_format="3_cards")
        return tarot_three_cards_answer(
            "Давай посмотрим это через расклад из 3 карт",
            f"расклад на {last_topic}",
            ["что сейчас влияет", "что может открыться дальше", "совет карт"],
        )

    if is_plain_tarot_menu_request(user_text):
        update_user_state(user_id, last_intent="tarot_menu", last_topic="", last_format="")
        return tarot_menu(user_name)

    topic = extract_topic_after_markers(user_text)
    if not is_meaningful_topic(topic):
        topic = state.get("last_topic") or ""

    if "недел" in t:
        update_user_state(user_id, last_intent="tarot_week", last_topic=topic or "неделя", last_format="week")
        return tarot_three_cards_answer(
            "Подсказка на неделю",
            user_text if topic else "расклад на неделю",
            ["главная энергия недели", "что может поддержать", "бережный совет"],
        )

    if "месяц" in t or "месяц" in topic:
        update_user_state(user_id, last_intent="tarot_month", last_topic=topic or "месяц", last_format="month")
        return tarot_three_cards_answer(
            "Энергия месяца",
            user_text if topic else "расклад на месяц",
            ["главная энергия месяца", "зона роста", "совет на месяц"],
        )

    if is_single_card_request(user_text):
        update_user_state(user_id, last_intent="tarot_single", last_topic=topic or "текущая ситуация", last_format="single")
        title = "Карта дня" if "дня" in t else "Одна карта"
        return tarot_single_answer(title, user_text if topic else f"карта на {topic or 'текущую ситуацию'}", intro="Посмотрим мягкую подсказку ✨")

    # Если явно просят расклад или есть нормальная тема, делаем расклад, не меню.
    if "расклад" in t or is_meaningful_topic(topic):
        update_user_state(user_id, last_intent="tarot_spread", last_topic=topic or "текущая ситуация", last_format="3_cards")
        return tarot_three_cards_answer(
            "Расклад из 3 карт",
            user_text if is_meaningful_topic(extract_topic_after_markers(user_text)) else f"расклад на {topic or 'текущую ситуацию'}",
            ["что сейчас влияет", "что может открыться дальше", "совет карт"],
        )

    update_user_state(user_id, last_intent="tarot_menu", last_topic="", last_format="")
    return tarot_menu(user_name)


def affirmation_topic(text: str) -> str:
    t = norm(text)
    m = re.search(r"(?:аффирмац\w*|утверждени\w*)\s+(?:на|для|про|о|об)\s+(.+)", t)
    if m:
        return m.group(1).strip(" .?!,:;—-")[:80]
    m = re.search(r"(?:на|для|про|о|об)\s+([а-яa-z\s-]{3,80})", t)
    if m:
        return m.group(1).strip(" .?!,:;—-")[:80]
    return "день"


def handle_affirmations(user_id: int, user_text: str, user_name: Optional[str] = None) -> str:
    count = count_requested(user_text, default=5, max_count=15)
    topic = affirmation_topic(user_text)
    update_user_state(user_id, last_intent="affirmations", last_topic=topic, last_format=str(count))

    prompt = (
        f"Составь {count} коротких, тёплых аффирмаций на тему: {topic}. "
        "Русский язык. Без Markdown. Нумерованный список. Без эзотерического давления, мягко и бережно."
    )
    ai = openrouter_simple(prompt, max_tokens=600)
    if ai:
        return clean_vk_text(f"Аффирмации на тему «{topic}»:\n\n{ai}")

    base = [
        f"Я разрешаю себе двигаться в теме «{topic}» спокойно и бережно.",
        "Я выбираю поддерживать себя, а не давить на себя.",
        "Я могу делать маленькие шаги и всё равно идти вперёд.",
        "Я замечаю свои чувства и отношусь к ним с уважением.",
        "Я достоин/достойна мягкости, любви и внутренней опоры.",
        "Я открываюсь хорошему в своём темпе.",
        "Я доверяю себе и своему пути.",
        "Я выбираю спокойствие там, где раньше выбирал/выбирала тревогу.",
        "Моя энергия возвращается ко мне мягко и естественно.",
        "Сегодня я могу быть на своей стороне.",
    ]
    return "Аффирмации:\n\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(base[:count], 1))


def handle_motivation(user_id: int, user_text: str, user_name: Optional[str] = None) -> str:
    topic = extract_topic_after_markers(user_text) or "сегодняшний день"
    update_user_state(user_id, last_intent="motivation", last_topic=topic, last_format="")
    prompt = (
        f"Дай короткую тёплую мотивацию на тему: {topic}. "
        "Тон: бережный, живой, без токсичной продуктивности. 5-7 предложений. Без Markdown."
    )
    ai = openrouter_simple(prompt, max_tokens=500)
    if ai:
        return clean_vk_text(ai)
    return (
        "Не всё сразу. Один спокойный шаг уже считается. "
        "Сегодня не нужно побеждать весь мир — достаточно выбрать одно действие, которое вернёт тебе ощущение опоры."
    )


def handle_support(user_id: int, user_text: str, user_name: Optional[str] = None) -> str:
    topic = extract_life_topic(user_text)
    update_user_state(user_id, last_intent="support", last_topic=topic, last_format="")
    name = first_name_part(user_name)
    context = build_memory_context(user_id)
    prompt = (
        "Ты — тёплый AI-помощник сообщества Зайка-чудодей. "
        "Пользователь пишет, что ему тяжело или нужна поддержка. "
        "Ответь бережно, по-человечески, без медицинских диагнозов и без давления. "
        "Не выдавай меню. Можешь задать один мягкий уточняющий вопрос в конце. "
        "Не обращайся к пользователю по имени в этом ответе, даже если имя известно. "
        "Без Markdown.\n"
        f"Имя пользователя известно: {name or 'неизвестно'}, но используй его только как внутренний контекст.\n"
        f"Контекст последних сообщений:\n{context}\n"
        f"Сообщение пользователя: {user_text}"
    )
    ai = openrouter_simple(prompt, max_tokens=650)
    if ai:
        return clean_vk_text(remove_leading_name_vocative(ai, user_name))
    return (
        f"{maybe_name(user_name)}я рядом. Похоже, сейчас правда непросто. "
        "Давай не будем требовать от себя сразу больших решений. Сделай один маленький шаг: выдохни, назови, что именно давит сильнее всего, и напиши мне."
    )


# -----------------------------
# OpenRouter
# -----------------------------
def openrouter_simple(prompt: str, max_tokens: int = 700) -> str:
    if not OPENROUTER_API_KEY:
        return ""
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://vk.com/",
                "X-Title": "Zaika Chudodey",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.75,
                "max_tokens": max_tokens,
            },
            timeout=45,
        )
        data = r.json()
        if "error" in data:
            print(f"OPENROUTER_ERROR {data['error']}", flush=True)
            return ""
        return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        print(f"OPENROUTER_EXCEPTION {e}", flush=True)
        return ""


def build_memory_context(user_id: int) -> str:
    rows = get_recent_messages(user_id, limit=10)
    if not rows:
        return "пока нет"
    lines = []
    for row in rows:
        role = "Пользователь" if row["role"] == "user" else "Бот"
        lines.append(f"{role}: {row['text'][:500]}")
    return "\n".join(lines[-10:])


def soft_dialogue_fallback(user_text: str, user_name: Optional[str] = None) -> str:
    """Живой запасной ответ, если AI временно недоступен.
    Не уводит в Таро без явной просьбы, но может мягко предложить карты как вариант.
    """
    text = norm(user_text)
    name = maybe_name(user_name)

    decision_markers = (
        "стоит ли", "нужно ли", "надо ли", "как быть", "что делать",
        "не знаю", "сомнева", "выбор", "выбрать", "решиться",
        "менять", "смен", "уходить", "уволь", "переезд", "ехать", "поезд",
        "отнош", "работ", "деньг", "покуп", "продаж", "дом", "квартир"
    )

    if any(m in text for m in decision_markers):
        return (
            f"{name}это решение всё равно должно остаться за тобой — я не хочу решать вместо тебя. "
            "Но могу помочь спокойно на него посмотреть. Попробуй прислушаться: от какого варианта внутри становится чуть свободнее, "
            "а от какого — тяжелее и теснее?\n\n"
            "Можно начать с трёх простых вопросов:\n"
            "1. Что я получу, если сделаю этот шаг?\n"
            "2. Что я потеряю, если останусь как есть?\n"
            "3. Чего я на самом деле боюсь в этом решении?\n\n"
            "Если хочешь, можем отдельно посмотреть это через карты: например, сделать расклад из 3 карт — что держит, что открывается дальше и какой совет."
        )

    return (
        f"{name}я рядом. Расскажи чуть подробнее, что именно происходит и какой вопрос сейчас самый главный. "
        "Я помогу спокойно разобрать ситуацию. Если захочешь, отдельно можем посмотреть её через карты."
    )


def general_openrouter_answer(user_id: int, user_text: str, user_name: Optional[str] = None) -> str:
    name = first_name_part(user_name)
    context = build_memory_context(user_id)
    prompt = (
        f"Ты — {BOT_NAME}, тёплый AI-помощник эзотерико-психологического сообщества. "
        "Отвечай живо, мягко и понятно. Не используй Markdown, решётки, жирный шрифт и кодовые блоки. "
        "Не начинай каждый ответ с приветствия. Не показывай меню без прямой просьбы. "
        "Если пользователь продолжает прошлую мысль, учитывай контекст. "
        "Если пользователь НЕ просит явно карты, Таро, расклад или гадание, не уводи ответ в Таро. "
        "На жизненные вопросы вроде 'стоит ли менять работу' отвечай как мягкий собеседник: решение остаётся за человеком, помоги посмотреть на чувства, риски и варианты. "
        "В конце можно коротко предложить: если хочешь, можем отдельно посмотреть через карты. "
        "Не давай медицинские, юридические или финансовые гарантии. "
        "Важно: не обращайся к пользователю по имени в обычных ответах. Имя можно использовать только в первом приветствии.\n"
        f"Имя пользователя известно: {name or 'неизвестно'}, но не вставляй его в текст ответа.\n"
        f"Контекст последних сообщений:\n{context}\n"
        f"Новое сообщение пользователя: {user_text}"
    )
    ai = openrouter_simple(prompt, max_tokens=800)
    if ai:
        return clean_vk_text(remove_leading_name_vocative(ai, user_name))
    return soft_dialogue_fallback(user_text, user_name)


# -----------------------------
# Router
# -----------------------------
def build_answer(user_id: int, user_text: str, user_name: Optional[str] = None) -> str:
    user_text = (user_text or "").strip()
    if not user_text:
        return capabilities_answer(user_name, compact=True)

    state = get_user_state(user_id)
    if user_name:
        update_user_state(user_id, user_name=user_name)

    save_message(user_id, "user", user_text)

    try:
        if is_short_greeting(user_text):
            if state.get("greeted"):
                answer = greeting_answer(user_name, compact=True)
            else:
                update_user_state(user_id, greeted=1, last_intent="greeting")
                answer = greeting_answer(user_name)
        elif wants_capabilities(user_text):
            update_user_state(user_id, greeted=1, last_intent="capabilities")
            answer = capabilities_answer(user_name)
        elif wants_affirmations(user_text):
            answer = handle_affirmations(user_id, user_text, user_name)
        elif wants_tarot(user_text, state):
            answer = handle_tarot(user_id, user_text, user_name)
        elif wants_motivation(user_text):
            answer = handle_motivation(user_id, user_text, user_name)
        elif wants_support(user_text):
            answer = handle_support(user_id, user_text, user_name)
        else:
            # Если пользователь только что выбрал Таро-меню, следующую тему можно принять
            # как тему расклада. В остальных случаях обычные жизненные фразы не уводим в Таро.
            last_intent = state.get("last_intent") or ""
            if last_intent in {"tarot_menu", "tarot_offer"} and len(user_text) > 3 and is_tarot_continuation_to_previous_topic(user_text):
                answer = handle_tarot(user_id, user_text, user_name)
            else:
                topic = extract_life_topic(user_text)
                if is_soft_decision_question(user_text) and is_meaningful_topic(topic):
                    # Запоминаем тему, чтобы следующее “давай по картам” относилось к этому вопросу.
                    update_user_state(user_id, last_intent="tarot_offer", last_topic=topic, last_format="")
                else:
                    update_user_state(user_id, last_intent="dialogue", last_topic=topic or state.get("last_topic") or "", last_format="")
                answer = general_openrouter_answer(user_id, user_text, user_name)
    except Exception as e:
        print(f"BUILD_ANSWER_ERROR user_id={user_id} error={e}", flush=True)
        answer = "Я рядом, но сейчас чуть споткнулся внутри. Напиши мне ещё раз — лучше чуть проще и конкретнее."

    answer = clean_vk_text(answer)
    save_message(user_id, "assistant", answer)
    return answer


# -----------------------------
# VK Callback
# -----------------------------
@app.route("/callback", methods=["POST"])
def callback():
    data = request.get_json(force=True, silent=True)
    if not data:
        return "ok"

    if VK_SECRET_KEY and not VK_SECRET_KEY.startswith("сюда_потом"):
        incoming_secret = data.get("secret")
        if incoming_secret != VK_SECRET_KEY:
            print("VK_BAD_SECRET", flush=True)
            return "ok"

    event_type = data.get("type")

    if event_type == "confirmation":
        return VK_CONFIRMATION_TOKEN or "c9c78fbe"

    if event_type == "message_new":
        message = data.get("object", {}).get("message", {})
        user_id = int(message.get("from_id") or 0)
        user_text = message.get("text", "") or ""
        print(f"VK_INCOMING from_id={user_id} text={user_text}", flush=True)

        if user_id <= 0:
            return "ok"

        user_name = vk_get_user_name(user_id)
        if user_name:
            update_user_state(user_id, user_name=user_name)

        if VK_MEMBERS_ONLY and not vk_is_member(user_id):
            name_part = first_name_part(user_name)
            prefix = f"{name_part}, " if name_part else ""
            vk_send_message(
                user_id,
                prefix + "я отвечаю только подписчикам сообщества. Подпишись на группу, а потом напиши мне ещё раз — и я с радостью продолжу ✨",
            )
            return "ok"

        answer = build_answer(user_id, user_text, user_name=user_name)
        vk_send_message(user_id, answer)
        return "ok"

    return "ok"


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000") or "5000")
    app.run(host="0.0.0.0", port=port)
