"""Fixed lines of an email an agent sends to the caller, in the call's language.

An email to the caller's own address on file (``send_email`` with ``me`` on a
phone call) gets a fixed subject start, first line and footer that say it
came from a phone call with the organization (see
``hailhq.core.agent_tools.send_email.caller_email_subject``). The recipient
reads them, so they follow the call's language like the voicebot's fixed
spoken lines. Unknown or missing language: English. Each line has a form
with ``{org}``, the organization's display name, and one for when the name
did not resolve. The subject never opens with ``{org}``: the email list
must show the phone call words first. Where a verb would mark the reader's gender, the line uses
a form that does not.
"""

from __future__ import annotations

from typing import NamedTuple

from hailhq.core.languages import by_language

__all__ = ["CALLER_EMAIL_LINES", "CallerEmailLines", "caller_email_lines"]


class CallerEmailLines(NamedTuple):
    """The fixed lines for one language."""

    subject: str
    subject_unnamed: str
    first_line: str
    first_line_unnamed: str
    footer: str
    footer_unnamed: str


# Keyed by ``hailhq.core.languages.SUPPORTED_LANGUAGES``.
CALLER_EMAIL_LINES: dict[str, CallerEmailLines] = {
    "en": CallerEmailLines(
        "From a phone call with {org}",
        "From a phone call",
        "This email was sent because of a phone call with {org}.",
        "This email was sent because of a phone call.",
        "You got this email because someone asked for it during a phone call"
        " with {org}, on the phone number saved with this email address. If"
        " that was not you, you can ignore it.",
        "You got this email because someone asked for it during a phone call,"
        " on the phone number saved with this email address. If that was not"
        " you, you can ignore it.",
    ),
    "ar": CallerEmailLines(
        "بخصوص مكالمة هاتفية مع {org}",
        "بخصوص مكالمة هاتفية",
        "أُرسلت رسالة البريد الإلكتروني هذه بسبب مكالمة هاتفية مع {org}.",
        "أُرسلت رسالة البريد الإلكتروني هذه بسبب مكالمة هاتفية.",
        "وصلتك هذه الرسالة لأن شخصًا طلبها أثناء مكالمة هاتفية مع {org}، من رقم"
        " الهاتف المحفوظ مع عنوان البريد الإلكتروني هذا. إذا لم يكن الطلب منك،"
        " يمكنك تجاهلها.",
        "وصلتك هذه الرسالة لأن شخصًا طلبها أثناء مكالمة هاتفية، من رقم الهاتف"
        " المحفوظ مع عنوان البريد الإلكتروني هذا. إذا لم يكن الطلب منك، يمكنك"
        " تجاهلها.",
    ),
    "bg": CallerEmailLines(
        "От телефонен разговор с {org}",
        "От телефонен разговор",
        "Този имейл е изпратен заради телефонен разговор с {org}.",
        "Този имейл е изпратен заради телефонен разговор.",
        "Получавате този имейл, защото някой го е поискал по време на телефонен"
        " разговор с {org} от телефонния номер, запазен към този имейл адрес. Ако"
        " това не сте били вие, можете да го пренебрегнете.",
        "Получавате този имейл, защото някой го е поискал по време на телефонен"
        " разговор от телефонния номер, запазен към този имейл адрес. Ако това не"
        " сте били вие, можете да го пренебрегнете.",
    ),
    "bn": CallerEmailLines(
        "ফোন কলের সূত্রে ({org})",
        "একটি ফোন কলের সূত্রে",
        "এই ইমেইলটি {org}-এর সঙ্গে একটি ফোন কলের কারণে পাঠানো হয়েছে।",
        "এই ইমেইলটি একটি ফোন কলের কারণে পাঠানো হয়েছে।",
        "আপনি এই ইমেইলটি পেয়েছেন কারণ কেউ {org}-এর সঙ্গে একটি ফোন কলের সময়,"
        " এই ইমেইল ঠিকানার সঙ্গে সংরক্ষিত ফোন নম্বর থেকে, এটি চেয়েছিলেন।"
        " অনুরোধটি আপনার না হলে, আপনি এটি উপেক্ষা করতে পারেন।",
        "আপনি এই ইমেইলটি পেয়েছেন কারণ কেউ একটি ফোন কলের সময়, এই ইমেইল"
        " ঠিকানার সঙ্গে সংরক্ষিত ফোন নম্বর থেকে, এটি চেয়েছিলেন। অনুরোধটি"
        " আপনার না হলে, আপনি এটি উপেক্ষা করতে পারেন।",
    ),
    "cs": CallerEmailLines(
        "Z telefonátu s {org}",
        "Z telefonátu",
        "Tento e-mail byl odeslán kvůli telefonátu s {org}.",
        "Tento e-mail byl odeslán kvůli telefonátu.",
        "Tento e-mail vám přišel, protože o něj někdo požádal během telefonátu"
        " s {org}, z telefonního čísla uloženého u této e-mailové adresy. Pokud"
        " to nebyla vaše žádost, můžete ho ignorovat.",
        "Tento e-mail vám přišel, protože o něj někdo požádal během telefonátu,"
        " z telefonního čísla uloženého u této e-mailové adresy. Pokud to nebyla"
        " vaše žádost, můžete ho ignorovat.",
    ),
    "da": CallerEmailLines(
        "Fra et telefonopkald med {org}",
        "Fra et telefonopkald",
        "Denne e-mail blev sendt på grund af et telefonopkald med {org}.",
        "Denne e-mail blev sendt på grund af et telefonopkald.",
        "Du har fået denne e-mail, fordi nogen bad om den under et telefonopkald"
        " med {org}, fra det telefonnummer, der er gemt med denne e-mailadresse."
        " Hvis det ikke var dig, kan du se bort fra den.",
        "Du har fået denne e-mail, fordi nogen bad om den under et telefonopkald,"
        " fra det telefonnummer, der er gemt med denne e-mailadresse. Hvis det"
        " ikke var dig, kan du se bort fra den.",
    ),
    "de": CallerEmailLines(
        "Aus einem Telefonat mit {org}",
        "Aus einem Telefonat",
        "Diese E-Mail wurde wegen eines Telefonats mit {org} gesendet.",
        "Diese E-Mail wurde wegen eines Telefonats gesendet.",
        "Sie erhalten diese E-Mail, weil jemand sie während eines Telefonats mit"
        " {org} angefordert hat, von der Telefonnummer, die zu dieser"
        " E-Mail-Adresse gespeichert ist. Falls Sie das nicht waren, können Sie"
        " sie ignorieren.",
        "Sie erhalten diese E-Mail, weil jemand sie während eines Telefonats"
        " angefordert hat, von der Telefonnummer, die zu dieser E-Mail-Adresse"
        " gespeichert ist. Falls Sie das nicht waren, können Sie sie ignorieren.",
    ),
    "el": CallerEmailLines(
        "Από τηλεφωνική κλήση με {org}",
        "Από τηλεφωνική κλήση",
        "Αυτό το email στάλθηκε λόγω τηλεφωνικής κλήσης με {org}.",
        "Αυτό το email στάλθηκε λόγω τηλεφωνικής κλήσης.",
        "Λάβατε αυτό το email επειδή κάποιος το ζήτησε κατά τη διάρκεια"
        " τηλεφωνικής κλήσης με {org}, από τον αριθμό τηλεφώνου που είναι"
        " αποθηκευμένος με αυτή τη διεύθυνση email. Αν δεν ήσασταν εσείς,"
        " μπορείτε να το αγνοήσετε.",
        "Λάβατε αυτό το email επειδή κάποιος το ζήτησε κατά τη διάρκεια"
        " τηλεφωνικής κλήσης, από τον αριθμό τηλεφώνου που είναι αποθηκευμένος"
        " με αυτή τη διεύθυνση email. Αν δεν ήσασταν εσείς, μπορείτε να το"
        " αγνοήσετε.",
    ),
    "es": CallerEmailLines(
        "De una llamada telefónica con {org}",
        "De una llamada telefónica",
        "Este correo se envió a raíz de una llamada telefónica con {org}.",
        "Este correo se envió a raíz de una llamada telefónica.",
        "Recibe este correo porque alguien lo pidió durante una llamada"
        " telefónica con {org}, desde el número de teléfono guardado con esta"
        " dirección de correo. Si no fue usted, puede ignorarlo.",
        "Recibe este correo porque alguien lo pidió durante una llamada"
        " telefónica, desde el número de teléfono guardado con esta dirección de"
        " correo. Si no fue usted, puede ignorarlo.",
    ),
    "fi": CallerEmailLines(
        "Puhelusta organisaation {org} kanssa",
        "Puhelusta",
        "Tämä sähköposti lähetettiin organisaation {org} kanssa käydyn puhelun"
        " vuoksi.",
        "Tämä sähköposti lähetettiin puhelun vuoksi.",
        "Sait tämän sähköpostin, koska joku pyysi sitä organisaation {org} kanssa"
        " käydyssä puhelussa tähän sähköpostiosoitteeseen tallennetusta"
        " puhelinnumerosta. Jos pyyntö ei ollut sinun, voit jättää viestin huomiotta.",
        "Sait tämän sähköpostin, koska joku pyysi sitä puhelussa tähän"
        " sähköpostiosoitteeseen tallennetusta puhelinnumerosta. Jos pyyntö ei ollut"
        " sinun, voit jättää viestin huomiotta.",
    ),
    "fr": CallerEmailLines(
        "Suite à un appel téléphonique avec {org}",
        "Suite à un appel téléphonique",
        "Cet e-mail a été envoyé suite à un appel téléphonique avec {org}.",
        "Cet e-mail a été envoyé suite à un appel téléphonique.",
        "Vous recevez cet e-mail parce que quelqu'un l'a demandé lors d'un appel"
        " téléphonique avec {org}, depuis le numéro de téléphone associé à cette"
        " adresse e-mail. Si ce n'était pas vous, vous pouvez l'ignorer.",
        "Vous recevez cet e-mail parce que quelqu'un l'a demandé lors d'un appel"
        " téléphonique, depuis le numéro de téléphone associé à cette adresse"
        " e-mail. Si ce n'était pas vous, vous pouvez l'ignorer.",
    ),
    "gu": CallerEmailLines(
        "ફોન કૉલ પરથી ({org})",
        "એક ફોન કૉલ પરથી",
        "આ ઇમેઇલ {org} સાથેના એક ફોન કૉલને કારણે મોકલવામાં આવ્યો છે.",
        "આ ઇમેઇલ એક ફોન કૉલને કારણે મોકલવામાં આવ્યો છે.",
        "તમને આ ઇમેઇલ મળ્યો કારણ કે કોઈએ {org} સાથેના ફોન કૉલ દરમિયાન, આ"
        " ઇમેઇલ સરનામા સાથે સાચવેલા ફોન નંબર પરથી, તેની વિનંતી કરી હતી. જો આ"
        " વિનંતી તમારી ન હોય, તો આ ઇમેઇલને અવગણી શકાય છે.",
        "તમને આ ઇમેઇલ મળ્યો કારણ કે કોઈએ એક ફોન કૉલ દરમિયાન, આ ઇમેઇલ સરનામા"
        " સાથે સાચવેલા ફોન નંબર પરથી, તેની વિનંતી કરી હતી. જો આ વિનંતી તમારી"
        " ન હોય, તો આ ઇમેઇલને અવગણી શકાય છે.",
    ),
    "he": CallerEmailLines(
        "בעקבות שיחת טלפון עם {org}",
        "בעקבות שיחת טלפון",
        "האימייל הזה נשלח בעקבות שיחת טלפון עם {org}.",
        "האימייל הזה נשלח בעקבות שיחת טלפון.",
        "האימייל הזה נשלח כי מישהו ביקש אותו במהלך שיחת טלפון עם {org}, ממספר"
        " הטלפון ששמור עם כתובת האימייל הזו. אם הבקשה לא הייתה שלך, אפשר להתעלם"
        " ממנו.",
        "האימייל הזה נשלח כי מישהו ביקש אותו במהלך שיחת טלפון, ממספר הטלפון"
        " ששמור עם כתובת האימייל הזו. אם הבקשה לא הייתה שלך, אפשר להתעלם ממנו.",
    ),
    "hi": CallerEmailLines(
        "फ़ोन कॉल से ({org})",
        "एक फ़ोन कॉल से",
        "यह ईमेल {org} के साथ हुई एक फ़ोन कॉल की वजह से भेजा गया है।",
        "यह ईमेल एक फ़ोन कॉल की वजह से भेजा गया है।",
        "आपको यह ईमेल इसलिए मिला क्योंकि किसी ने {org} के साथ हुई फ़ोन कॉल के"
        " दौरान, इस ईमेल पते के साथ सहेजे गए फ़ोन नंबर से, इसका अनुरोध किया था।"
        " अगर यह अनुरोध आपका नहीं था, तो इस ईमेल को अनदेखा किया जा सकता है।",
        "आपको यह ईमेल इसलिए मिला क्योंकि किसी ने एक फ़ोन कॉल के दौरान, इस ईमेल"
        " पते के साथ सहेजे गए फ़ोन नंबर से, इसका अनुरोध किया था। अगर यह अनुरोध"
        " आपका नहीं था, तो इस ईमेल को अनदेखा किया जा सकता है।",
    ),
    "hr": CallerEmailLines(
        "Iz telefonskog poziva s {org}",
        "Iz telefonskog poziva",
        "Ova e-poruka poslana je zbog telefonskog poziva s {org}.",
        "Ova e-poruka poslana je zbog telefonskog poziva.",
        "Ovu e-poruku primate jer ju je netko zatražio tijekom telefonskog poziva"
        " s {org}, s telefonskog broja spremljenog uz ovu adresu e-pošte. Ako to"
        " nije bio vaš zahtjev, možete je zanemariti.",
        "Ovu e-poruku primate jer ju je netko zatražio tijekom telefonskog"
        " poziva, s telefonskog broja spremljenog uz ovu adresu e-pošte. Ako to"
        " nije bio vaš zahtjev, možete je zanemariti.",
    ),
    "hu": CallerEmailLines(
        "Telefonhívás alapján ({org})",
        "Telefonhívás alapján",
        "Ezt az e-mailt egy {org} szervezettel folytatott telefonhívás miatt"
        " küldtük.",
        "Ezt az e-mailt egy telefonhívás miatt küldtük.",
        "Azért kapta ezt az e-mailt, mert valaki kérte egy {org} szervezettel"
        " folytatott telefonhívás során, az ehhez az e-mail-címhez mentett"
        " telefonszámról. Ha nem Ön volt az, nyugodtan figyelmen kívül hagyhatja.",
        "Azért kapta ezt az e-mailt, mert valaki kérte egy telefonhívás során, az"
        " ehhez az e-mail-címhez mentett telefonszámról. Ha nem Ön volt az,"
        " nyugodtan figyelmen kívül hagyhatja.",
    ),
    "id": CallerEmailLines(
        "Dari panggilan telepon dengan {org}",
        "Dari panggilan telepon",
        "Email ini dikirim karena panggilan telepon dengan {org}.",
        "Email ini dikirim karena panggilan telepon.",
        "Anda menerima email ini karena seseorang memintanya saat panggilan"
        " telepon dengan {org}, dari nomor telepon yang tersimpan dengan alamat"
        " email ini. Jika itu bukan Anda, Anda dapat mengabaikannya.",
        "Anda menerima email ini karena seseorang memintanya saat panggilan"
        " telepon, dari nomor telepon yang tersimpan dengan alamat email ini."
        " Jika itu bukan Anda, Anda dapat mengabaikannya.",
    ),
    "it": CallerEmailLines(
        "Da una telefonata con {org}",
        "Da una telefonata",
        "Questa email è stata inviata in seguito a una telefonata con {org}.",
        "Questa email è stata inviata in seguito a una telefonata.",
        "Ha ricevuto questa email perché qualcuno l'ha richiesta durante una"
        " telefonata con {org}, dal numero di telefono associato a questo"
        " indirizzo email. Se non ha fatto Lei questa richiesta, può ignorarla.",
        "Ha ricevuto questa email perché qualcuno l'ha richiesta durante una"
        " telefonata, dal numero di telefono associato a questo indirizzo email."
        " Se non ha fatto Lei questa richiesta, può ignorarla.",
    ),
    "ja": CallerEmailLines(
        "通話より（{org}）",
        "通話より",
        "このメールは、{org}との通話をきっかけに送信されました。",
        "このメールは、通話をきっかけに送信されました。",
        "このメールアドレスに登録された電話番号から、{org}との通話中にどなたかが"
        "送信を依頼したため、このメールをお送りしています。お心当たりがない場合は、"
        "このメールを無視してください。",
        "このメールアドレスに登録された電話番号から、通話中にどなたかが送信を"
        "依頼したため、このメールをお送りしています。お心当たりがない場合は、"
        "このメールを無視してください。",
    ),
    "kn": CallerEmailLines(
        "ಫೋನ್ ಕರೆಯಿಂದ ({org})",
        "ಒಂದು ಫೋನ್ ಕರೆಯಿಂದ",
        "ಈ ಇಮೇಲ್ ಅನ್ನು {org} ಜೊತೆಗಿನ ಒಂದು ಫೋನ್ ಕರೆಯ ಕಾರಣದಿಂದ ಕಳುಹಿಸಲಾಗಿದೆ.",
        "ಈ ಇಮೇಲ್ ಅನ್ನು ಒಂದು ಫೋನ್ ಕರೆಯ ಕಾರಣದಿಂದ ಕಳುಹಿಸಲಾಗಿದೆ.",
        "ಯಾರೋ {org} ಜೊತೆಗಿನ ಫೋನ್ ಕರೆಯ ಸಮಯದಲ್ಲಿ, ಈ ಇಮೇಲ್ ವಿಳಾಸದೊಂದಿಗೆ ಉಳಿಸಲಾದ"
        " ಫೋನ್ ಸಂಖ್ಯೆಯಿಂದ ಇದನ್ನು ಕೇಳಿದ್ದರಿಂದ ನಿಮಗೆ ಈ ಇಮೇಲ್ ಬಂದಿದೆ. ಆ ವಿನಂತಿ"
        " ನಿಮ್ಮದಲ್ಲದಿದ್ದರೆ, ಇದನ್ನು ನಿರ್ಲಕ್ಷಿಸಬಹುದು.",
        "ಯಾರೋ ಒಂದು ಫೋನ್ ಕರೆಯ ಸಮಯದಲ್ಲಿ, ಈ ಇಮೇಲ್ ವಿಳಾಸದೊಂದಿಗೆ ಉಳಿಸಲಾದ ಫೋನ್"
        " ಸಂಖ್ಯೆಯಿಂದ ಇದನ್ನು ಕೇಳಿದ್ದರಿಂದ ನಿಮಗೆ ಈ ಇಮೇಲ್ ಬಂದಿದೆ. ಆ ವಿನಂತಿ"
        " ನಿಮ್ಮದಲ್ಲದಿದ್ದರೆ, ಇದನ್ನು ನಿರ್ಲಕ್ಷಿಸಬಹುದು.",
    ),
    "ko": CallerEmailLines(
        "전화 통화 관련 안내 ({org})",
        "전화 통화 관련 안내",
        "이 이메일은 {org}의 전화 통화와 관련하여 발송되었습니다.",
        "이 이메일은 전화 통화와 관련하여 발송되었습니다.",
        "이 이메일 주소에 저장된 전화번호로 연결된 {org}의 통화 중에 누군가"
        " 요청하여 이 이메일이 발송되었습니다. 본인이 요청하지 않았다면 이"
        " 이메일을 무시하셔도 됩니다.",
        "이 이메일 주소에 저장된 전화번호로 연결된 통화 중에 누군가 요청하여 이"
        " 이메일이 발송되었습니다. 본인이 요청하지 않았다면 이 이메일을 무시하셔도"
        " 됩니다.",
    ),
    "mr": CallerEmailLines(
        "फोन कॉलमधून ({org})",
        "एका फोन कॉलमधून",
        "हा ईमेल {org} सोबतच्या एका फोन कॉलमुळे पाठवला आहे.",
        "हा ईमेल एका फोन कॉलमुळे पाठवला आहे.",
        "हा ईमेल तुम्हाला मिळाला कारण कोणीतरी {org} सोबतच्या फोन कॉलदरम्यान, या"
        " ईमेल पत्त्यासोबत जतन केलेल्या फोन नंबरवरून, याची विनंती केली. ही विनंती"
        " तुमची नसेल, तर हा ईमेल दुर्लक्षित करता येईल.",
        "हा ईमेल तुम्हाला मिळाला कारण कोणीतरी एका फोन कॉलदरम्यान, या ईमेल"
        " पत्त्यासोबत जतन केलेल्या फोन नंबरवरून, याची विनंती केली. ही विनंती"
        " तुमची नसेल, तर हा ईमेल दुर्लक्षित करता येईल.",
    ),
    "ms": CallerEmailLines(
        "Daripada panggilan telefon dengan {org}",
        "Daripada panggilan telefon",
        "E-mel ini dihantar kerana panggilan telefon dengan {org}.",
        "E-mel ini dihantar kerana panggilan telefon.",
        "Anda menerima e-mel ini kerana seseorang memintanya semasa panggilan"
        " telefon dengan {org}, daripada nombor telefon yang disimpan bersama"
        " alamat e-mel ini. Jika itu bukan anda, anda boleh mengabaikannya.",
        "Anda menerima e-mel ini kerana seseorang memintanya semasa panggilan"
        " telefon, daripada nombor telefon yang disimpan bersama alamat e-mel"
        " ini. Jika itu bukan anda, anda boleh mengabaikannya.",
    ),
    "nl": CallerEmailLines(
        "Van een telefoongesprek met {org}",
        "Van een telefoongesprek",
        "Deze e-mail is verstuurd naar aanleiding van een telefoongesprek met"
        " {org}.",
        "Deze e-mail is verstuurd naar aanleiding van een telefoongesprek.",
        "U ontvangt deze e-mail omdat iemand erom heeft gevraagd tijdens een"
        " telefoongesprek met {org}, vanaf het telefoonnummer dat bij dit"
        " e-mailadres is opgeslagen. Was u dat niet, dan kunt u deze e-mail"
        " negeren.",
        "U ontvangt deze e-mail omdat iemand erom heeft gevraagd tijdens een"
        " telefoongesprek, vanaf het telefoonnummer dat bij dit e-mailadres is"
        " opgeslagen. Was u dat niet, dan kunt u deze e-mail negeren.",
    ),
    "no": CallerEmailLines(
        "Fra en telefonsamtale med {org}",
        "Fra en telefonsamtale",
        "Denne e-posten ble sendt på grunn av en telefonsamtale med {org}.",
        "Denne e-posten ble sendt på grunn av en telefonsamtale.",
        "Du fikk denne e-posten fordi noen ba om den under en telefonsamtale med"
        " {org}, fra telefonnummeret som er lagret med denne e-postadressen. Hvis"
        " det ikke var deg, kan du se bort fra den.",
        "Du fikk denne e-posten fordi noen ba om den under en telefonsamtale, fra"
        " telefonnummeret som er lagret med denne e-postadressen. Hvis det ikke"
        " var deg, kan du se bort fra den.",
    ),
    "pl": CallerEmailLines(
        "Po rozmowie telefonicznej z {org}",
        "Po rozmowie telefonicznej",
        "Ten e-mail został wysłany w związku z rozmową telefoniczną z {org}.",
        "Ten e-mail został wysłany w związku z rozmową telefoniczną.",
        "Otrzymujesz ten e-mail, ponieważ ktoś poprosił o niego podczas rozmowy"
        " telefonicznej z {org}, z numeru telefonu zapisanego z tym adresem"
        " e-mail. Jeśli to nie Ty, możesz go zignorować.",
        "Otrzymujesz ten e-mail, ponieważ ktoś poprosił o niego podczas rozmowy"
        " telefonicznej, z numeru telefonu zapisanego z tym adresem e-mail."
        " Jeśli to nie Ty, możesz go zignorować.",
    ),
    "pt": CallerEmailLines(
        "De uma chamada com {org}",
        "De uma chamada",
        "Este e-mail foi enviado por causa de uma chamada com {org}.",
        "Este e-mail foi enviado por causa de uma chamada.",
        "Você recebeu este e-mail porque alguém o pediu durante uma chamada com"
        " {org}, a partir do número de telefone associado a este endereço de"
        " e-mail. Se não foi você, pode ignorá-lo.",
        "Você recebeu este e-mail porque alguém o pediu durante uma chamada, a"
        " partir do número de telefone associado a este endereço de e-mail. Se"
        " não foi você, pode ignorá-lo.",
    ),
    "ro": CallerEmailLines(
        "În urma unui apel telefonic cu {org}",
        "În urma unui apel telefonic",
        "Acest e-mail a fost trimis în urma unui apel telefonic cu {org}.",
        "Acest e-mail a fost trimis în urma unui apel telefonic.",
        "Ați primit acest e-mail pentru că cineva l-a solicitat în timpul unui"
        " apel telefonic cu {org}, de pe numărul de telefon salvat pentru această"
        " adresă de e-mail. Dacă nu ați fost dumneavoastră, îl puteți ignora.",
        "Ați primit acest e-mail pentru că cineva l-a solicitat în timpul unui"
        " apel telefonic, de pe numărul de telefon salvat pentru această adresă"
        " de e-mail. Dacă nu ați fost dumneavoastră, îl puteți ignora.",
    ),
    "ru": CallerEmailLines(
        "По телефонному звонку с {org}",
        "По телефонному звонку",
        "Это письмо отправлено после телефонного звонка с {org}.",
        "Это письмо отправлено после телефонного звонка.",
        "Вы получили это письмо, потому что кто-то запросил его во время"
        " телефонного звонка с {org} с номера телефона, сохранённого для этого"
        " адреса электронной почты. Если это были не вы, просто проигнорируйте"
        " его.",
        "Вы получили это письмо, потому что кто-то запросил его во время"
        " телефонного звонка с номера телефона, сохранённого для этого адреса"
        " электронной почты. Если это были не вы, просто проигнорируйте его.",
    ),
    "sk": CallerEmailLines(
        "Z telefonátu s {org}",
        "Z telefonátu",
        "Tento e-mail bol odoslaný na základe telefonátu s {org}.",
        "Tento e-mail bol odoslaný na základe telefonátu.",
        "Tento e-mail vám prišiel, pretože oň niekto požiadal počas telefonátu"
        " s {org}, z telefónneho čísla uloženého pri tejto e-mailovej adrese. Ak"
        " to nebola vaša žiadosť, môžete ho ignorovať.",
        "Tento e-mail vám prišiel, pretože oň niekto požiadal počas telefonátu,"
        " z telefónneho čísla uloženého pri tejto e-mailovej adrese. Ak to"
        " nebola vaša žiadosť, môžete ho ignorovať.",
    ),
    "sv": CallerEmailLines(
        "Från ett telefonsamtal med {org}",
        "Från ett telefonsamtal",
        "Det här mejlet skickades på grund av ett telefonsamtal med {org}.",
        "Det här mejlet skickades på grund av ett telefonsamtal.",
        "Du fick det här mejlet eftersom någon bad om det under ett"
        " telefonsamtal med {org}, från telefonnumret som är sparat med den här"
        " e-postadressen. Om det inte var du kan du ignorera det.",
        "Du fick det här mejlet eftersom någon bad om det under ett"
        " telefonsamtal, från telefonnumret som är sparat med den här"
        " e-postadressen. Om det inte var du kan du ignorera det.",
    ),
    "ta": CallerEmailLines(
        "தொலைபேசி அழைப்பிலிருந்து ({org})",
        "ஒரு தொலைபேசி அழைப்பிலிருந்து",
        "இந்த மின்னஞ்சல் {org} உடனான ஒரு தொலைபேசி அழைப்பின் காரணமாக அனுப்பப்பட்டது.",
        "இந்த மின்னஞ்சல் ஒரு தொலைபேசி அழைப்பின் காரணமாக அனுப்பப்பட்டது.",
        "யாரோ ஒருவர் {org} உடனான தொலைபேசி அழைப்பின்போது, இந்த மின்னஞ்சல்"
        " முகவரியுடன் சேமிக்கப்பட்ட தொலைபேசி எண்ணிலிருந்து கேட்டதால், இந்த"
        " மின்னஞ்சல் உங்களுக்கு வந்தது. அந்தக் கோரிக்கை உங்களுடையது"
        " இல்லையென்றால், இதைப் புறக்கணிக்கலாம்.",
        "யாரோ ஒருவர் ஒரு தொலைபேசி அழைப்பின்போது, இந்த மின்னஞ்சல் முகவரியுடன்"
        " சேமிக்கப்பட்ட தொலைபேசி எண்ணிலிருந்து கேட்டதால், இந்த மின்னஞ்சல்"
        " உங்களுக்கு வந்தது. அந்தக் கோரிக்கை உங்களுடையது இல்லையென்றால், இதைப்"
        " புறக்கணிக்கலாம்.",
    ),
    "te": CallerEmailLines(
        "ఫోన్ కాల్ నుండి ({org})",
        "ఒక ఫోన్ కాల్ నుండి",
        "ఈ ఈమెయిల్ {org}తో జరిగిన ఒక ఫోన్ కాల్ కారణంగా పంపబడింది.",
        "ఈ ఈమెయిల్ ఒక ఫోన్ కాల్ కారణంగా పంపబడింది.",
        "ఎవరో {org}తో జరిగిన ఫోన్ కాల్ సమయంలో, ఈ ఈమెయిల్ చిరునామాతో సేవ్ చేసిన"
        " ఫోన్ నంబర్ నుండి అడిగినందున మీకు ఈ ఈమెయిల్ వచ్చింది. ఆ అభ్యర్థన మీది"
        " కాకపోతే, దీన్ని పట్టించుకోనవసరం లేదు.",
        "ఎవరో ఒక ఫోన్ కాల్ సమయంలో, ఈ ఈమెయిల్ చిరునామాతో సేవ్ చేసిన ఫోన్ నంబర్"
        " నుండి అడిగినందున మీకు ఈ ఈమెయిల్ వచ్చింది. ఆ అభ్యర్థన మీది కాకపోతే,"
        " దీన్ని పట్టించుకోనవసరం లేదు.",
    ),
    "th": CallerEmailLines(
        "จากการโทรศัพท์กับ {org}",
        "จากการโทรศัพท์",
        "อีเมลนี้ส่งมาเนื่องจากการโทรศัพท์กับ {org}",
        "อีเมลนี้ส่งมาเนื่องจากการโทรศัพท์",
        "คุณได้รับอีเมลนี้เพราะมีผู้ขอระหว่างการโทรศัพท์กับ {org}"
        " จากหมายเลขโทรศัพท์ที่บันทึกไว้กับที่อยู่อีเมลนี้ หากไม่ใช่คุณ"
        " สามารถเพิกเฉยต่ออีเมลนี้ได้",
        "คุณได้รับอีเมลนี้เพราะมีผู้ขอระหว่างการโทรศัพท์"
        " จากหมายเลขโทรศัพท์ที่บันทึกไว้กับที่อยู่อีเมลนี้ หากไม่ใช่คุณ"
        " สามารถเพิกเฉยต่ออีเมลนี้ได้",
    ),
    "tl": CallerEmailLines(
        "Mula sa isang tawag sa telepono kasama ang {org}",
        "Mula sa isang tawag sa telepono",
        "Ipinadala ang email na ito dahil sa isang tawag sa telepono kasama ang"
        " {org}.",
        "Ipinadala ang email na ito dahil sa isang tawag sa telepono.",
        "Natanggap mo ang email na ito dahil may humiling nito habang may tawag"
        " sa telepono kasama ang {org}, mula sa numero ng telepono na naka-save"
        " sa email address na ito. Kung hindi ikaw iyon, maaari mo itong"
        " balewalain.",
        "Natanggap mo ang email na ito dahil may humiling nito habang may tawag"
        " sa telepono, mula sa numero ng telepono na naka-save sa email address"
        " na ito. Kung hindi ikaw iyon, maaari mo itong balewalain.",
    ),
    "tr": CallerEmailLines(
        "Telefon görüşmesi üzerine ({org})",
        "Telefon görüşmesi üzerine",
        "Bu e-posta, {org} ile yapılan bir telefon görüşmesi nedeniyle gönderildi.",
        "Bu e-posta, bir telefon görüşmesi nedeniyle gönderildi.",
        "Bu e-postayı, biri {org} ile yapılan bir telefon görüşmesi sırasında bu"
        " e-posta adresiyle kayıtlı telefon numarasından istediği için aldınız."
        " Bu siz değilseniz, e-postayı yok sayabilirsiniz.",
        "Bu e-postayı, biri bir telefon görüşmesi sırasında bu e-posta adresiyle"
        " kayıtlı telefon numarasından istediği için aldınız. Bu siz"
        " değilseniz, e-postayı yok sayabilirsiniz.",
    ),
    "uk": CallerEmailLines(
        "Після телефонного дзвінка з {org}",
        "Після телефонного дзвінка",
        "Цей лист надіслано після телефонного дзвінка з {org}.",
        "Цей лист надіслано після телефонного дзвінка.",
        "Ви отримали цей лист, бо хтось попросив його під час телефонного"
        " дзвінка з {org} з номера телефону, збереженого для цієї адреси"
        " електронної пошти. Якщо це були не ви, можете його проігнорувати.",
        "Ви отримали цей лист, бо хтось попросив його під час телефонного"
        " дзвінка з номера телефону, збереженого для цієї адреси електронної"
        " пошти. Якщо це були не ви, можете його проігнорувати.",
    ),
    "vi": CallerEmailLines(
        "Từ cuộc gọi điện thoại với {org}",
        "Từ một cuộc gọi điện thoại",
        "Email này được gửi vì một cuộc gọi điện thoại với {org}.",
        "Email này được gửi vì một cuộc gọi điện thoại.",
        "Bạn nhận được email này vì có người đã yêu cầu trong một cuộc gọi điện"
        " thoại với {org}, từ số điện thoại được lưu cùng địa chỉ email này. Nếu"
        " đó không phải là bạn, bạn có thể bỏ qua email này.",
        "Bạn nhận được email này vì có người đã yêu cầu trong một cuộc gọi điện"
        " thoại, từ số điện thoại được lưu cùng địa chỉ email này. Nếu đó không"
        " phải là bạn, bạn có thể bỏ qua email này.",
    ),
    "zh": CallerEmailLines(
        "来自与{org}的通话",
        "来自一次通话",
        "此邮件因与{org}的一次通话而发送。",
        "此邮件因一次通话而发送。",
        "您收到此邮件，是因为有人在与{org}的通话中，通过此邮箱地址关联的电话号码"
        "提出了请求。如果不是您本人，请忽略此邮件。",
        "您收到此邮件，是因为有人在一次通话中，通过此邮箱地址关联的电话号码"
        "提出了请求。如果不是您本人，请忽略此邮件。",
    ),
}


def caller_email_lines(language: str | None) -> CallerEmailLines:
    """The lines in ``language``, English when it is unknown."""
    return by_language(CALLER_EMAIL_LINES, language)
