import os
import re
import random
import requests
from flask import Flask, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

VK_GROUP_TOKEN = os.getenv("VK_GROUP_TOKEN")
VK_CONFIRMATION_TOKEN = os.getenv("VK_CONFIRMATION_TOKEN")
VK_SECRET_KEY = os.getenv("VK_SECRET_KEY")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")

VK_API_VERSION = os.getenv("VK_API_VERSION", "5.199")
BOT_NAME = os.getenv("BOT_NAME", "Зайка-Чудодей")

# Простая память диалога. После перезапуска сервера очищается.
USER_STATE = {}

TAROT_CARDS = [
    "Шут", "Маг", "Жрица", "Императрица", "Император",
    "Иерофант", "Влюблённые", "Колесница", "Сила", "Отшельник",
    "Колесо Фортуны", "Справедливость", "Повешенный", "Смерть",
    "Умеренность", "Дьявол", "Башня", "Звезда", "Луна", "Солнце",
    "Суд", "Мир"
]

TAROT_MEANINGS = {
    "Шут": "новое начало, доверие пути, свежий взгляд",
    "Маг": "действие, личная сила, умение влиять на ситуацию",
    "Жрица": "интуиция, тишина, скрытое знание",
    "Императрица": "рост, забота, плодородие идей",
    "Император": "структура, опора, ответственность",
    "Иерофант": "мудрость, традиции, наставничество",
    "Влюблённые": "выбор, отношения, согласие с собой",
    "Колесница": "движение, воля, управление направлением",
    "Сила": "мягкая внутренняя мощь, терпение, самообладание",
    "Отшельник": "пауза, поиск смысла, внутренний ответ",
    "Колесо Фортуны": "перемены, цикл, неожиданный поворот",
    "Справедливость": "честность, баланс, последствия решений",
    "Повешенный": "новый взгляд, пауза, переоценка",
    "Смерть": "завершение этапа, освобождение, обновление",
    "Умеренность": "гармония, исцеление, спокойный ритм",
    "Дьявол": "привязанности, искушения, честный взгляд на зависимости",
    "Башня": "разрушение старого, правда, освобождение от иллюзий",
    "Звезда": "надежда, вдохновение, мягкое восстановление",
    "Луна": "сомнения, эмоции, неясность, интуиция",
    "Солнце": "ясность, радость, энергия, открытость",
    "Суд": "пробуждение, важный вывод, переход на новый уровень",
    "Мир": "завершение, целостность, результат"
}

AFFIRMATION_TOPICS = [
    "любовь", "уверенность", "спокойствие", "деньги",
    "отношения", "принятие себя", "энергия", "счастье",
    "женственность", "самооценка", "работа", "успех"
]

MOTIVATION_TOPICS = [
    "поддержка в усталости", "мотивация на работу",
    "уверенность перед шагом", "спокойный план на день",
    "поддержка в тревоге", "внутренняя опора",
    "начать новое", "не сдаваться"
]


def is_placeholder(value: str | None) -> bool:
    if not value:
        return True
    return value.startswith("сюда_потом")


def normalize(text: str) -> str:
    return (text or "").lower().strip()


def contains_any(text: str, words: list[str]) -> bool:
    return any(word in text for word in words)


def parse_count(text: str, default_count: int = 5) -> int:
    match = re.search(r"\b(\d{1,2})\b", text)
    if not match:
        return default_count

    count = int(match.group(1))
    return max(1, min(count, 10))


def vk_send_message(user_id: int, text: str):
    """Отправка сообщения пользователю VK."""
    if is_placeholder(VK_GROUP_TOKEN):
        print("VK_GROUP_TOKEN не настроен. Ответ не отправлен.")
        return

    url = "https://api.vk.com/method/messages.send"
    payload = {
        "user_id": user_id,
        "message": text[:3900],
        "random_id": random.randint(1, 2_000_000_000),
        "access_token": VK_GROUP_TOKEN,
        "v": VK_API_VERSION,
    }

    try:
        response = requests.post(url, data=payload, timeout=10)
        print("VK response:", response.text)
    except Exception as error:
        print("VK send error:", error)


def call_openrouter(system_prompt: str, user_prompt: str, max_tokens: int = 650):
    """Ответ через OpenRouter. Если ключа нет или ошибка — вернём None."""
    if is_placeholder(OPENROUTER_API_KEY):
        return None

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.85,
                "max_tokens": max_tokens,
            },
            timeout=30,
        )

        data = response.json()

        if "choices" not in data:
            print("OpenRouter unexpected response:", data)
            return None

        return data["choices"][0]["message"]["content"].strip()

    except Exception as error:
        print("OpenRouter error:", error)
        return None


def base_system_prompt() -> str:
    return f"""
Ты {BOT_NAME}, мягкий VK-помощник для сообщества.

Главные направления:
1. Таро-подсказки.
2. Аффирмации.
3. Мотивация и поддержка.

Стиль:
- живой диалог, не сухое меню;
- тепло, бережно, понятно;
- без постоянного обращения "Зай";
- не используй фразу "мягкий пинок";
- не пиши огромные простыни;
- если человек просит общий раздел, мягко предложи варианты;
- если человек уже дал конкретный запрос, сразу отвечай по делу;
- обращайся на "ты", но без фамильярности;
- не пугай;
- не давай фатальных предсказаний;
- Таро подавай как символическую подсказку, а не как приговор;
- не ставь диагнозы;
- не отменяй врачей, психологов и специалистов;
- при опасных темах мягко советуй обратиться за срочной помощью к близким или специалистам.

Ответ должен быть красивым, но не перегруженным.
"""


def capabilities_answer():
    return (
        "Привет ✨\n\n"
        "Я могу помочь в трёх направлениях:\n\n"
        "🃏 Таро — карта дня, подсказка на неделю, расклад на ситуацию, энергия месяца.\n"
        "🌷 Аффирмации — по теме любви, уверенности, спокойствия, денег, отношений и не только.\n"
        "🌿 Мотивация — поддержка, слова опоры, настрой на день или помощь, когда тяжело начать.\n\n"
        "Можно написать просто: «таро», «аффирмации» или «мотивация» — и я предложу подходящий формат."
    )


def tarot_menu():
    return (
        "Конечно ✨\n"
        "Могу сделать Таро-подсказку в нескольких форматах:\n\n"
        "🃏 карта дня;\n"
        "🌙 подсказка на неделю;\n"
        "🔮 расклад на ситуацию из 3 карт;\n"
        "❔ расклад на вопрос;\n"
        "🌌 энергия месяца;\n"
        "🕯 совет карт;\n"
        "👁 что мне важно увидеть сейчас.\n\n"
        "Напиши, какой формат выбираешь, или задай свой вопрос своими словами."
    )


def affirmation_menu():
    return (
        "Конечно 🌷\n"
        "Могу дать одну аффирмацию на сейчас или подборку по теме.\n\n"
        "Например:\n"
        "— аффирмации на любовь;\n"
        "— 10 аффирмаций на уверенность;\n"
        "— аффирмации на спокойствие;\n"
        "— аффирмации на деньги;\n"
        "— аффирмации на принятие себя;\n"
        "— аффирмации на отношения.\n\n"
        "Напиши тему и, если хочешь, количество."
    )


def motivation_menu():
    return (
        "Конечно 🌿\n"
        "Могу поддержать в разных форматах:\n\n"
        "— короткое мотивационное послание;\n"
        "— поддержка в усталости;\n"
        "— мотивация на работу;\n"
        "— уверенность перед важным шагом;\n"
        "— спокойный план на день;\n"
        "— слова опоры, когда тревожно.\n\n"
        "Напиши, что сейчас ближе."
    )


def tarot_card_description(card: str) -> str:
    meaning = TAROT_MEANINGS.get(card, "символическая подсказка и повод прислушаться к себе")
    return f"{card} — {meaning}"


def tarot_single_answer(format_name: str, user_text: str, intro: str):
    card = random.choice(TAROT_CARDS)

    system_prompt = base_system_prompt()
    user_prompt = f"""
Сделай ответ в формате: {format_name}.

Вытянутая карта: {card}.
Краткое значение карты: {TAROT_MEANINGS.get(card)}.

Запрос пользователя:
{user_text}

Структура ответа:
1. Назови карту.
2. Дай мягкую интерпретацию.
3. Объясни, что карта подсвечивает.
4. Дай бережный совет.
5. Напомни коротко, что Таро — это символическая подсказка, не приговор.

Без фатальности. Без запугивания. Без медицинских/финансовых гарантий.
"""

    ai_answer = call_openrouter(system_prompt, user_prompt, max_tokens=650)
    if ai_answer:
        return ai_answer

    return (
        f"{intro}\n\n"
        f"🃏 Карта: {tarot_card_description(card)}.\n\n"
        f"Эта карта мягко подсказывает: сейчас важно прислушаться к себе, "
        f"не торопить события и выбрать самый честный следующий шаг.\n\n"
        f"Таро здесь — символическая подсказка, не приговор."
    )


def tarot_three_cards_answer(format_name: str, user_text: str, positions: list[str]):
    cards = random.sample(TAROT_CARDS, 3)

    card_lines = []
    for position, card in zip(positions, cards):
        card_lines.append(f"{position}: {card} — {TAROT_MEANINGS.get(card)}")

    cards_text = "\n".join(card_lines)

    system_prompt = base_system_prompt()
    user_prompt = f"""
Сделай Таро-ответ в формате: {format_name}.

Вытянутые карты:
{cards_text}

Запрос пользователя:
{user_text}

Структура:
1. Короткое вступление.
2. Каждая карта отдельно: позиция, смысл, что подсвечивает.
3. Общий вывод.
4. Бережный совет.
5. Напоминание: Таро — символическая подсказка, не приговор.

Ответ должен быть живым, достаточно развёрнутым, но без огромной простыни.
Не пугай. Не обещай точных событий. Не делай диагнозов.
"""

    ai_answer = call_openrouter(system_prompt, user_prompt, max_tokens=900)
    if ai_answer:
        return ai_answer

    answer = [f"Сделаем {format_name.lower()} ✨\n"]
    for position, card in zip(positions, cards):
        answer.append(f"**{position}**\n🃏 {tarot_card_description(card)}.\n")

    answer.append(
        "Общий смысл расклада: сейчас важно смотреть на ситуацию спокойнее, "
        "замечать не только внешние события, но и своё внутреннее состояние.\n\n"
        "Таро — это символическая подсказка, не приговор."
    )

    return "\n".join(answer)


def handle_tarot(user_id: int, user_text: str):
    text = normalize(user_text)

    general_tarot_words = ["таро", "карты", "расклад"]
    specific_words = [
        "карта дня", "на день", "недел", "3 карт", "три карт",
        "ситуац", "вопрос", "месяц", "совет", "важно увидеть"
    ]

    if contains_any(text, general_tarot_words) and not contains_any(text, specific_words):
        return tarot_menu()

    if "карта дня" in text or "на день" in text:
        return tarot_single_answer(
            "Карта дня",
            user_text,
            "Карта дня для тебя ✨"
        )

    if "недел" in text:
        return tarot_three_cards_answer(
            "Подсказка на неделю",
            user_text,
            ["Главная энергия недели", "Что может поддержать", "Совет"]
        )

    if "месяц" in text:
        return tarot_three_cards_answer(
            "Энергия месяца",
            user_text,
            ["Фон месяца", "Возможность", "На что обратить внимание"]
        )

    if "важно увидеть" in text:
        return tarot_single_answer(
            "Что мне важно увидеть сейчас",
            user_text,
            "Посмотрим, что сейчас важно заметить ✨"
        )

    if "совет" in text:
        return tarot_single_answer(
            "Совет карт",
            user_text,
            "Совет карт на сейчас ✨"
        )

    if (
        "расклад" in text
        or "3 карт" in text
        or "три карт" in text
        or "ситуац" in text
        or "вопрос" in text
    ):
        # Если человек попросил расклад, но не описал ситуацию — уточняем.
        short_request = len(text.split()) <= 7 and "?" not in text
        if short_request:
            USER_STATE[user_id] = {"awaiting": "tarot_question"}
            return (
                "Хорошо, сделаем расклад из 3 карт ✨\n\n"
                "Напиши ситуацию или вопрос в одном-двух предложениях.\n"
                "Я разложу так:\n"
                "1. суть ситуации;\n"
                "2. что влияет;\n"
                "3. совет."
            )

        return tarot_three_cards_answer(
            "Расклад на ситуацию из 3 карт",
            user_text,
            ["Суть ситуации", "Что влияет", "Совет"]
        )

    return tarot_single_answer(
        "Таро-подсказка",
        user_text,
        "Посмотрим мягкую подсказку карт ✨"
    )


def extract_topic_after_markers(text: str, markers: list[str]) -> str | None:
    for marker in markers:
        if marker in text:
            topic = text.split(marker, 1)[1].strip(" .,!?:;-")
            topic = re.sub(r"\b\d{1,2}\b", "", topic).strip(" .,!?:;-")
            if topic:
                return topic
    return None


def handle_affirmations(user_text: str):
    text = normalize(user_text)

    count = parse_count(text, default_count=5)
    topic = extract_topic_after_markers(text, ["на тему", "на", "для", "про"])

    has_specific_topic = bool(topic)
    is_plural = "аффирмации" in text or "афирмации" in text

    if not has_specific_topic and len(text.split()) <= 3:
        return affirmation_menu()

    if not has_specific_topic:
        topic = "внутреннюю опору"

    if not is_plural and not re.search(r"\b\d{1,2}\b", text):
        count = 1

    system_prompt = base_system_prompt()
    user_prompt = f"""
Пользователь просит аффирмации.

Тема: {topic}
Количество: {count}

Сделай {count} аффирмаций.
Они должны быть:
- тёплые;
- простые;
- без токсичной позитивности;
- без обещаний невозможного;
- в настоящем времени;
- красивые, но не слишком длинные.

Если количество больше 1 — оформи списком.
"""

    ai_answer = call_openrouter(system_prompt, user_prompt, max_tokens=650)
    if ai_answer:
        return ai_answer

    if count == 1:
        return f"✨ Аффирмация на тему «{topic}»:\n\nЯ выбираю бережность к себе и доверяю своему пути."

    lines = [f"✨ Аффирмации на тему «{topic}»:\n"]
    templates = [
        "Я разрешаю себе двигаться в своём темпе.",
        "Я выбираю спокойствие и внутреннюю опору.",
        "Я достойна/достоин любви, уважения и заботы.",
        "Я могу делать маленькие шаги и всё равно идти вперёд.",
        "Я слышу себя и доверяю своим чувствам.",
        "Я открываюсь хорошему бережно и спокойно.",
        "Я принимаю себя без давления и спешки.",
        "Я создаю вокруг себя больше тепла и ясности.",
        "Я имею право на поддержку.",
        "Сегодня я выбираю быть на своей стороне."
    ]

    for i, phrase in enumerate(templates[:count], start=1):
        lines.append(f"{i}. {phrase}")

    return "\n".join(lines)


def handle_motivation(user_text: str):
    text = normalize(user_text)

    if len(text.split()) <= 3 and contains_any(text, ["мотивация", "мотивируй", "поддержка"]):
        return motivation_menu()

    system_prompt = base_system_prompt()
    user_prompt = f"""
Пользователь просит мотивацию или поддержку.

Сообщение пользователя:
{user_text}

Ответь живо и бережно.
Структура:
1. Коротко отрази состояние человека.
2. Дай тёплую поддержку.
3. Предложи 1-2 маленьких конкретных шага.
4. Заверши спокойной фразой опоры.

Не используй фразу "мягкий пинок".
Не дави. Не обесценивай. Не пиши огромную простыню.
"""

    ai_answer = call_openrouter(system_prompt, user_prompt, max_tokens=650)
    if ai_answer:
        return ai_answer

    return (
        "Я рядом 🌿\n\n"
        "Сейчас не нужно требовать от себя идеальности. "
        "Попробуй выбрать один маленький шаг, который правда по силам: "
        "открыть заметку, написать первое предложение, сделать один звонок или просто выдохнуть и начать с малого.\n\n"
        "Не всё сразу. Один спокойный шаг уже считается."
    )


def general_openrouter_answer(user_text: str):
    system_prompt = base_system_prompt()
    user_prompt = f"""
Сообщение пользователя:
{user_text}

Ответь как мягкий помощник сообщества.
Если по смыслу человек просит Таро, аффирмации или мотивацию — направь его в подходящий формат.
Если это обычное приветствие — коротко расскажи, чем можешь помочь.
"""

    ai_answer = call_openrouter(system_prompt, user_prompt, max_tokens=500)
    if ai_answer:
        return ai_answer

    return (
        "Привет ✨\n\n"
        "Я могу сделать Таро-подсказку, подобрать аффирмации или дать бережную мотивацию.\n"
        "Напиши, что хочется сейчас: «таро», «аффирмации» или «мотивация»."
    )


def build_answer(user_id: int, user_text: str):
    text = normalize(user_text)

    if not text:
        return "Напиши мне пару слов, и я подскажу, чем могу помочь ✨"

    if text in ["отмена", "стоп", "не надо", "сброс"]:
        USER_STATE.pop(user_id, None)
        return "Хорошо, остановились. Можем начать заново, когда будет удобно 🌿"

    state = USER_STATE.get(user_id)
    if state and state.get("awaiting") == "tarot_question":
        USER_STATE.pop(user_id, None)
        return tarot_three_cards_answer(
            "Расклад на ситуацию из 3 карт",
            user_text,
            ["Суть ситуации", "Что влияет", "Совет"]
        )

    if contains_any(text, ["что ты умеешь", "помощь", "команды", "начать", "старт"]):
        return capabilities_answer()

    tarot_triggers = [
        "таро", "карта", "карты", "расклад", "энергия месяца",
        "карта дня", "подсказка на неделю", "совет карт", "важно увидеть"
    ]

    affirmation_triggers = [
        "аффирмация", "аффирмации", "афирмация", "афирмации"
    ]

    motivation_triggers = [
        "мотивация", "мотивируй", "поддержи", "поддержка",
        "нет сил", "устала", "устал", "страшно", "не могу начать",
        "опора", "вдохнови"
    ]

    if contains_any(text, tarot_triggers):
        return handle_tarot(user_id, user_text)

    if contains_any(text, affirmation_triggers):
        return handle_affirmations(user_text)

    if contains_any(text, motivation_triggers):
        return handle_motivation(user_text)

    return general_openrouter_answer(user_text)


@app.route("/", methods=["GET"])
def index():
    return "VK Zaika Chudodey bot is alive"


@app.route("/callback", methods=["POST"])
def callback():
    data = request.get_json(force=True, silent=True)

    if not data:
        return "ok"

    if VK_SECRET_KEY and not VK_SECRET_KEY.startswith("сюда_потом"):
        incoming_secret = data.get("secret")
        if incoming_secret != VK_SECRET_KEY:
            return "ok"

    event_type = data.get("type")

    if event_type == "confirmation":
        return "c9c78fbe"

    if event_type == "message_new":
        message = data.get("object", {}).get("message", {})
        user_id = message.get("from_id")
        user_text = message.get("text", "")

        if user_id and user_text:
            answer = build_answer(user_id, user_text)
            vk_send_message(user_id, answer)

        return "ok"

    return "ok"


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)