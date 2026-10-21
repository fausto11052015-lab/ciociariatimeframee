"""
Agente AI - Monitoraggio Notizie Ciociaria / Provincia di Frosinone
Scraper RSS + KPI scoring + generazione articoli con Claude AI
Invio su Telegram + pubblicazione bozza su WordPress
"""

import os
import time
import json
import hashlib
import logging
import feedparser
import requests
from datetime import datetime, timezone
from anthropic import Anthropic

# ─────────────────────────────────────────────
# CONFIGURAZIONE
# ─────────────────────────────────────────────

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
WP_URL              = os.environ.get("WP_URL", "")          # es. https://tuosito.it
WP_USER             = os.environ.get("WP_USER", "")
WP_APP_PASSWORD     = os.environ.get("WP_APP_PASSWORD", "")  # Application Password di WordPress
INTERVAL_MINUTES    = int(os.environ.get("INTERVAL_MINUTES", "10"))

# ─────────────────────────────────────────────
# FONTI RSS - Giornali locali Frosinone / Ciociaria
# ─────────────────────────────────────────────

RSS_FEEDS = [
    # --- Fonti principali Frosinone ---
    {"name": "FrosinoneToday",      "url": "https://www.frosinonetoday.it/rss.xml"},
    {"name": "CiociariaOggi",       "url": "https://www.ciociariaoggi.it/feed/"},
    {"name": "FrosinoneInDiretta",  "url": "https://www.frosinoneindiretta.it/feed/"},
    {"name": "FrosinoneCity",       "url": "https://www.frosinionecity.it/feed/"},
    {"name": "FrosinoneInside",     "url": "https://www.frosinoneinside.it/feed/"},
    {"name": "FrosinoneMagazine",   "url": "https://www.frosinonemagazine.it/feed/"},
    {"name": "CiociariaNews",       "url": "https://www.ciociarianews.it/feed/"},
    {"name": "H24Frosinone",        "url": "https://www.h24frosinone.it/feed/"},
    {"name": "TG24Info",            "url": "https://www.tg24.info/feed/"},
    # --- Google News - query locale ---
    {"name": "GoogleNews-Frosinone",
     "url": "https://news.google.com/rss/search?q=Frosinone+%22provincia+di+Frosinone%22&hl=it&gl=IT&ceid=IT:it"},
    {"name": "GoogleNews-Ciociaria",
     "url": "https://news.google.com/rss/search?q=Ciociaria&hl=it&gl=IT&ceid=IT:it"},
    # --- Il Messaggero (sezione Frosinone/Lazio) ---
    {"name": "IlMessaggero-Lazio",  "url": "https://www.ilmessaggero.it/rss/lazio.xml"},
    # --- Corriere della Sera Lazio ---
    {"name": "Corriere-Lazio",      "url": "https://roma.corriere.it/rss/sezioni/lazio/"},
]

# Parole chiave per rilevanza geografica
GEO_KEYWORDS = [
    "frosinone", "ciociaria", "cassino", "sora", "anagni", "alatri",
    "ferentino", "ceccano", "pontecorvo", "monte san giovanni campano",
    "fiuggi", "veroli", "boville ernica", "isola del liri", "piedimonte san germano",
    "arce", "atina", "arpino", "aquino", "cervaro", "paliano",
    "provincia di frosinone", "valle di comino", "valle del liri", "valle del sacco",
]

# Parole chiave ad alto interesse giornalistico (alzano il KPI score)
HIGH_INTEREST_KEYWORDS = [
    "incidente", "arresto", "omicidio", "rapina", "furto", "incendio",
    "terremoto", "alluvione", "maltempo", "emergenza", "protesta",
    "sciopero", "appalto", "corruzione", "indagato", "arrestato",
    "inaugurazione", "chiusura", "licenziamento", "crisi", "ospedale",
    "pronto soccorso", "elezioni", "candidato", "sindaco", "assessore",
    "lavori", "cantiere", "disoccupazione", "azienda", "investimento",
]

# Score minimo per scrivere un articolo (0-100)
KPI_THRESHOLD = 45

# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("ciociaria-agent")

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

# File JSON locale per tenere traccia degli articoli già processati
SEEN_FILE = "seen_articles.json"

def load_seen() -> set:
    try:
        with open(SEEN_FILE, "r") as f:
            data = json.load(f)
            return set(data)
    except Exception:
        return set()

def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

# ─────────────────────────────────────────────
# FETCH RSS
# ─────────────────────────────────────────────

def fetch_feed(source: dict) -> list[dict]:
    """Scarica un feed RSS e restituisce lista di articoli."""
    articles = []
    try:
        feed = feedparser.parse(source["url"])
        for entry in feed.entries:
            title   = getattr(entry, "title", "")
            summary = getattr(entry, "summary", "")
            link    = getattr(entry, "link", "")
            pub     = getattr(entry, "published", str(datetime.now(timezone.utc)))
            articles.append({
                "source":  source["name"],
                "title":   title,
                "summary": summary[:600],
                "link":    link,
                "pub":     pub,
                "id":      hashlib.md5(link.encode()).hexdigest(),
            })
    except Exception as e:
        log.warning(f"Errore feed {source['name']}: {e}")
    return articles

# ─────────────────────────────────────────────
# KPI SCORING
# ─────────────────────────────────────────────

def kpi_score(article: dict) -> int:
    """
    Calcola un punteggio 0-100 per la rilevanza di un articolo.
    Criteri:
      - Geo-rilevanza (il fatto riguarda davvero la Ciociaria)
      - Interesse giornalistico (parole chiave calde)
      - Presenza di titolo significativo
    """
    text = (article["title"] + " " + article["summary"]).lower()
    score = 0

    # Geo check (obbligatorio per base score)
    geo_hits = sum(1 for kw in GEO_KEYWORDS if kw in text)
    score += min(geo_hits * 15, 40)   # max 40 punti dalla geo

    # Interesse giornalistico
    hi_hits = sum(1 for kw in HIGH_INTEREST_KEYWORDS if kw in text)
    score += min(hi_hits * 12, 40)    # max 40 punti dall'interesse

    # Bonus titolo lungo e informativo
    if len(article["title"]) > 40:
        score += 10

    # Bonus fonte locale diretta
    if article["source"] in ("FrosinoneToday", "CiociariaOggi", "FrosinoneInDiretta",
                              "FrosinoneCity", "FrosinoneInside", "CiociariaNews"):
        score += 10

    return min(score, 100)

# ─────────────────────────────────────────────
# CROSS-REFERENCING (verifica su più fonti)
# ─────────────────────────────────────────────

def cross_reference(article: dict, all_articles: list[dict]) -> list[dict]:
    """
    Cerca altri articoli sullo stesso argomento.
    Usa le parole principali del titolo come query.
    """
    words = [w for w in article["title"].lower().split() if len(w) > 4][:5]
    related = []
    for a in all_articles:
        if a["id"] == article["id"]:
            continue
        a_text = (a["title"] + " " + a["summary"]).lower()
        matches = sum(1 for w in words if w in a_text)
        if matches >= 2:
            related.append(a)
    return related[:5]  # max 5 fonti correlate

# ─────────────────────────────────────────────
# GENERAZIONE ARTICOLO CON CLAUDE
# ─────────────────────────────────────────────

def generate_article(article: dict, related: list[dict]) -> dict:
    """
    Chiede a Claude di elaborare un articolo giornalistico originale
    partendo dalla notizia e dalle fonti correlate.
    """
    related_text = ""
    for r in related:
        related_text += f"\n- [{r['source']}] {r['title']}: {r['summary'][:200]}"

    prompt = f"""Sei un giornalista esperto di cronaca locale per la Ciociaria (provincia di Frosinone, Lazio).
Hai intercettato questa notizia:

TITOLO: {article['title']}
FONTE: {article['source']}
PUBBLICATO: {article['pub']}
RIASSUNTO: {article['summary']}
LINK ORIGINALE: {article['link']}

FONTI CORRELATE CHE CONFERMANO/INTEGRANO LA NOTIZIA:{related_text if related_text else " (nessuna fonte correlata trovata)"}

Il tuo compito:
1. Rielabora completamente la notizia in modo originale, NON copiare le fonti.
2. Scrivi un articolo giornalistico professionale in italiano, stile neutro e informativo.
3. Struttura: titolo accattivante, occhiello (max 1 riga), 3-5 paragrafi, una frase di conclusione con contesto locale.
4. Se ci sono più fonti, integrale per dare profondità.
5. Inserisci un tag [WP_TITLE] prima del titolo e [WP_BODY] prima del corpo dell'articolo.
6. Alla fine aggiungi una riga [WP_EXCERPT]: con un riassunto di 2 righe per i social.
7. NON inventare fatti non presenti nelle fonti. Se qualcosa non è chiaro, usare il condizionale.

Scrivi l'articolo ora:"""

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text

        # Parsing dei tag
        wp_title   = ""
        wp_body    = ""
        wp_excerpt = ""

        if "[WP_TITLE]" in raw:
            after_title = raw.split("[WP_TITLE]")[1]
            wp_title = after_title.split("\n")[0].strip()

        if "[WP_BODY]" in raw:
            body_part = raw.split("[WP_BODY]")[1]
            if "[WP_EXCERPT]" in body_part:
                wp_body = body_part.split("[WP_EXCERPT]")[0].strip()
                wp_excerpt = body_part.split("[WP_EXCERPT]:")[1].strip() if "[WP_EXCERPT]:" in body_part else ""
            else:
                wp_body = body_part.strip()

        if not wp_title:
            wp_title = article["title"]
        if not wp_body:
            wp_body = raw

        return {
            "title":   wp_title,
            "body":    wp_body,
            "excerpt": wp_excerpt,
            "source_link": article["link"],
            "source_name": article["source"],
        }
    except Exception as e:
        log.error(f"Errore generazione articolo: {e}")
        return {}

# ─────────────────────────────────────────────
# INVIO TELEGRAM
# ─────────────────────────────────────────────

def send_telegram(article_data: dict, score: int, num_sources: int):
    """Invia notifica Telegram con anteprima dell'articolo."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("Telegram non configurato, skip.")
        return

    text = (
        f"📰 *NUOVO ARTICOLO GENERATO* (score: {score}/100)\n\n"
        f"*{article_data['title']}*\n\n"
        f"🔗 Fonte: {article_data['source_name']}\n"
        f"📎 Link originale: {article_data['source_link']}\n"
        f"📊 Fonti incrociate: {num_sources}\n\n"
        f"_{article_data['excerpt'][:300]}_"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=10)
        log.info("✅ Telegram inviato")
    except Exception as e:
        log.error(f"Errore Telegram: {e}")

# ─────────────────────────────────────────────
# PUBBLICAZIONE BOZZA WORDPRESS
# ─────────────────────────────────────────────

def publish_wordpress_draft(article_data: dict, score: int):
    """
    Crea una BOZZA (draft) su WordPress tramite REST API.
    Richiede un Application Password di WP (impostazione utente).
    """
    if not WP_URL or not WP_USER or not WP_APP_PASSWORD:
        log.info("WordPress non configurato, skip.")
        return

    api_url = f"{WP_URL.rstrip('/')}/wp-json/wp/v2/posts"
    payload = {
        "title":   article_data["title"],
        "content": article_data["body"],
        "excerpt": article_data["excerpt"],
        "status":  "draft",   # BOZZA - tu la rivedi e pubblichi
        "meta": {
            "_ciociaria_score": score,
            "_ciociaria_source": article_data["source_link"],
        },
        "tags": [],
        "categories": [],
    }

    try:
        r = requests.post(
            api_url,
            json=payload,
            auth=(WP_USER, WP_APP_PASSWORD),
            timeout=15,
        )
        if r.status_code in (200, 201):
            wp_post = r.json()
            post_id  = wp_post.get("id")
            edit_url = f"{WP_URL.rstrip('/')}/wp-admin/post.php?post={post_id}&action=edit"
            log.info(f"✅ Bozza WordPress creata: {edit_url}")
            return edit_url
        else:
            log.error(f"Errore WordPress {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"Errore WordPress: {e}")
    return None

# ─────────────────────────────────────────────
# CICLO PRINCIPALE
# ─────────────────────────────────────────────

def run_cycle(seen: set) -> set:
    """
    Un singolo ciclo di monitoraggio:
    1. Scarica tutti i feed RSS
    2. Filtra articoli nuovi
    3. Calcola KPI score
    4. Se score >= threshold: cross-reference + genera articolo + notifica
    """
    log.info("=" * 50)
    log.info(f"🔍 Avvio ciclo monitoraggio [{datetime.now().strftime('%H:%M:%S')}]")

    all_articles = []
    for source in RSS_FEEDS:
        fetched = fetch_feed(source)
        log.info(f"  [{source['name']}] {len(fetched)} articoli")
        all_articles.extend(fetched)

    log.info(f"Totale articoli scaricati: {len(all_articles)}")

    new_articles = [a for a in all_articles if a["id"] not in seen]
    log.info(f"Nuovi articoli (non ancora processati): {len(new_articles)}")

    for article in new_articles:
        seen.add(article["id"])
        score = kpi_score(article)

        if score < KPI_THRESHOLD:
            log.info(f"  SKIP (score {score}) — {article['title'][:60]}")
            continue

        log.info(f"  ✨ INTERESSANTE (score {score}) — {article['title'][:60]}")

        # Cross-referencing
        related = cross_reference(article, all_articles)
        log.info(f"    → Fonti correlate trovate: {len(related)}")

        # Genera articolo
        article_data = generate_article(article, related)
        if not article_data:
            continue

        log.info(f"    → Articolo generato: {article_data['title'][:60]}")

        # Salva articolo su file locale (sempre)
        filename = f"articoli/{datetime.now().strftime('%Y%m%d_%H%M%S')}_{article['id'][:8]}.md"
        os.makedirs("articoli", exist_ok=True)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"# {article_data['title']}\n\n")
            f.write(f"**Score KPI:** {score}/100  \n")
            f.write(f"**Fonte originale:** [{article['source']}]({article['link']})  \n")
            f.write(f"**Fonti correlate:** {len(related)}  \n")
            f.write(f"**Data:** {datetime.now().strftime('%d/%m/%Y %H:%M')}  \n\n")
            f.write("---\n\n")
            f.write(article_data["body"])
            f.write("\n\n---\n")
            f.write(f"*{article_data['excerpt']}*\n")
        log.info(f"    → Salvato in: {filename}")

        # Invia Telegram
        send_telegram(article_data, score, len(related))

        # Pubblica bozza WordPress
        wp_url = publish_wordpress_draft(article_data, score)
        if wp_url:
            log.info(f"    → Bozza WP: {wp_url}")

        # Piccola pausa per non sovraccaricare l'API
        time.sleep(3)

    save_seen(seen)
    log.info(f"Ciclo completato. Prossimo controllo tra {INTERVAL_MINUTES} minuti.")
    return seen


def main():
    log.info("🚀 Agente Ciociaria News avviato")
    log.info(f"   Intervallo: ogni {INTERVAL_MINUTES} minuti")
    log.info(f"   Fonti monitorate: {len(RSS_FEEDS)}")
    log.info(f"   KPI threshold: {KPI_THRESHOLD}/100")
    seen = load_seen()

    while True:
        seen = run_cycle(seen)
        time.sleep(INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    main()
