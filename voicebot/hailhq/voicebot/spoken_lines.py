"""Fixed lines the voicebot speaks without the LLM, in the call's language.

The time-limit goodbye, the goodbye when a per-call LLM endpoint keeps
failing, and the handover intro to the contact. Unknown or missing language:
English. Where a verb would mark the speaker's gender, the line uses a form
that does not.
"""

from __future__ import annotations

from typing import NamedTuple

__all__ = ["SPOKEN_LINES", "SpokenLines", "spoken_lines"]


class SpokenLines(NamedTuple):
    soft_cap: str
    llm_failure: str
    # ``{name}`` is the contact; ``{reason}`` is the caller's reason.
    handover_intro: str
    handover_intro_reason: str


# Keyed by ``hailhq.core.languages.SUPPORTED_LANGUAGES``.
SPOKEN_LINES: dict[str, SpokenLines] = {
    "en": SpokenLines(
        "Sorry, we've reached the time limit for this call. Thanks for talking, I'll let you go now. Goodbye.",
        "Sorry, I'm having trouble reaching the service that powers this call, so I have to end it here. Goodbye.",
        "Hi {name}, I have a caller on the line. Connecting you now.",
        "Hi {name}, I have a caller on the line. Reason given: {reason}. Connecting you now.",
    ),
    "ar": SpokenLines(
        "عذرًا، لقد وصلنا إلى الحد الزمني لهذه المكالمة. شكرًا على وقتك، سأنهي المكالمة الآن. مع السلامة.",
        "عذرًا، أواجه مشكلة في الوصول إلى الخدمة التي تشغّل هذه المكالمة، لذا يجب أن أنهيها الآن. مع السلامة.",
        "مرحبًا {name}، لدي متصل على الخط. سأوصلك الآن.",
        "مرحبًا {name}، لدي متصل على الخط. السبب المذكور: {reason}. سأوصلك الآن.",
    ),
    "bg": SpokenLines(
        "Съжалявам, достигнахме времевия лимит за този разговор. Благодаря за разговора, ще приключа сега. Довиждане.",
        "Съжалявам, имам проблем с връзката към услугата, която обслужва този разговор, затова трябва да го прекратя. Довиждане.",
        "Здравейте, {name}, имам обаждащ се на линията. Свързвам ви сега.",
        "Здравейте, {name}, имам обаждащ се на линията. Посочена причина: {reason}. Свързвам ви сега.",
    ),
    "bn": SpokenLines(
        "দুঃখিত, এই কলের সময়সীমা শেষ হয়ে গেছে। কথা বলার জন্য ধন্যবাদ, এখন কলটি শেষ করছি। বিদায়।",
        "দুঃখিত, এই কলটি যে পরিষেবা চালায় তার সাথে সংযোগ করতে সমস্যা হচ্ছে, তাই কলটি এখানেই শেষ করতে হচ্ছে। বিদায়।",
        "হ্যালো {name}, লাইনে একজন কলার আছেন। এখনই আপনাকে যুক্ত করছি।",
        "হ্যালো {name}, লাইনে একজন কলার আছেন। জানানো কারণ: {reason}। এখনই আপনাকে যুক্ত করছি।",
    ),
    "cs": SpokenLines(
        "Omlouvám se, dosáhli jsme časového limitu tohoto hovoru. Děkuji za rozhovor, teď hovor ukončím. Na shledanou.",
        "Omlouvám se, nedaří se mi spojit se službou, která tento hovor zajišťuje, takže ho musím ukončit. Na shledanou.",
        "Dobrý den, {name}, mám na lince volajícího. Hned vás spojím.",
        "Dobrý den, {name}, mám na lince volajícího. Uvedený důvod: {reason}. Hned vás spojím.",
    ),
    "da": SpokenLines(
        "Beklager, vi har nået tidsgrænsen for dette opkald. Tak for snakken, jeg lader dig gå nu. Farvel.",
        "Beklager, jeg har problemer med at nå den tjeneste, der driver dette opkald, så jeg må afslutte det her. Farvel.",
        "Hej {name}, jeg har en opkalder på linjen. Jeg stiller om nu.",
        "Hej {name}, jeg har en opkalder på linjen. Oplyst årsag: {reason}. Jeg stiller om nu.",
    ),
    "de": SpokenLines(
        "Entschuldigung, wir haben das Zeitlimit für diesen Anruf erreicht. Danke für das Gespräch, ich beende den Anruf jetzt. Auf Wiederhören.",
        "Entschuldigung, ich kann den Dienst, der diesen Anruf betreibt, gerade nicht erreichen und muss den Anruf hier beenden. Auf Wiederhören.",
        "Hallo {name}, ich habe jemanden in der Leitung. Ich verbinde Sie jetzt.",
        "Hallo {name}, ich habe jemanden in der Leitung. Angegebener Grund: {reason}. Ich verbinde Sie jetzt.",
    ),
    "el": SpokenLines(
        "Λυπάμαι, φτάσαμε στο χρονικό όριο αυτής της κλήσης. Ευχαριστώ για τη συζήτηση, θα κλείσω τώρα. Αντίο.",
        "Λυπάμαι, δυσκολεύομαι να συνδεθώ με την υπηρεσία που λειτουργεί αυτή την κλήση, οπότε πρέπει να την τερματίσω. Αντίο.",
        "Γεια σας {name}, έχω μια κλήση στη γραμμή. Σας συνδέω τώρα.",
        "Γεια σας {name}, έχω μια κλήση στη γραμμή. Αναφερόμενος λόγος: {reason}. Σας συνδέω τώρα.",
    ),
    "es": SpokenLines(
        "Lo siento, hemos llegado al límite de tiempo de esta llamada. Gracias por hablar conmigo, ya me despido. Adiós.",
        "Lo siento, tengo problemas para conectar con el servicio que hace funcionar esta llamada, así que tengo que terminarla aquí. Adiós.",
        "Hola {name}, tengo una llamada en línea. Le conecto ahora.",
        "Hola {name}, tengo una llamada en línea. Motivo indicado: {reason}. Le conecto ahora.",
    ),
    "fi": SpokenLines(
        "Pahoittelen, tämän puhelun aikaraja on täynnä. Kiitos keskustelusta, lopetan puhelun nyt. Näkemiin.",
        "Pahoittelen, en saa yhteyttä palveluun, joka hoitaa tämän puhelun, joten minun täytyy lopettaa se tähän. Näkemiin.",
        "Hei {name}, minulla on soittaja linjalla. Yhdistän nyt.",
        "Hei {name}, minulla on soittaja linjalla. Kerrottu syy: {reason}. Yhdistän nyt.",
    ),
    "fr": SpokenLines(
        "Toutes mes excuses, nous avons atteint la durée maximale de cet appel. Merci pour cet échange, je vais raccrocher. Au revoir.",
        "Toutes mes excuses, je n'arrive pas à joindre le service qui fait fonctionner cet appel, je dois donc y mettre fin. Au revoir.",
        "Bonjour {name}, j'ai un appel en ligne pour vous. Je vous mets en relation.",
        "Bonjour {name}, j'ai un appel en ligne pour vous. Motif indiqué : {reason}. Je vous mets en relation.",
    ),
    "gu": SpokenLines(
        "માફ કરશો, આ કૉલની સમય મર્યાદા પૂરી થઈ ગઈ છે. વાત કરવા બદલ આભાર. આવજો.",
        "માફ કરશો, આ કૉલ ચલાવતી સેવા સુધી પહોંચવામાં મુશ્કેલી છે, તેથી કૉલ અહીં પૂરો કરવો પડશે. આવજો.",
        "નમસ્તે {name}, લાઇન પર એક કૉલર છે. હવે તમને જોડું છું.",
        "નમસ્તે {name}, લાઇન પર એક કૉલર છે. જણાવેલ કારણ: {reason}. હવે તમને જોડું છું.",
    ),
    "he": SpokenLines(
        "מצטערים, הגענו למגבלת הזמן של השיחה. תודה על השיחה, השיחה תסתיים עכשיו. להתראות.",
        "מצטערים, יש בעיה בחיבור לשירות שמפעיל את השיחה, ולכן השיחה תסתיים כאן. להתראות.",
        "שלום {name}, יש שיחה על הקו. החיבור מתבצע עכשיו.",
        "שלום {name}, יש שיחה על הקו. הסיבה שנמסרה: {reason}. החיבור מתבצע עכשיו.",
    ),
    "hi": SpokenLines(
        "माफ़ कीजिए, इस कॉल की समय सीमा पूरी हो गई है। बात करने के लिए धन्यवाद। नमस्ते।",
        "माफ़ कीजिए, इस कॉल को चलाने वाली सेवा से संपर्क नहीं हो पा रहा है, इसलिए कॉल यहीं समाप्त करनी होगी। नमस्ते।",
        "नमस्ते {name}, लाइन पर एक कॉलर हैं। अब आपको जोड़ा जा रहा है।",
        "नमस्ते {name}, लाइन पर एक कॉलर हैं। बताया गया कारण: {reason}। अब आपको जोड़ा जा रहा है।",
    ),
    "hr": SpokenLines(
        "Žao mi je, dosegnuli smo vremensko ograničenje ovog poziva. Hvala na razgovoru, sada završavam poziv. Doviđenja.",
        "Žao mi je, ne mogu dobiti uslugu koja pokreće ovaj poziv, pa ga moram ovdje završiti. Doviđenja.",
        "Dobar dan, {name}, imam poziv na liniji. Sada vas spajam.",
        "Dobar dan, {name}, imam poziv na liniji. Navedeni razlog: {reason}. Sada vas spajam.",
    ),
    "hu": SpokenLines(
        "Elnézést, elértük a hívás időkorlátját. Köszönöm a beszélgetést, most bontom a hívást. Viszonthallásra.",
        "Elnézést, nem érem el a hívást működtető szolgáltatást, ezért itt be kell fejeznem a hívást. Viszonthallásra.",
        "Jó napot, {name}, egy hívó van a vonalban. Most kapcsolom.",
        "Jó napot, {name}, egy hívó van a vonalban. Megadott ok: {reason}. Most kapcsolom.",
    ),
    "id": SpokenLines(
        "Maaf, kita sudah mencapai batas waktu panggilan ini. Terima kasih sudah berbicara, saya akhiri panggilan sekarang. Sampai jumpa.",
        "Maaf, saya kesulitan menghubungi layanan yang menjalankan panggilan ini, jadi saya harus mengakhirinya di sini. Sampai jumpa.",
        "Halo {name}, ada penelepon di saluran. Saya sambungkan sekarang.",
        "Halo {name}, ada penelepon di saluran. Alasan yang disampaikan: {reason}. Saya sambungkan sekarang.",
    ),
    "it": SpokenLines(
        "Mi scusi, abbiamo raggiunto il limite di tempo per questa chiamata. Grazie per la conversazione, ora chiudo. Arrivederci.",
        "Mi scusi, non riesco a raggiungere il servizio che gestisce questa chiamata, quindi devo chiuderla qui. Arrivederci.",
        "Salve {name}, ho una persona in linea. La metto in contatto ora.",
        "Salve {name}, ho una persona in linea. Motivo indicato: {reason}. La metto in contatto ora.",
    ),
    "ja": SpokenLines(
        "申し訳ありません、この通話の制限時間に達しました。お話しいただきありがとうございました。これで失礼いたします。",
        "申し訳ありません、この通話を支えるサービスに接続できないため、ここで通話を終了いたします。失礼いたします。",
        "{name}様、お電話の方がいらっしゃいます。今からおつなぎします。",
        "{name}様、お電話の方がいらっしゃいます。ご用件：{reason}。今からおつなぎします。",
    ),
    "kn": SpokenLines(
        "ಕ್ಷಮಿಸಿ, ಈ ಕರೆಯ ಸಮಯ ಮಿತಿ ಮುಗಿದಿದೆ. ಮಾತನಾಡಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. ವಿದಾಯ.",
        "ಕ್ಷಮಿಸಿ, ಈ ಕರೆಯನ್ನು ನಡೆಸುವ ಸೇವೆಯನ್ನು ತಲುಪಲು ತೊಂದರೆಯಾಗುತ್ತಿದೆ, ಆದ್ದರಿಂದ ಕರೆಯನ್ನು ಇಲ್ಲಿಗೆ ಮುಗಿಸಬೇಕಾಗಿದೆ. ವಿದಾಯ.",
        "ನಮಸ್ಕಾರ {name}, ಲೈನ್‌ನಲ್ಲಿ ಒಬ್ಬ ಕರೆದಾರರಿದ್ದಾರೆ. ಈಗ ನಿಮ್ಮನ್ನು ಸಂಪರ್ಕಿಸುತ್ತಿದ್ದೇನೆ.",
        "ನಮಸ್ಕಾರ {name}, ಲೈನ್‌ನಲ್ಲಿ ಒಬ್ಬ ಕರೆದಾರರಿದ್ದಾರೆ. ತಿಳಿಸಿದ ಕಾರಣ: {reason}. ಈಗ ನಿಮ್ಮನ್ನು ಸಂಪರ್ಕಿಸುತ್ತಿದ್ದೇನೆ.",
    ),
    "ko": SpokenLines(
        "죄송합니다, 이 통화의 시간 제한에 도달했습니다. 통화해 주셔서 감사합니다. 이만 끊겠습니다. 안녕히 계세요.",
        "죄송합니다, 이 통화를 지원하는 서비스에 연결할 수 없어 여기서 통화를 종료해야 합니다. 안녕히 계세요.",
        "안녕하세요 {name}님, 통화를 원하시는 분이 계십니다. 지금 연결해 드리겠습니다.",
        "안녕하세요 {name}님, 통화를 원하시는 분이 계십니다. 전달된 용건: {reason}. 지금 연결해 드리겠습니다.",
    ),
    "mr": SpokenLines(
        "माफ करा, या कॉलची वेळ मर्यादा संपली आहे. बोलल्याबद्दल धन्यवाद. नमस्कार.",
        "माफ करा, हा कॉल चालवणाऱ्या सेवेशी संपर्क होत नाही, त्यामुळे कॉल इथेच संपवावा लागेल. नमस्कार.",
        "नमस्कार {name}, लाइनवर एक कॉलर आहेत. आता तुम्हाला जोडले जात आहे.",
        "नमस्कार {name}, लाइनवर एक कॉलर आहेत. सांगितलेले कारण: {reason}. आता तुम्हाला जोडले जात आहे.",
    ),
    "ms": SpokenLines(
        "Maaf, kita sudah sampai had masa untuk panggilan ini. Terima kasih kerana bercakap, saya tamatkan panggilan sekarang. Selamat tinggal.",
        "Maaf, saya menghadapi masalah menghubungi perkhidmatan yang menjalankan panggilan ini, jadi saya perlu menamatkannya di sini. Selamat tinggal.",
        "Helo {name}, ada pemanggil di talian. Saya sambungkan sekarang.",
        "Helo {name}, ada pemanggil di talian. Sebab yang diberi: {reason}. Saya sambungkan sekarang.",
    ),
    "nl": SpokenLines(
        "Sorry, we hebben de tijdslimiet voor dit gesprek bereikt. Bedankt voor het gesprek, ik hang nu op. Tot ziens.",
        "Sorry, ik kan de dienst achter dit gesprek niet bereiken, dus ik moet het hier beëindigen. Tot ziens.",
        "Hallo {name}, ik heb iemand aan de lijn. Ik verbind u nu door.",
        "Hallo {name}, ik heb iemand aan de lijn. Opgegeven reden: {reason}. Ik verbind u nu door.",
    ),
    "no": SpokenLines(
        "Beklager, vi har nådd tidsgrensen for denne samtalen. Takk for praten, jeg avslutter nå. Ha det.",
        "Beklager, jeg får ikke kontakt med tjenesten som driver denne samtalen, så jeg må avslutte her. Ha det.",
        "Hei {name}, jeg har en innringer på linjen. Jeg setter deg over nå.",
        "Hei {name}, jeg har en innringer på linjen. Oppgitt grunn: {reason}. Jeg setter deg over nå.",
    ),
    "pl": SpokenLines(
        "Przepraszam, osiągnęliśmy limit czasu tej rozmowy. Dziękuję za rozmowę, teraz się rozłączam. Do widzenia.",
        "Przepraszam, nie mogę połączyć się z usługą obsługującą tę rozmowę, więc muszę ją tu zakończyć. Do widzenia.",
        "Dzień dobry, {name}, mam połączenie na linii. Już łączę.",
        "Dzień dobry, {name}, mam połączenie na linii. Podany powód: {reason}. Już łączę.",
    ),
    "pt": SpokenLines(
        "Desculpe, chegamos ao limite de tempo desta ligação. Agradeço a conversa, vou encerrar agora. Tchau.",
        "Desculpe, estou com dificuldade para acessar o serviço que faz esta ligação funcionar, então preciso encerrá-la aqui. Tchau.",
        "Olá {name}, tenho uma pessoa na linha. Vou transferir agora.",
        "Olá {name}, tenho uma pessoa na linha. Motivo informado: {reason}. Vou transferir agora.",
    ),
    "ro": SpokenLines(
        "Îmi pare rău, am atins limita de timp pentru acest apel. Mulțumesc pentru discuție, închid acum. La revedere.",
        "Îmi pare rău, nu reușesc să contactez serviciul care susține acest apel, așa că trebuie să-l închei aici. La revedere.",
        "Bună ziua, {name}, am un apel pe linie. Vă conectez acum.",
        "Bună ziua, {name}, am un apel pe linie. Motivul indicat: {reason}. Vă conectez acum.",
    ),
    "ru": SpokenLines(
        "Извините, время этого звонка истекло. Спасибо за разговор, сейчас я завершу звонок. До свидания.",
        "Извините, не удаётся связаться с сервисом, который обслуживает этот звонок, поэтому мне нужно его завершить. До свидания.",
        "Здравствуйте, {name}, у меня на линии звонящий. Соединяю вас.",
        "Здравствуйте, {name}, у меня на линии звонящий. Указанная причина: {reason}. Соединяю вас.",
    ),
    "sk": SpokenLines(
        "Prepáčte, dosiahli sme časový limit tohto hovoru. Ďakujem za rozhovor, teraz hovor ukončím. Dovidenia.",
        "Prepáčte, nedarí sa mi spojiť so službou, ktorá zabezpečuje tento hovor, takže ho musím ukončiť. Dovidenia.",
        "Dobrý deň, {name}, mám na linke volajúceho. Hneď vás spojím.",
        "Dobrý deň, {name}, mám na linke volajúceho. Uvedený dôvod: {reason}. Hneď vás spojím.",
    ),
    "sv": SpokenLines(
        "Tyvärr har vi nått tidsgränsen för det här samtalet. Tack för pratstunden, jag avslutar nu. Hej då.",
        "Tyvärr får jag inte kontakt med tjänsten som driver det här samtalet, så jag måste avsluta här. Hej då.",
        "Hej {name}, jag har en uppringare på linjen. Jag kopplar dig nu.",
        "Hej {name}, jag har en uppringare på linjen. Angivet skäl: {reason}. Jag kopplar dig nu.",
    ),
    "ta": SpokenLines(
        "மன்னிக்கவும், இந்த அழைப்பின் நேர வரம்பை எட்டிவிட்டோம். பேசியதற்கு நன்றி. வணக்கம்.",
        "மன்னிக்கவும், இந்த அழைப்பை இயக்கும் சேவையை அணுக முடியவில்லை, எனவே அழைப்பை இங்கே முடிக்க வேண்டும். வணக்கம்.",
        "வணக்கம் {name}, லைனில் ஒருவர் அழைக்கிறார். இப்போது உங்களை இணைக்கிறேன்.",
        "வணக்கம் {name}, லைனில் ஒருவர் அழைக்கிறார். தெரிவிக்கப்பட்ட காரணம்: {reason}. இப்போது உங்களை இணைக்கிறேன்.",
    ),
    "te": SpokenLines(
        "క్షమించండి, ఈ కాల్ సమయ పరిమితి ముగిసింది. మాట్లాడినందుకు ధన్యవాదాలు. సెలవు.",
        "క్షమించండి, ఈ కాల్‌ను నడిపే సేవను చేరుకోవడంలో సమస్య ఉంది, కాబట్టి కాల్‌ను ఇక్కడే ముగించాలి. సెలవు.",
        "నమస్కారం {name}, లైన్‌లో ఒకరు కాల్ చేస్తున్నారు. ఇప్పుడు మిమ్మల్ని కలుపుతున్నాను.",
        "నమస్కారం {name}, లైన్‌లో ఒకరు కాల్ చేస్తున్నారు. తెలిపిన కారణం: {reason}. ఇప్పుడు మిమ్మల్ని కలుపుతున్నాను.",
    ),
    "th": SpokenLines(
        "ขออภัย การโทรนี้ถึงเวลาที่กำหนดแล้ว ขอบคุณที่สนทนา ขอวางสายก่อน สวัสดี",
        "ขออภัย ไม่สามารถเชื่อมต่อกับบริการที่ใช้ในการโทรนี้ได้ จึงต้องวางสายตรงนี้ สวัสดี",
        "สวัสดี {name} มีสายโทรเข้ารออยู่ กำลังโอนสายให้",
        "สวัสดี {name} มีสายโทรเข้ารออยู่ เหตุผลที่แจ้งไว้: {reason} กำลังโอนสายให้",
    ),
    "tl": SpokenLines(
        "Paumanhin, naabot na natin ang limitasyon sa oras ng tawag na ito. Salamat sa pakikipag-usap, tatapusin ko na ang tawag. Paalam.",
        "Paumanhin, nahihirapan akong maabot ang serbisyong nagpapatakbo ng tawag na ito, kaya kailangan ko na itong tapusin. Paalam.",
        "Hello {name}, may tumatawag sa linya. Ikokonekta na kita.",
        "Hello {name}, may tumatawag sa linya. Dahilang ibinigay: {reason}. Ikokonekta na kita.",
    ),
    "tr": SpokenLines(
        "Üzgünüm, bu görüşmenin süre sınırına ulaştık. Görüşme için teşekkürler, şimdi kapatıyorum. Hoşça kalın.",
        "Üzgünüm, bu görüşmeyi sağlayan hizmete ulaşamıyorum, bu yüzden görüşmeyi burada bitirmem gerekiyor. Hoşça kalın.",
        "Merhaba {name}, hatta bir arayan var. Şimdi bağlıyorum.",
        "Merhaba {name}, hatta bir arayan var. Belirtilen neden: {reason}. Şimdi bağlıyorum.",
    ),
    "uk": SpokenLines(
        "Вибачте, час цього дзвінка вичерпано. Дякую за розмову, зараз я завершу дзвінок. До побачення.",
        "Вибачте, не вдається з'єднатися із сервісом, який обслуговує цей дзвінок, тому мушу його завершити. До побачення.",
        "Добрий день, {name}, у мене на лінії абонент. З'єдную вас.",
        "Добрий день, {name}, у мене на лінії абонент. Вказана причина: {reason}. З'єдную вас.",
    ),
    "vi": SpokenLines(
        "Xin lỗi, cuộc gọi này đã đến giới hạn thời gian. Cảm ơn bạn đã trò chuyện, tôi xin phép kết thúc cuộc gọi. Tạm biệt.",
        "Xin lỗi, tôi không kết nối được với dịch vụ vận hành cuộc gọi này, nên phải kết thúc cuộc gọi tại đây. Tạm biệt.",
        "Xin chào {name}, có người gọi đang chờ máy. Tôi kết nối ngay bây giờ.",
        "Xin chào {name}, có người gọi đang chờ máy. Lý do được nêu: {reason}. Tôi kết nối ngay bây giờ.",
    ),
    "zh": SpokenLines(
        "抱歉，本次通话已达到时间上限。感谢您的交谈，我现在挂断了。再见。",
        "抱歉，我无法连接到支持本次通话的服务，只能在此结束通话。再见。",
        "{name}您好，有一位来电者在线。现在为您接通。",
        "{name}您好，有一位来电者在线。来电原因：{reason}。现在为您接通。",
    ),
}


def spoken_lines(language: str | None) -> SpokenLines:
    """The lines in ``language``, English when it is unknown."""
    if not isinstance(language, str):
        return SPOKEN_LINES["en"]
    return SPOKEN_LINES.get(language, SPOKEN_LINES["en"])
