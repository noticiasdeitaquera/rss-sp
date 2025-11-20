import os
import time
import hashlib
import threading
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response
from feedgen.feed import FeedGenerator

app = Flask(__name__)

BASE_URL = "https://prefeitura.sp.gov.br"
NEWS_PAGE = f"{BASE_URL}/noticias"
ALL_NEWS_PAGE = f"{BASE_URL}/todas-as-not%C3%ADcias"

STRUCTURE_IDS_FALLBACK = [79914]
GENERIC_SOURCES = [
    f"{BASE_URL}/o/headless-delivery/v1.0/sites/34276/structured-contents?pageSize=100&sort=datePublished:desc"
]

DEFAULT_IMAGE = "https://www.noticiasdeitaquera.com.br/imagens/logoprefsp.png"
INCLUDE_KEYWORDS = []
EXCLUDE_KEYWORDS = []
MIN_ITEMS = 10
MAX_ITEMS = 10

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (RSS Generator; +https://rss-sp.onrender.com)"})
TIMEOUT = 12

CACHE = {"feed": None, "ts": 0}
CACHE_TTL = 600


# Utilidades ---------------------------------------------------------------

def safe_title(item):
    raw_title = item.get("title")
    if isinstance(raw_title, dict):
        return raw_title.get("pt_BR") or "Sem título"
    if isinstance(raw_title, str) and raw_title.strip():
        return raw_title.strip()
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


def http_get(url, timeout=TIMEOUT, max_retries=2):
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = SESSION.get(url, timeout=timeout)
            return resp
        except Exception as e:
            last_exc = e
            time.sleep(0.6 * (attempt + 1))
    raise last_exc


# Raspagem HTML ------------------------------------------------------------

def scrape_latest_from_list_page(page_url):
    """Raspagem robusta das páginas de listagem (/noticias e /todas-as-notícias)."""
    items = []
    try:
        resp = http_get(page_url)
        if resp.status_code != 200 or not resp.text:
            return items

        soup = BeautifulSoup(resp.text, "html.parser")

        # Heurística: blocos de notícia
        blocks = soup.select("article, div.news-item, li")
        for block in blocks:
            a = block.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            title = a.get_text(strip=True)
            if not title:
                continue

            link = normalize_url(href)

            # Imagem: tenta várias opções
            img_url = None
            img_tag = block.find("img", src=True)
            if img_tag:
                img_url = normalize_image_url(img_tag["src"])
            if not img_url:
                meta_img = soup.find("meta", property="og:image")
                if meta_img and meta_img.get("content"):
                    img_url = normalize_image_url(meta_img["content"])
            if not img_url:
                img_url = DEFAULT_IMAGE

            items.append({
                "title": title,
                "contentUrl": link,
                "linkVisited": page_url,
                "datePublished": datetime.now(timezone.utc).isoformat(),
                "contentFields": [{"name": "imagem", "contentFieldValue": {"image": {"contentUrl": img_url}}}]
            })

        return items
    except Exception:
        return []


# Consolidação de fontes ---------------------------------------------------

def fetch_all_sources():
    combined = []
    combined.extend(scrape_latest_from_list_page(NEWS_PAGE))
    combined.extend(scrape_latest_from_list_page(ALL_NEWS_PAGE))

    # JSON fontes
    for sid in STRUCTURE_IDS_FALLBACK:
        try:
            resp = http_get(
                f"{BASE_URL}/o/headless-delivery/v1.0/content-structures/{sid}/structured-contents?pageSize=100&sort=datePublished:desc&filter=siteId eq 34276"
            )
            if resp.status_code == 200:
                combined.extend(resp.json().get("items", []))
        except Exception:
            continue

    for url in GENERIC_SOURCES:
        try:
            resp = http_get(url)
            if resp.status_code == 200:
                combined.extend(resp.json().get("items", []))
        except Exception:
            continue

    # Deduplicação
    dedup = {}
    for it in combined:
        link_key = it.get("contentUrl") or it.get("linkVisited") or ALL_NEWS_PAGE
        dt = safe_date(it.get("datePublished"))
        if link_key not in dedup or dt > safe_date(dedup[link_key].get("datePublished")):
            dedup[link_key] = it

    items = list(dedup.values())
    items.sort(key=lambda x: safe_date(x.get("datePublished")), reverse=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=180)
    items_recent = [i for i in items if safe_date(i.get("datePublished")) >= cutoff]
    if len(items_recent) < MIN_ITEMS:
        items_recent = items

    return items_recent[:MAX_ITEMS]


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
        link = item.get("contentUrl") or item.get("linkVisited") or ALL_NEWS_PAGE
        link = normalize_url(link)

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
        fe.link(href=ALL_NEWS_PAGE)
        fe.description("Nenhum item foi encontrado com os filtros atuais.")
        fe.enclosure(DEFAULT_IMAGE, 0, "image/jpeg")
        fe.pubDate(datetime.now(timezone.utc))

    return fg.rss_str(pretty=True)


@app.route("/")
def index():
    return Response("Service running", mimetype="text/plain")


@app.route("/feed.xml")
def feed():
    now = time.time()
