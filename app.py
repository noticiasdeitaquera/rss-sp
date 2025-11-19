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
    "https://prefeitura.sp.gov.br/agenda-do-prefeito",
]

# 🔧 Palavras-chave
# INCLUDE_KEYWORDS: se vazio, todas as notícias entram
# EXCLUDE_KEYWORDS: notícias contendo essas palavras são removidas
INCLUDE_KEYWORDS = []  # exemplo: ["saúde", "educação"]
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


def extract_content_and_image(article_html, article_url):
    """Extrai texto e imagem de uma página de notícia."""
    soup = BeautifulSoup(article_html, "html.parser")

    # pega o máximo de texto possível
    content_blocks = soup.select("article p, .content p, div.texto p, p")
    content = " ".join([p.get_text(strip=True) for p in content_blocks])

    # tenta pegar imagem principal em vários formatos
    img_candidates = [
        soup.find("meta", property="og:image"),
        soup.find("meta", attrs={"name": "twitter:image"}),
        soup.select_one("article img"),
        soup.select_one(".content img"),
        soup.select_one("img"),
    ]
    img_url = None
    for candidate in img_candidates:
        if not candidate:
            continue
        src = candidate.get("content") or candidate.get("src")
        if src:
            img_url = urljoin(article_url, src)
            break

    return content, img_url


def build_feed():
    fg = FeedGenerator()
    fg.title("Notícias de Itaquera")
    fg.link(href=PAGES_TO_SCRAPE[0])
    fg.description("Feed confiável com filtros e múltiplas páginas.")
    fg.language("pt-br")

    seen_links = set()
    entries_added = 0

    for page in PAGES_TO_SCRAPE:
        listing_html = safe_get(page)
        if not listing_html:
            continue

        soup = BeautifulSoup(listing_html, "html.parser")

        # Seleciona links de notícias em diferentes blocos
        news_links = soup.select("ul li a, article a, .noticia a, .listagem a")

        for item in news_links[:30]:  # limite para não sobrecarregar
            link = item.get("href")
            title_tag = item.select_one("p")
            title = title_tag.get_text(strip=True) if title_tag else item.get_text(strip=True)

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

            content, img_url = extract_content_and_image(article_html, link)

            full_text = f"{title} {content}"

            # 🔍 FILTRO:
            # - Se INCLUDE_KEYWORDS estiver vazio → todas as notícias entram
            # - Se houver palavras em INCLUDE_KEYWORDS → só entram notícias que contenham pelo menos uma delas
            # - Notícias com palavras em EXCLUDE_KEYWORDS são removidas
            include_ok = True
            if INCLUDE_KEYWORDS:
                include_ok = any(k.lower() in full_text.lower() for k in INCLUDE_KEYWORDS)

            exclude_ok = not any(k.lower() in full_text.lower() for k in EXCLUDE_KEYWORDS)

            if include_ok and exclude_ok:
                fe = fg.add_entry()
                fe.title(title)
                fe.link(href=link)
                fe.description(content if content else "Sem conteúdo disponível")
                if img_url:
                    fe.enclosure(img_url, 0, "image/jpeg")
                fe.guid(hashlib.sha256(link.encode()).hexdigest(), permalink=False)
                fe.pubDate(datetime.now(timezone.utc))  # data/hora correta em UTC
                entries_added += 1

    # se nada foi encontrado, adiciona item informativo
    if entries_added == 0:
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
