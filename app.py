import time
import hashlib
import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from flask import Flask, Response
from urllib.parse import urljoin
from datetime import datetime, timezone

app = Flask(__name__)

# 🔧 URLs que serão raspadas (adicione aqui)
PAGES_TO_SCRAPE = [
    "https://prefeitura.sp.gov.br/noticias",
    # Exemplo de outras páginas:
    # "https://www.prefeitura.sp.gov.br/cidade/secretarias/saude/noticias/",
]

# 🔧 Palavras-chave (busca em TÍTULO + TEXTO + atributos de imagem)
INCLUDE_KEYWORDS = ["saúde", "educação", "deficiência"]
EXCLUDE_KEYWORDS = ["esporte", "cultura"]

# Configuração de rede: sessão com cabeçalho, timeout e retries
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (RSS Fetcher; +https://rss-sp.onrender.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})
RETRY_COUNT = 2
TIMEOUT = 10

# Cache simples em memória (TTL 10 minutos)
CACHE = {"feed": None, "ts": 0}
CACHE_TTL_SECONDS = 600


def safe_get(url):
    """Faz GET com retries e timeout, retornando texto ou ''."""
    for _ in range(RETRY_COUNT + 1):
        try:
            resp = SESSION.get(url, timeout=TIMEOUT)
            if 200 <= resp.status_code < 300:
                return resp.text
        except Exception:
            pass
        time.sleep(0.5)
    return ""


def extract_items_from_listing(listing_html, base_page):
    """Tenta extrair links e títulos de uma página de listagem, de forma resiliente."""
    soup = BeautifulSoup(listing_html, "html.parser")
    items = []

    # Estratégias de seleção, da mais específica à mais genérica
    selectors = [
        # comuns em páginas de notícias
        "article h2 a",
        "article .title a",
        "article a",
        ".news-list a",
        ".listagem a",
        ".noticias a",
        "a",
    ]

    seen = set()
    for sel in selectors:
        for a in soup.select(sel):
            href = a.get("href")
            title = a.get_text(strip=True)
            if not href or not title:
                continue
            link = urljoin(base_page, href)
            if link in seen:
                continue
            seen.add(link)
            items.append({"link": link, "title": title})

        if items:
            break  # se encontrou com este seletor, não precisa tentar os próximos

    return items


def extract_content_and_image(article_html, article_url):
    """Extrai conteúdo textual e melhor imagem disponível da página do artigo."""
    soup = BeautifulSoup(article_html, "html.parser")

    # Conteúdo: prioridade para <article>, depois textos gerais
    article_node = soup.select_one("article")
    paragraphs = (article_node.select("p") if article_node else soup.select("p"))
    content = " ".join([p.get_text(strip=True) for p in paragraphs])[:10000]  # limita tamanho

    # Imagem: tenta meta tags e imagens dentro do artigo
    img_candidates = [
        soup.find("meta", property="og:image"),
        soup.find("meta", attrs={"name": "twitter:image"}),
        (article_node.select_one("img") if article_node else soup.select_one("img")),
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


def matches_filters(title, content):
    """Aplica filtros de inclusão/exclusão em todo o conteúdo."""
    full_text = f"{title} {content}".lower()

    if INCLUDE_KEYWORDS and not any(k.lower() in full_text for k in INCLUDE_KEYWORDS):
        return False
    if EXCLUDE_KEYWORDS and any(k.lower() in full_text for k in EXCLUDE_KEYWORDS):
        return False
    return True


def build_feed():
    fg = FeedGenerator()
    fg.title("Notícias de Itaquera")
    fg.link(href=PAGES_TO_SCRAPE[0])
    fg.description("Feed confiável com filtros fixos e múltiplas páginas.")
    fg.language("pt-br")

    # Deduplicação global
    seen_links = set()

    for page in PAGES_TO_SCRAPE:
        listing_html = safe_get(page)
        if not listing_html:
            continue

        items = extract_items_from_listing(listing_html, page)
        for item in items:
            link = item["link"]
            title = item["title"]

            if link in seen_links:
                continue
            seen_links.add(link)

            article_html = safe_get(link)
            if not article_html:
                # Sem artigo: adiciona entry mínima (não bloqueia feed)
                if matches_filters(title, ""):
                    fe = fg.add_entry()
                    fe.title(title)
                    fe.link(href=link)
                    fe.description("Sem conteúdo disponível no momento.")
                    fe.pubDate(datetime.now(timezone.utc))
                continue

            content, img_url = extract_content_and_image(article_html, link)
            if not matches_filters(title, content):
                continue

            fe = fg.add_entry()
            fe.title(title)
            fe.link(href=link)
            fe.description(content if content else "Sem conteúdo disponível.")
            if img_url:
                fe.enclosure(img_url, 0, "image/jpeg")
            fe.guid(hashlib.sha256(link.encode()).hexdigest(), permalink=False)
            fe.pubDate(datetime.now(timezone.utc))

    # Se nada for encontrado, publica um item informativo para evitar feed vazio
    if not fg._feed.get("entry"):
        fe = fg.add_entry()
        fe.title("Sem notícias no momento")
        fe.link(href=PAGES_TO_SCRAPE[0])
        fe.description("Nenhum item foi encontrado com os filtros atuais.")
        fe.pubDate(datetime.now(timezone.utc))

    return fg.rss_str(pretty=True)


@app.route("/feed.xml")
def feed():
    # Cache de 10 minutos
    now = time.time()
    if CACHE["feed"] and (now - CACHE["ts"] < CACHE_TTL_SECONDS):
        return Response(CACHE["feed"], mimetype="application/rss+xml")

    rss = build_feed()
    CACHE["feed"] = rss
    CACHE["ts"] = now
    return Response(rss, mimetype="application/rss+xml")
