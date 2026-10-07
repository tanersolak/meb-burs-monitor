# ABDİGM Duyuru → Telegram Botu

[abdigm.meb.gov.tr/www/duyurular/kategori/2](https://abdigm.meb.gov.tr/www/duyurular/kategori/2) sayfasındaki duyuruları günde 1 kez kontrol eder, yeni duyuru çıkarsa Telegram'dan mesaj atar.

## Nasıl çalışır?

- Duyuru listesi sayfada JavaScript ile yüklendiği için HTML'i kazımak yerine sitenin **RSS beslemesi** kullanılır (`rss_duyurular.xml`). Bu hem daha sağlam hem daha hafiftir.
- Beslemede `category=2` olanlar "Duyurular" sayfasına, `category=1` olanlar "Haberler"e karşılık gelir. Başka kategori istersen `CATEGORY` ortam değişkenini değiştir.
- Her duyurunun sitedeki benzersiz ID'si (`/icerik/2878` gibi) `state/seen.json` içinde tutulur. Listede olmayan ID = yeni duyuru.
- **İlk çalıştırmada** mevcut duyurular sessizce kaydedilir (eski duyurularla mesaj yağmuru olmaz) ve "bot aktif" mesajı gelir.
- Bir mesaj gönderilemezse o duyuru "görüldü" sayılmaz, ertesi gün tekrar denenir.

## Kurulum

### 1) Telegram botu oluştur
1. Telegram'da [@BotFather](https://t.me/BotFather) → `/newbot` → isim ve kullanıcı adı ver → **token**'ı al.
2. Oluşturduğun bota gidip **Start**'a bas ve herhangi bir mesaj yaz.
3. Tarayıcıda `https://api.telegram.org/bot<TOKEN>/getUpdates` adresini aç. Çıktıdaki `"chat":{"id": 123456789 ...}` değeri **chat id**'dir.
   - Grup/kanal için: botu gruba/kanala ekle (kanalda yönetici yap), aynı `getUpdates` ile (negatif, `-100...` ile başlayan) id'yi bul.

### 2) GitHub Actions ile çalıştır (sunucu gerekmez, ücretsiz)
1. Bu klasörü yeni bir **GitHub reposuna** yükle (private olabilir).
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Repo → **Settings → Actions → General → Workflow permissions** → "Read and write permissions" seç.
4. **Actions** sekmesi → "ABDİGM duyuru kontrolü" → **Run workflow** ile ilk çalıştırmayı elle yap. Telegram'a "bot aktif" mesajı gelmeli.
5. Bundan sonra her gün **09:00 (TR)** civarında otomatik çalışır. (GitHub zamanlayıcısı yoğun saatlerde 5–30 dk gecikebilir.)

> Her çalıştırmada `state/seen.json` içindeki `last_check` güncellenip commit'lendiği için, GitHub'ın "60 gün hareketsiz repoda zamanlayıcıyı durdurma" kuralına takılmazsın.

### Alternatif: Kendi bilgisayarında / sunucuda cron ile
Python 3.10+ yeterli, paket kurulumu gerekmez.

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="123456789"
python3 duyuru_bot.py            # ilk çalıştırma: mevcutları kaydeder
```

Crontab (`crontab -e`) — her gün 09:00:
```
0 9 * * * cd /yol/abdigm-duyuru-bot && TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... /usr/bin/python3 duyuru_bot.py >> bot.log 2>&1
```

## Test

```bash
# Telegram'a göndermeden, ne gönderileceğini gör
python3 duyuru_bot.py --dry-run

# Yerel örnek RSS ile test
STATE_FILE=/tmp/s.json python3 duyuru_bot.py --dry-run --feed-file tests/sample_feed.xml
```

Tekrar "ilk çalıştırma" davranışı istersen `state/seen.json` dosyasını sil.

## Sorun giderme

| Belirti | Çözüm |
|---|---|
| Actions'ta "Besleme alınamadı" | Site yurt dışı IP'lerini (GitHub sunucuları) engelliyor olabilir. Türkiye'deki bir bilgisayar/VPS/Raspberry Pi'de cron yöntemini kullan. |
| `Telegram hatası 400: chat not found` | Bota önce mesaj atmadın veya chat id yanlış. |
| `Telegram hatası 401` | Token hatalı. |
| Actions `git push` hatası | Workflow permissions "Read and write" olmalı (Kurulum adım 3). |
| Başka kategori de gelsin | `CATEGORY` değişkenini değiştir (ör. Haberler için `1`). |
