import os
import time
import hashlib
import threading
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response
from feedgen.feed import FeedGenerator

app = Flask(__name__)

# Configurações gerais
BASE_URL = "https://prefeitura.sp.gov.br"
NEWS_PAGE = f"{BASE_URL}/noticias"
ALL_NEWS_PAGE = f"{BASE_URL}/todas-as-not%C3%ADcias"

# Estruturas conhecidas (IDs) e fontes genéricas
STRUCTURE_IDS_FALLBACK = [79914]
GENERIC_SOURCES = [
    f"{BASE_URL}/o/headless-delivery/v1.0/sites/34276/structured-contents?pageSize=100&sort=datePublished:desc"
]

# Imagem padrão
DEFAULT_IMAGE = "https://www.noticiasdeitaquera.com.br/imagens/logoprefsp.png"

# Filtros configuráveis
INCLUDE_KEYWORDS = []
EXCLUDE_KEYWORDS = []

# Limites de feed
MIN_ITEMS = 10
MAX_ITEMS = 10

# Sessão HTTP + retries
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (RSS Generator; +https://rss-sp.onrender.com)"})
TIMEOUT = 12

# Cache
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
    """GET com retries e backoff simples."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = SESSION.get(url, timeout=timeout)
            return resp
        except Exception as e:
            last_exc = e
            time.sleep(0.6 * (attempt + 1))
    raise last_exc


# Fontes JSON --------------------------------------------------------------

def fetch_json_items_from_structure(structure_id: int):
    url = (
        f"{BASE_URL}/o/headless-delivery/v1.0/content-structures/{structure_id}/structured-contents"
        f"?pageSize=100&sort=datePublished:desc&filter=siteId eq 34276"
    )
    try:
        resp = http_get(url)
        if resp.status_code == 200:
            return resp.json().get("items", [])
    except Exception:
        return []
    return []


def fetch_json_items_from_generic_sources():
    items = []
    for url in GENERIC_SOURCES:
        try:
            resp = http_get(url)
            if resp.status_code == 200:
                items.extend(resp.json().get("items", []))
        except Exception:
            continue
    return items


# Raspagem HTML ------------------------------------------------------------

def extract_article_content(article_url):
    """Raspa página da notícia específica para obter conteúdo completo, título, imagem e data."""
    content, title, img_url, date_str = "", "", None, None
    try:
        resp = http_get(article_url)
        if resp.status_code != 200 or not resp.text:
            return content, title, img_url, date_str

        soup = BeautifulSoup(resp.text, "html.parser")

        # Título: tenta h1/h2 padrão
        h1 = soup.find(["h1", "h2"])
        if h1 and h1.text.strip():
            title = h1.text.strip()

        # Conteúdo: tenta selecionar áreas comuns de texto
        main = soup.find("main") or soup
        paragraphs = main.select("p")
        if paragraphs:
            content = "\n\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))

        # Imagem: busca figura hero ou primeira img relevante
        img = soup.select_one("img[src]") or soup.find("img")
        if img and img.get("src"):
            img_url = normalize_image_url(img.get("src"))

        # Data: tenta encontrar padrões comuns
        # Ex.: <time datetime="..."> ou texto com datas
        time_tag = soup.find("time")
        if time_tag and (time_tag.get("datetime") or time_tag.text.strip()):
            date_str = time_tag.get("datetime") or time_tag.text.strip()

    except Exception:
        pass

    return content, title, img_url, date_str


def scrape_latest_from_list_page(page_url):
    """
    Raspagem das páginas de listagem (/noticias e /todas-as-notícias).
    Captura blocos de notícia com link, título e imagem.
    """
    items = []
    try:
        resp = http_get(page_url)
        if resp.status_code != 200 or not resp.text:
            return items

        soup = BeautifulSoup(resp.text, "html.parser")

        # Heurísticas: blocos com links que contenham '/w/noticia/' ou links internos de notícia
        anchors = soup.select('a[href]')
        for a in anchors:
            href = a.get("href", "")
            text = a.get_text(strip=True)
            if not text:
                continue

            # Link candidato
            if "/w/noticia/" in href or (href.startswith("/") and "noticia" in href):
                link = normalize_url(href)
                # Imagem próxima ao link (no mesmo bloco)
                block = a.find_parent(["article", "div", "li"]) or a
                img_tag = block.select_one("img[src]") if block else None
                img_url = normalize_image_url(img_tag.get("src")) if img_tag and img_tag.get("src") else DEFAULT_IMAGE

                # Item básico (com fallback de data agora)
                items.append({
                    "title": text,
                    "contentUrl": link,                       # link específico, se presente
                    "linkVisited": page_url,                  # link visitado da listagem (fallback)
                    "datePublished": datetime.now(timezone.utc).isoformat(),
                    "contentFields": [{"name": "imagem", "contentFieldValue": {"image": {"contentUrl": img_url}}}]
                })

        return items
    except Exception:
        return []


# Consolidação de fontes ---------------------------------------------------

def fetch_all_sources():
    """
    Junta raspagem HTML das duas páginas + JSON de múltiplas fontes.
    Deduplica por link, enriquece com conteúdo completo e aplica ordenação/filtros.
    """
    combined = []

    # 1) Raspagem da página principal
    combined.extend(scrape_latest_from_list_page(NEWS_PAGE))

    # 2) Raspagem da página "todas as notícias"
    combined.extend(scrape_latest_from_list_page(ALL_NEWS_PAGE))

    # 3) Estruturas fallback
    for sid in STRUCTURE_IDS_FALLBACK:
        combined.extend(fetch_json_items_from_structure(sid))

    # 4) Fontes genéricas
    combined.extend(fetch_json_items_from_generic_sources())

    # 5) Enriquecimento: para itens com link específico, tenta raspar conteúdo completo
    enriched = []
    for it in combined:
        link_specific = it.get("contentUrl")
        if link_specific:
            content, title2, img2, date2 = extract_article_content(link_specific)
            # Se achar conteúdo real, prioriza-o
            if content:
                it.setdefault("contentFields", [])
                # Substitui/insere campo de texto
                it["contentFields"] = [
                    cf for cf in it["contentFields"] if cf.get("name", "").lower() not in ["texto", "conteudo", "body"]
                ]
                it["contentFields"].append({"name": "texto", "contentFieldValue": {"data": content}})
            # Atualiza título se estiver mais preciso
            if title2 and len(title2) > len(safe_title(it)):
                it["title"] = title2
            # Atualiza imagem se vier uma melhor
            if img2:
                it.setdefault("contentFields", [])
                it["contentFields"] = [
                    cf for cf in it["contentFields"] if cf.get("name", "").lower() not in ["imagem", "image"]
                ]
                it["contentFields"].append({"name": "imagem", "contentFieldValue": {"image": {"contentUrl": img2}}})
            # Data real se disponível
            if date2:
                it["datePublished"] = date2 if "T" in date2 else datetime.now(timezone.utc).isoformat()
        enriched.append(it)

    # 6) Deduplicação por link (contentUrl). Se não houver, usa hash do título
    dedup = {}
    for it in enriched:
        link_key = it.get("contentUrl")
        if not link_key:
            link_key = f"no-link-{hashlib.sha256(safe_title(it).encode()).hexdigest()}"
        dt = safe_date(it.get("datePublished"))
        if link_key not in dedup or dt > safe_date(dedup[link_key].get("datePublished")):
            dedup[link_key] = it

    items = list(dedup.values())

    # 7) Ordena por data decrescente
    items.sort(key=lambda x: safe_date(x.get("datePublished")), reverse=True)

    # 8) Filtro últimos 180 dias, com relaxamento se necessário
    cutoff = datetime.now(timezone.utc) - timedelta(days=180)
    items_recent = [i for i in items if safe_date(i.get("datePublished")) >= cutoff]
    if len(items_recent) < MIN_ITEMS:
        items_recent = items

    # 9) Retorna no máximo 10 itens
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

        # Link preferencial: contentUrl → linkVisited → link padrão
        preferred_link = item.get("contentUrl") or item.get("linkVisited") or ALL_NEWS_PAGE
        link = normalize_url(preferred_link) or ALL_NEWS_PAGE

        dt = safe_date(item.get("datePublished"))

        # Conteúdo e imagem
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

        # Filtros de palavras
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
            fe.link(href=link)  # link sempre válido com fallback
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


# Endpoints ----------------------------------------------------------------

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
        
