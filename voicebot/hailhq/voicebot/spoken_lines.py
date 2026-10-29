"""Fixed lines the voicebot speaks without the LLM, in the call's language.

The time-limit goodbye, the goodbye when a per-call LLM endpoint keeps
failing. Unknown or missing language: English. Where a verb would mark the speaker's gender, the line uses a form
that does not.
"""

from __future__ import annotations

from typing import NamedTuple

__all__ = ["SPOKEN_LINES", "SpokenLines", "spoken_lines"]


class SpokenLines(NamedTuple):
    soft_cap: str
    llm_failure: str


# Keyed by ``hailhq.core.languages.SUPPORTED_LANGUAGES``.
SPOKEN_LINES: dict[str, SpokenLines] = {
    "en": SpokenLines(
        "Sorry, we've reached the time limit for this call. Thanks for talking, I'll let you go now. Goodbye.",
        "Sorry, I'm having trouble reaching the service that powers this call, so I have to end it here. Goodbye.",
    ),
    "ar": SpokenLines(
        "عذرًا، لقد وصلنا إلى الحد الزمني لهذه المكالمة. شكرًا على وقتك، سأنهي المكالمة الآن. مع السلامة.",
        "عذرًا، أواجه مشكلة في الوصول إلى الخدمة التي تشغّل هذه المكالمة، لذا يجب أن أنهيها الآن. مع السلامة.",
    ),
    "bg": SpokenLines(
        "Съжалявам, достигнахме времевия лимит за този разговор. Благодаря за разговора, ще приключа сега. Довиждане.",
        "Съжалявам, имам проблем с връзката към услугата, която обслужва този разговор, затова трябва да го прекратя. Довиждане.",
    ),
    "bn": SpokenLines(
        "দুঃখিত, এই কলের সময়সীমা শেষ হয়ে গেছে। কথা বলার জন্য ধন্যবাদ, এখন কলটি শেষ করছি। বিদায়।",
        "দুঃখিত, এই কলটি যে পরিষেবা চালায় তার সাথে সংযোগ করতে সমস্যা হচ্ছে, তাই কলটি এখানেই শেষ করতে হচ্ছে। বিদায়।",
    ),
    "cs": SpokenLines(
        "Omlouvám se, dosáhli jsme časového limitu tohoto hovoru. Děkuji za rozhovor, teď hovor ukončím. Na shledanou.",
        "Omlouvám se, nedaří se mi spojit se službou, která tento hovor zajišťuje, takže ho musím ukončit. Na shledanou.",
    ),
    "da": SpokenLines(
        "Beklager, vi har nået tidsgrænsen for dette opkald. Tak for snakken, jeg lader dig gå nu. Farvel.",
        "Beklager, jeg har problemer med at nå den tjeneste, der driver dette opkald, så jeg må afslutte det her. Farvel.",
    ),
    "de": SpokenLines(
        "Entschuldigung, wir haben das Zeitlimit für diesen Anruf erreicht. Danke für das Gespräch, ich beende den Anruf jetzt. Auf Wiederhören.",
        "Entschuldigung, ich kann den Dienst, der diesen Anruf betreibt, gerade nicht erreichen und muss den Anruf hier beenden. Auf Wiederhören.",
    ),
    "el": SpokenLines(
        "Λυπάμαι, φτάσαμε στο χρονικό όριο αυτής της κλήσης. Ευχαριστώ για τη συζήτηση, θα κλείσω τώρα. Αντίο.",
        "Λυπάμαι, δυσκολεύομαι να συνδεθώ με την υπηρεσία που λειτουργεί αυτή την κλήση, οπότε πρέπει να την τερματίσω. Αντίο.",
    ),
    "es": SpokenLines(
        "Lo siento, hemos llegado al límite de tiempo de esta llamada. Gracias por hablar conmigo, ya me despido. Adiós.",
        "Lo siento, tengo problemas para conectar con el servicio que hace funcionar esta llamada, así que tengo que terminarla aquí. Adiós.",
    ),
    "fi": SpokenLines(
        "Pahoittelen, tämän puhelun aikaraja on täynnä. Kiitos keskustelusta, lopetan puhelun nyt. Näkemiin.",
        "Pahoittelen, en saa yhteyttä palveluun, joka hoitaa tämän puhelun, joten minun täytyy lopettaa se tähän. Näkemiin.",
    ),
    "fr": SpokenLines(
        "Toutes mes excuses, nous avons atteint la durée maximale de cet appel. Merci pour cet échange, je vais raccrocher. Au revoir.",
        "Toutes mes excuses, je n'arrive pas à joindre le service qui fait fonctionner cet appel, je dois donc y mettre fin. Au revoir.",
    ),
    "gu": SpokenLines(
        "માફ કરશો, આ કૉલની સમય મર્યાદા પૂરી થઈ ગઈ છે. વાત કરવા બદલ આભાર. હવે હું કૉલ પૂરો કરું છું. આવજો.",
        "માફ કરશો, આ કૉલ ચલાવતી સેવા સુધી પહોંચવામાં મુશ્કેલી છે, તેથી કૉલ અહીં પૂરો કરવો પડશે. આવજો.",
    ),
    "he": SpokenLines(
        "מצטערים, הגענו למגבלת הזמן של השיחה. תודה על השיחה, השיחה תסתיים עכשיו. להתראות.",
        "מצטערים, יש בעיה בחיבור לשירות שמפעיל את השיחה, ולכן השיחה תסתיים כאן. להתראות.",
    ),
    "hi": SpokenLines(
        "माफ़ कीजिए, इस कॉल की समय सीमा पूरी हो गई है। बात करने के लिए धन्यवाद। अब कॉल समाप्त की जा रही है। नमस्ते।",
        "माफ़ कीजिए, इस कॉल को चलाने वाली सेवा से संपर्क नहीं हो पा रहा है, इसलिए कॉल यहीं समाप्त करनी होगी। नमस्ते।",
    ),
    "hr": SpokenLines(
        "Žao mi je, dosegnuli smo vremensko ograničenje ovog poziva. Hvala na razgovoru, sada završavam poziv. Doviđenja.",
        "Žao mi je, ne mogu dobiti uslugu koja pokreće ovaj poziv, pa ga moram ovdje završiti. Doviđenja.",
    ),
    "hu": SpokenLines(
        "Elnézést, elértük a hívás időkorlátját. Köszönöm a beszélgetést, most bontom a hívást. Viszonthallásra.",
        "Elnézést, nem érem el a hívást működtető szolgáltatást, ezért itt be kell fejeznem a hívást. Viszonthallásra.",
    ),
    "id": SpokenLines(
        "Maaf, kita sudah mencapai batas waktu panggilan ini. Terima kasih sudah berbicara, saya akhiri panggilan sekarang. Sampai jumpa.",
        "Maaf, saya kesulitan menghubungi layanan yang menjalankan panggilan ini, jadi saya harus mengakhirinya di sini. Sampai jumpa.",
    ),
    "it": SpokenLines(
        "Mi scusi, abbiamo raggiunto il limite di tempo per questa chiamata. Grazie per la conversazione, ora chiudo. Arrivederci.",
        "Mi scusi, non riesco a raggiungere il servizio che gestisce questa chiamata, quindi devo chiuderla qui. Arrivederci.",
    ),
    "ja": SpokenLines(
        "申し訳ありません、この通話の制限時間に達しました。お話しいただきありがとうございました。これで失礼いたします。",
        "申し訳ありません、この通話を支えるサービスに接続できないため、ここで通話を終了いたします。失礼いたします。",
    ),
    "kn": SpokenLines(
        "ಕ್ಷಮಿಸಿ, ಈ ಕರೆಯ ಸಮಯ ಮಿತಿ ಮುಗಿದಿದೆ. ಮಾತನಾಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. ಈಗ ಕರೆಯನ್ನು ಮುಗಿಸುತ್ತಿದ್ದೇನೆ. ವಿದಾಯ.",
        "ಕ್ಷಮಿಸಿ, ಈ ಕರೆಯನ್ನು ನಡೆಸುವ ಸೇವೆಯನ್ನು ತಲುಪಲು ತೊಂದರೆಯಾಗುತ್ತಿದೆ, ಆದ್ದರಿಂದ ಕರೆಯನ್ನು ಇಲ್ಲಿಗೆ ಮುಗಿಸಬೇಕಾಗಿದೆ. ವಿದಾಯ.",
    ),
    "ko": SpokenLines(
        "죄송합니다, 이 통화의 시간 제한에 도달했습니다. 통화해 주셔서 감사합니다. 이만 끊겠습니다. 안녕히 계세요.",
        "죄송합니다, 이 통화를 지원하는 서비스에 연결할 수 없어 여기서 통화를 종료해야 합니다. 안녕히 계세요.",
    ),
    "mr": SpokenLines(
        "माफ करा, या कॉलची वेळ मर्यादा संपली आहे. बोलल्याबद्दल धन्यवाद. मी आता कॉल संपवत आहे. नमस्कार.",
        "माफ करा, हा कॉल चालवणाऱ्या सेवेशी संपर्क होत नाही, त्यामुळे कॉल इथेच संपवावा लागेल. नमस्कार.",
    ),
    "ms": SpokenLines(
        "Maaf, kita sudah sampai had masa untuk panggilan ini. Terima kasih kerana bercakap, saya tamatkan panggilan sekarang. Selamat tinggal.",
        "Maaf, saya menghadapi masalah menghubungi perkhidmatan yang menjalankan panggilan ini, jadi saya perlu menamatkannya di sini. Selamat tinggal.",
    ),
    "nl": SpokenLines(
        "Sorry, we hebben de tijdslimiet voor dit gesprek bereikt. Bedankt voor het gesprek, ik hang nu op. Tot ziens.",
        "Sorry, ik kan de dienst achter dit gesprek niet bereiken, dus ik moet het hier beëindigen. Tot ziens.",
    ),
    "no": SpokenLines(
        "Beklager, vi har nådd tidsgrensen for denne samtalen. Takk for praten, jeg avslutter nå. Ha det.",
        "Beklager, jeg får ikke kontakt med tjenesten som driver denne samtalen, så jeg må avslutte her. Ha det.",
    ),
    "pl": SpokenLines(
        "Przepraszam, osiągnęliśmy limit czasu tej rozmowy. Dziękuję za rozmowę, teraz się rozłączam. Do widzenia.",
        "Przepraszam, nie mogę połączyć się z usługą obsługującą tę rozmowę, więc muszę ją tu zakończyć. Do widzenia.",
    ),
    "pt": SpokenLines(
        "Desculpe, chegamos ao limite de tempo desta ligação. Agradeço a conversa, vou encerrar agora. Tchau.",
        "Desculpe, estou com dificuldade para acessar o serviço que faz esta ligação funcionar, então preciso encerrá-la aqui. Tchau.",
    ),
    "ro": SpokenLines(
        "Îmi pare rău, am atins limita de timp pentru acest apel. Mulțumesc pentru discuție, închid acum. La revedere.",
        "Îmi pare rău, nu reușesc să contactez serviciul care susține acest apel, așa că trebuie să-l închei aici. La revedere.",
    ),
    "ru": SpokenLines(
        "Извините, время этого звонка истекло. Спасибо за разговор, сейчас я завершу звонок. До свидания.",
        "Извините, не удаётся связаться с сервисом, который обслуживает этот звонок, поэтому мне нужно его завершить. До свидания.",
    ),
    "sk": SpokenLines(
        "Prepáčte, dosiahli sme časový limit tohto hovoru. Ďakujem za rozhovor, teraz hovor ukončím. Dovidenia.",
        "Prepáčte, nedarí sa mi spojiť so službou, ktorá zabezpečuje tento hovor, takže ho musím ukončiť. Dovidenia.",
    ),
    "sv": SpokenLines(
        "Tyvärr har vi nått tidsgränsen för det här samtalet. Tack för pratstunden, jag avslutar nu. Hej då.",
        "Tyvärr får jag inte kontakt med tjänsten som driver det här samtalet, så jag måste avsluta här. Hej då.",
    ),
    "ta": SpokenLines(
        "மன்னிக்கவும், இந்த அழைப்பின் நேர வரம்பை எட்டிவிட்டோம். பேசியதற்கு நன்றி. இப்போது அழைப்பை முடிக்கிறேன். வணக்கம்.",
        "மன்னிக்கவும், இந்த அழைப்பை இயக்கும் சேவையை அணுக முடியவில்லை, எனவே அழைப்பை இங்கே முடிக்க வேண்டும். வணக்கம்.",
    ),
    "te": SpokenLines(
        "క్షమించండి, ఈ కాల్ సమయ పరిమితి ముగిసింది. మాట్లాడినందుకు ధన్యవాదాలు. ఇప్పుడు కాల్‌ను ముగిస్తున్నాను. సెలవు.",
        "క్షమించండి, ఈ కాల్‌ను నడిపే సేవను చేరుకోవడంలో సమస్య ఉంది, కాబట్టి కాల్‌ను ఇక్కడే ముగించాలి. సెలవు.",
    ),
    "th": SpokenLines(
        "ขออภัย การโทรนี้ถึงเวลาที่กำหนดแล้ว ขอบคุณที่สนทนา ขอวางสายก่อน สวัสดี",
        "ขออภัย ไม่สามารถเชื่อมต่อกับบริการที่ใช้ในการโทรนี้ได้ จึงต้องวางสายตรงนี้ สวัสดี",
    ),
    "tl": SpokenLines(
        "Paumanhin, naabot na natin ang limitasyon sa oras ng tawag na ito. Salamat sa pakikipag-usap, tatapusin ko na ang tawag. Paalam.",
        "Paumanhin, nahihirapan akong maabot ang serbisyong nagpapatakbo ng tawag na ito, kaya kailangan ko na itong tapusin. Paalam.",
    ),
    "tr": SpokenLines(
        "Üzgünüm, bu görüşmenin süre sınırına ulaştık. Görüşme için teşekkürler, şimdi kapatıyorum. Hoşça kalın.",
        "Üzgünüm, bu görüşmeyi sağlayan hizmete ulaşamıyorum, bu yüzden görüşmeyi burada bitirmem gerekiyor. Hoşça kalın.",
    ),
    "uk": SpokenLines(
        "Вибачте, час цього дзвінка вичерпано. Дякую за розмову, зараз я завершу дзвінок. До побачення.",
        "Вибачте, не вдається з'єднатися із сервісом, який обслуговує цей дзвінок, тому мушу його завершити. До побачення.",
    ),
    "vi": SpokenLines(
        "Xin lỗi, cuộc gọi này đã đến giới hạn thời gian. Cảm ơn bạn đã trò chuyện, tôi xin phép kết thúc cuộc gọi. Tạm biệt.",
        "Xin lỗi, tôi không kết nối được với dịch vụ vận hành cuộc gọi này, nên phải kết thúc cuộc gọi tại đây. Tạm biệt.",
    ),
    "zh": SpokenLines(
        "抱歉，本次通话已达到时间上限。感谢您的交谈，现在为您挂断。再见。",
        "抱歉，我无法连接到支持本次通话的服务，只能在此结束通话。再见。",
    ),
}


def spoken_lines(language: str | None) -> SpokenLines:
    """The lines in ``language``, English when it is unknown."""
    if not isinstance(language, str):
        return SPOKEN_LINES["en"]
    return SPOKEN_LINES.get(language, SPOKEN_LINES["en"])
