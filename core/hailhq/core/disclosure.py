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
A custom line is spoken as written; only a ``{org}`` with no organization name
takes the call's language.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from hailhq.core.languages import by_language

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


class Lines(NamedTuple):
    """The built-in lines for one language."""

    outbound: str
    inbound: str
    after_greeting: str
    unnamed: str
    unnamed_after_greeting: str
    # What ``{org}`` becomes in a custom line when the organization name did
    # not resolve.
    filler_outbound: str
    filler_inbound: str


# Keyed by ``hailhq.core.languages.SUPPORTED_LANGUAGES``. Where the verb
# would mark the speaker's gender, the line uses a form that does not. Some
# languages' word for "assistant" stays masculine. No line opens with
# ``{org}``: see :data:`_UNSPEAKABLE_OPENERS`.
BUILT_IN_LINES: dict[str, Lines] = {
    "en": Lines(
        DEFAULT_OUTBOUND_LINE,
        DEFAULT_INBOUND_LINE,
        AFTER_GREETING_LINE,
        _UNNAMED_DEFAULT_LINE,
        _UNNAMED_AFTER_GREETING_LINE,
        "whoever requested this call",
        "this number",
    ),
    "ar": Lines(
        "مرحبًا، أنا مساعد ذكاء اصطناعي أتصل نيابةً عن {org}.",
        "مرحبًا، أنا مساعد ذكاء اصطناعي أرد نيابةً عن {org}.",
        "أنا مساعد ذكاء اصطناعي لدى {org}.",
        "مرحبًا، أنا مساعد ذكاء اصطناعي.",
        "أنا مساعد ذكاء اصطناعي.",
        "من طلب هذه المكالمة",
        "هذا الرقم",
    ),
    "bg": Lines(
        "Здравейте, аз съм асистент с изкуствен интелект и се обаждам от името на {org}.",
        "Здравейте, аз съм асистент с изкуствен интелект и отговарям от името на {org}.",
        "Аз съм асистент с изкуствен интелект на {org}.",
        "Здравейте, аз съм асистент с изкуствен интелект.",
        "Аз съм асистент с изкуствен интелект.",
        "този, който поиска това обаждане",
        "този номер",
    ),
    "bn": Lines(
        "হ্যালো, আমি একজন এআই সহকারী, {org}-এর পক্ষ থেকে ফোন করছি।",
        "হ্যালো, আমি একজন এআই সহকারী, {org}-এর পক্ষ থেকে উত্তর দিচ্ছি।",
        "আমি {org}-এর একজন এআই সহকারী।",
        "হ্যালো, আমি একজন এআই সহকারী।",
        "আমি একজন এআই সহকারী।",
        "যিনি এই কলের অনুরোধ করেছেন",
        "এই নম্বর",
    ),
    "cs": Lines(
        "Dobrý den, tady AI asistent, volám jménem {org}.",
        "Dobrý den, tady AI asistent, odpovídám jménem {org}.",
        "Jsem AI asistent pro {org}.",
        "Dobrý den, tady AI asistent.",
        "Jsem AI asistent.",
        "ten, kdo o tento hovor požádal",
        "toto číslo",
    ),
    "da": Lines(
        "Hej, det er en AI-assistent, der ringer på vegne af {org}.",
        "Hej, det er en AI-assistent, der svarer på vegne af {org}.",
        "Jeg er en AI-assistent for {org}.",
        "Hej, det er en AI-assistent.",
        "Jeg er en AI-assistent.",
        "den, der har bestilt dette opkald",
        "dette nummer",
    ),
    "de": Lines(
        "Hallo, hier ist ein KI-Assistent, der im Auftrag von {org} anruft.",
        "Hallo, hier ist ein KI-Assistent, der im Auftrag von {org} antwortet.",
        "Ich bin ein KI-Assistent von {org}.",
        "Hallo, hier ist ein KI-Assistent.",
        "Ich bin ein KI-Assistent.",
        "die Person, die diesen Anruf veranlasst hat",
        "diese Nummer",
    ),
    "el": Lines(
        "Γεια σας, είμαι βοηθός τεχνητής νοημοσύνης και καλώ εκ μέρους {org}.",
        "Γεια σας, είμαι βοηθός τεχνητής νοημοσύνης και απαντώ εκ μέρους {org}.",
        "Είμαι βοηθός τεχνητής νοημοσύνης για {org}.",
        "Γεια σας, είμαι βοηθός τεχνητής νοημοσύνης.",
        "Είμαι βοηθός τεχνητής νοημοσύνης.",
        "όποιον ζήτησε αυτή την κλήση",
        "αυτόν τον αριθμό",
    ),
    "es": Lines(
        "Hola, soy una inteligencia artificial y llamo en nombre de {org}.",
        "Hola, soy una inteligencia artificial y atiendo en nombre de {org}.",
        "Soy una inteligencia artificial al servicio de {org}.",
        "Hola, soy una inteligencia artificial.",
        "Soy una inteligencia artificial.",
        "quien solicitó esta llamada",
        "este número",
    ),
    "fi": Lines(
        "Hei, olen tekoälyavustaja ja soitan yrityksen {org} puolesta.",
        "Hei, olen tekoälyavustaja ja vastaan yrityksen {org} puolesta.",
        "Olen yrityksen {org} tekoälyavustaja.",
        "Hei, olen tekoälyavustaja.",
        "Olen tekoälyavustaja.",
        "tämän puhelun pyytäjä",
        "tämä numero",
    ),
    "fr": Lines(
        "Bonjour, je suis une intelligence artificielle et j'appelle au nom de {org}.",
        "Bonjour, je suis une intelligence artificielle et je réponds au nom de {org}.",
        "Je suis une intelligence artificielle au service de {org}.",
        "Bonjour, je suis une intelligence artificielle.",
        "Je suis une intelligence artificielle.",
        "la personne qui a demandé cet appel",
        "ce numéro",
    ),
    "gu": Lines(
        "નમસ્તે, હું {org} વતી એક AI સહાયક છું.",
        "નમસ્તે, હું {org} વતી એક AI સહાયક છું.",
        "હું {org} માટે એક AI સહાયક છું.",
        "નમસ્તે, હું એક AI સહાયક છું.",
        "હું એક AI સહાયક છું.",
        "જેમણે આ કૉલની વિનંતી કરી છે",
        "આ નંબર",
    ),
    "he": Lines(
        "שלום, כאן בינה מלאכותית מטעם {org}.",
        "שלום, כאן בינה מלאכותית מטעם {org}.",
        "אני בינה מלאכותית של {org}.",
        "שלום, כאן בינה מלאכותית.",
        "אני בינה מלאכותית.",
        "מי שביקש את השיחה הזו",
        "המספר הזה",
    ),
    "hi": Lines(
        "नमस्ते, मैं {org} की ओर से एक AI सहायक हूँ।",
        "नमस्ते, मैं {org} की ओर से एक AI सहायक हूँ।",
        "मैं {org} का AI सहायक हूँ।",
        "नमस्ते, मैं एक AI सहायक हूँ।",
        "मैं एक AI सहायक हूँ।",
        "जिन्होंने यह कॉल करने का अनुरोध किया",
        "यह नंबर",
    ),
    "hr": Lines(
        "Dobar dan, ovdje AI asistent, zovem u ime {org}.",
        "Dobar dan, ovdje AI asistent, javljam se u ime {org}.",
        "Ja sam AI asistent za {org}.",
        "Dobar dan, ovdje AI asistent.",
        "Ja sam AI asistent.",
        "onaj tko je zatražio ovaj poziv",
        "ovaj broj",
    ),
    "hu": Lines(
        "Jó napot, mesterséges intelligencia vagyok, {org} nevében telefonálok.",
        "Jó napot, mesterséges intelligencia vagyok, {org} nevében válaszolok.",
        "Mesterséges intelligencia vagyok, {org} nevében.",
        "Jó napot, mesterséges intelligencia vagyok.",
        "Mesterséges intelligencia vagyok.",
        "aki ezt a hívást kérte",
        "ez a szám",
    ),
    "id": Lines(
        "Halo, saya asisten AI yang menelepon atas nama {org}.",
        "Halo, saya asisten AI yang menjawab atas nama {org}.",
        "Saya asisten AI dari {org}.",
        "Halo, saya asisten AI.",
        "Saya asisten AI.",
        "pihak yang meminta panggilan ini",
        "nomor ini",
    ),
    "it": Lines(
        "Salve, sono un'intelligenza artificiale e chiamo per conto di {org}.",
        "Salve, sono un'intelligenza artificiale e rispondo per conto di {org}.",
        "Sono un'intelligenza artificiale al servizio di {org}.",
        "Salve, sono un'intelligenza artificiale.",
        "Sono un'intelligenza artificiale.",
        "chi ha richiesto questa chiamata",
        "questo numero",
    ),
    "ja": Lines(
        "こんにちは、{org}の代理でお電話しているAIアシスタントです。",
        "こんにちは、{org}の代理で応対しているAIアシスタントです。",
        "私は{org}のAIアシスタントです。",
        "こんにちは、AIアシスタントです。",
        "AIアシスタントです。",
        "このお電話を依頼した方",
        "この番号",
    ),
    "kn": Lines(
        "ನಮಸ್ಕಾರ, ನಾನು {org} ಪರವಾಗಿ ಕರೆ ಮಾಡುತ್ತಿರುವ AI ಸಹಾಯಕ.",
        "ನಮಸ್ಕಾರ, ನಾನು {org} ಪರವಾಗಿ ಉತ್ತರಿಸುತ್ತಿರುವ AI ಸಹಾಯಕ.",
        "ನಾನು {org} ನ AI ಸಹಾಯಕ.",
        "ನಮಸ್ಕಾರ, ನಾನು ಒಂದು AI ಸಹಾಯಕ.",
        "ನಾನು ಒಂದು AI ಸಹಾಯಕ.",
        "ಈ ಕರೆಯನ್ನು ಕೋರಿದವರು",
        "ಈ ಸಂಖ್ಯೆ",
    ),
    "ko": Lines(
        "안녕하세요, {org}에서 전화드리는 AI 어시스턴트입니다.",
        "안녕하세요, {org}에서 응대하는 AI 어시스턴트입니다.",
        "저는 {org}의 AI 어시스턴트입니다.",
        "안녕하세요, AI 어시스턴트입니다.",
        "저는 AI 어시스턴트입니다.",
        "이 전화를 요청하신 분",
        "이 번호",
    ),
    "mr": Lines(
        "नमस्कार, मी {org} चा AI सहाय्यक आहे.",
        "नमस्कार, मी {org} चा AI सहाय्यक आहे.",
        "मी {org} चा AI सहाय्यक आहे.",
        "नमस्कार, मी एक AI सहाय्यक आहे.",
        "मी एक AI सहाय्यक आहे.",
        "ज्यांनी हा कॉल करण्याची विनंती केली",
        "हा नंबर",
    ),
    "ms": Lines(
        "Helo, saya pembantu AI yang menelefon bagi pihak {org}.",
        "Helo, saya pembantu AI yang menjawab bagi pihak {org}.",
        "Saya pembantu AI untuk {org}.",
        "Helo, saya pembantu AI.",
        "Saya pembantu AI.",
        "pihak yang meminta panggilan ini",
        "nombor ini",
    ),
    "nl": Lines(
        "Hallo, u spreekt met een AI-assistent die belt namens {org}.",
        "Hallo, u spreekt met een AI-assistent die opneemt namens {org}.",
        "Ik ben een AI-assistent van {org}.",
        "Hallo, u spreekt met een AI-assistent.",
        "Ik ben een AI-assistent.",
        "degene die dit gesprek heeft aangevraagd",
        "dit nummer",
    ),
    "no": Lines(
        "Hei, dette er en KI-assistent som ringer på vegne av {org}.",
        "Hei, dette er en KI-assistent som svarer på vegne av {org}.",
        "Jeg er en KI-assistent for {org}.",
        "Hei, dette er en KI-assistent.",
        "Jeg er en KI-assistent.",
        "den som ba om denne samtalen",
        "dette nummeret",
    ),
    "pl": Lines(
        "Dzień dobry, tu sztuczna inteligencja, dzwonię w imieniu {org}.",
        "Dzień dobry, tu sztuczna inteligencja, odbieram w imieniu {org}.",
        "Jestem sztuczną inteligencją działającą dla {org}.",
        "Dzień dobry, tu sztuczna inteligencja.",
        "Jestem sztuczną inteligencją.",
        "osoba, która poprosiła o to połączenie",
        "ten numer",
    ),
    "pt": Lines(
        "Olá, sou uma inteligência artificial e estou ligando em nome de {org}.",
        "Olá, sou uma inteligência artificial e estou atendendo em nome de {org}.",
        "Sou uma inteligência artificial a serviço de {org}.",
        "Olá, sou uma inteligência artificial.",
        "Sou uma inteligência artificial.",
        "quem solicitou esta chamada",
        "este número",
    ),
    "ro": Lines(
        "Bună ziua, sunt un asistent AI și sun în numele {org}.",
        "Bună ziua, sunt un asistent AI și răspund în numele {org}.",
        "Sunt un asistent AI pentru {org}.",
        "Bună ziua, sunt un asistent AI.",
        "Sunt un asistent AI.",
        "persoana care a solicitat acest apel",
        "acest număr",
    ),
    "ru": Lines(
        "Здравствуйте, это искусственный интеллект, звоню от имени {org}.",
        "Здравствуйте, это искусственный интеллект, отвечаю от имени {org}.",
        "Я искусственный интеллект компании {org}.",
        "Здравствуйте, это искусственный интеллект.",
        "Я искусственный интеллект.",
        "тот, кто запросил этот звонок",
        "этот номер",
    ),
    "sk": Lines(
        "Dobrý deň, tu AI asistent, volám v mene {org}.",
        "Dobrý deň, tu AI asistent, odpovedám v mene {org}.",
        "Som AI asistent pre {org}.",
        "Dobrý deň, tu AI asistent.",
        "Som AI asistent.",
        "ten, kto o tento hovor požiadal",
        "toto číslo",
    ),
    "sv": Lines(
        "Hej, det här är en AI-assistent som ringer på uppdrag av {org}.",
        "Hej, det här är en AI-assistent som svarar på uppdrag av {org}.",
        "Jag är en AI-assistent för {org}.",
        "Hej, det här är en AI-assistent.",
        "Jag är en AI-assistent.",
        "den som begärde det här samtalet",
        "det här numret",
    ),
    "ta": Lines(
        "வணக்கம், நான் {org} சார்பாக அழைக்கும் AI உதவியாளர்.",
        "வணக்கம், நான் {org} சார்பாக பதிலளிக்கும் AI உதவியாளர்.",
        "நான் {org} இன் AI உதவியாளர்.",
        "வணக்கம், நான் ஒரு AI உதவியாளர்.",
        "நான் ஒரு AI உதவியாளர்.",
        "இந்த அழைப்பைக் கோரியவர்",
        "இந்த எண்",
    ),
    "te": Lines(
        "నమస్కారం, నేను {org} తరఫున కాల్ చేస్తున్న AI అసిస్టెంట్‌ని.",
        "నమస్కారం, నేను {org} తరఫున సమాధానం ఇస్తున్న AI అసిస్టెంట్‌ని.",
        "నేను {org} యొక్క AI అసిస్టెంట్‌ని.",
        "నమస్కారం, నేను ఒక AI అసిస్టెంట్‌ని.",
        "నేను ఒక AI అసిస్టెంట్‌ని.",
        "ఈ కాల్‌ను కోరిన వారు",
        "ఈ నంబర్",
    ),
    "th": Lines(
        "สวัสดี นี่คือผู้ช่วย AI ที่โทรมาในนามของ {org}",
        "สวัสดี นี่คือผู้ช่วย AI ที่รับสายในนามของ {org}",
        "นี่คือผู้ช่วย AI ของ {org}",
        "สวัสดี นี่คือผู้ช่วย AI",
        "นี่คือผู้ช่วย AI",
        "ผู้ที่ขอให้โทรสายนี้",
        "หมายเลขนี้",
    ),
    "tl": Lines(
        "Hello, ako ay isang AI assistant na tumatawag para sa {org}.",
        "Hello, ako ay isang AI assistant na sumasagot para sa {org}.",
        "Ako ay isang AI assistant ng {org}.",
        "Hello, ako ay isang AI assistant.",
        "Ako ay isang AI assistant.",
        "ang humiling ng tawag na ito",
        "ang numerong ito",
    ),
    "tr": Lines(
        "Merhaba, {org} adına arayan bir yapay zekâ asistanıyım.",
        "Merhaba, {org} adına yanıt veren bir yapay zekâ asistanıyım.",
        "Ben {org} için çalışan bir yapay zekâ asistanıyım.",
        "Merhaba, bir yapay zekâ asistanıyım.",
        "Bir yapay zekâ asistanıyım.",
        "bu aramayı talep eden kişi",
        "bu numara",
    ),
    "uk": Lines(
        "Добрий день, це штучний інтелект, телефоную від імені {org}.",
        "Добрий день, це штучний інтелект, відповідаю від імені {org}.",
        "Я штучний інтелект компанії {org}.",
        "Добрий день, це штучний інтелект.",
        "Я штучний інтелект.",
        "той, хто попросив про цей дзвінок",
        "цей номер",
    ),
    "vi": Lines(
        "Xin chào, tôi là trợ lý AI gọi thay mặt cho {org}.",
        "Xin chào, tôi là trợ lý AI trả lời thay mặt cho {org}.",
        "Tôi là trợ lý AI của {org}.",
        "Xin chào, tôi là trợ lý AI.",
        "Tôi là trợ lý AI.",
        "người đã yêu cầu cuộc gọi này",
        "số này",
    ),
    "zh": Lines(
        "您好，我是代表{org}致电的AI助手。",
        "您好，我是代表{org}接听电话的AI助手。",
        "我是{org}的AI助手。",
        "您好，我是AI助手。",
        "我是AI助手。",
        "请求此次通话的人",
        "此号码",
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
    lines = by_language(BUILT_IN_LINES, language)
    name = (org_name or "").strip()
    if name:
        default = lines.inbound if direction == "inbound" else lines.outbound
        default = default.replace("{org}", name)
    else:
        default = lines.unnamed
    line = (template or "").strip()
    if not line:
        return default
    filler = lines.filler_inbound if direction == "inbound" else lines.filler_outbound
    spoken = line.replace("{org}", name or filler)
    if spoken.lstrip().startswith(_UNSPEAKABLE_OPENERS):
        return default
    return spoken


def after_greeting_text(org_name: str | None, language: str | None = None) -> str:
    """The short AI line spoken inside an inbound agent's own greeting."""
    lines = by_language(BUILT_IN_LINES, language)
    name = (org_name or "").strip()
    if not name:
        return lines.unnamed_after_greeting
    return lines.after_greeting.replace("{org}", name)
