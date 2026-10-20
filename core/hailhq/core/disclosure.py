"""The spoken AI line.

Spoken by the voicebot as the first thing on every call unless the agent or
the request opted out. The text is a template with one placeholder,
``{org}``, the organization's display name. Precedence, resolved by the
caller: the agent's line, else the workspace line
(``organization_call_settings.ai_disclosure_line``), else the built-in line
for the call's direction. Templates come from agents (``/agents``) and the
workspace call settings, never from the ``POST /calls`` body.

An inbound agent with a first message and no custom line speaks the short
:data:`AFTER_GREETING_LINE` inside its own greeting instead (see
:func:`after_greeting_text`), so the caller hears one greeting, not two.

The built-in lines are spoken in the call's language (:data:`BUILT_IN_LINES`).
A custom line is spoken as written.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

Direction = Literal["outbound", "inbound"]

DEFAULT_OUTBOUND_LINE = "Hi, this is an AI assistant calling on behalf of {org}."
DEFAULT_INBOUND_LINE = "Hi, this is an AI assistant answering on behalf of {org}."
AFTER_GREETING_LINE = "I'm an AI assistant for {org}."

# The built-in lines when the organization name did not resolve. "On behalf
# of this number" told the caller nothing.
_UNNAMED_DEFAULT_LINE = "Hi, this is an AI assistant."
_UNNAMED_AFTER_GREETING_LINE = "I'm an AI assistant."

# The voicebot's TTS filter (``SpeechSanitizingAgent``) drops any turn that
# opens with tool-call syntax. A line that opens with one of these would be
# dropped whole and the disclosure never spoken.
_UNSPEAKABLE_OPENERS = ("{", "[", "`")

# What ``{org}`` becomes in a custom line when the organization name did not
# resolve.
_UNNAMED = {
    "outbound": "whoever requested this call",
    "inbound": "this number",
}


class Lines(NamedTuple):
    """The built-in lines for one language."""

    outbound: str
    inbound: str
    after_greeting: str
    unnamed: str
    unnamed_after_greeting: str


# Keyed by ``hailhq.core.languages.SUPPORTED_LANGUAGES``. Where the verb
# would mark the speaker's gender, the line uses a form that does not.
BUILT_IN_LINES: dict[str, Lines] = {
    "en": Lines(
        DEFAULT_OUTBOUND_LINE,
        DEFAULT_INBOUND_LINE,
        AFTER_GREETING_LINE,
        _UNNAMED_DEFAULT_LINE,
        _UNNAMED_AFTER_GREETING_LINE,
    ),
    "ar": Lines(
        "مرحبًا، أنا مساعد ذكاء اصطناعي أتصل نيابةً عن {org}.",
        "مرحبًا، أنا مساعد ذكاء اصطناعي أرد نيابةً عن {org}.",
        "أنا مساعد ذكاء اصطناعي لدى {org}.",
        "مرحبًا، أنا مساعد ذكاء اصطناعي.",
        "أنا مساعد ذكاء اصطناعي.",
    ),
    "bg": Lines(
        "Здравейте, аз съм асистент с изкуствен интелект и се обаждам от името на {org}.",
        "Здравейте, аз съм асистент с изкуствен интелект и отговарям от името на {org}.",
        "Аз съм асистент с изкуствен интелект на {org}.",
        "Здравейте, аз съм асистент с изкуствен интелект.",
        "Аз съм асистент с изкуствен интелект.",
    ),
    "bn": Lines(
        "হ্যালো, আমি একজন এআই সহকারী, {org}-এর পক্ষ থেকে ফোন করছি।",
        "হ্যালো, আমি একজন এআই সহকারী, {org}-এর পক্ষ থেকে উত্তর দিচ্ছি।",
        "আমি {org}-এর একজন এআই সহকারী।",
        "হ্যালো, আমি একজন এআই সহকারী।",
        "আমি একজন এআই সহকারী।",
    ),
    "cs": Lines(
        "Dobrý den, tady AI asistent, volám jménem {org}.",
        "Dobrý den, tady AI asistent, odpovídám jménem {org}.",
        "Jsem AI asistent pro {org}.",
        "Dobrý den, tady AI asistent.",
        "Jsem AI asistent.",
    ),
    "da": Lines(
        "Hej, det er en AI-assistent, der ringer på vegne af {org}.",
        "Hej, det er en AI-assistent, der svarer på vegne af {org}.",
        "Jeg er en AI-assistent for {org}.",
        "Hej, det er en AI-assistent.",
        "Jeg er en AI-assistent.",
    ),
    "de": Lines(
        "Hallo, hier ist ein KI-Assistent, der im Auftrag von {org} anruft.",
        "Hallo, hier ist ein KI-Assistent, der im Auftrag von {org} antwortet.",
        "Ich bin ein KI-Assistent von {org}.",
        "Hallo, hier ist ein KI-Assistent.",
        "Ich bin ein KI-Assistent.",
    ),
    "el": Lines(
        "Γεια σας, είμαι βοηθός τεχνητής νοημοσύνης και καλώ εκ μέρους {org}.",
        "Γεια σας, είμαι βοηθός τεχνητής νοημοσύνης και απαντώ εκ μέρους {org}.",
        "Είμαι βοηθός τεχνητής νοημοσύνης για {org}.",
        "Γεια σας, είμαι βοηθός τεχνητής νοημοσύνης.",
        "Είμαι βοηθός τεχνητής νοημοσύνης.",
    ),
    "es": Lines(
        "Hola, soy un asistente de IA que llama en nombre de {org}.",
        "Hola, soy un asistente de IA que atiende en nombre de {org}.",
        "Soy un asistente de IA de {org}.",
        "Hola, soy un asistente de IA.",
        "Soy un asistente de IA.",
    ),
    "fi": Lines(
        "Hei, olen tekoälyavustaja ja soitan {org}:n puolesta.",
        "Hei, olen tekoälyavustaja ja vastaan {org}:n puolesta.",
        "Olen {org}:n tekoälyavustaja.",
        "Hei, olen tekoälyavustaja.",
        "Olen tekoälyavustaja.",
    ),
    "fr": Lines(
        "Bonjour, je suis un assistant IA et j'appelle au nom de {org}.",
        "Bonjour, je suis un assistant IA et je réponds au nom de {org}.",
        "Je suis un assistant IA pour {org}.",
        "Bonjour, je suis un assistant IA.",
        "Je suis un assistant IA.",
    ),
    "gu": Lines(
        "નમસ્તે, હું {org} વતી એક AI સહાયક છું.",
        "નમસ્તે, હું {org} વતી એક AI સહાયક છું.",
        "હું {org} માટે એક AI સહાયક છું.",
        "નમસ્તે, હું એક AI સહાયક છું.",
        "હું એક AI સહાયક છું.",
    ),
    "he": Lines(
        "שלום, כאן עוזר בינה מלאכותית מטעם {org}.",
        "שלום, כאן עוזר בינה מלאכותית מטעם {org}.",
        "אני עוזר בינה מלאכותית של {org}.",
        "שלום, כאן עוזר בינה מלאכותית.",
        "אני עוזר בינה מלאכותית.",
    ),
    "hi": Lines(
        "नमस्ते, मैं {org} की ओर से एक AI सहायक हूँ।",
        "नमस्ते, मैं {org} की ओर से एक AI सहायक हूँ।",
        "मैं {org} का AI सहायक हूँ।",
        "नमस्ते, मैं एक AI सहायक हूँ।",
        "मैं एक AI सहायक हूँ।",
    ),
    "hr": Lines(
        "Dobar dan, ovdje AI asistent, zovem u ime {org}.",
        "Dobar dan, ovdje AI asistent, javljam se u ime {org}.",
        "Ja sam AI asistent za {org}.",
        "Dobar dan, ovdje AI asistent.",
        "Ja sam AI asistent.",
    ),
    "hu": Lines(
        "Jó napot, MI-asszisztens vagyok, {org} nevében telefonálok.",
        "Jó napot, MI-asszisztens vagyok, {org} nevében válaszolok.",
        "{org} MI-asszisztense vagyok.",
        "Jó napot, MI-asszisztens vagyok.",
        "MI-asszisztens vagyok.",
    ),
    "id": Lines(
        "Halo, saya asisten AI yang menelepon atas nama {org}.",
        "Halo, saya asisten AI yang menjawab atas nama {org}.",
        "Saya asisten AI dari {org}.",
        "Halo, saya asisten AI.",
        "Saya asisten AI.",
    ),
    "it": Lines(
        "Salve, sono un assistente IA e chiamo per conto di {org}.",
        "Salve, sono un assistente IA e rispondo per conto di {org}.",
        "Sono un assistente IA di {org}.",
        "Salve, sono un assistente IA.",
        "Sono un assistente IA.",
    ),
    "ja": Lines(
        "こんにちは、{org}の代理でお電話しているAIアシスタントです。",
        "こんにちは、{org}の代理で応対しているAIアシスタントです。",
        "{org}のAIアシスタントです。",
        "こんにちは、AIアシスタントです。",
        "AIアシスタントです。",
    ),
    "kn": Lines(
        "ನಮಸ್ಕಾರ, ನಾನು {org} ಪರವಾಗಿ ಕರೆ ಮಾಡುತ್ತಿರುವ AI ಸಹಾಯಕ.",
        "ನಮಸ್ಕಾರ, ನಾನು {org} ಪರವಾಗಿ ಉತ್ತರಿಸುತ್ತಿರುವ AI ಸಹಾಯಕ.",
        "ನಾನು {org} ನ AI ಸಹಾಯಕ.",
        "ನಮಸ್ಕಾರ, ನಾನು ಒಂದು AI ಸಹಾಯಕ.",
        "ನಾನು ಒಂದು AI ಸಹಾಯಕ.",
    ),
    "ko": Lines(
        "안녕하세요, {org}에서 전화드리는 AI 어시스턴트입니다.",
        "안녕하세요, {org}에서 응대하는 AI 어시스턴트입니다.",
        "저는 {org}의 AI 어시스턴트입니다.",
        "안녕하세요, AI 어시스턴트입니다.",
        "저는 AI 어시스턴트입니다.",
    ),
    "mr": Lines(
        "नमस्कार, मी {org} चा AI सहाय्यक आहे.",
        "नमस्कार, मी {org} चा AI सहाय्यक आहे.",
        "मी {org} चा AI सहाय्यक आहे.",
        "नमस्कार, मी एक AI सहाय्यक आहे.",
        "मी एक AI सहाय्यक आहे.",
    ),
    "ms": Lines(
        "Helo, saya pembantu AI yang menelefon bagi pihak {org}.",
        "Helo, saya pembantu AI yang menjawab bagi pihak {org}.",
        "Saya pembantu AI untuk {org}.",
        "Helo, saya pembantu AI.",
        "Saya pembantu AI.",
    ),
    "nl": Lines(
        "Hallo, u spreekt met een AI-assistent die belt namens {org}.",
        "Hallo, u spreekt met een AI-assistent die opneemt namens {org}.",
        "Ik ben een AI-assistent van {org}.",
        "Hallo, u spreekt met een AI-assistent.",
        "Ik ben een AI-assistent.",
    ),
    "no": Lines(
        "Hei, dette er en KI-assistent som ringer på vegne av {org}.",
        "Hei, dette er en KI-assistent som svarer på vegne av {org}.",
        "Jeg er en KI-assistent for {org}.",
        "Hei, dette er en KI-assistent.",
        "Jeg er en KI-assistent.",
    ),
    "pl": Lines(
        "Dzień dobry, tu asystent AI, dzwonię w imieniu {org}.",
        "Dzień dobry, tu asystent AI, odbieram w imieniu {org}.",
        "Jestem asystentem AI dla {org}.",
        "Dzień dobry, tu asystent AI.",
        "Jestem asystentem AI.",
    ),
    "pt": Lines(
        "Olá, sou um assistente de IA ligando em nome de {org}.",
        "Olá, sou um assistente de IA atendendo em nome de {org}.",
        "Sou um assistente de IA de {org}.",
        "Olá, sou um assistente de IA.",
        "Sou um assistente de IA.",
    ),
    "ro": Lines(
        "Bună ziua, sunt un asistent AI și sun în numele {org}.",
        "Bună ziua, sunt un asistent AI și răspund în numele {org}.",
        "Sunt un asistent AI pentru {org}.",
        "Bună ziua, sunt un asistent AI.",
        "Sunt un asistent AI.",
    ),
    "ru": Lines(
        "Здравствуйте, это ИИ-ассистент, звоню от имени {org}.",
        "Здравствуйте, это ИИ-ассистент, отвечаю от имени {org}.",
        "Я ИИ-ассистент от {org}.",
        "Здравствуйте, это ИИ-ассистент.",
        "Я ИИ-ассистент.",
    ),
    "sk": Lines(
        "Dobrý deň, tu AI asistent, volám v mene {org}.",
        "Dobrý deň, tu AI asistent, odpovedám v mene {org}.",
        "Som AI asistent pre {org}.",
        "Dobrý deň, tu AI asistent.",
        "Som AI asistent.",
    ),
    "sv": Lines(
        "Hej, det här är en AI-assistent som ringer på uppdrag av {org}.",
        "Hej, det här är en AI-assistent som svarar på uppdrag av {org}.",
        "Jag är en AI-assistent för {org}.",
        "Hej, det här är en AI-assistent.",
        "Jag är en AI-assistent.",
    ),
    "ta": Lines(
        "வணக்கம், நான் {org} சார்பாக அழைக்கும் AI உதவியாளர்.",
        "வணக்கம், நான் {org} சார்பாக பதிலளிக்கும் AI உதவியாளர்.",
        "நான் {org} இன் AI உதவியாளர்.",
        "வணக்கம், நான் ஒரு AI உதவியாளர்.",
        "நான் ஒரு AI உதவியாளர்.",
    ),
    "te": Lines(
        "నమస్కారం, నేను {org} తరఫున కాల్ చేస్తున్న AI అసిస్టెంట్‌ని.",
        "నమస్కారం, నేను {org} తరఫున సమాధానం ఇస్తున్న AI అసిస్టెంట్‌ని.",
        "నేను {org} యొక్క AI అసిస్టెంట్‌ని.",
        "నమస్కారం, నేను ఒక AI అసిస్టెంట్‌ని.",
        "నేను ఒక AI అసిస్టెంట్‌ని.",
    ),
    "th": Lines(
        "สวัสดี นี่คือผู้ช่วย AI ที่โทรมาในนามของ {org}",
        "สวัสดี นี่คือผู้ช่วย AI ที่รับสายในนามของ {org}",
        "นี่คือผู้ช่วย AI ของ {org}",
        "สวัสดี นี่คือผู้ช่วย AI",
        "นี่คือผู้ช่วย AI",
    ),
    "tl": Lines(
        "Hello, ako ay isang AI assistant na tumatawag para sa {org}.",
        "Hello, ako ay isang AI assistant na sumasagot para sa {org}.",
        "Ako ay isang AI assistant ng {org}.",
        "Hello, ako ay isang AI assistant.",
        "Ako ay isang AI assistant.",
    ),
    "tr": Lines(
        "Merhaba, {org} adına arayan bir yapay zekâ asistanıyım.",
        "Merhaba, {org} adına yanıt veren bir yapay zekâ asistanıyım.",
        "{org} için çalışan bir yapay zekâ asistanıyım.",
        "Merhaba, bir yapay zekâ asistanıyım.",
        "Bir yapay zekâ asistanıyım.",
    ),
    "uk": Lines(
        "Добрий день, це ШІ-асистент, телефоную від імені {org}.",
        "Добрий день, це ШІ-асистент, відповідаю від імені {org}.",
        "Я ШІ-асистент від {org}.",
        "Добрий день, це ШІ-асистент.",
        "Я ШІ-асистент.",
    ),
    "vi": Lines(
        "Xin chào, tôi là trợ lý AI gọi thay mặt cho {org}.",
        "Xin chào, tôi là trợ lý AI trả lời thay mặt cho {org}.",
        "Tôi là trợ lý AI của {org}.",
        "Xin chào, tôi là trợ lý AI.",
        "Tôi là trợ lý AI.",
    ),
    "zh": Lines(
        "您好，我是代表{org}致电的AI助手。",
        "您好，我是代表{org}接听电话的AI助手。",
        "我是{org}的AI助手。",
        "您好，我是AI助手。",
        "我是AI助手。",
    ),
}

__all__ = [
    "AFTER_GREETING_LINE",
    "BUILT_IN_LINES",
    "DEFAULT_INBOUND_LINE",
    "DEFAULT_OUTBOUND_LINE",
    "Direction",
    "Lines",
    "after_greeting_text",
    "disclosure_text",
]


def _lines(language: str | None) -> Lines:
    """The built-in lines in ``language``, English when it is unknown."""
    if not isinstance(language, str):
        return BUILT_IN_LINES["en"]
    return BUILT_IN_LINES.get(language, BUILT_IN_LINES["en"])


def disclosure_text(
    direction: Direction,
    org_name: str | None,
    template: str | None = None,
    language: str | None = None,
) -> str:
    """The exact line to speak. A blank template means "use the default", and
    so does one whose spoken text would open with tool-call syntax (``[``,
    ``{`` or a backtick): the voicebot would drop it, and the call would
    open with no AI line at all. Only the default follows ``language``."""
    lines = _lines(language)
    name = (org_name or "").strip()
    if name:
        default = lines.inbound if direction == "inbound" else lines.outbound
        default = default.replace("{org}", name)
    else:
        default = lines.unnamed
    line = (template or "").strip()
    if not line:
        return default
    spoken = line.replace("{org}", name or _UNNAMED[direction])
    if spoken.lstrip().startswith(_UNSPEAKABLE_OPENERS):
        return default
    return spoken


def after_greeting_text(org_name: str | None, language: str | None = None) -> str:
    """The short AI line spoken inside an inbound agent's own greeting."""
    lines = _lines(language)
    name = (org_name or "").strip()
    if not name:
        return lines.unnamed_after_greeting
    return lines.after_greeting.replace("{org}", name)
