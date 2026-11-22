# راهنمای استقرار AngelDl

## ۱. پیش‌نیازها

- سرور لینوکس (اوبونتو ۲۰.۰۴ به بالا) با دسترسی root/SSH
- Python 3.10+ و ffmpeg (یا `imageio-ffmpeg` که در `requirements.txt` هست)
- ربات ساخته‌شده در [@BotFather](https://t.me/BotFather) و توکن آن
- ربات باید **ادمین کانال عضویت اجباری** باشد (دسترسی «مدیریت اعضا» لازم نیست؛
  فقط ادمین‌بودن کافی است تا `get_chat_member` کار کند)

## ۲. نصب

```bash
git clone <repo-url> /root/dlbot && cd /root/dlbot
apt update && apt install -y python3-venv ffmpeg
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env
bash scripts/set-token.sh        # در ترمینال از شما می‌پرسد: توکن ربات، آیدی عددی تلگرام ادمین، و کانال عضویت اجباری (پیش‌فرض @ange1Vpn) — همه در .env ذخیره می‌شوند (chmod 600)
```

## ۳. اجرا با systemd (پیشنهادی)

```bash
# مسیرها را در فایل‌های systemd/ با مسیر واقعی سرور چک کنید (/root/dlbot)
sudo cp systemd/dlbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dlbot
sudo journalctl -u dlbot -f     # باید «bot v3 started ... cap=49MB» را نشان بدهد
```

بدون systemd:

```bash
bash scripts/run.sh start       # وضعیت: bash scripts/run.sh status
```

## ۴. سقف ۲ گیگابایت (سرور محلی Bot API — اختیاری)

تلگرام برای ربات‌ها سقف ارسال ۵۰ مگابایت دارد. با سرور محلی Bot API سقف به
۲۰۰۰ مگابایت می‌رسد:

1. در [my.telegram.org/apps](https://my.telegram.org/apps) یک اپ بسازید و
   `api_id` و `api_hash` را بردارید (هر اکانتی می‌شود؛ لازم نیست اکانت اصلی باشد).
2. باینری آماده را بگیرید (نیازی به کامپایل نیست):
   [SwissCore92/telegram-bot-api-bin](https://github.com/SwissCore92/telegram-bot-api-bin/releases)
   — فایل `telegram-bot-api-vX.Y-linux-amd64.tar.gz` را دانلود و SHA256 آن را
   با فایل `.sha256` همان ریلیز چک کنید، بعد در `/usr/local/bin/telegram-bot-api` بگذارید.
3. مقادیر را وارد کنید:

   ```bash
   bash scripts/set-api.sh      # api_id و api_hash را تایپ می‌کنید؛ در api.env ذخیره می‌شود
   ```

4. سرویس را فعال کنید و ربات را به آن وصل کنید:

   ```bash
   sudo mkdir -p /var/lib/telegram-bot-api
   sudo cp systemd/telegram-bot-api.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now telegram-bot-api
   echo "LOCAL_BOT_API=http://127.0.0.1:8081" >> .env
   sudo systemctl restart dlbot
   ```

5. در لاگ باید ببینید: `local_api=True cap=2000MB`

> **مهم:** قبل از وصل‌کردن ربات به سرور محلی، ربات باید از API ابری خارج شود
> (`logOut`)، وگرنه دو نشست `getUpdates` با هم تداخل می‌کنند. با یک بار
> ری‌استارت بعد از تنظیم `LOCAL_BOT_API` این کار انجام می‌شود؛ اگر خطای
> `Conflict` دیدید، یک بار دیگر سرویس را ری‌استارت کنید.

## ۵. انتقال به سرور جدید / بازیابی

1. همین ریپو را کلون کنید و مرحله‌های ۲ و ۳ را انجام بدهید.
2. سه فایل را از سرور قبلی بیاورید:
   - `.env` و `api.env` — یا با `scripts/set-token.sh` و `scripts/set-api.sh`
     از اول بسازیدشان.
   - `data.db` — کاربران، پریمیوم و تنظیمات؛ بدون آن همه‌چیز از صفر شروع می‌شود.
3. سرویس‌ها را استارت کنید و لاگ را چک کنید.

## ۶. عیب‌یابی

| علامت | علت محتمل |
|---|---|
| همه پشت قفل عضویت می‌مانند | ربات ادمین کانال نیست |
| `Conflict: terminated by other getUpdates` | دو نسخه از ربات هم‌زمان در حال اجراست؛ نسخه‌ی اضافه را متوقف کنید |
| `cap=49MB` با وجود سرور محلی | `LOCAL_BOT_API` در `.env` نیست یا سرویس `telegram-bot-api` بالا نیست |
| دانلودها خطا می‌دهند | `ffmpeg` نصب نیست؛ `apt install ffmpeg` یا `imageio-ffmpeg` را چک کنید |
