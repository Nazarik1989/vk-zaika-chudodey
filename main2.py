import os
import re
import time
import random
import sqlite3
import hashlib
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
MAX_MEMORY_MESSAGES = int(os.getenv("MAX_MEMORY_MESSAGES", "40") or "40")
AI_HISTORY_MESSAGES = max(6, min(10, int(os.getenv("AI_HISTORY_MESSAGES", "10") or "10")))
DUPLICATE_TEXT_COOLDOWN_SECONDS = int(os.getenv("DUPLICATE_TEXT_COOLDOWN_SECONDS", "12") or "12")


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


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in cur.fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


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
                last_bot_question TEXT,
                last_format TEXT,
                updated_at TEXT
            )
            """
        )
        ensure_column(conn, "users", "last_bot_question", "TEXT")
        ensure_column(conn, "users", "last_format", "TEXT")
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS vk_processed_events (
                event_key TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                message_text TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS vk_recent_user_texts (
                user_id INTEGER NOT NULL,
                text_hash TEXT NOT NULL,
                created_at_epoch INTEGER NOT NULL,
                PRIMARY KEY (user_id, text_hash)
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
                "last_bot_question": "",
                "last_format": "",
            }
        data = dict(row)
        data.setdefault("last_bot_question", "")
        data.setdefault("last_format", "")
        return data


def update_user_state(user_id: int, **kwargs) -> None:
    state = get_user_state(user_id)
    state.update(kwargs)
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, user_name, greeted, last_intent, last_topic, last_bot_question, last_format, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                user_name=excluded.user_name,
                greeted=excluded.greeted,
                last_intent=excluded.last_intent,
                last_topic=excluded.last_topic,
                last_bot_question=excluded.last_bot_question,
                last_format=excluded.last_format,
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                state.get("user_name") or "",
                int(state.get("greeted") or 0),
                state.get("last_intent") or "",
                state.get("last_topic") or "",
                state.get("last_bot_question") or "",
                state.get("last_format") or "",
                now_iso(),
            ),
        )
        conn.commit()


def save_message(user_id: int, role: str, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    if role not in {"user", "assistant"}:
        return
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, text, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, text[:4000], now_iso()),
        )
        conn.execute(
            """
            DELETE FROM messages
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?
            )
            """,
            (user_id, user_id, MAX_MEMORY_MESSAGES),
        )
        conn.commit()


def make_vk_event_key(data: Dict, message: Dict) -> str:
    """Stable key for VK Callback retries. One incoming VK event must produce at most one answer."""
    group_id = str(data.get("group_id") or "")
    event_id = str(data.get("event_id") or "")
    peer_id = str(message.get("peer_id") or message.get("from_id") or "")
    message_id = str(message.get("id") or "")
    conversation_message_id = str(message.get("conversation_message_id") or "")
    date = str(message.get("date") or "")
    text_hash = hashlib.sha1((message.get("text") or "").encode("utf-8", "ignore")).hexdigest()
    raw = "|".join([group_id, event_id, peer_id, message_id, conversation_message_id, date, text_hash])
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()


def mark_vk_event_processed(event_key: str, user_id: int, message_text: str) -> bool:
    """Returns True only for the first time we see this VK event."""
    if not event_key:
        return True
    with db_connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO vk_processed_events (event_key, user_id, message_text, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (event_key, user_id, (message_text or "")[:500], now_iso()),
        )
        conn.execute(
            """
            DELETE FROM vk_processed_events
            WHERE rowid NOT IN (
                SELECT rowid FROM vk_processed_events ORDER BY rowid DESC LIMIT 5000
            )
            """
        )
        conn.commit()
        return cur.rowcount == 1


def mark_user_text_not_in_cooldown(user_id: int, text: str) -> bool:
    """Short cooldown for identical user texts. Protects from double replies when VK/user repeats a message."""
    if DUPLICATE_TEXT_COOLDOWN_SECONDS <= 0:
        return True
    normalized = norm(text)
    if not normalized:
        return True
    text_hash = hashlib.sha1(normalized.encode("utf-8", "ignore")).hexdigest()
    now_epoch = int(time.time())
    threshold = now_epoch - DUPLICATE_TEXT_COOLDOWN_SECONDS
    with db_connect() as conn:
        row = conn.execute(
            "SELECT created_at_epoch FROM vk_recent_user_texts WHERE user_id = ? AND text_hash = ?",
            (user_id, text_hash),
        ).fetchone()
        if row and int(row[0]) >= threshold:
            return False
        conn.execute(
            """
            INSERT INTO vk_recent_user_texts (user_id, text_hash, created_at_epoch)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, text_hash) DO UPDATE SET created_at_epoch=excluded.created_at_epoch
            """,
            (user_id, text_hash, now_epoch),
        )
        conn.execute("DELETE FROM vk_recent_user_texts WHERE created_at_epoch < ?", (now_epoch - 3600,))
        conn.commit()
    return True


def get_recent_messages(user_id: int, limit: int = AI_HISTORY_MESSAGES) -> List[Dict[str, str]]:
    limit = max(1, min(int(limit or AI_HISTORY_MESSAGES), MAX_MEMORY_MESSAGES))
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
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def norm(text: str) -> str:
    text = (text or "").lower().replace("ё", "е").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def norm_compact(text: str) -> str:
    return re.sub(r"[.!?,;:…\-—\s]+", " ", norm(text)).strip()


def first_name_part(name: Optional[str]) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    return name.split()[0]


def maybe_name(user_name: Optional[str]) -> str:
    name = first_name_part(user_name)
    if not name:
        return ""
    # Имя звучит теплее, когда используется редко.
    return f"{name}, " if random.random() < 0.15 else ""


def is_short_greeting(text: str) -> bool:
    t = norm_compact(text)
    return t in {"привет", "здравствуй", "здравствуйте", "добрый день", "доброе утро", "добрый вечер", "хай", "ку", "hello", "hi"}


def count_requested(text: str, default: int = 5, min_count: int = 1, max_count: int = 20) -> int:
    m = re.search(r"\b(\d{1,2})\b", text or "")
    if not m:
        return default
    return max(min_count, min(max_count, int(m.group(1))))



def has_explicit_topic_marker(text: str) -> bool:
    t = norm(text)
    markers = [
        " на ", " по ", " про ", " об ", " о ", " для ", "насчет", "по поводу",
        "стоит ли", "нужно ли", "можно ли", "что меня ждет", "что мне ждать", "что будет",
    ]
    return any(marker in f" {t} " for marker in markers)


def extract_topic_after_markers(text: str) -> str:
    raw = (text or "").strip()
    t = norm(raw)

    patterns = [
        r"(?:расклад|подсказк[ауи]?|совет|карту|карта|таро)\s+(?:на|по|про|о|об|для|насчет|по поводу)\s+(.+)",
        r"(?:сделай|дай|посмотри|вытащи|вытяни|хочу|нужен|нужна)\s+(?:мне\s+)?(?:расклад|подсказк[ауи]?|совет|карту|карта|таро)\s*(?:на|по|про|о|об|для|насчет|по поводу)?\s*(.+)",
        r"(?:что\s+(?:меня|мне)\s+ждет|что\s+будет)\s+(.+)",
        r"(?:стоит\s+ли|нужно\s+ли|можно\s+ли)\s+(.+)",
        r"(?:по поводу|насчет)\s+(.+)",
    ]
    for p in patterns:
        m = re.search(p, t, flags=re.I)
        if m:
            topic = m.group(1).strip(" .?!,:;—-")
            topic = re.sub(r"^(сейчас|сегодня|пожалуйста|плиз|мне|я|давай)\s+", "", topic).strip()
            if topic and topic not in {"расклад", "таро", "карту", "карта", "карты", "совет", "подсказку", "по картам"}:
                return topic[:180]

    cleaned = re.sub(
        r"\b(сделай|дай|посмотри|вытяни|вытащи|хочу|нужен|нужна|мне|пожалуйста|плиз|расклад|таро|карту|карта|карты|совет|подсказку|подсказка|на|по|про|о|об|для|давай|можно|ок|окей)\b",
        " ",
        t,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .?!,:;—-")
    if len(cleaned) >= 4 and cleaned not in {"картам", "картам расклад", "расклад картам"}:
        return cleaned[:180]
    return ""


def is_probable_topic_fragment(text: str) -> bool:
    t = norm_compact(text)
    if not t or len(t) < 3:
        return False
    if len(t.split()) > 5:
        return False
    service_words = {
        "да", "нет", "ок", "окей", "ага", "меню", "помощь", "команды", "привет", "спасибо",
        "карта", "карты", "таро", "расклад", "давай", "хочу", "можно",
    }
    return t not in service_words


def infer_contextual_topic(user_text: str, state: Dict) -> str:
    if norm_compact(user_text) in {"давай по картам", "посмотри по картам", "по картам", "давай", "да", "хочу", "можно", "ок", "окей", "сделай"}:
        return ""
    topic = extract_topic_after_markers(user_text) if has_explicit_topic_marker(user_text) else ""
    if topic:
        return topic
    last_intent = state.get("last_intent") or ""
    last_question = norm(state.get("last_bot_question") or "")
    if is_probable_topic_fragment(user_text) and (
        last_intent in {"support", "dialogue", "date_clarify"}
        or any(word in last_question for word in ["что именно", "о чем", "какую тему", "что тревожит", "что хочешь"])
    ):
        return user_text.strip()[:180]
    return ""


def extract_last_bot_question(answer: str) -> str:
    text = clean_vk_text(answer)
    if "?" not in text:
        return ""
    candidates = re.findall(r"([^?]{5,220}\?)", text.replace("\n", " "))
    if not candidates:
        return ""
    return candidates[-1].strip()[:240]


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
    t = norm_compact(text)
    return t in {"что ты умеешь", "помощь", "команды", "меню"}


def wants_affirmations(text: str) -> bool:
    t = norm(text)
    return any(x in t for x in ["аффирмац", "утверждени", "позитивные фразы"])


def wants_motivation(text: str) -> bool:
    t = norm(text)
    return any(x in t for x in ["мотивац", "мотивируй", "настрой на день", "настрой на неделю", "вдохнови", "сил на"])


def wants_support(text: str) -> bool:
    t = norm(text)
    support_words = [
        "тяжело", "плохо", "грустно", "страшно", "тревожно", "тревога", "волнуюсь", "устала", "устал", "выгорел",
        "выгорела", "не могу", "не получается", "нет сил", "поддержи", "хочу поговорить",
        "сомневаюсь", "переживаю", "паника", "одиноко", "не знаю что делать", "опустились руки",
    ]
    return any(x in t for x in support_words)


def is_crisis_message(text: str) -> bool:
    t = norm(text)
    crisis_words = [
        "суицид", "самоуб", "убью себя", "покончить с собой", "покончу с собой", "не хочу жить", "жить не хочу",
        "хочу умереть", "самоповреж", "режу себя", "порезать себя", "выпил таблетки", "выпила таблетки",
        "меня бьют", "меня избивают", "мне угрожают", "угроза жизни", "насилие", "изнасил", "убить меня",
        "я убью", "хочу убить", "причинить вред",
    ]
    return any(x in t for x in crisis_words)


def wants_tarot(text: str, state: Optional[Dict] = None) -> bool:
    t = norm(text)
    tarot_words = [
        "таро", "расклад", "карт", "аркан", "погада", "вытяни", "вытащи",
        "что меня ждет", "что мне ждать", "что будет", "энергия недели", "энергия месяца",
        "стоит ли", "узнать у карт", "посмотри по картам", "давай по картам", "подсказка на неделю",
    ]
    if any(x in t for x in tarot_words):
        return True
    if state and (state.get("last_intent") or "").startswith("tarot"):
        if norm_compact(text) in {"да", "давай", "хочу", "можно", "конечно", "ага", "ок", "окей", "подскажи", "сделай"}:
            return True
    return False


def wants_numerology(text: str) -> bool:
    t = norm(text)
    return any(x in t for x in ["нумеролог", "число судьбы", "число имени", "матрица", "по дате рождения", "цифр", "число дня"])


def wants_astrology(text: str) -> bool:
    t = norm(text)
    return any(x in t for x in ["астролог", "гороскоп", "натальн", "знак зодиака", "зодиак", "луна в", "ретроград", "асцендент", "соляр"])


def wants_symbolic(text: str) -> bool:
    t = norm(text)
    return any(x in t for x in ["талисман", "амулет", "оберег", "символик", "символ", "как назвать", "имя для", "название для"])


MONTHS_RU = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"


def is_standalone_date_query(text: str) -> bool:
    t = norm_compact(text)
    if re.fullmatch(r"\d{1,2} \d{1,2} \d{2,4}", t):
        return True
    if re.fullmatch(r"\d{1,2} \d{1,2}", t):
        return True
    if re.fullmatch(rf"\d{{1,2}} ({MONTHS_RU})( \d{{2,4}})?", t):
        return True
    return False


def detect_tarot_mode(text: str, state: Optional[Dict] = None) -> str:
    t = norm(text)
    compact = norm_compact(text)

    if "карта дня" in t or ("карт" in t and "дня" in t):
        return "day"

    spread_cues = [
        "сделай расклад", "расклад", "посмотри по картам", "давай по картам", "по картам", "разложи", "три карты", "3 карты",
        "что меня ждет", "что мне ждать", "что будет", "стоит ли", "энергия недели", "энергия месяца",
    ]
    if any(x in t for x in spread_cues):
        return "spread"

    one_card_cues = [
        "вытяни карту", "вытащи карту", "одну карту", "1 карту", "дай карту", "карту на", "карта на", "карту для", "карта для",
    ]
    if any(x in t for x in one_card_cues):
        return "single"

    if state and (state.get("last_intent") or "").startswith("tarot"):
        if compact in {"да", "давай", "хочу", "можно", "конечно", "ага", "ок", "окей", "подскажи", "сделай"}:
            return "spread"

    if compact in {"таро", "карты", "карт", "погадай"}:
        return "ask_topic"

    if "карт" in t or "таро" in t:
        return "spread"
    return "spread"


def resolve_tarot_topic(text: str, state: Dict, mode: str) -> str:
    compact = norm_compact(text)
    command_only = {
        "давай по картам", "посмотри по картам", "по картам", "давай", "да", "хочу", "можно", "ок", "окей", "сделай"
    }
    if mode in {"spread", "single"} and compact in command_only:
        return (state.get("last_topic") or "").strip()

    topic = extract_topic_after_markers(text)
    if topic:
        return topic
    if mode == "day":
        return "день"
    return (state.get("last_topic") or "").strip()


# -----------------------------
# Bot answers
# -----------------------------
def gentle_retry_answer(user_name: Optional[str] = None) -> str:
    return f"{maybe_name(user_name)}я рядом. Напиши чуть подробнее, что хочется разобрать, и я продолжу с этого места."


def greeting_answer(user_name: Optional[str] = None) -> str:
    return (
        f"{maybe_name(user_name)}привет. Я рядом — можешь написать, что тревожит, какой вопрос хочется разобрать, "
        "или попросить карту дня."
    )


def capabilities_answer(user_name: Optional[str] = None) -> str:
    prefix = maybe_name(user_name)
    return (
        f"{prefix}я могу помочь так:\n\n"
        "1. Карта дня.\n"
        "2. Одна карта на вопрос.\n"
        "3. Расклад из 3 карт на ситуацию.\n"
        "4. Нумерология по понятному запросу.\n"
        "5. Астрологический фон или символика даты.\n"
        "6. Имена для талисманов, оберегов и символов.\n"
        "7. Мягкая поддержка, аффирмации и настрой.\n\n"
        "Можешь написать живыми словами, например: карта дня, расклад на переезд, нумерология даты рождения или как назвать талисман."
    )


def date_clarification_answer() -> str:
    return "Что именно хочешь посмотреть по этой дате: нумерологию, астрологический фон, символику или расклад?"


def membership_required_answer() -> str:
    return "Я с радостью пообщаюсь с тобой, но сейчас отвечаю только подписчикам сообщества. Подпишись, пожалуйста, и возвращайся — я буду рядом."


def crisis_answer() -> str:
    return (
        "Мне очень важно, чтобы ты сейчас был не один на один с этим. Если есть риск, что ты можешь навредить себе или кому-то, "
        "обратись к человеку рядом и свяжись с местной экстренной помощью прямо сейчас.\n\n"
        "Я могу побыть рядом в переписке, но в такой ситуации лучше не уходить в карты или символы. Напиши коротко: ты сейчас в безопасности?"
    )


def varied_advice(card: str, topic: str) -> str:
    variants = [
        f"Сделай один небольшой шаг в теме «{topic}» и посмотри, где становится спокойнее.",
        "Выбери действие, которое возвращает ощущение опоры, а не усиливает напряжение.",
        "Проверь факты, свои силы и реальный темп — этого достаточно для ближайшего шага.",
        "Оставь себе пространство для паузы: иногда ясность приходит после маленькой передышки.",
        "Держись ближе к тому решению, рядом с которым появляется больше честности и устойчивости.",
    ]
    return random.choice(variants)


def tarot_fallback_single(mode: str, topic: str, card: str) -> str:
    meaning = TAROT_MEANINGS.get(card, "символическая подсказка")
    title = "Карта дня" if mode == "day" else "Одна карта"
    if mode == "day":
        return (
            f"{title}: {card}.\n\n"
            f"Эта карта говорит о теме: {meaning}. Сегодня она подсвечивает главный внутренний настрой и то, как лучше распределить силы. "
            "В ней есть приглашение заметить не только внешние события, но и собственную реакцию на них. "
            "День может стать понятнее, если двигаться без спешки и смотреть, где появляется больше ясности. "
            "Карта работает как символическая подсказка, а не как точное предсказание.\n\n"
            f"Совет: {varied_advice(card, topic)}"
        )
    return (
        f"Одна карта на тему «{topic}»: {card}.\n\n"
        f"Трактовка: карта связана с темой: {meaning}. Она показывает важный оттенок ситуации и помогает увидеть, где сейчас находится точка внимания. "
        "В ней есть не приговор, а образ, через который можно мягче посмотреть на происходящее. "
        "Такой знак лучше читать вместе с реальными обстоятельствами, а не отдельно от них.\n\n"
        f"Связь с вопросом: в теме «{topic}» карта предлагает заметить, что уже созрело, а что пока требует времени. "
        "Она может указывать на внутренний ресурс, скрытое напряжение или следующий небольшой шаг. "
        "Если вопрос связан с выбором, полезно сверить желание, факты и собственное состояние.\n\n"
        f"Совет: {varied_advice(card, topic)}"
    )


def tarot_fallback_spread(topic: str, cards: List[str]) -> str:
    positions = ["что сейчас влияет", "что может открыться дальше", "совет карт"]
    lines = [f"Расклад из 3 карт на тему «{topic}».", ""]
    for i, (position, card) in enumerate(zip(positions, cards), start=1):
        meaning = TAROT_MEANINGS.get(card, "символическая подсказка")
        lines.append(f"{i}. {position}: {card}.")
        lines.append(
            f"Карта несёт значение: {meaning}. В этой позиции она показывает отдельный слой ситуации и помогает увидеть, "
            "куда сейчас уходит внимание. Её лучше воспринимать как мягкий символический ориентир. "
            "Она не фиксирует будущее, а раскрывает настроение момента и возможную точку роста."
        )
        lines.append(f"Совет: {varied_advice(card, topic)}")
        lines.append("")
    lines.append(
        "Итог: расклад показывает ситуацию не как готовый сценарий, а как карту внимания. "
        "Сейчас важны спокойный темп, честная оценка фактов и бережное отношение к себе. "
        "Если внутри много тревоги, полезно сначала вернуть опору, а потом принимать решения. "
        "Карты дают символический ракурс, а окончательный выбор лучше делать с учётом реальности."
    )
    return "\n".join(lines).strip()


def build_tarot_task_prompt(mode: str, topic: str, cards: List[str]) -> str:
    card_lines = "\n".join(f"{i}. {card}: {TAROT_MEANINGS.get(card, '')}" for i, card in enumerate(cards, start=1))
    if mode == "day":
        format_rules = (
            "Формат ответа: карта дня. Используй 1 карту. Трактовка карты занимает 4–6 предложений. "
            "Совет занимает 1–2 предложения. Основной объём ответа приходится на трактовку карты."
        )
    elif mode == "single":
        format_rules = (
            "Формат ответа: одна карта. Используй 1 карту. Трактовка занимает 3–5 предложений. "
            "Связь с вопросом занимает 3–5 предложений. Совет занимает 1–2 предложения. "
            "Основной объём ответа приходится на трактовку и связь с вопросом."
        )
    else:
        format_rules = (
            "Формат ответа: расклад из 3 карт. По каждой карте дай трактовку 4–6 предложений и краткий совет 1–2 предложения. "
            "После трёх карт дай общий итог 4–6 предложений. Основной объём ответа приходится на трактовки, связь с темой и общий итог."
        )
    return (
        f"Задача: сделать Таро-ответ для пользователя. Тема: {topic}.\n"
        f"Карты уже выбраны, используй только их:\n{card_lines}\n\n"
        f"{format_rules}\n"
        "Пиши живо, тепло, без пугающих обещаний и без гарантированных событий. "
        "Следи за разнообразием формулировок. Не повторяй одинаковые советы и финальные фразы."
    )


def generate_tarot_answer(user_id: int, mode: str, topic: str, cards: List[str]) -> str:
    task_prompt = build_tarot_task_prompt(mode, topic, cards)
    ai = openrouter_with_history(user_id, task_prompt=task_prompt, max_tokens=1500 if mode == "spread" else 900, temperature=0.78)
    if ai:
        return clean_vk_text(ai)
    if mode in {"day", "single"}:
        return tarot_fallback_single(mode, topic, cards[0])
    return tarot_fallback_spread(topic, cards)


def handle_tarot(user_id: int, user_text: str, user_name: Optional[str] = None) -> str:
    state = get_user_state(user_id)
    mode = detect_tarot_mode(user_text, state)

    if mode == "ask_topic":
        update_user_state(user_id, last_intent="tarot_ask_topic", last_bot_question="О какой теме сделать расклад или карту?")
        return "О какой теме сделать расклад или карту? Можешь написать одним словом, например: переезд, отношения, работа или деньги."

    topic = resolve_tarot_topic(user_text, state, mode)
    if not topic:
        topic = "текущая ситуация"

    if mode == "day":
        cards = draw_cards(1)
        update_user_state(user_id, last_intent="tarot_day", last_topic=topic, last_format="day")
        return generate_tarot_answer(user_id, "day", topic, cards)

    if mode == "single":
        cards = draw_cards(1)
        update_user_state(user_id, last_intent="tarot_single", last_topic=topic, last_format="single")
        return generate_tarot_answer(user_id, "single", topic, cards)

    cards = draw_cards(3)
    update_user_state(user_id, last_intent="tarot_spread", last_topic=topic, last_format="3_cards")
    return generate_tarot_answer(user_id, "spread", topic, cards)


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

    task = (
        f"Составь {count} коротких, тёплых аффирмаций на тему: {topic}. "
        "Нумерованный список обычным текстом. Тон мягкий и взрослый, без эзотерического давления."
    )
    ai = openrouter_with_history(user_id, task_prompt=task, max_tokens=600, temperature=0.75)
    if ai:
        return clean_vk_text(ai)

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
    topic = extract_topic_after_markers(user_text) or get_user_state(user_id).get("last_topic") or "сегодняшний день"
    update_user_state(user_id, last_intent="motivation", last_topic=topic, last_format="")
    task = (
        f"Дай короткую тёплую мотивацию на тему: {topic}. "
        "Тон бережный, живой, без токсичной продуктивности. 5–7 предложений."
    )
    ai = openrouter_with_history(user_id, task_prompt=task, max_tokens=500, temperature=0.75)
    if ai:
        return clean_vk_text(ai)
    return (
        "Не всё сразу. Один спокойный шаг уже считается. "
        "Сегодня не нужно побеждать весь мир — достаточно выбрать одно действие, которое вернёт ощущение опоры."
    )


def handle_support(user_id: int, user_text: str, user_name: Optional[str] = None) -> str:
    topic = infer_contextual_topic(user_text, get_user_state(user_id))
    update_user_state(user_id, last_intent="support", last_topic=topic or get_user_state(user_id).get("last_topic") or "", last_format="")
    task = (
        "Пользователь пишет, что ему тяжело или нужна поддержка. Ответь бережно, по-человечески. "
        "Сначала признай чувство, потом дай мягкую опору. В конце можно задать один уточняющий вопрос, если он помогает продолжить диалог."
    )
    ai = openrouter_with_history(user_id, task_prompt=task, max_tokens=650, temperature=0.76)
    if ai:
        return clean_vk_text(ai)
    return (
        f"{maybe_name(user_name)}я рядом. Похоже, сейчас правда непросто. "
        "Давай не будем требовать от себя сразу больших решений. Что именно тревожит сильнее всего?"
    )


def handle_numerology_astrology(user_id: int, user_text: str, kind: str) -> str:
    last_topic = infer_contextual_topic(user_text, get_user_state(user_id)) or get_user_state(user_id).get("last_topic") or ""
    intent = "numerology" if kind == "numerology" else "astrology"
    update_user_state(user_id, last_intent=intent, last_topic=last_topic, last_format="")
    if kind == "numerology":
        task = (
            "Ответь на понятный запрос по нумерологии как символическому инструменту. "
            "Дай разбор мягко, без точных обещаний и без давления. Если данных не хватает, задай один конкретный уточняющий вопрос."
        )
    else:
        task = (
            "Ответь на понятный запрос по астрологии или астрологическому фону как символическому инструменту. "
            "Дай разбор мягко, без точных обещаний и без давления. Если данных не хватает, задай один конкретный уточняющий вопрос."
        )
    ai = openrouter_with_history(user_id, task_prompt=task, max_tokens=850, temperature=0.74)
    if ai:
        return clean_vk_text(ai)
    return "Могу посмотреть это символически. Напиши, пожалуйста, что именно важно: дата, имя, период, отношения, работа или личное состояние."


def handle_symbolic(user_id: int, user_text: str) -> str:
    topic = infer_contextual_topic(user_text, get_user_state(user_id)) or "талисман, имя или символика"
    update_user_state(user_id, last_intent="symbolic", last_topic=topic, last_format="")
    task = (
        "Ответь на запрос про талисманы, имена, обереги или символику. "
        "Если пользователь просит придумать имя для талисмана, дай 8–12 вариантов с короткими пояснениями. "
        "Тон тёплый, живой, без приторности."
    )
    ai = openrouter_with_history(user_id, task_prompt=task, max_tokens=850, temperature=0.82)
    if ai:
        return clean_vk_text(ai)
    return (
        "Можно назвать талисман так: Луми, Искра, Тихоня, Северинка, Мира, Обережка, Злата, Руна, Соня или Лучик. "
        "Выбирай имя по ощущению: пусть оно звучит так, будто талисман действительно откликается."
    )


# -----------------------------
# OpenRouter and Constitution
# -----------------------------
ZAIKA_SYSTEM_PROMPT = """
Конституция Зайки-чудодея.

Роль.
Ты — Зайка-чудодей, тёплый живой помощник VK-сообщества. Ты поддерживаешь диалог, помогаешь с Таро, нумерологией, астрологическим фоном, талисманами, именами, символикой, аффирмациями и мягкой человеческой поддержкой.

Главный принцип контекста.
Всегда учитывай последние сообщения диалога. Если пользователь отвечает коротко после твоего вопроса, продолжай прежнюю тему. Пример: пользователь пишет «волнуюсь», ты спрашиваешь «Что именно тревожит?», пользователь пишет «переезд» — дальше речь идёт о тревоге вокруг переезда.

Память.
Используй историю последних сообщений, last_topic, last_bot_question и last_intent как опору для продолжения разговора. Когда тема уже известна, не проси пользователя повторять её.

Тон.
Пиши тепло, бережно, живо и по-человечески. Стиль взрослый и спокойный, без приторности. Эмодзи используй редко: ориентир 0–1 на ответ. Имя пользователя используй редко: ориентир не чаще одного обращения на пять предложений и не в каждом ответе подряд.

Формат VK.
Форматируй как обычное личное сообщение: короткие абзацы, простой текст, понятные фразы. Основной ответ должен выглядеть как живое сообщение, а не как статья или техническая инструкция.

Меню.
Показывай список возможностей только при прямой просьбе: «что ты умеешь», «помощь», «команды», «меню». На живые вопросы отвечай по смыслу вопроса.

Таро.
Таро — символический инструмент для размышления, а не точное предсказание. Карта дня — одна карта. Просьба «вытяни карту» — одна карта. Просьбы «сделай расклад», «посмотри по картам», «давай по картам» — расклад из 3 карт. Если пользователь сначала дал тему, а потом пишет «давай по картам», используй прошлую тему.

Формат Таро.
Карта дня: 1 карта, трактовка 4–6 предложений, совет 1–2 предложения.
Одна карта: 1 карта, трактовка 3–5 предложений, связь с вопросом 3–5 предложений, совет 1–2 предложения.
Расклад: 3 карты. По каждой карте: трактовка 4–6 предложений, совет 1–2 предложения. Итог: 4–6 предложений.
Совет по каждой карте должен быть кратким: 1–2 предложения. Основной объём ответа должен приходиться на трактовку карты, связь с вопросом и общий итог.

Разнообразие.
Следи за разнообразием формулировок. Меняй советы, переходы и финальные фразы. Одинаковые фразы вроде «прислушайся к себе» и «решение остаётся в твоих руках» заменяй более живыми и точными формулировками.

Нумерология, астрология, даты.
Если запрос понятный, отвечай через символический разбор. Если пользователь пишет только дату, например «29 июля 2026», задай уточнение: «Что именно хочешь посмотреть по этой дате: нумерологию, астрологический фон, символику или расклад?»

Талисманы, имена, символика.
Запросы про талисманы, имена, обереги и символы поддерживаются. На просьбу «как назвать талисман» дай варианты имён с короткими пояснениями.

Безопасность.
Таро, нумерология и астрология подаются как символические инструменты, не как гарантированные события. В темах здоровья, права, финансов и важных решений сохраняй бережность и предлагай сверяться с реальными обстоятельствами и профильными специалистами. При темах самоповреждения, насилия, угрозы жизни и риска причинить вред себе или другим переходи к поддержке и экстренной помощи, без карт и эзотерических трактовок.
""".strip()


def openrouter_request_messages(messages: List[Dict[str, str]], max_tokens: int = 700, temperature: float = 0.75) -> str:
    if not OPENROUTER_API_KEY:
        return ""
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://vk.com/",
                "X-Title": "Zaika Chudodey VK Bot",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": messages,
                "temperature": temperature,
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


def ai_history_messages(user_id: int) -> List[Dict[str, str]]:
    messages = []
    for row in get_recent_messages(user_id, limit=AI_HISTORY_MESSAGES):
        role = row.get("role")
        text = (row.get("text") or "").strip()
        if role in {"user", "assistant"} and text:
            messages.append({"role": role, "content": text[:1400]})
    return messages


def state_system_note(user_id: int) -> str:
    state = get_user_state(user_id)
    return (
        "Текущее состояние пользователя:\n"
        f"last_topic: {state.get('last_topic') or 'пока нет'}\n"
        f"last_bot_question: {state.get('last_bot_question') or 'пока нет'}\n"
        f"last_intent: {state.get('last_intent') or 'пока нет'}"
    )


def openrouter_with_history(user_id: int, task_prompt: str = "", max_tokens: int = 700, temperature: float = 0.75) -> str:
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": ZAIKA_SYSTEM_PROMPT},
        {"role": "system", "content": state_system_note(user_id)},
    ]
    if task_prompt:
        messages.append({"role": "system", "content": task_prompt})
    messages.extend(ai_history_messages(user_id))
    return openrouter_request_messages(messages, max_tokens=max_tokens, temperature=temperature)


def openrouter_simple(prompt: str, max_tokens: int = 700) -> str:
    messages = [
        {"role": "system", "content": ZAIKA_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    return openrouter_request_messages(messages, max_tokens=max_tokens, temperature=0.75)


def build_memory_context(user_id: int) -> str:
    rows = get_recent_messages(user_id, limit=AI_HISTORY_MESSAGES)
    if not rows:
        return "пока нет"
    lines = []
    for row in rows:
        role = "Пользователь" if row["role"] == "user" else "Бот"
        lines.append(f"{role}: {row['text'][:500]}")
    return "\n".join(lines[-AI_HISTORY_MESSAGES:])


def general_openrouter_answer(user_id: int, user_text: str, user_name: Optional[str] = None) -> str:
    task = (
        "Ответь на новое сообщение пользователя, сохраняя контекст. "
        "Если пользователь коротко отвечает на твой предыдущий вопрос, продолжай прежнюю тему. "
        "Если запрос понятный, отвечай сразу. Если данных не хватает, задай один мягкий уточняющий вопрос."
    )
    ai = openrouter_with_history(user_id, task_prompt=task, max_tokens=850, temperature=0.76)
    if ai:
        return clean_vk_text(ai)

    state = get_user_state(user_id)
    topic = infer_contextual_topic(user_text, state) or state.get("last_topic") or user_text.strip()[:120]
    if topic and is_probable_topic_fragment(topic):
        return (
            f"Понял, речь про {topic}. Давай посмотрим на это спокойно: что в этой теме сейчас тревожит сильнее всего — "
            "сам факт перемен, неопределённость, люди вокруг или практические дела?"
        )
    return gentle_retry_answer(user_name)


# -----------------------------
# Router
# -----------------------------
def build_answer(user_id: int, user_text: str, user_name: Optional[str] = None) -> str:
    user_text = (user_text or "").strip()
    if user_name:
        update_user_state(user_id, user_name=user_name)

    if not user_text:
        return gentle_retry_answer(user_name)

    state = get_user_state(user_id)
    contextual_topic = infer_contextual_topic(user_text, state)
    if not contextual_topic and is_probable_topic_fragment(user_text) and not (
        is_short_greeting(user_text)
        or wants_capabilities(user_text)
        or is_standalone_date_query(user_text)
        or wants_support(user_text)
        or wants_tarot(user_text, state)
        or wants_affirmations(user_text)
        or wants_motivation(user_text)
        or wants_numerology(user_text)
        or wants_astrology(user_text)
        or wants_symbolic(user_text)
    ):
        contextual_topic = user_text.strip()[:180]

    if contextual_topic:
        update_user_state(user_id, last_topic=contextual_topic)
        state = get_user_state(user_id)

    save_message(user_id, "user", user_text)

    try:
        if is_crisis_message(user_text):
            update_user_state(user_id, last_intent="safety_crisis", last_topic=contextual_topic or state.get("last_topic") or "", last_format="")
            answer = crisis_answer()
        elif is_short_greeting(user_text):
            update_user_state(user_id, greeted=1, last_intent="greeting", last_format="")
            answer = greeting_answer(user_name)
        elif wants_capabilities(user_text):
            update_user_state(user_id, greeted=1, last_intent="capabilities", last_format="menu")
            answer = capabilities_answer(user_name)
        elif is_standalone_date_query(user_text):
            update_user_state(user_id, last_intent="date_clarify", last_topic=user_text, last_bot_question=date_clarification_answer(), last_format="")
            answer = date_clarification_answer()
        elif wants_affirmations(user_text):
            answer = handle_affirmations(user_id, user_text, user_name)
        elif wants_tarot(user_text, state):
            answer = handle_tarot(user_id, user_text, user_name)
        elif wants_numerology(user_text):
            answer = handle_numerology_astrology(user_id, user_text, "numerology")
        elif wants_astrology(user_text):
            answer = handle_numerology_astrology(user_id, user_text, "astrology")
        elif wants_symbolic(user_text):
            answer = handle_symbolic(user_id, user_text)
        elif wants_motivation(user_text):
            answer = handle_motivation(user_id, user_text, user_name)
        elif wants_support(user_text):
            answer = handle_support(user_id, user_text, user_name)
        else:
            last_intent = state.get("last_intent") or ""
            if last_intent == "tarot_ask_topic" and len(user_text) > 2:
                answer = handle_tarot(user_id, f"расклад на {user_text}", user_name)
            elif last_intent == "date_clarify" and contextual_topic:
                answer = general_openrouter_answer(user_id, user_text, user_name)
            else:
                update_user_state(user_id, last_intent="dialogue", last_topic=contextual_topic or state.get("last_topic") or "", last_format="")
                answer = general_openrouter_answer(user_id, user_text, user_name)
    except Exception as e:
        print(f"BUILD_ANSWER_ERROR user_id={user_id} error={e}", flush=True)
        answer = "Я рядом, но сейчас чуть споткнулся внутри. Напиши мне ещё раз — лучше чуть проще и конкретнее."

    answer = clean_vk_text(answer)
    save_message(user_id, "assistant", answer)
    update_user_state(user_id, last_bot_question=extract_last_bot_question(answer))
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

        event_key = make_vk_event_key(data, message)
        if not mark_vk_event_processed(event_key, user_id, user_text):
            print(f"VK_DUPLICATE_EVENT from_id={user_id} key={event_key}", flush=True)
            return "ok"

        if not mark_user_text_not_in_cooldown(user_id, user_text):
            print(f"VK_DUPLICATE_TEXT_COOLDOWN from_id={user_id} text={user_text[:80]}", flush=True)
            return "ok"

        user_name = vk_get_user_name(user_id)
        if user_name:
            update_user_state(user_id, user_name=user_name)

        if VK_MEMBERS_ONLY and not vk_is_member(user_id):
            vk_send_message(user_id, membership_required_answer())
            return "ok"

        answer = build_answer(user_id, user_text, user_name=user_name)
        vk_send_message(user_id, answer)
        return "ok"

    return "ok"


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000") or "5000")
    app.run(host="0.0.0.0", port=port)
