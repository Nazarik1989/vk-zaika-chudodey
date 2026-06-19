import os
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


TAROT_CARDS = [
    "Шут", "Маг", "Жрица", "Императрица", "Император",
    "Иерофант", "Влюблённые", "Колесница", "Сила", "Отшельник",
    "Колесо Фортуны", "Справедливость", "Повешенный", "Смерть",
    "Умеренность", "Дьявол", "Башня", "Звезда", "Луна", "Солнце",
    "Суд", "Мир"
]

AFFIRMATIONS = [
    "Я справляюсь шаг за шагом.",
    "Я выбираю бережность к себе.",
    "Сегодня я могу сделать маленький, но важный шаг.",
    "Я достоин спокойствия, любви и поддержки.",
    "Я доверяю себе и своему пути."
]

MOTIVATIONS = [
    "Зай, не надо сразу всю гору двигать. Сдвинь один камушек — и уже победа.",
    "Сегодня достаточно одного честного шага. Не идеального, а живого.",
    "Ты не обязан быть железным. Главное — не бросай себя.",
    "Маленькое действие сегодня сильнее, чем большой план когда-нибудь.",
    "Выдохни. Соберись мягко. И сделай самое простое первое действие."
]


def vk_send_message(user_id: int, text: str):
    """Отправка сообщения пользователю VK."""
    if not VK_GROUP_TOKEN or VK_GROUP_TOKEN.startswith("сюда_потом"):
        print("VK_GROUP_TOKEN не настроен. Ответ не отправлен.")
        return

    url = "https://api.vk.com/method/messages.send"
    payload = {
        "user_id": user_id,
        "message": text,
        "random_id": random.randint(1, 2_000_000_000),
        "access_token": VK_GROUP_TOKEN,
        "v": VK_API_VERSION,
    }

    response = requests.post(url, data=payload, timeout=10)
    print("VK response:", response.text)


def tarot_answer():
    card = random.choice(TAROT_CARDS)
    return (
        f"🃏 Карта для тебя: {card}\n\n"
        f"Зай, это не приговор и не жёсткое предсказание. "
        f"Карта скорее подсказывает: прислушайся к себе, не торопись "
        f"и выбери самый бережный шаг."
    )


def affirmation_answer():
    return "✨ Аффирмация для тебя:\n\n" + random.choice(AFFIRMATIONS)


def motivation_answer():
    return "🌿 Мягкий пинок от Зайки:\n\n" + random.choice(MOTIVATIONS)


def safe_fallback():
    return (
        "Зай, я рядом 🌿\n"
        "Сейчас не получилось красиво ответить, но ты можешь написать ещё раз — "
        "я попробую помочь мягче и точнее."
    )


def openrouter_answer(user_text: str):
    """Ответ через OpenRouter. Если ключа нет или ошибка — fallback."""
    if not OPENROUTER_API_KEY or OPENROUTER_API_KEY.startswith("сюда_потом"):
        return (
            "Зай, я пока работаю в тестовом режиме 🌿\n"
            "Могу уже дать Таро, аффирмацию или мотивацию. "
            "Напиши: таро, аффирмация или мотивация."
        )

    system_prompt = f"""
Ты {BOT_NAME}, мягкий VK-бот поддержки.

Стиль:
- коротко;
- живо;
- тепло;
- без длинных простыней;
- не повторяй постоянно одно и то же обращение;
- не пугай пользователя;
- не давай фатальных предсказаний;
- в Таро говори, что это символическая подсказка, а не приговор;
- не ставь диагнозы;
- не отменяй врачей, психологов и специалистов;
- при опасных темах мягко советуй обратиться за срочной помощью к близким или специалистам.

Ты умеешь:
- поддержать;
- дать мотивацию;
- объяснить мягко;
- помочь человеку сделать маленький следующий шаг.
"""

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
                    {"role": "user", "content": user_text},
                ],
                "temperature": 0.8,
                "max_tokens": 300,
            },
            timeout=25,
        )

        data = response.json()
        return data["choices"][0]["message"]["content"].strip()

    except Exception as error:
        print("OpenRouter error:", error)
        return safe_fallback()


def build_answer(user_text: str):
    text = user_text.lower().strip()

    if any(word in text for word in ["таро", "карта", "расклад"]):
        return tarot_answer()

    if any(word in text for word in ["аффирмация", "афирмация", "аффирмацию"]):
        return affirmation_answer()

    if any(word in text for word in ["мотивация", "поддержи", "пинок", "сил нет"]):
        return motivation_answer()

    return openrouter_answer(user_text)


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
        return VK_CONFIRMATION_TOKEN or "confirmation_token_not_set"

    if event_type == "message_new":
        message = data.get("object", {}).get("message", {})
        user_id = message.get("from_id")
        user_text = message.get("text", "")

        if user_id and user_text:
            answer = build_answer(user_text)
            vk_send_message(user_id, answer)

        return "ok"

    return "ok"


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)