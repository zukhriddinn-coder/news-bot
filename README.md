# Trading Laboratory — News bot

Bu bot har ~10 daqiqada moliyaviy yangilik saytlarini tekshiradi va yangi yangiliklarni guruhingizdagi **News** bo'limiga qisqa post qilib yuboradi. Postning tagida manba havolasi bo'ladi.

Hammasi **bepul**: dastur GitHub'ning serverlarida ishlaydi, sizning kompyuteringiz yoqiq turishi shart emas.

Post namunasi:

> **Fed holds rates steady, signals caution on inflation**
>
> The Federal Reserve kept its benchmark rate unchanged on Wednesday…
>
> #Fed #Macro
> 🔗 Source: Federal Reserve

---

## 1-qadam. Telegram bot yaratish (3 daqiqa)

1. Telegramda **@BotFather** ni oching va **Start** ni bosing.
2. `/newbot` deb yozing.
3. Botga nom bering (masalan: `Trading Lab News`).
4. Username bering, oxiri `bot` bilan tugashi kerak (masalan: `tradinglab_news_bot`).
5. BotFather sizga **token** beradi. U shunga o'xshaydi:
   `7123456789:AAH4k...xyz`
   Uni nusxalab, xavfsiz joyga saqlab qo'ying. **Tokenni hech kimga bermang.**

## 2-qadam. Botni guruhga qo'shish

1. **Trading Laboratory** guruhini oching → guruh nomiga bosing → **Administrators** (Adminlar) → **Add Admin**.
2. Botingizning username'ini qidirib toping va admin qiling.
3. Huquqlardan kamida **Send messages / Post messages** yoqilgan bo'lsin. Qolganlarini o'chirib qo'yishingiz mumkin.

## 3-qadam. Guruh ID va News bo'limi ID'sini topish

1. Telegramda **News** bo'limining o'ziga (ro'yxatda) bosib turing yoki o'ng tugma bosing → **Copy link** (Havolani nusxalash).
2. Havola quyidagi ko'rinishlardan birida bo'ladi:

| Havola | Guruh ID (CHAT_ID) | News ID (THREAD_ID) |
|---|---|---|
| `https://t.me/c/1987654321/45` | `-1001987654321` | `45` |
| `https://t.me/c/1987654321/45/678` | `-1001987654321` | `45` |
| `https://t.me/tradinglab/45` (ochiq guruh) | `@tradinglab` | `45` |

**Qoida:**
- **CHAT_ID** — `c/` dan keyingi raqam, oldiga `-100` qo'shiladi. Agar guruh ochiq bo'lsa, `@` bilan guruh username'i.
- **THREAD_ID** — guruh raqamidan keyin keladigan **birinchi** raqam.

## 4-qadam. GitHub'da loyiha ochish

1. **github.com** ga kiring va bepul akkaunt oching (bor bo'lsa, kiring).
2. O'ng yuqoridagi **+** → **New repository**.
3. Nomi: `news-bot`. **Public** ni tanlang (Public bo'lsa GitHub cheksiz bepul ishlatadi. Token va ID'lar baribir yashirin saqlanadi, hech kim ko'rmaydi).
4. **Create repository** ni bosing.
5. Ochilgan sahifada **uploading an existing file** havolasini bosing va quyidagi 3 ta faylni sudrab tashlang:
   - `bot.py`
   - `requirements.txt`
   - `README.md`
   So'ng pastdagi **Commit changes** ni bosing.
6. Endi yana **Add file** → **Create new file** ni bosing.
   - Fayl nomi joyiga aynan shuni yozing: `.github/workflows/news.yml`
     (`/` belgisini yozganingizda papkalar o'zi yaratiladi)
   - Pastdagi katta oynaga `news.yml` faylining ichidagi hamma matnni nusxalab qo'ying.
   - **Commit changes** ni bosing.

## 5-qadam. Token va ID'larni yashirin saqlash

1. Repository'da **Settings** → chap menyuda **Secrets and variables** → **Actions**.
2. **New repository secret** tugmasi orqali 3 ta secret qo'shing:

| Name | Secret |
|---|---|
| `BOT_TOKEN` | BotFather bergan token |
| `CHAT_ID` | masalan `-1001987654321` |
| `THREAD_ID` | masalan `45` |

Nomlarni aynan shunday, katta harflarda yozing.

## 6-qadam. Ishga tushirish

1. Repository'da yuqoridagi **Actions** bo'limiga o'ting. Agar so'rasa, **I understand my workflows, go ahead and enable them** ni bosing.
2. Chapdan **News to Telegram** ni tanlang → o'ngdagi **Run workflow** → yana **Run workflow**.
3. 1 daqiqa kuting. News bo'limida **"✅ News bot connected."** xabari chiqsa — hammasi ishlayapti!

**Birinchi ishga tushishda** bot hozir mavjud yangiliklarni faqat eslab qoladi va ularni chiqarmaydi (aks holda bo'limga birdan 100 ta post tushardi). Shundan keyin chiqqan har bir yangi yangilik avtomatik post bo'ladi.

Shundan keyin hech narsa qilish shart emas — bot har ~10 daqiqada o'zi ishlaydi.

---

## Sozlamalar (ixtiyoriy)

`bot.py` faylini GitHub'da ochib, qalamcha ✏️ belgisini bosib o'zgartirishingiz mumkin:

- **Manbalar** — `FEEDS` ro'yxati. Yangi manba qo'shish uchun RSS havolasini xuddi shu ko'rinishda qo'shing:
  ```python
  {"name": "Manba nomi", "url": "https://.../rss", "tag": "#Heshteg"},
  ```
  Keraksiz manbani o'chirish uchun uning qatorini o'chiring.
- `MAX_POSTS_PER_RUN = 8` — bir tekshiruvda ko'pi bilan nechta post chiqadi (qolganlari keyingi safar chiqadi).
- `SUMMARY_LENGTH = 280` — qisqa izoh uzunligi (belgilarda).
- `MAX_AGE_HOURS = 12` — shundan eski yangiliklar chiqarilmaydi.

Tekshirish tezligi `news.yml` dagi `*/10` qismida (10 daqiqa). Uni `*/5` ga o'zgartirish mumkin, bu GitHub ruxsat bergan eng tez qiymat.

## Bilib qo'yish kerak

- GitHub jadvalni ba'zan bir necha daqiqa kechiktiradi, shuning uchun yangilik odatda **chiqqanidan 10–20 daqiqa ichida** guruhda paydo bo'ladi.
- Bot har safar `seen.json` faylini yangilaydi — u qaysi yangiliklar chiqarilganini eslab qoladi. Bu faylni o'chirmang.
- Agar GitHub bir kun "workflow disabled" degan xat yuborsa, **Actions** → **News to Telegram** → **Enable workflow** ni bosing.

## Muammo bo'lsa

**Actions** bo'limida qizil ❌ belgili ishga tushirishni bosing → **post-news** → **Post news** qadamini oching. U yerdagi xato matniga qarang:

| Xato | Sababi va yechimi |
|---|---|
| `Unauthorized` | Token noto'g'ri. `BOT_TOKEN` secret'ini qayta kiriting. |
| `chat not found` | `CHAT_ID` noto'g'ri yoki bot guruhga qo'shilmagan. `-100` qo'shilganini tekshiring. |
| `message thread not found` | `THREAD_ID` noto'g'ri. 3-qadamni qayta bajaring. |
| `not enough rights` | Botga admin huquqi berilmagan (2-qadam). |
| `WARNING: could not read ...` | O'sha manba vaqtincha ishlamayapti. Bot uni o'tkazib yuborib, qolganlaridan davom etadi. |
