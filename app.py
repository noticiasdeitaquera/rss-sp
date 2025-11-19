import time
import hashlib
import requests
from feedgen.feed import FeedGenerator
from flask import Flask, Response
from datetime import datetime, timezone

app = Flask(__name__)

# 🔧 Endpoint genérico: últimas notícias sem filtro de data fixa
NEWS_JSON = "https://prefeitura.sp.gov.br/o/headless-delivery/v1.0/content-structures/79914/structured-contents?pageSize=30&sort=datePublished:desc&filter=siteId eq 34276"

# 🔧 Imagem padrão
DEFAULT_IMAGE = "https://www.noticiasdeitaquera.com.br/imagens/logoprefsp.png"

# 🔧 Filtros configuráveis
INCLUDE_KEYWORDS = []  # se vazio → todas entram
EXCLUDE_KEYWORDS = ["esporte", "cultura"]  # se vazio → nenhuma é excluída

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (RSS Generator; +https://rss-sp.onrender.com)"})
TIMEOUT = 8

CACHE = {"feed": None, "ts": 0}
CACHE_TTL = 600


def fetch_news_json():
    """Busca notícias diretamente do endpoint JSON da Prefeitura."""
    try:
        resp = SESSION.get(NEWS_JSON, timeout=TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get("items", [])
    except Exception:
        return []
    return []


def safe_title(item):
    """Retorna título seguro (string ou dict)."""
    raw_title = item.get("title")
    if isinstance(raw_title, dict):
        return raw_title.get("pt_BR") or "Sem título"
    if isinstance(raw_title, str):
        return raw_title
    return "Sem título"


def safe_date(pub_date):
    """Retorna data segura (ISO ou fallback para agora)."""
    dt = datetime.now(timezone.utc)
    if pub_date:
        try:
            dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
        except Exception:
            pass
    return dt


def build_feed():
    """Constrói o feed RSS com as últimas notícias."""
    fg = FeedGenerator()
    fg.title("Notícias de Itaquera")
    fg.link(href="https://prefeitura.sp.gov.br/noticias")
    fg.description("Feed confiável com as últimas notícias da Prefeitura.")
    fg.language("pt-br")

    entries_added = 0
    news_items = fetch_news_json()

    for item in news_items:
        title = safe_title(item)
        link = item.get("contentUrl") or "https://prefeitura.sp.gov.br/noticias"

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
                img_url = field["contentFieldValue"].get("image", {}).get("contentUrl")

        if not img_url:
            img_url = DEFAULT_IMAGE

        # 🔍 Aplicação dos filtros
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
        fe.link(href="https://prefeitura.sp.gov.br/noticias")
        fe.description("Nenhum item foi encontrado com os filtros atuais.")
        fe.enclosure(DEFAULT_IMAGE, 0, "image/jpeg")
        fe.pubDate(datetime.now(timezone.utc))

    return fg.rss_str(pretty=True)


@app.route("/feed.xml")
def feed():
    """Endpoint do feed RSS com cache de 10 minutos."""
    now = time.time()
    if CACHE["feed"] and (now - CACHE["ts"] < CACHE_TTL):
        return Response(CACHE["feed"], mimetype="application/rss+xml")

    rss = build_feed()
    CACHE["feed"] = rss
    CACHE["ts"] = now
    return Response(rss, mimetype="application/rss+xml")
