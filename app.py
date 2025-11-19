import time
import hashlib
import json
import re
import requests
from feedgen.feed import FeedGenerator
from flask import Flask, Response
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

app = Flask(__name__)

# 🔧 Configurações gerais
BASE_URL = "https://prefeitura.sp.gov.br"
NEWS_PAGE = f"{BASE_URL}/noticias"

# 🔧 Estruturas conhecidas e genéricas (multi-fonte)
# STRUCTURE_IDS será analisado dinamicamente na página; esta lista é fallback
STRUCTURE_IDS_FALLBACK = [79914]  # adicione IDs conhecidos aqui se desejar

GENERIC_SOURCES = [
    f"{BASE_URL}/o/headless-delivery/v1.0/sites/34276/structured-contents?pageSize=100&sort=datePublished:desc"
]

# 🔧 Imagem padrão
DEFAULT_IMAGE = "https://www.noticiasdeitaquera.com.br/imagens/logoprefsp.png"

# 🔧 Filtros configuráveis
INCLUDE_KEYWORDS = []  # se vazio → todas entram
EXCLUDE_KEYWORDS = []  # se vazio → nenhuma é excluída

# 🔧 Requisitos mínimos
MIN_ITEMS = 10  # garante no mínimo 10 notícias publicadas

# 🔧 Sessão HTTP
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (RSS Generator; +https://rss-sp.onrender.com)"})
TIMEOUT = 10

# 🔧 Cache persistente e em memória
CACHE = {"feed": None, "ts": 0}
CACHE_TTL = 600  # 10 minutos
DISK_CACHE_FILE = "/tmp/rss_sp_cache.xml"  # caminho persistente no Render


# Utilidades ---------------------------------------------------------------

def safe_title(item):
    """Retorna título seguro (string ou dict)."""
    raw_title = item.get("title")
    if isinstance(raw_title, dict):
        return raw_title.get("pt_BR") or "Sem título"
    if isinstance(raw_title, str):
        return raw_title
    return "Sem título"


def safe_date(pub_date):
    """Retorna datetime seguro (ISO ou agora)."""
    dt = datetime.now(timezone.utc)
    if pub_date:
        try:
            # Liferay usa Z UTC; normaliza para +00:00
            dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
        except Exception:
            pass
    return dt


def normalize_url(url):
    """Torna uma URL absoluta usando BASE_URL."""
    if not url:
        return None
    # se já for absoluta, retorna
    if bool(urlparse(url).netloc):
        return url
    # se começar com '/', junta com domínio
    return urljoin(BASE_URL, url)


def normalize_image_url(url):
    """Normaliza URL de imagem para formato absoluto completo."""
    normalized = normalize_url(url)
    # Alguns endpoints retornam 'contentUrl' relativo dentro de `image`
    return normalized or DEFAULT_IMAGE


def read_disk_cache():
    """Lê último feed persistido em disco."""
    try:
        with open(DISK_CACHE_FILE, "rb") as f:
            return f.read()
    except Exception:
        return None


def write_disk_cache(data: bytes):
    """Escreve feed persistido em disco."""
    try:
        with open(DISK_CACHE_FILE, "wb") as f:
            f.write(data)
    except Exception:
        pass


# Descoberta de fontes (estilo rss.app) -----------------------------------

def discover_structure_ids_from_page():
    """
    Analisa a página de notícias para encontrar IDs de estruturas utilizados.
    Procura padrões de 'content-structures/{id}/structured-contents' em scripts/HTML.
    """
    try:
        resp = SESSION.get(NEWS_PAGE, timeout=TIMEOUT)
        if resp.status_code != 200 or not resp.text:
            return STRUCTURE_IDS_FALLBACK.copy()

        html = resp.text

        # Encontra possíveis chamadas a content-structures/{ID}
        ids = set()

        # Regex para capturar IDs numéricos após 'content-structures/'
        for match in re.findall(r"content-structures/(\d+)/structured-contents", html):
            try:
                ids.add(int(match))
            except Exception:
                continue

        # Busca também em URLs codificados (com %2F etc.)
        for match in re.findall(r"content-structures%2F(\d+)%2Fstructured-contents", html):
            try:
                ids.add(int(match))
            except Exception:
                continue

        # Se nada encontrado, retorna fallback
        found = list(ids)
        if not found:
            return STRUCTURE_IDS_FALLBACK.copy()

        return found
    except Exception:
        return STRUCTURE_IDS_FALLBACK.copy()


def fetch_json_items_from_structure(structure_id: int):
    """Busca itens JSON para um ID de estrutura específico."""
    url = (
        f"{BASE_URL}/o/headless-delivery/v1.0/content-structures/{structure_id}/structured-contents"
        f"?pageSize=100&sort=datePublished:desc&filter=siteId eq 34276"
    )
    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("items", [])
    except Exception:
        return []
    return []


def fetch_json_items_from_generic_sources():
    """Busca itens JSON de fontes genéricas adicionais."""
    items = []
    for url in GENERIC_SOURCES:
        try:
            resp = SESSION.get(url, timeout=TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                items.extend(data.get("items", []))
        except Exception:
            continue
    return items


def scrape_latest_from_html():
    """
    Raspagem real da página de notícias principal.
    Objetivo: obter títulos, links e eventualmente imagem das notícias exibidas na home/listagem.
    Como o HTML pode variar, usamos heurísticas simples: anchors com hrefs para /w/noticia/ ou friendly URLs.
    """
    items = []
    try:
        resp = SESSION.get(NEWS_PAGE, timeout=TIMEOUT)
        if resp.status_code != 200 or not resp.text:
            return items

        html = resp.text

        # Heurística: capturar blocos de notícia pelo padrão de links
        # Ex.: <a href="/w/noticia/...">Título</a> ou <a href="https://prefeitura.sp.gov.br/w/noticia/...">
        # Também capturar possíveis imagens próximas
        link_pattern = re.compile(r'href="([^"]+/w/noticia/[^"]+)"[^>]*>([^<]+)</a>', re.IGNORECASE)
        for href, anchor_text in link_pattern.findall(html):
            link = normalize_url(href.strip())
            title = anchor_text.strip()
            # Tenta capturar imagem próxima ao link (mesmo bloco)
            img_match = re.search(
                r'<img[^>]+src="([^"]+)"[^>]*',
                html
            )
            img_url = normalize_image_url(img_match.group(1)) if img_match else DEFAULT_IMAGE

            # Sem data no HTML: usa agora; JSON mais abaixo complementa
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
    Estilo rss.app: descobre IDs dinâmicos na página, consulta múltiplas fontes JSON,
    complementa com raspagem HTML. Junta tudo, ordena e filtra.
    """
    combined = []

    # 1) Raspagem HTML da listagem principal (prioridade)
    html_items = scrape_latest_from_html()
    combined.extend(html_items)

    # 2) Descobrir dinamicamente IDs de estruturas atuais
    discovered_ids = discover_structure_ids_from_page()

    # 3) Buscar JSON para cada ID descoberto
    for sid in discovered_ids:
        sid_items = fetch_json_items_from_structure(sid)
        combined.extend(sid_items)

    # 4) Fontes genéricas complementares
    combined.extend(fetch_json_items_from_generic_sources())

    # 5) Deduplicação por link (contentUrl) mantendo o mais recente
    dedup = {}
    for it in combined:
        link = it.get("contentUrl") or ""
        if not link:
            # se não tiver link, cria chave pela hash do título
            link = f"no-link-{hashlib.sha256(safe_title(it).encode()).hexdigest()}"
        dt = safe_date(it.get("datePublished"))
        if link not in dedup or dt > safe_date(dedup[link].get("datePublished")):
            dedup[link] = it

    items = list(dedup.values())

    # 6) Ordena por data decrescente
    items.sort(key=lambda x: safe_date(x.get("datePublished")), reverse=True)

    # 7) Filtra últimos 180 dias (ajustável para segurança)
    cutoff = datetime.now(timezone.utc) - timedelta(days=180)
    items = [i for i in items if safe_date(i.get("datePublished")) >= cutoff]

    # 8) Garante no mínimo MIN_ITEMS; se faltar, relaxa cutoff (pega mais antigos)
    if len(items) < MIN_ITEMS:
        # re-ordena toda base sem cutoff
        items = list(dedup.values())
        items.sort(key=lambda x: safe_date(x.get("datePublished")), reverse=True)

    # 9) Limita a 100 para não pesar
    return items[:100]


# Construção do feed -------------------------------------------------------

def build_feed():
    """Constrói o feed RSS com as últimas notícias combinando raspagem e JSON."""
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

        # Aplicação dos filtros
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

        # Para garantir no mínimo 10 rapidamente, se já temos MIN_ITEMS, podemos parar (opcional)
        # if entries_added >= MIN_ITEMS:
        #     break

    # Se nada foi encontrado, adiciona item informativo
    if entries_added == 0:
        fe = fg.add_entry()
        fe.title("Sem notícias no momento")
        fe.link(href=NEWS_PAGE)
        fe.description("Nenhum item foi encontrado com os filtros atuais.")
        fe.enclosure(DEFAULT_IMAGE, 0, "image/jpeg")
        fe.pubDate(datetime.now(timezone.utc))

    rss_bytes = fg.rss_str(pretty=True)
    return rss_bytes


# Endpoint com cache robusto ----------------------------------------------

@app.route("/feed.xml")
def feed():
    """
    Endpoint do feed RSS com cache:
    - Cache em memória por 10 min
    - Cache persistente em disco usado como fallback
    - Nunca retorna vazio ao atualizar/F5 se já houve um feed válido antes
    """
    now = time.time()

    # Serve cache em memória se válido
    if CACHE["feed"] and (now - CACHE["ts"] < CACHE_TTL):
        return Response(CACHE["feed"], mimetype="application/rss+xml")

    try:
        rss = build_feed()
        # Atualiza cache memória e disco
        CACHE["feed"] = rss
        CACHE["ts"] = now
        write_disk_cache(rss)
        return Response(rss, mimetype="application/rss+xml")
    except Exception:
        # Fallback: serve último feed em memória, ou disco, ou mensagem
        if CACHE["feed"]:
            return Response(CACHE["feed"], mimetype="application/rss+xml")
        disk = read_disk_cache()
        if disk:
            CACHE["feed"] = disk
            CACHE["ts"] = now
            return Response(disk, mimetype="application/rss+xml")
        return Response("Erro ao gerar feed", mimetype="text/plain")
