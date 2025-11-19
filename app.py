import time
import hashlib
import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from flask import Flask, Response
from urllib.parse import urljoin
from datetime import datetime, timezone

app = Flask(__name__)

# 🔧 Páginas que serão raspadas
PAGES_TO_SCRAPE = [
    "https://prefeitura.sp.gov.br/noticias",
    # Adicione outras páginas aqui se quiser
]

# 🔧 Palavras-chave
INCLUDE_KEYWORDS = ["saúde", "educação", "deficiência"]
EXCLUDE_KEYWORDS = ["esporte", "cultura"]

# Sessão HTTP com cabeçalho e timeout
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (RSS Generator; +https://rss-sp.onrender.com)"
})
TIMEOUT = 8

# Cache simples em memória (10 minutos)
CACHE = {"feed": None, "ts": 0}
CACHE_TTL = 600  # segundos


def safe_get(url):
    """Faz GET com timeout e fallback seguro."""
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        if resp.status_code == 200:
            return resp.text
    except Exception:
        return ""
    return ""


def build_feed():
    fg = FeedGenerator()
    fg.title("Notícias de Itaquera")
    fg.link(href=PAGES_TO_SCRAPE[0])
    fg.description("Feed confiável com filtros fixos e múltiplas páginas.")
    fg.language("pt-br")

    seen_links = set()

    for page in PAGES_TO_SCRAPE:
        listing_html = safe_get(page)
        if not listing_html:
            continue

        soup = BeautifulSoup(listing_html, "html.parser")

        # pega até 10 links por página para desempenho
        for item in soup.select("a")[:10]:
            link = item.get("href")
            title = item.get_text(strip=True)

            if not link or not title:
                continue

            link = urljoin(page, link)
            if link in seen_links:
                continue
            seen_links.add(link)

            # tenta pegar conteúdo da notícia
            article_html = safe_get(link)
            if not article_html:
                continue

            news_soup = BeautifulSoup(article_html, "html.parser")
            content = " ".join([p.get_text(strip=True) for p in news_soup.select("p")])
            img_tag = news_soup.select_one("img")
            img_url = urljoin(link, img_tag["src"]) if img_tag and img_tag.get("src") else None

            full_text = f"{title} {content}"

            # filtros
            if INCLUDE_KEYWORDS and not any(k.lower() in full_text.lower() for k in INCLUDE_KEYWORDS):
                continue
            if EXCLUDE_KEYWORDS and any(k.lower() in full_text.lower() for k in EXCLUDE_KEYWORDS):
                continue

            # adiciona notícia ao feed
            fe = fg.add_entry()
            fe.title(title)
            fe.link(href=link)
            fe.description(content if content else "Sem conteúdo disponível")
            if img_url:
                fe.enclosure(img_url, 0, "image/jpeg")
            fe.guid(hashlib.sha256(link.encode()).hexdigest(), permalink=False)
            fe.pubDate(datetime.now(timezone.utc))

    # se nada for encontrado, adiciona item informativo
    if not fg._feed.get("entry"):
        fe = fg.add_entry()
        fe.title("Sem notícias no momento")
        fe.link(href=PAGES_TO_SCRAPE[0])
        fe.description("Nenhum item foi encontrado com os filtros atuais.")
        fe.pubDate(datetime.now(timezone.utc))

    return fg.rss_str(pretty=True)


@app.route("/feed.xml")
def feed():
    # cache leve em memória (10 minutos)
    now = time.time()
    if CACHE["feed"] and (now - CACHE["ts"] < CACHE_TTL):
        return Response(CACHE["feed"], mimetype="application/rss+xml")

    rss = build_feed()
    CACHE["feed"] = rss
    CACHE["ts"] = now
    return Response(rss, mimetype="application/rss+xml")
