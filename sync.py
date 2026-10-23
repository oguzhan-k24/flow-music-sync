#!/usr/bin/env python3
"""
Slack kanalına atılan Spotify linklerini bir Spotify playlistine ekler.

Durum (hangi mesaj işlendi) Slack'in kendisinde tutulur: bot, işlediği her
mesaja emoji bırakır ve bir sonraki çalışmada o mesajları atlar.
  ✅  yeni şarkı(lar) playliste eklendi
  🔁  link geçerli ama şarkı zaten playlistte vardı
  ❔  YouTube / Apple Music linkindeki şarkı Spotify'da güvenle bulunamadı
YouTube linklerinde videonun başlığı, Apple Music linklerinde Apple'ın
şarkı/albüm bilgisi okunur; Spotify'da aranır ve sadece sanatçı + ad
yeterince eşleşirse eklenir.
Ayrıca playlistin mevcut içeriği her çalışmada okunur, yani aynı şarkı
asla iki kez eklenmez.
"""
import os
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher

import requests

SPOTIFY_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[A-Za-z-]+/)?"
    r"(track|album|playlist|episode)/([A-Za-z0-9]{22})"
)
SHORT_RE = re.compile(r"https?://spotify\.link/[A-Za-z0-9]+")
YOUTUBE_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?\S*?v=|shorts/|live/|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)
APPLE_RE = re.compile(
    r"https?://(?:geo\.)?music\.apple\.com/([a-z]{2})/(album|song)/"
    r"(?:[^/\s?|>]+/)?(?:id)?(\d+)(?:\?[^\s|>]*?\bi=(\d+))?"
)

REACT_ADDED = "white_check_mark"
REACT_DUPLICATE = "repeat"
REACT_NOT_FOUND = "grey_question"
HANDLED = (REACT_ADDED, REACT_DUPLICATE, REACT_NOT_FOUND)
MATCH_THRESHOLD = 0.75


def env(name, default=None):
    # Kopyalarken kaçan boşluk, satır sonu ve tırnakları temizle
    value = (os.environ.get(name) or default or "").strip().strip('"').strip("'").strip()
    if not value:
        sys.exit(f"Eksik ayar: {name} (GitHub Secrets'a eklendi mi?)")
    return value


def env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "evet")


# ---------------------------------------------------------------- Slack

class Slack:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def call(self, method, post=False, ok_errors=(), **params):
        url = f"https://slack.com/api/{method}"
        for _ in range(6):
            if post:
                r = self.s.post(url, data=params, timeout=30)
            else:
                r = self.s.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
                continue
            data = r.json()
            if not data.get("ok") and data.get("error") not in ok_errors:
                raise RuntimeError(f"Slack {method} hatası: {data.get('error')}")
            return data
        raise RuntimeError(f"Slack {method}: çok fazla rate-limit")

    def history(self, channel, oldest, include_threads):
        messages, cursor = [], None
        while True:
            params = {"channel": channel, "oldest": f"{oldest:.6f}", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self.call("conversations.history", **params)
            for m in data.get("messages", []):
                messages.append(m)
                if include_threads and m.get("reply_count"):
                    messages.extend(self.replies(channel, m["ts"], oldest))
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return messages

    def replies(self, channel, thread_ts, oldest):
        out, cursor = [], None
        while True:
            params = {"channel": channel, "ts": thread_ts, "oldest": f"{oldest:.6f}", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self.call("conversations.replies", **params)
            out.extend(m for m in data.get("messages", []) if m.get("ts") != thread_ts)
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return out

    def react(self, channel, ts, name):
        self.call("reactions.add", post=True, ok_errors=("already_reacted",),
                  channel=channel, timestamp=ts, name=name)


def already_handled(message, bot_user_id):
    for reaction in message.get("reactions", []):
        if reaction.get("name") in HANDLED and bot_user_id in reaction.get("users", []):
            return True
    return False


def message_text(message):
    parts = [message.get("text", "")]
    for att in message.get("attachments", []) or []:
        parts += [att.get("from_url", ""), att.get("original_url", ""), att.get("title_link", "")]
    return "\n".join(p for p in parts if p)


def resolve_short_link(url):
    """spotify.link/xxxx kısa linklerini gerçek open.spotify.com linkine çevirir."""
    try:
        r = requests.get(url, allow_redirects=True, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        match = SPOTIFY_RE.search(r.url) or SPOTIFY_RE.search(r.text)
        return match.group(0) if match else None
    except requests.RequestException:
        return None


def extract_refs(text):
    refs = [(m.group(1), m.group(2)) for m in SPOTIFY_RE.finditer(text)]
    for short in SHORT_RE.findall(text):
        full = resolve_short_link(short)
        if full:
            m = SPOTIFY_RE.search(full)
            refs.append((m.group(1), m.group(2)))
    return list(dict.fromkeys(refs))  # sırayı koruyarak tekrarları at


# -------------------------------------------------------------- YouTube

NOISE_BRACKETS = re.compile(
    r"[\(\[\{【][^\)\]\}】]*(official|video|audio|lyric|visuali[sz]er|clip|klip|"
    r"\bhd\b|\bhq\b|4k|\bmv\b|m/v|söz|resmi|full)[^\)\]\}】]*[\)\]\}】]",
    re.I,
)
NOISE_WORDS = re.compile(
    r"\b(official\s+(music\s+)?video|official\s+audio|official\s+lyric\s+video|"
    r"lyric\s+video|lyrics?|video\s*clip|music\s+video|klip|şarkı\s+sözleri)\b",
    re.I,
)
FEAT = re.compile(r"\s*[\(\[]?\s*\b(ft|feat|featuring)\b\.?\s.*$", re.I)
VERSION_WORDS = ("live", "canli", "remix", "cover", "karaoke", "instrumental",
                 "acoustic", "akustik", "sped up", "slowed", "nightcore", "8d")


def norm(text):
    text = (text or "").replace("ı", "i").replace("İ", "i")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def ratio(a, b):
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def youtube_info(video_id, message):
    """Video başlığı ve kanal adı. Önce YouTube oEmbed, olmazsa Slack önizlemesi."""
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("title", ""), data.get("author_name", "")
    except requests.RequestException:
        pass
    for att in message.get("attachments", []) or []:
        if video_id in (att.get("from_url", "") + att.get("original_url", "")):
            return att.get("title", ""), att.get("author_name", "")
    return "", ""


def parse_youtube_title(title, channel):
    """'Sanatçı - Şarkı (Official Video)' -> ('Sanatçı', 'Şarkı')."""
    t = NOISE_BRACKETS.sub(" ", title)
    t = NOISE_WORDS.sub(" ", t)
    t = re.sub(r"#\S+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -|–—")
    channel = re.sub(r"\s*-\s*Topic$|VEVO$|\s+Official$|\s+Music$", "", channel or "", flags=re.I).strip()

    parts = re.split(r"\s+[-–—|]\s+|\s*[\"“”]\s*", t, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        artist, song = parts[0], parts[1]
    else:
        artist, song = channel, t
    song = FEAT.sub("", song).strip(" -|\"“”")
    artist = FEAT.sub("", artist).strip(" -|\"“”")
    return artist, song, channel


# ---------------------------------------------------------- Apple Music

def clean_name(name):
    name = FEAT.sub("", name or "")
    name = re.sub(r"\s*[\(\[][^\)\]]*remaster[^\)\]]*[\)\]]", "", name, flags=re.I)
    name = re.sub(r"\s+-\s+(Single|EP)$", "", name, flags=re.I)
    return name.strip()


def apple_info(country, kind, item_id, track_id):
    """Apple Music linkinden {'type': 'track'|'album', 'artist', 'name'}."""
    lookup_id = track_id or item_id
    is_track = bool(track_id) or kind == "song"
    try:
        r = requests.get("https://itunes.apple.com/lookup",
                         params={"id": lookup_id, "country": country}, timeout=15)
        results = r.json().get("results", []) if r.status_code == 200 else []
    except (requests.RequestException, ValueError):
        return None
    if not results:
        return None
    x = results[0]
    if is_track and x.get("trackName"):
        return {"type": "track", "artist": x.get("artistName", ""), "name": x["trackName"]}
    if x.get("collectionName"):
        return {"type": "album", "artist": x.get("artistName", ""), "name": x["collectionName"]}
    return None


# -------------------------------------------------------------- Spotify

class Spotify:
    API = "https://api.spotify.com/v1"

    def __init__(self, client_id, client_secret, refresh_token):
        r = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(client_id, client_secret), timeout=30,
        )
        if r.status_code != 200:
            print(f"Spotify girişi başarısız ({r.status_code}): {r.text}")
            # Teşhis: sadece ID+Secret ile giriş dene, token'ı devre dışı bırak
            test = requests.post("https://accounts.spotify.com/api/token",
                                 data={"grant_type": "client_credentials"},
                                 auth=(client_id, client_secret), timeout=30)
            print(f"  SPOTIFY_CLIENT_ID uzunluğu: {len(client_id)} (32 olmalı), "
                  f"başı: {client_id[:4]}...")
            print(f"  SPOTIFY_CLIENT_SECRET uzunluğu: {len(client_secret)} (32 olmalı)")
            if client_id == client_secret:
                sys.exit("SONUÇ: ID ve Secret aynı girilmiş. Secret'a Client Secret'ı koy.")
            if test.status_code == 200:
                sys.exit("SONUÇ: Client ID ve Secret DOĞRU. Sorun SPOTIFY_REFRESH_TOKEN'da, "
                         "yeni token almak gerekiyor.")
            sys.exit("SONUÇ: Client ID ve Secret ikilisi yanlış. Spotify panelindeki "
                     "güncel değerlerle karşılaştır (secret yenilendiyse yenisini gir).")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        self._items_suffix = None

    def req(self, method, url, **kw):
        if not url.startswith("http"):
            url = self.API + url
        for _ in range(6):
            r = self.s.request(method, url, timeout=30, **kw)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
                continue
            return r
        raise RuntimeError("Spotify: çok fazla rate-limit")

    def _items_path(self, playlist_id):
        # Spotify playlist uç noktasının adı "/items" ya da eski "/tracks" olabilir;
        # hangisi çalışıyorsa onu kullan.
        if self._items_suffix is None:
            for suffix in ("items", "tracks"):
                r = self.req("GET", f"/playlists/{playlist_id}/{suffix}", params={"limit": 1})
                if r.status_code == 200:
                    self._items_suffix = suffix
                    break
            else:
                sys.exit(f"Playliste erişilemedi ({r.status_code}): {r.text}\n"
                         "Playlist ID doğru mu, playlist bu Spotify hesabına mı ait?")
        return f"/playlists/{playlist_id}/{self._items_suffix}"

    def playlist_uris(self, playlist_id):
        uris = set()
        url = self._items_path(playlist_id)
        params = {"limit": 50}
        while url:
            r = self.req("GET", url, params=params)
            r.raise_for_status()
            data = r.json()
            for row in data.get("items", []):
                obj = row.get("item") or row.get("track") or {}
                if obj.get("uri"):
                    uris.add(obj["uri"])
            url, params = data.get("next"), None
        return uris

    def add(self, playlist_id, uris):
        path = self._items_path(playlist_id)
        for i in range(0, len(uris), 100):
            r = self.req("POST", path, json={"uris": uris[i:i + 100]})
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Playliste eklenemedi ({r.status_code}): {r.text}")

    def search_tracks(self, query):
        r = self.req("GET", "/search", params={"q": query, "type": "track", "limit": 10,
                                                "market": "from_token"})
        if r.status_code != 200:
            print(f"  ! Spotify araması başarısız ({r.status_code}): {query}")
            return []
        return r.json().get("tracks", {}).get("items", []) or []

    def best_track(self, artist, song, context):
        """Sanatçı + şarkı adına en iyi uyan Spotify şarkısı; emin değilse None."""
        if not song:
            return None, 0.0
        n_song, n_artist = norm(song), norm(artist)
        context = norm(context)

        candidates = {}
        for query in (f"track:{song} artist:{artist}" if artist else None, f"{artist} {song}".strip()):
            if query:
                for t in self.search_tracks(query):
                    candidates.setdefault(t["id"], t)

        best, best_score = None, 0.0
        for t in candidates.values():
            full_name = norm(t.get("name"))
            base_name = norm(re.sub(r"\s+-\s+.*$|\s*[\(\[](feat|with|ft)\b[^\)\]]*[\)\]]",
                                    "", t.get("name", ""), flags=re.I))
            s_song = ratio(base_name, n_song)
            if base_name and f" {base_name} " in f" {context} ":
                s_song = max(s_song, 0.9)

            s_artist = 0.0
            for a in t.get("artists", []):
                na = norm(a.get("name"))
                s_artist = max(s_artist, ratio(na, n_artist))
                if na and f" {na} " in f" {context} ":
                    s_artist = max(s_artist, 0.95)

            score = min(s_song, s_artist)
            # Kaynak "live/remix/cover" demiyorsa o versiyonları tercih etme
            for word in VERSION_WORDS:
                if word in full_name and word not in context:
                    score -= 0.3
            score += (t.get("popularity") or 0) / 10000  # eşitlikte popüler olanı seç
            if score > best_score:
                best, best_score = t, score

        if best_score >= MATCH_THRESHOLD:
            return best, best_score
        return None, best_score

    def find_youtube_match(self, title, channel):
        artist, song, channel = parse_youtube_title(title, channel)
        return self.best_track(artist, song, f"{title} {channel}")

    def best_album(self, artist, album):
        r = self.req("GET", "/search", params={"q": f"album:{album} artist:{artist}",
                                                "type": "album", "limit": 10, "market": "from_token"})
        items = r.json().get("albums", {}).get("items", []) if r.status_code == 200 else []
        n_album, n_artist = norm(album), norm(artist)
        best, best_score = None, 0.0
        for a in items or []:
            s_name = ratio(norm(a.get("name")), n_album)
            s_artist = max((ratio(norm(x.get("name")), n_artist) for x in a.get("artists", [])), default=0)
            score = min(s_name, s_artist)
            if score > best_score:
                best, best_score = a, score
        if best_score >= MATCH_THRESHOLD:
            return best, best_score
        return None, best_score

    def album_track_uris(self, album_id):
        uris, url, params = [], f"/albums/{album_id}/tracks", {"limit": 50}
        while url:
            r = self.req("GET", url, params=params)
            if r.status_code != 200:
                print(f"  ! Albüm okunamadı {album_id}: {r.status_code}")
                return uris
            data = r.json()
            uris += [t["uri"] for t in data.get("items", []) if t.get("uri")]
            url, params = data.get("next"), None
        return uris


# ----------------------------------------------------------------- main

def main():
    slack = Slack(env("SLACK_BOT_TOKEN"))
    channel = env("SLACK_CHANNEL_ID")
    spotify = Spotify(env("SPOTIFY_CLIENT_ID"), env("SPOTIFY_CLIENT_SECRET"), env("SPOTIFY_REFRESH_TOKEN"))
    playlist_id = env("SPOTIFY_PLAYLIST_ID")

    lookback_hours = float(os.environ.get("LOOKBACK_HOURS") or 48)
    add_albums = env_bool("ADD_ALBUMS", True)
    include_threads = env_bool("INCLUDE_THREADS", True)
    youtube_lookup = env_bool("YOUTUBE_LOOKUP", True)
    apple_lookup = env_bool("APPLE_LOOKUP", True)
    dry_run = env_bool("DRY_RUN", False)

    bot_user_id = slack.call("auth.test")["user_id"]
    oldest = time.time() - lookback_hours * 3600
    messages = sorted(slack.history(channel, oldest, include_threads), key=lambda m: float(m["ts"]))
    existing = spotify.playlist_uris(playlist_id)
    print(f"{len(messages)} mesaj tarandı (son {lookback_hours:g} saat), playlistte {len(existing)} öğe var.")

    added_total, errors = 0, 0
    for m in messages:
        if already_handled(m, bot_user_id):
            continue
        text = message_text(m)
        refs = extract_refs(text)
        yt_ids = list(dict.fromkeys(YOUTUBE_RE.findall(text))) if youtube_lookup else []
        apple_refs = list(dict.fromkeys(APPLE_RE.findall(text))) if apple_lookup else []
        if not refs and not yt_ids and not apple_refs:
            continue

        uris = []
        for kind, item_id in refs:
            if kind in ("track", "episode"):
                uris.append(f"spotify:{kind}:{item_id}")
            elif kind == "album" and add_albums:
                uris += spotify.album_track_uris(item_id)
            # playlist linkleri bilinçli olarak atlanır

        yt_missing = 0
        for vid in yt_ids:
            title, channel = youtube_info(vid, m)
            track, score = spotify.find_youtube_match(title, channel) if title else (None, 0.0)
            if track:
                artists = ", ".join(a["name"] for a in track.get("artists", []))
                print(f"  ▶ YouTube '{title}' → Spotify '{artists} - {track['name']}' ({score:.2f})")
                uris.append(track["uri"])
            else:
                yt_missing += 1
                print(f"  ? YouTube '{title or vid}' Spotify'da güvenle bulunamadı ({score:.2f})")

        for country, kind, item_id, track_id in apple_refs:
            info = apple_info(country, kind, item_id, track_id)
            if not info:
                yt_missing += 1
                print(f"  ? Apple Music {item_id} bilgisi okunamadı")
                continue
            label = f"{info['artist']} - {info['name']}"
            if info["type"] == "track":
                track, score = spotify.best_track(info["artist"], clean_name(info["name"]), label)
                if track:
                    artists = ", ".join(a["name"] for a in track.get("artists", []))
                    print(f"  ▶ Apple '{label}' → Spotify '{artists} - {track['name']}' ({score:.2f})")
                    uris.append(track["uri"])
                    continue
            elif add_albums:
                album, score = spotify.best_album(info["artist"], clean_name(info["name"]))
                if album:
                    print(f"  ▶ Apple albüm '{label}' → Spotify albüm '{album['name']}' ({score:.2f})")
                    uris += spotify.album_track_uris(album["id"])
                    continue
            else:
                continue  # albümler kapalı
            yt_missing += 1
            print(f"  ? Apple '{label}' Spotify'da güvenle bulunamadı ({score:.2f})")

        if not uris:
            if yt_missing and not dry_run:
                slack.react(channel, m["ts"], REACT_NOT_FOUND)
            continue

        new = [u for u in dict.fromkeys(uris) if u not in existing]
        try:
            if dry_run:
                print(f"  [deneme] {m['ts']}: {len(new)} yeni, {len(uris) - len(new)} zaten var")
                continue
            if new:
                spotify.add(playlist_id, new)
                existing.update(new)
                added_total += len(new)
                slack.react(channel, m["ts"], REACT_ADDED)
                print(f"  + {m['ts']}: {len(new)} öğe eklendi")
            else:
                slack.react(channel, m["ts"], REACT_DUPLICATE)
                print(f"  = {m['ts']}: zaten playlistte")
            if yt_missing:
                slack.react(channel, m["ts"], REACT_NOT_FOUND)
        except Exception as exc:  # bir mesaj patlasa da diğerleri devam etsin
            errors += 1
            print(f"  ! {m['ts']}: {exc}")

    print(f"Bitti: {added_total} öğe eklendi, {errors} hata.")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
