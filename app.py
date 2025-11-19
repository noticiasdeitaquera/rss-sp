import time
import hashlib
import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from flask import Flask, Response
from urllib.parse import urljoin
from datetime import datetime, timezone

app = Flask(__name__)

# 🔧 Página principal de notícias
NEWS_PAGE = "https://prefeitura.sp.gov.br/noticias"

# 🔧 Palavras-chave
# INCLUDE_KEYWORDS: se vazio, todas as notícias entram
# EXCLUDE_KEYWORDS: notícias contendo essas palavras são removidas
INCLUDE_KEYWORDS = []  # exemplo: ["saúde", "educação"]
EXCLUDE_KEYWORDS = ["esporte", "cultura"]

# Imagem padrão caso a notícia não tenha imagem
DEFAULT_IMAGE = "https://www.noticiasdeitaquera.com.br/imagens/logoprefsp.png"

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
    """Faz GET com timeout e fallback seguro, limitando tamanho da resposta."""
    try:
        resp = SESSION.get(url, timeout=TIMEOUT, stream=False)
        if resp.status_code == 200:
            return resp.text[:200000]  # limita tamanho para evitar estourar memória
    except Exception:
        return ""
    return ""


def build_feed():
    fg = FeedGenerator()
    fg.title("Notícias de Itaquera")
    fg.link(href=NEWS_PAGE)
    fg.description("Feed confiável com filtros e múltiplas páginas.")
    fg.language("pt-br")

    seen_links = set()
    entries_added = 0

    listing_html = safe_get(NEWS_PAGE)
    if listing_html:
        soup = BeautifulSoup(listing_html, "html.parser")

        # Seleciona apenas os itens da lista de notícias
        news_items = soup.select("ul li a")[:30]  # limite de 30 links

        for item in news_items:
            link = item.get("href")
            title_tag = item.select_one("p")
            title = title_tag.get_text(strip=True) if title_tag else item.get_text(strip=True)

            if not link or not title:
                continue

            link = urljoin(NEWS_PAGE, link)
            if link in seen_links:
                continue
            seen_links.add(link)

            # tenta pegar a data se existir
            date_tag = item.select_one("span.psp-badge")
            pub_date = None
            if date_tag:
                try:
                    pub_date = datetime.strptime(date_tag.get_text(strip=True), "%d/%m/%Y")
                    pub_date = pub_date.replace(tzinfo=timezone.utc)
                except Exception:
                    pub_date = datetime.now(timezone.utc)

            # 🔍 FILTRO:
            full_text = f"{title}"
            include_ok = True
            if INCLUDE_KEYWORDS:
                include_ok = any(k.lower() in full_text.lower() for k in INCLUDE_KEYWORDS)

            exclude_ok = not any(k.lower() in full_text.lower() for k in EXCLUDE_KEYWORDS)

            if include_ok and exclude_ok:
                fe = fg.add_entry()
                fe.title(title)
                fe.link(href=link)
                fe.description(title)
                fe.enclosure(DEFAULT_IMAGE, 0, "image/jpeg")  # usa imagem padrão
                fe.guid(hashlib.sha256(link.encode()).hexdigest(), permalink=False)
                fe.pubDate(pub_date if pub_date else datetime.now(timezone.utc))
                entries_added += 1

    # se nada foi encontrado, adiciona item informativo
    if entries_added == 0:
        fe = fg.add_entry()
        fe.title("Sem notícias no momento")
        fe.link(href=NEWS_PAGE)
        fe.description("Nenhum item foi encontrado com os filtros atuais.")
        fe.enclosure(DEFAULT_IMAGE, 0, "image/jpeg")
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
