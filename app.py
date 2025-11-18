import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator
from flask import Flask, Response
from urllib.parse import urljoin
from datetime import datetime

app = Flask(__name__)

# 🔧 Páginas que serão raspadas
PAGES_TO_SCRAPE = [
    "https://prefeitura.sp.gov.br/noticias",
]

# 🔧 Palavras-chave fixas
INCLUDE_KEYWORDS = ["saúde", "educação", "deficiência"]
EXCLUDE_KEYWORDS = ["esporte", "cultura"]

def get_news():
    fg = FeedGenerator()
    fg.title("Notícias de Itaquera")
    fg.link(href="https://prefeitura.sp.gov.br/noticias")
    fg.description("Feed não-oficial com filtros fixos e múltiplas páginas.")
    fg.language("pt-br")

    for page in PAGES_TO_SCRAPE:
        resp = requests.get(page)
        soup = BeautifulSoup(resp.text, "html.parser")

        for item in soup.select("article a"):
            link = urljoin(page, item.get("href"))
            title = item.get_text(strip=True)

            # Filtros fixos
            if INCLUDE_KEYWORDS and not any(k.lower() in title.lower() for k in INCLUDE_KEYWORDS):
                continue
            if EXCLUDE_KEYWORDS and any(k.lower() in title.lower() for k in EXCLUDE_KEYWORDS):
                continue

            # Pegar conteúdo da notícia
            try:
                news_resp = requests.get(link)
                news_soup = BeautifulSoup(news_resp.text, "html.parser")
                content = " ".join([p.get_text(strip=True) for p in news_soup.select("article p")])
                img_tag = news_soup.select_one("article img")
                img_url = urljoin(link, img_tag["src"]) if img_tag else None
            except Exception:
                content = ""
                img_url = None

            fe = fg.add_entry()
            fe.title(title)
            fe.link(href=link)
            fe.description(content)
            if img_url:
                fe.enclosure(img_url, 0, "image/jpeg")
            fe.pubDate(datetime.utcnow())

    return fg.rss_str(pretty=True)

@app.route("/feed.xml")
def feed():
    rss_feed = get_news()
    return Response(rss_feed, mimetype="application/rss+xml")
