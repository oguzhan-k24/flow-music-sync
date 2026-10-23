# Slack #music → Spotify playlist

Bu otomasyon her 10 dakikada bir #music kanalını kontrol eder. Yeni Spotify linklerini playliste ekler ve işlediği mesaja emoji bırakır: ✅ eklendi, 🔁 zaten playlistteydi, ❔ YouTube'daki şarkı Spotify'da bulunamadı. YouTube linklerinde videonun başlığını okur ve şarkıyı Spotify'da arar. Sanatçı ve şarkı adı yeterince eşleşirse ekler. Aynı şarkı asla iki kez eklenmez. Albüm linklerinde albümün tüm şarkıları, thread yanıtlarındaki linkler de dahil edilir. Kurulum bir kerelik ve yaklaşık 20-30 dakika sürer.

Kurulum boyunca 6 bilgi toplayacaksın. Bunları bir not dosyasına yaz, en sonda GitHub'a gireceksin:
`SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN`, `SPOTIFY_PLAYLIST_ID`, `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`

---

## 1. Spotify uygulaması oluştur

1. https://developer.spotify.com/dashboard adresine git ve playlistin sahibi olan Spotify hesabıyla giriş yap.
2. **Create app**'e tıkla. İsim ve açıklama alanlarına istediğini yaz.
3. **Redirect URI** alanına tam olarak şunu yaz: `http://127.0.0.1:8888/callback` (`localhost` değil, `127.0.0.1` olmalı).
4. "Which API/SDKs" kısmında **Web API**'yi işaretle ve kaydet.
5. Uygulamanın **Settings** sayfasından **Client ID** ve **Client Secret** değerlerini kopyala.

## 2. Spotify refresh token'ını al (tek sefer)

Bu adım Spotify'a "bu program playlistime şarkı ekleyebilir" iznini verir. Bir kez yaparsın, sonra bir daha giriş gerekmez.

Kendi bilgisayarında, bilgisayarında Python yüklü olmalı, bu klasörde bir terminal aç:

```
pip install requests
python get_refresh_token.py
```

Client ID ve Secret'ı yapıştır. Tarayıcı açılınca giriş yapıp izin ver. Terminalde çıkan uzun yazı senin `SPOTIFY_REFRESH_TOKEN`'ın.

## 3. Playlist ID'sini bul

Spotify'da playliste sağ tıkla → **Paylaş** → **Bağlantıyı kopyala**. Link şuna benzer:
`https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=...`
`playlist/` ile `?` arasındaki kısım (`37i9dQZF1DXcBWIGoYBM5M`) `SPOTIFY_PLAYLIST_ID`'dir. Playlist, 2. adımda giriş yaptığın hesaba ait olmalı.

## 4. Slack uygulaması oluştur

1. https://api.slack.com/apps adresine git → **Create New App** → **From a manifest** → çalışma alanını seç.
2. YAML sekmesine aşağıdakini yapıştır ve oluştur:

```yaml
display_information:
  name: Music Sync
features:
  bot_user:
    display_name: music-sync
    always_online: false
oauth_config:
  scopes:
    bot:
      - channels:history
      - groups:history
      - reactions:read
      - reactions:write
settings:
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

3. **Install to Workspace** → izin ver. Workspace admini değilsen bu adımda admin onayı gerekebilir.
4. **OAuth & Permissions** sayfasındaki **Bot User OAuth Token**'ı (`xoxb-` ile başlar) kopyala. Bu senin `SLACK_BOT_TOKEN`'ın.
5. Slack'te #music kanalına şunu yaz: `/invite @music-sync`
6. Kanal adına tıkla. Açılan pencerenin en altındaki **Channel ID** (`C` ile başlar) senin `SLACK_CHANNEL_ID`'n.

## 5. GitHub'a yükle ve çalıştır

1. https://github.com adresinde hesap aç veya giriş yap → **New repository**. İsim ver ve **Public** seç (nedeni aşağıda) → **Create repository**.
2. **uploading an existing file** linkine tıkla. `sync.py`, `get_refresh_token.py`, `requirements.txt` ve `README.md`'yi sürükle → **Commit changes**.
3. Workflow dosyası için: **Add file → Create new file**. Dosya adına tam olarak `.github/workflows/sync.yml` yaz. `sync.yml` dosyasının içeriğini kopyalayıp yapıştır → **Commit changes**. (`.github` gizli klasör olduğu için sürükle-bırakta bazen görünmez. Bu yüzden elle oluşturmak en garantisi.)
4. Repo'da **Settings → Secrets and variables → Actions → New repository secret** kısmına git. 6 bilgiyi tek tek, yukarıdaki isimlerle ekle.
5. **Actions** sekmesine git ve istenirse workflow'ları etkinleştir.
6. **Slack → Spotify → Run workflow**'a tıkla. Kanaldaki eski linkleri de almak istiyorsan "Kaç saat geriye" kutusuna `8760` (1 yıl) gibi büyük bir sayı yaz. Sonra çalıştır.
7. Yeşil tik görürsen tamam. Kanalda ✅ emojilerini ve playlistte şarkıları görmelisin. Bundan sonra sistem kendiliğinden 10 dakikada bir çalışır.

---

## Notlar

**Neden public repo?** Public repolarda GitHub Actions süresi sınırsız ve ücretsiz. Private repolarda aylık 2000 dakika ücretsiz kota var, 10 dakikada bir çalışma bunu aşar. Secret'lar public repoda da gizlidir, kimse göremez. Kodda gizli bir bilgi yok. Yine de private istersen `sync.yml` içindeki `*/10` değerini `*/30` yap, kota içinde kalırsın.

**"Uyanık tut" adımı:** GitHub, 60 gün commit olmayan public repolarda zamanlanmış işleri durdurur. Workflow, 45 günde bir boş commit atarak bunu engeller.

**YouTube eşleştirme:** Başlık "Sanatçı - Şarkı (Official Video)" gibi düzenliyse eşleşme neredeyse her zaman doğrudur. "Official Video", "Lyrics", "4K" gibi ekler ve "ft." kısımları temizlenir. Videoda "live", "remix" veya "cover" yazmıyorsa bot bu versiyonları seçmez. Sanatçı ya da şarkı adı uymuyorsa bot tahmin yürütmez, şarkıyı eklemez ve ❔ bırakır. Hangi videonun hangi şarkıyla eşleştiği Actions loglarında görünür.

**Ayarlar** (`sync.yml` içinde): `YOUTUBE_LOOKUP: "false"` yaparsan YouTube linkleri yok sayılır. `ADD_ALBUMS: "false"` yaparsan albüm linkleri atlanır. `INCLUDE_THREADS: "false"` yaparsan sadece ana mesajlara bakılır. Playlist linkleri her zaman atlanır.

**Bir şarkıyı tekrar işletmek istersen:** O mesajdaki botun ✅/🔁/❔ emojisini kaldıramazsın, bot eklemiştir. Ama şarkıyı playlistten silip mesaja tekrar link atabilirsin.

**Sorun olursa:** Actions sekmesinde kırmızı çarpılı çalışmaya tıkla. "Linkleri playliste ekle" adımındaki hata mesajı ne olduğunu Türkçe söyler (eksik secret, yanlış ID, kanala davet edilmemiş bot gibi). Spotify geliştirici kuralları zaman zaman değişebiliyor. Spotify kaynaklı bir hata görürsen mesajı bana yapıştırman yeterli.
