import os
import re
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


def make_vk_event_key(data: Dict, message: Dict) -> str:
    """Stable key for VK Callback retries. VK may resend the same message_new event if
    our server answers too slowly, so we must not reply twice to the same event.
    Do not rely only on event_id: for retries/platform quirks it can be unstable.
    """
    group_id = str(data.get("group_id") or "")
    peer_id = str(message.get("peer_id") or message.get("from_id") or "")
    message_id = str(message.get("id") or "")
    conversation_message_id = str(message.get("conversation_message_id") or "")
    date = str(message.get("date") or "")
    text_hash = hashlib.sha1((message.get("text") or "").encode("utf-8", "ignore")).hexdigest()
    raw = "|".join([group_id, peer_id, message_id, conversation_message_id, date, text_hash])
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
        # Keep only recent processed event keys so the sqlite file does not grow forever.
        conn.execute(
            """
            DELETE FROM vk_processed_events
            WHERE rowid NOT IN (
                SELECT rowid FROM vk_processed_events ORDER BY rowid DESC LIMIT 3000
            )
            """
        )
        conn.commit()
        return cur.rowcount == 1


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
    name = first_name_part(user_name)
    if not name:
        return ""
    # Name should sound warm, not repetitive.
    return f"{name}, " if random.random() < 0.12 else ""


def is_short_greeting(text: str) -> bool:
    t = norm(text)
    return t in {"привет", "здравствуй", "здравствуйте", "добрый день", "доброе утро", "добрый вечер", "хай", "ку", "hello", "hi"}




def is_tarot_continuation(text: str) -> bool:
    t = norm(text)
    return t in {
        "давай по картам", "давай посмотрим по картам", "посмотри по картам", "можно по картам",
        "хочу по картам", "давай карты", "давай расклад", "сделай расклад", "давай", "да", "хочу",
        "ок", "окей", "ага", "можно", "конечно",
    }


def is_single_card_request(text: str) -> bool:
    t = norm(text)
    if "расклад" in t:
        return False
    if "карта дня" in t or ("карт" in t and "дня" in t):
        return True
    if "подсказ" in t and any(x in t for x in ["сегодня", "день", "сейчас"]):
        return True
    return any(x in t for x in ["вытяни карту", "вытащи карту", "достань карту", "одну карту", "1 карту"])


def is_spread_request(text: str) -> bool:
    t = norm(text)
    return any(x in t for x in ["расклад", "посмотри по картам", "узнать у карт", "совет карт", "погада"])


def single_card_topic(text: str) -> str:
    t = norm(text)
    m = re.search(r"(?:карту|карта|подсказк\w*)\s+(?:на|по|про|о|об|для)\s+(.+)", t)
    if m:
        topic = m.group(1).strip(" .?!,:;—-")
        if topic:
            return topic[:120]
    return ""


def extract_life_topic(text: str) -> str:
    t = norm(text)
    patterns = [
        r"стоит\s+ли\s+(.+)",
        r"нужно\s+ли\s+(.+)",
        r"можно\s+ли\s+(.+)",
        r"хочу\s+(.+)",
        r"думаю\s+(?:о|об|про)?\s*(.+)",
        r"переживаю\s+(?:за|из-за|насчет|про)?\s*(.+)",
        r"боюсь\s+(.+)",
    ]
    for p in patterns:
        m = re.search(p, t)
        if m:
            topic = m.group(1).strip(" .?!,:;—-")
            topic = re.sub(r"^(что|как|если|мне|я|это)\s+", "", topic).strip()
            if len(topic) >= 3:
                return topic[:180]
    return ""


def is_date_only_request(text: str) -> bool:
    t = norm(text)
    t = re.sub(r"\s+", " ", t).strip(" .?!,:;—-")
    months = "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
    if re.fullmatch(r"\d{1,2}[./-]\d{1,2}([./-]\d{2,4})?", t):
        return True
    if re.fullmatch(rf"\d{{1,2}}\s+({months})(\s+\d{{4}})?", t):
        return True
    if re.fullmatch(r"\d{4}", t):
        return True
    return False


def is_ambiguous_request(text: str) -> bool:
    t = norm(text)
    if is_date_only_request(text):
        return True
    words = re.findall(r"[а-яa-z0-9]+", t)
    if len(words) <= 1 and not any(x in t for x in ["таро", "карта", "расклад", "аффирма", "мотивац"]):
        return True
    if len(words) <= 2 and any(w in t for w in ["талисман", "дата", "число", "сон", "работа", "любовь", "переезд"]):
        return True
    return False


def clarify_request(user_text: str, user_name: Optional[str] = None) -> str:
    if is_date_only_request(user_text):
        return (
            "Поняла дату. А что именно хочешь посмотреть по ней?\n\n"
            "Могу разобрать нумерологию даты, энергию дня, астрологический фон, подсказку по событию или сделать расклад по картам."
        )
    topic = (user_text or "это").strip() or "это"
    return (
        f"Поняла: {topic}. Уточни, пожалуйста, что именно хочешь узнать?\n\n"
        "Могу дать мягкий совет, посмотреть символику, разобрать через нумерологию/астрологию или сделать расклад по картам."
    )



def soft_general_fallback(user_text: str) -> str:
    t = norm(user_text)

    if "нумеролог" in t or "энерг" in t and is_date_only_request(user_text):
        months = {
            "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
            "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
        }
        nums = re.findall(r"\d+", t)
        month_num = ""
        for m_name, m_num in months.items():
            if m_name in t:
                month_num = str(m_num)
                break
        digits_source = "".join(nums) + month_num
        digits = [int(ch) for ch in digits_source if ch.isdigit()]
        if digits:
            total = sum(digits)
            root = total
            while root > 9:
                root = sum(int(ch) for ch in str(root))
            meanings = {
                1: "начало, самостоятельность, первый шаг и личное решение",
                2: "партнёрство, чувствительность, баланс и умение слышать себя",
                3: "творчество, проявленность, общение и лёгкость",
                4: "структура, опора, порядок и практичные шаги",
                5: "перемены, движение, свобода и выход из привычного",
                6: "забота, отношения, красота и выбор сердцем",
                7: "внутренний поиск, интуиция, анализ и тишина",
                8: "сила, деньги, ответственность и управление ресурсами",
                9: "завершение, мудрость, отпускание и широкий взгляд",
            }
            return (
                f"Если смотреть это мягко через простую нумерологическую сумму, получается число {root}.\n\n"
                f"Энергия числа {root} — {meanings.get(root, 'символическая подсказка для размышления')}. "
                "Это не приговор и не точный прогноз, а скорее настроение даты. Его можно использовать как подсказку: что поддержать в себе, на что обратить внимание и какой шаг сделать осознаннее."
            )

    if "код изобилия" in t:
        return (
            "Могу помочь с кодом изобилия, но уточни, пожалуйста, по какой системе считать. "
            "Если ориентируемся на дату рождения, пришли дату полностью — день, месяц и год. Тогда я мягко разберу число и дам подсказку."
        )

    if "талисман" in t and any(x in t for x in ["имя", "назв", "как назвать", "выбрать"]):
        return (
            "Для талисмана лучше выбирать имя не только по красоте, но и по ощущению: чтобы оно звучало как опора, защита или намерение.\n\n"
            "Можно рассмотреть такие варианты:\n"
            "1. Луми — про свет и мягкое ведение.\n"
            "2. Аура — про защитное поле и внутреннее сияние.\n"
            "3. Мира — про спокойствие, лад и гармонию.\n"
            "4. Сея — про рост, намерение и посеянное желание.\n"
            "5. Веста — про домашний огонь, защиту и внутреннюю силу.\n\n"
            "А если хочешь точнее, напиши, для чего талисман: любовь, деньги, защита, уверенность, дорога или раскрытие женской энергии."
        )

    if "астролог" in t or "наталь" in t or "асценд" in t or "гороскоп" in t:
        return (
            "Могу посмотреть это в мягком астрологическом ключе. Для точного разбора нужны дата рождения, точное время и город рождения. "
            "Если точных данных нет, я могу дать общий символический фон без претензии на профессиональный расчёт."
        )

    topic = extract_life_topic(user_text)
    if topic:
        return (
            "Это решение всё равно должно остаться за тобой — я не хочу решать вместо тебя. "
            "Но могу помочь спокойно на него посмотреть. Попробуй прислушаться: от какого варианта внутри становится чуть свободнее, а от какого — тяжелее и теснее?\n\n"
            "Можно начать с трёх вопросов:\n"
            "1. Что я получу, если сделаю этот шаг?\n"
            "2. Что я потеряю, если оставлю всё как есть?\n"
            "3. Чего я на самом деле боюсь в этом решении?\n\n"
            "Если хочешь, можем отдельно посмотреть это через карты: например, сделать расклад из 3 карт — что держит, что открывается дальше и какой совет."
        )
    return (
        "Я рядом. Могу ответить мягко и по смыслу, но сейчас мне нужно чуть больше контекста. "
        "Напиши вопрос чуть подробнее — что именно хочешь понять или почувствовать в этой ситуации?"
    )



def count_requested(text: str, default: int = 5, min_count: int = 1, max_count: int = 20) -> int:
    m = re.search(r"\b(\d{1,2})\b", text or "")
    if not m:
        return default
    return max(min_count, min(max_count, int(m.group(1))))


def extract_topic_after_markers(text: str) -> str:
    raw = (text or "").strip()
    t = norm(raw)

    patterns = [
        r"(?:расклад|подсказк[ауи]?|совет|карту|карта|таро)\s+(?:на|по|про|о|об|для|насчет|по поводу)\s+(.+)",
        r"(?:сделай|дай|посмотри|вытащи|вытяни|хочу|нужен|нужна)\s+(?:мне\s+)?(?:расклад|подсказк[ауи]?|совет|карту|таро)\s*(?:на|по|про|о|об|для|насчет|по поводу)?\s*(.+)",
        r"(?:что\s+(?:меня|мне)\s+ждет|что\s+будет)\s+(.+)",
        r"(?:стоит\s+ли|нужно\s+ли|можно\s+ли)\s+(.+)",
        r"(?:по поводу)\s+(.+)",
    ]
    for p in patterns:
        m = re.search(p, t, flags=re.I)
        if m:
            topic = m.group(1).strip(" .?!,:;—-")
            topic = re.sub(r"^(сейчас|сегодня|пожалуйста|плиз|мне|я)\s+", "", topic).strip()
            if topic and topic not in {"расклад", "таро", "карту", "карта", "совет", "подсказку"}:
                return topic[:180]

    # Clean command words and use the rest as topic if it still looks meaningful.
    cleaned = re.sub(
        r"\b(сделай|дай|посмотри|вытяни|вытащи|хочу|нужен|нужна|мне|пожалуйста|плиз|расклад|таро|карту|карта|совет|подсказку|подсказка|на|по|про|о|об|для)\b",
        " ",
        t,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .?!,:;—-")
    if len(cleaned) >= 4:
        return cleaned[:180]
    return ""


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
    explicit_markers = [
        "таро", "расклад", "карт", "аркан", "погада", "вытяни", "вытащи",
        "узнать у карт", "посмотри по картам", "совет карт", "подсказка на неделю",
        "энергия недели", "энергия месяца", "карта дня", "карты скажут",
    ]
    if any(x in t for x in explicit_markers):
        return True
    if is_single_card_request(text):
        return True
    if is_tarot_continuation(text) and state and (state.get("last_topic") or ""):
        return True
    return False


def is_plain_tarot_menu_request(text: str) -> bool:
    t = norm(text)
    return t in {"таро", "карты", "хочу таро", "давай таро", "что по таро", "погадай"}


# -----------------------------
# Bot answers
# -----------------------------
def capabilities_answer(user_name: Optional[str] = None, compact: bool = False) -> str:
    name = first_name_part(user_name)
    if compact:
        return (
            "Я на связи. Можешь написать живыми словами: нужна карта дня, расклад на ситуацию, "
            "аффирмации, мотивация, мягкая поддержка, нумерология или астрологическая подсказка."
        )
    greeting = f"Привет, {name}!" if name else "Привет!"
    return (
        f"{greeting} Я могу помочь в нескольких форматах:\n\n"
        "🃏 карта дня;\n"
        "🌙 подсказка на неделю;\n"
        "🔮 расклад на ситуацию из 3 карт;\n"
        "❓ расклад на вопрос;\n"
        "💬 мягкая поддержка;\n"
        "✨ аффирмации;\n"
        "🔥 мотивация и настрой;\n"
        "🔢 нумерология и символика дат;\n"
        "🌌 астрологические подсказки.\n\n"
        "Напиши обычными словами, например: “сделай расклад на смену работы”, “дай 7 аффирмаций на любовь” или “что значит эта дата в нумерологии”."
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
    last_topic = (state.get("last_topic") or "").strip()

    # Continuation: user accepts previous offer to look through cards.
    if is_tarot_continuation(user_text):
        if not last_topic:
            update_user_state(user_id, last_intent="clarify", last_topic="", last_format="")
            return "Давай посмотрим по картам. Только уточни, пожалуйста, тему: работа, отношения, поездка, состояние или конкретный вопрос?"
        update_user_state(user_id, last_intent="tarot_spread", last_topic=last_topic, last_format="3_cards")
        return tarot_three_cards_answer(
            "Расклад из 3 карт",
            f"расклад на {last_topic}",
            ["что сейчас влияет", "что может открыться дальше", "совет карт"],
        )

    if is_plain_tarot_menu_request(user_text):
        update_user_state(user_id, last_intent="tarot_menu", last_topic="", last_format="")
        return tarot_menu(user_name)

    topic = extract_topic_after_markers(user_text)

    # One-card formats: card of the day, pull a card, soft hint for today.
    if is_single_card_request(user_text):
        single_topic = topic or single_card_topic(user_text) or "день"
        update_user_state(user_id, last_intent="tarot_single", last_topic=single_topic, last_format="single")
        format_name = "Карта дня" if "дня" in t or "сегодня" in t else "Одна карта"
        intro = "Посмотрим мягкую подсказку на сегодня ✨" if format_name == "Карта дня" else "Вытянем одну карту как мягкую подсказку."
        return tarot_single_answer(format_name, f"карта на {single_topic}", intro=intro)

    if "недел" in t:
        update_user_state(user_id, last_intent="tarot_week", last_topic=topic or "неделя", last_format="week")
        return tarot_three_cards_answer(
            "Подсказка на неделю",
            user_text,
            ["главная энергия недели", "что может поддержать", "бережный совет"],
        )

    if "месяц" in t or "месяц" in topic:
        update_user_state(user_id, last_intent="tarot_month", last_topic=topic or "месяц", last_format="month")
        return tarot_three_cards_answer(
            "Энергия месяца",
            user_text,
            ["главная энергия месяца", "зона роста", "совет на месяц"],
        )

    # Explicit spread/card reading request -> 3 cards.
    if is_spread_request(user_text) or topic:
        spread_topic = topic or "текущая ситуация"
        update_user_state(user_id, last_intent="tarot_spread", last_topic=spread_topic, last_format="3_cards")
        return tarot_three_cards_answer(
            "Расклад из 3 карт",
            f"расклад на {spread_topic}",
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
        "Если в сообщении есть тема, сохрани её в ответе мягко, но не делай расклад без прямой просьбы о картах. "
        "Без Markdown.\n"
        f"Имя пользователя: {name or 'неизвестно'}. Не обращайся по имени слишком часто.\n"
        f"Контекст последних сообщений:\n{context}\n"
        f"Сообщение пользователя: {user_text}"
    )
    ai = openrouter_simple(prompt, max_tokens=700)
    if ai:
        return clean_vk_text(ai)
    return (
        "Я рядом. Похоже, сейчас правда непросто. "
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
                "temperature": 0.72,
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


def general_openrouter_answer(user_id: int, user_text: str, user_name: Optional[str] = None) -> str:
    name = first_name_part(user_name)
    context = build_memory_context(user_id)
    prompt = (
        f"Ты — {BOT_NAME}, тёплый AI-помощник эзотерико-психологического сообщества. "
        "Отвечай живо, мягко и понятно. Не используй Markdown, решётки, жирный шрифт и кодовые блоки. "
        "Не начинай каждый ответ с приветствия. Не показывай меню без прямой просьбы. "
        "Если пользователь продолжает прошлую мысль, учитывай контекст, но не цепляй старую тему к новым самостоятельным запросам. "
        "Если пользователь НЕ просит карты, Таро, расклад или гадание явно, НЕ делай расклад и не притворяйся, что вытянул карты. "
        "Можно мягко предложить посмотреть через карты в конце, если это уместно. "
        "Ты можешь отвечать на мягкие вопросы по нумерологии, символике чисел, датам, талисманам, астрологии и энергии дня. "
        "Если для точного нумерологического или астрологического расчёта не хватает данных, попроси уточнить дату, время, место рождения или методику. "
        "Не выдумывай точные натальные карты, асцендент, дома гороскопа и сложные расчёты без нужных данных. "
        "Не давай медицинские, юридические или финансовые гарантии. "
        "Тон: бережный, образный, но без давления и категоричных предсказаний.\n"
        f"Имя пользователя: {name or 'неизвестно'}. Не обращайся по имени в обычном ответе, кроме редких случаев, когда это звучит естественно.\n"
        f"Контекст последних сообщений:\n{context}\n"
        f"Новое сообщение пользователя: {user_text}"
    )
    ai = openrouter_simple(prompt, max_tokens=950)
    if ai:
        return clean_vk_text(ai)
    return soft_general_fallback(user_text)


# -----------------------------
# Router
# -----------------------------
def build_answer(user_id: int, user_text: str, user_name: Optional[str] = None) -> str:
    user_text = (user_text or "").strip()
    if not user_text:
        return clarify_request(user_text, user_name)

    state = get_user_state(user_id)
    if user_name:
        update_user_state(user_id, user_name=user_name)

    save_message(user_id, "user", user_text)

    try:
        if is_short_greeting(user_text):
            update_user_state(user_id, greeted=1, last_intent="greeting", last_topic="", last_format="")
            answer = capabilities_answer(user_name, compact=bool(state.get("greeted")))
        elif wants_capabilities(user_text):
            update_user_state(user_id, greeted=1, last_intent="capabilities", last_topic="", last_format="")
            answer = capabilities_answer(user_name)
        elif wants_affirmations(user_text):
            answer = handle_affirmations(user_id, user_text, user_name)
        elif wants_tarot(user_text, state):
            answer = handle_tarot(user_id, user_text, user_name)
        elif wants_motivation(user_text):
            answer = handle_motivation(user_id, user_text, user_name)
        elif wants_support(user_text):
            answer = handle_support(user_id, user_text, user_name)
        elif is_ambiguous_request(user_text):
            update_user_state(user_id, last_intent="clarify", last_topic="", last_format="")
            answer = clarify_request(user_text, user_name)
        else:
            topic = extract_life_topic(user_text)
            update_user_state(
                user_id,
                last_intent="dialogue",
                last_topic=topic,
                last_format="",
            )
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

        event_key = make_vk_event_key(data, message)
        if not mark_vk_event_processed(event_key, user_id, user_text):
            print(f"VK_DUPLICATE_EVENT from_id={user_id} key={event_key}", flush=True)
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
