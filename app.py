import time
import hashlib
import requests
import threading
import re
from feedgen.feed import FeedGenerator
from flask import Flask, Response
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

app = Flask(__name__)

# 🔧 Configurações gerais
BASE_URL = "https://prefeitura.sp.gov.br"
NEWS_PAGE = f"{BASE_URL}/noticias"
ALL_NEWS_PAGE = f"{BASE_URL}/todas-as-not%C3%ADcias"

# 🔧 Estruturas conhecidas (IDs) e fontes genéricas
STRUCTURE_IDS_FALLBACK = [79914]
GENERIC_SOURCES = [
    f"{BASE_URL}/o/headless-delivery/v1.0/sites/34276/structured-contents?pageSize=100&sort=datePublished:desc"
]

# 🔧 Imagem padrão
DEFAULT_IMAGE = "https://www.noticiasdeitaquera.com.br/imagens/logoprefsp.png"

# 🔧 Filtros configuráveis
INCLUDE_KEYWORDS = []
EXCLUDE_KEYWORDS = []

# 🔧 Requisitos mínimos
MIN_ITEMS = 10

# 🔧 Sessão HTTP
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (RSS Generator; +https://rss-sp.onrender.com)"})
TIMEOUT = 10

# 🔧 Cache
CACHE = {"feed": None, "ts": 0}
CACHE_TTL = 600


# Utilidades ---------------------------------------------------------------

def safe_title(item):
    raw_title = item.get("title")
    if isinstance(raw_title, dict):
        return raw_title.get("pt_BR") or "Sem título"
    if isinstance(raw_title, str):
        return raw_title
    return "Sem título"


def safe_date(pub_date):
    dt = datetime.now(timezone.utc)
    if pub_date:
        try:
            dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
        except Exception:
            pass
    return dt


def normalize_url(url):
    if not url:
        return None
    if bool(urlparse(url).netloc):
        return url
    return urljoin(BASE_URL, url)


def normalize_image_url(url):
    normalized = normalize_url(url)
    return normalized or DEFAULT_IMAGE


# Fontes -------------------------------------------------------------------

def fetch_json_items_from_structure(structure_id: int):
    url = (
        f"{BASE_URL}/o/headless-delivery/v1.0/content-structures/{structure_id}/structured-contents"
        f"?pageSize=100&sort=datePublished:desc&filter=siteId eq 34276"
    )
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get("items", [])
    except Exception:
        return []
    return []


def fetch_json_items_from_generic_sources():
    items = []
    for url in GENERIC_SOURCES:
        try:
            resp = SESSION.get(url, timeout=TIMEOUT)
            if resp.status_code == 200:
                items.extend(resp.json().get("items", []))
        except Exception:
            continue
    return items


def scrape_latest_from_html(page_url):
    """
    Raspagem real da página de notícias (home ou todas-as-notícias).
    Captura links e títulos de notícias exibidas.
    """
    items = []
    try:
        resp = SESSION.get(page_url, timeout=TIMEOUT)
        if resp.status_code != 200 or not resp.text:
            return items

        html = resp.text

        # Captura links e títulos
        link_pattern = re.compile(r'href="([^"]+/w/noticia/[^"]+)"[^>]*>([^<]+)</a>', re.IGNORECASE)
        for href, anchor_text in link_pattern.findall(html):
            link = normalize_url(href.strip())
            title = anchor_text.strip()
            # Captura imagem próxima (heurística simples)
            img_match = re.search(r'<img[^>]+src="([^"]+)"[^>]*', html)
            img_url = normalize_image_url(img_match.group(1)) if img_match else DEFAULT_IMAGE

            items.append({
                "title": title,
                "contentUrl": link,
                "datePublished": datetime.now(timezone.utc).isoformat(),
                "contentFields": [{"name": "imagem", "contentFieldValue": {"image": {"contentUrl": img_url}}}]
            })

        return items
    except Exception:
        return []


def fetch_all_sources():
    """
    Junta raspagem HTML + JSON de múltiplas fontes.
    """
    combined = []

    # 1) Raspagem da página principal
    combined.extend(scrape_latest_from_html(NEWS_PAGE))

    # 2) Raspagem da página "todas as notícias"
    combined.extend(scrape_latest_from_html(ALL_NEWS_PAGE))

    # 3) Estruturas fallback
    for sid in STRUCTURE_IDS_FALLBACK:
        combined.extend(fetch_json_items_from_structure(sid))

    # 4) Fontes genéricas
    combined.extend(fetch_json_items_from_generic_sources())

    # Deduplicação
    dedup = {}
    for it in combined:
        link = it.get("contentUrl") or f"no-link-{hashlib.sha256(safe_title(it).encode()).hexdigest()}"
        dt = safe_date(it.get("datePublished"))
        if link not in dedup or dt > safe_date(dedup[link].get("datePublished")):
            dedup[link] = it

    items = list(dedup.values())
    items.sort(key=lambda x: safe_date(x.get("datePublished")), reverse=True)

    # Filtro últimos 180 dias
    cutoff = datetime.now(timezone.utc) - timedelta(days=180)
    items = [i for i in items if safe_date(i.get("datePublished")) >= cutoff]

    # Garante mínimo de itens
    if len(items) < MIN_ITEMS:
        items = list(dedup.values())
        items.sort(key=lambda x: safe_date(x.get("datePublished")), reverse=True)

    return items[:100]


# Feed ---------------------------------------------------------------------

def build_feed():
    fg = FeedGenerator()
    fg.title("Notícias de Itaquera")
    fg.link(href=NEWS_PAGE)
    fg.description("Feed confiável com as últimas notícias da Prefeitura.")
    fg.language("pt-br")

    entries_added = 0
    news_items = fetch_all_sources()

    for item in news_items:
        title = safe_title(item)
        link = item.get("contentUrl") or NEWS_PAGE
        if not title or not link:
            continue

        dt = safe_date(item.get("datePublished"))

        content = ""
        img_url = None
        for field in item.get("contentFields", []):
            if not isinstance(field, dict):
                continue
            name = field.get("name", "").lower()
            if name in ["texto", "conteudo", "body"] and "contentFieldValue" in field:
                content = field["contentFieldValue"].get("data", "") or content
            if name in ["imagem", "image"] and "contentFieldValue" in field:
                raw_img = field["contentFieldValue"].get("image", {}).get("contentUrl")
                img_url = normalize_image_url(raw_img)

        if not img_url:
            img_url = DEFAULT_IMAGE

        full_text = f"{title} {content}"
        include_ok = True
        if INCLUDE_KEYWORDS:
            include_ok = any(k.lower() in full_text.lower() for k in INCLUDE_KEYWORDS)
        exclude_ok = True
        if EXCLUDE_KEYWORDS:
            exclude_ok = not any(k.lower() in full_text.lower() for k in EXCLUDE_KEYWORDS)

        if include_ok and exclude_ok:
            fe = fg.add_entry()
            fe.title(title)
            fe.link(href=link)
            fe.description(content if content else title)
            fe.enclosure(img_url, 0, "image/jpeg")
            fe.guid(hashlib.sha256(link.encode()).hexdigest(), permalink=False)
            fe.pubDate(dt)
            entries_added += 1

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
    now = time.time()
    if CACHE["feed"] and (now - CACHE["ts"] < CACHE_TTL):
        return Response(CACHE["feed"], mimetype="application/rss+xml")

    try:
        rss = build_feed()
        CACHE["feed"] = rss
        CACHE["ts"] = now
        return Response(rss, mimetype="application/rss+xml")
    except Exception:
        if CACHE["feed"]:
            return Response(CACHE["feed"], mimetype="application/rss+xml")
        return Response("Erro ao gerar feed", mimetype="text/plain")


# 🔧 Auto-ping para evitar hibernação --------------------------------------

def ping_self():
    """Função que pinga o próprio feed a cada 5 minutos para evitar hibernação."""
    while
