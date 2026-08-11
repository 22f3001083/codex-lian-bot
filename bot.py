````python
import json
import os
import time
import threading
import re
from html import unescape
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from xml.etree import ElementTree as ET
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()


# ============================================================
# Configuration
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
AIPIPE_API_KEY = os.getenv("AIPIPE_API_KEY")

TELEGRAM_BOT_TOKEN = TELEGRAM_TOKEN or BOT_TOKEN
AIPIPE_TOKEN = AIPIPE_API_KEY

PORT = int(os.getenv("PORT", "8000"))

# Railway public URL.
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

LOG_FILE = "run.jsonl"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "Missing Telegram bot token. "
        "Set BOT_TOKEN or TELEGRAM_TOKEN."
    )

if not AIPIPE_TOKEN:
    raise RuntimeError(
        "Missing AIPIPE_API_KEY."
    )

if not BASE_URL:
    raise RuntimeError(
        "Missing BASE_URL. Set BASE_URL to your public Railway URL."
    )

LOG_URL = f"{BASE_URL}/run.jsonl"


# ============================================================
# AI Pipe / OpenAI client
# ============================================================

client = OpenAI(
    base_url="https://aipipe.org/openai/v1",
    api_key=AIPIPE_TOKEN,
)


# ============================================================
# Conversation history
# ============================================================

conversation_history = {}


# ============================================================
# Logging
# ============================================================

def log_event(event: dict):
    event["timestamp"] = time.time()

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                event,
                ensure_ascii=False
            ) + "\n"
        )


# ============================================================
# Public run.jsonl HTTP server
# ============================================================

class LogRequestHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path == "/run.jsonl":

            try:
                with open(LOG_FILE, "rb") as f:
                    content = f.read()

                self.send_response(200)

                self.send_header(
                    "Content-Type",
                    "application/jsonl; charset=utf-8"
                )

                self.send_header(
                    "Content-Length",
                    str(len(content))
                )

                self.send_header(
                    "Access-Control-Allow-Origin",
                    "*"
                )

                self.end_headers()

                self.wfile.write(content)

            except FileNotFoundError:

                self.send_response(404)

                self.send_header(
                    "Content-Type",
                    "text/plain; charset=utf-8"
                )

                self.end_headers()

                self.wfile.write(
                    b"run.jsonl not found"
                )

        elif self.path == "/health":

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain; charset=utf-8"
            )

            self.end_headers()

            self.wfile.write(b"OK")

        else:

            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def start_http_server():

    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        LogRequestHandler
    )

    print(
        f"HTTP server running on port {PORT}"
    )

    server.serve_forever()


# ============================================================
# HTML text extraction
# ============================================================

class TextExtractor(HTMLParser):

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip_depth = 0
        self.skip_tags = {
            "script",
            "style",
            "noscript",
            "svg",
            "iframe"
        }

    def handle_starttag(self, tag, attrs):

        if tag.lower() in self.skip_tags:
            self.skip_depth += 1

    def handle_endtag(self, tag):

        if tag.lower() in self.skip_tags and self.skip_depth > 0:
            self.skip_depth -= 1

    def handle_data(self, data):

        if self.skip_depth == 0:
            text = data.strip()

            if text:
                self.parts.append(text)

    def get_text(self):

        text = " ".join(self.parts)

        text = unescape(text)

        text = re.sub(
            r"\s+",
            " ",
            text
        )

        return text.strip()


# ============================================================
# Fetch public webpage
# ============================================================

def fetch_webpage(url, max_chars=5000):

    try:

        request = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "Chrome/131.0 Safari/537.36"
                )
            }
        )

        with urlopen(
            request,
            timeout=12
        ) as response:

            content_type = response.headers.get(
                "Content-Type",
                ""
            ).lower()

            if (
                "text/html" not in content_type
                and "application/xhtml" not in content_type
            ):
                return ""

            raw = response.read(
                400000
            )

        html = raw.decode(
            "utf-8",
            errors="ignore"
        )

        parser = TextExtractor()

        parser.feed(html)

        text = parser.get_text()

        return text[:max_chars]

    except (
        HTTPError,
        URLError,
        TimeoutError,
        ValueError,
        Exception
    ):

        return ""


# ============================================================
# Google News RSS public search
# ============================================================

def google_news_search(query, limit=6):

    rss_url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=en-IN"
        + "&gl=IN"
        + "&ceid=IN:en"
    )

    try:

        request = Request(
            rss_url,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        with urlopen(
            request,
            timeout=12
        ) as response:

            xml_data = response.read(
                500000
            )

        root = ET.fromstring(xml_data)

        results = []

        for item in root.findall(".//item"):

            title_element = item.find("title")
            link_element = item.find("link")
            description_element = item.find("description")
            pubdate_element = item.find("pubDate")

            title = (
                title_element.text.strip()
                if title_element is not None
                and title_element.text
                else ""
            )

            link = (
                link_element.text.strip()
                if link_element is not None
                and link_element.text
                else ""
            )

            description = (
                description_element.text
                if description_element is not None
                and description_element.text
                else ""
            )

            pubdate = (
                pubdate_element.text.strip()
                if pubdate_element is not None
                and pubdate_element.text
                else ""
            )

            description = unescape(
                re.sub(
                    r"<[^>]+>",
                    " ",
                    description
                )
            )

            description = re.sub(
                r"\s+",
                " ",
                description
            ).strip()

            if not title or not link:
                continue

            results.append({
                "title": title,
                "url": link,
                "description": description[:1200],
                "published": pubdate
            })

            if len(results) >= limit:
                break

        return results

    except (
        HTTPError,
        URLError,
        TimeoutError,
        ET.ParseError,
        Exception
    ):

        return []


# ============================================================
# Detect whether public data search is useful
# ============================================================

def needs_public_data_search(text):

    lower = text.lower()

    data_keywords = [
        "mospi",
        "mo spi",
        "ministry of statistics",
        "government data",
        "official data",
        "official statistics",
        "latest",
        "current",
        "recent",
        "2025",
        "2026",
        "gdp",
        "gross domestic product",
        "cpi",
        "consumer price",
        "inflation",
        "iip",
        "industrial production",
        "plfs",
        "labour",
        "labor",
        "employment",
        "unemployment",
        "population",
        "census",
        "annual survey",
        "asi",
        "asuse",
        "nss",
        "national sample survey",
        "india statistics",
        "government statistics",
        "data.gov.in",
        "according to",
        "according to mospi",
        "according to the government",
        "according to official",
        "public data",
        "dataset",
        "statistics"
    ]

    for keyword in data_keywords:

        if keyword in lower:
            return True

    return False


# ============================================================
# Build public-data search context
# ============================================================

def search_public_data(user_question):

    searches = []

    lower = user_question.lower()

    # --------------------------------------------------------
    # Always search the exact question for public-data queries.
    # --------------------------------------------------------

    searches.append(
        user_question
    )

    # --------------------------------------------------------
    # MoSPI-specific search.
    # --------------------------------------------------------

    if any(
        keyword in lower
        for keyword in [
            "mospi",
            "mo spi",
            "gdp",
            "gross domestic product",
            "cpi",
            "consumer price",
            "inflation",
            "iip",
            "industrial production",
            "plfs",
            "labour",
            "labor",
            "employment",
            "unemployment",
            "asuse",
            "annual survey",
            "nss",
            "national sample survey"
        ]
    ):

        searches.append(
            "site:mospi.gov.in " + user_question
        )

    # --------------------------------------------------------
    # Government data portal search.
    # --------------------------------------------------------

    if any(
        keyword in lower
        for keyword in [
            "government",
            "dataset",
            "data",
            "statistics",
            "population",
            "census"
        ]
    ):

        searches.append(
            "site:data.gov.in " + user_question
        )

    all_results = []
    seen_urls = set()

    # --------------------------------------------------------
    # Perform searches.
    # --------------------------------------------------------

    for query in searches:

        results = google_news_search(
            query,
            limit=6
        )

        for result in results:

            url = result.get(
                "url",
                ""
            )

            if not url:
                continue

            if url in seen_urls:
                continue

            seen_urls.add(url)

            all_results.append(result)

            if len(all_results) >= 10:
                break

        if len(all_results) >= 10:
            break

    # --------------------------------------------------------
    # Fetch actual pages where possible.
    # --------------------------------------------------------

    enriched_results = []

    for result in all_results[:8]:

        page_text = fetch_webpage(
            result["url"],
            max_chars=4500
        )

        enriched_results.append({
            "title": result["title"],
            "url": result["url"],
            "published": result["published"],
            "search_snippet": result["description"],
            "page_content": page_text
        })

    # --------------------------------------------------------
    # Direct MoSPI homepage search fallback.
    #
    # This makes sure the model knows the official MoSPI
    # source exists even when Google News has poor coverage.
    # --------------------------------------------------------

    if any(
        keyword in lower
        for keyword in [
            "mospi",
            "gdp",
            "cpi",
            "plfs",
            "iip",
            "inflation",
            "employment",
            "unemployment",
            "statistics"
        ]
    ):

        mospi_url = (
            "https://www.mospi.gov.in/"
        )

        mospi_text = fetch_webpage(
            mospi_url,
            max_chars=5000
        )

        if mospi_text:

            enriched_results.append({
                "title": "Ministry of Statistics and Programme Implementation",
                "url": mospi_url,
                "published": "",
                "search_snippet": (
                    "Official Government of India "
                    "MoSPI website."
                ),
                "page_content": mospi_text
            })

    return enriched_results


# ============================================================
# Format search results for the model
# ============================================================

def build_search_context(results):

    if not results:
        return (
            "PUBLIC DATA SEARCH RESULT:\n"
            "No public search results were retrieved. "
            "Do not invent external data."
        )

    chunks = []

    for index, result in enumerate(
        results,
        start=1
    ):

        chunk = (
            f"SOURCE {index}\n"
            f"Title: {result.get('title', '')}\n"
            f"URL: {result.get('url', '')}\n"
            f"Published: {result.get('published', '')}\n"
            f"Search snippet: "
            f"{result.get('search_snippet', '')}\n"
            f"Page content: "
            f"{result.get('page_content', '')}\n"
        )

        chunks.append(
            chunk
        )

    return (
        "PUBLIC DATA SEARCH RESULTS\n"
        "Use these sources as evidence when answering "
        "the user's question.\n\n"
        + "\n--------------------\n".join(chunks)
    )


# ============================================================
# Extract JSON object from model response
# ============================================================

def extract_json_object(text):

    text = text.strip()

    # Direct JSON.
    try:

        return json.loads(
            text
        )

    except json.JSONDecodeError:
        pass

    # Remove markdown code fences.
    if text.startswith("```"):

        lines = text.splitlines()

        if lines:

            lines = lines[1:]

        if lines and lines[-1].strip() == "```":

            lines = lines[:-1]

        text = "\n".join(
            lines
        ).strip()

        try:

            return json.loads(
                text
            )

        except json.JSONDecodeError:
            pass

    # Extract outer JSON object.
    start = text.find("{")
    end = text.rfind("}")

    if (
        start == -1
        or end == -1
        or end <= start
    ):

        raise ValueError(
            "Model did not return a JSON object."
        )

    return json.loads(
        text[start:end + 1]
    )


# ============================================================
# Telegram message handler
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.message.text:
        return

    chat_id = update.effective_chat.id

    user_text = update.message.text.strip()

    # --------------------------------------------------------
    # Log incoming message.
    # --------------------------------------------------------

    log_event({
        "type": "incoming",
        "chat_id": chat_id,
        "text": user_text
    })

    # --------------------------------------------------------
    # Conversation history.
    # --------------------------------------------------------

    history = conversation_history.setdefault(
        chat_id,
        []
    )

    history.append({
        "role": "user",
        "content": user_text
    })

    # --------------------------------------------------------
    # Public-data search.
    # --------------------------------------------------------

    search_results = []

    if needs_public_data_search(
        user_text
    ):

        print(
            f"Public data search: {user_text}"
        )

        search_results = search_public_data(
            user_text
        )

        print(
            f"Public data sources found: "
            f"{len(search_results)}"
        )

    search_context = build_search_context(
        search_results
    )

    # --------------------------------------------------------
    # System prompt.
    # --------------------------------------------------------

    system_prompt = (
        "You are a careful data analyst and research agent. "

        "Answer the user's LAST data-analysis question accurately. "

        "The conversation may contain multiple messages that provide "
        "context. Use relevant earlier messages when necessary. "

        "When the question requires public, current, government, "
        "official, or external data, use the PUBLIC DATA SEARCH RESULTS "
        "provided below. "

        "Prefer official primary sources. For Indian government "
        "statistics, prefer Ministry of Statistics and Programme "
        "Implementation (MoSPI), Government of India, official "
        "government datasets, and other primary government sources. "

        "Do not claim that data was unavailable merely because the "
        "user did not paste the data into Telegram. The program may "
        "have retrieved the required public data for you. "

        "Use the retrieved source information to perform the actual "
        "reasoning, comparison, calculation, filtering, aggregation, "
        "or analysis required by the user's question. "

        "If the retrieved sources do not contain the required fact, "
        "do not invent a number. Use information from the conversation "
        "if available; otherwise return the requested JSON structure "
        "with an appropriate indication that the requested value could "
        "not be verified from the retrieved sources. "

        "The user's question specifies the exact JSON shape required "
        "for the ANSWER. Follow that requested shape exactly. "

        "Return ONLY the JSON object representing the ANSWER portion. "

        "Do NOT include an outer 'answer' key. "

        "Do NOT include 'log_url'. "

        "Do NOT include markdown. "

        "Do NOT include explanations outside the requested JSON. "

        "Do NOT include code fences. "

        "Do not change the requested JSON field names. "

        "The Python program will add the required outer "
        "'answer' and 'log_url' fields."
    )

    # --------------------------------------------------------
    # Messages sent to GPT-5 Mini.
    # --------------------------------------------------------

    messages = [
        {
            "role": "system",
            "content": system_prompt
        }
    ]

    # Add public-data evidence as a separate context message.
    if search_results:

        messages.append({
            "role": "system",
            "content": search_context
        })

    else:

        messages.append({
            "role": "system",
            "content": (
                "No external public-data search was necessary "
                "or no search results were retrieved."
            )
        })

    # Add recent conversation.
    messages.extend(
        history[-8:]
    )

    # --------------------------------------------------------
    # Ask AIPipe / OpenAI.
    # --------------------------------------------------------

    response = client.chat.completions.create(
        model="gpt-5-mini",
        messages=messages
    )

    # --------------------------------------------------------
    # Get model response.
    # --------------------------------------------------------

    if not response.choices:

        raise ValueError(
            "Model returned no choices."
        )

    reply_content = response.choices[0].message.content

    if not reply_content:

        raise ValueError(
            "Model returned an empty response."
        )

    reply_text = reply_content.strip()

    # --------------------------------------------------------
    # Save model response to history.
    # --------------------------------------------------------

    history.append({
        "role": "assistant",
        "content": reply_text
    })

    # --------------------------------------------------------
    # Parse JSON.
    # --------------------------------------------------------

    answer = extract_json_object(
        reply_text
    )

    # --------------------------------------------------------
    # Normalize accidental outer answer.
    # --------------------------------------------------------

    if (
        isinstance(answer, dict)
        and "answer" in answer
        and len(answer) == 1
    ):

        answer = answer["answer"]

    # --------------------------------------------------------
    # Construct exact grader response.
    # --------------------------------------------------------

    final_reply = json.dumps(
        {
            "answer": answer,
            "log_url": LOG_URL
        },
        ensure_ascii=False,
        separators=(",", ":")
    )

    # --------------------------------------------------------
    # Log outgoing response.
    # --------------------------------------------------------

    log_event({
        "type": "outgoing",
        "chat_id": chat_id,
        "text": final_reply
    })

    # --------------------------------------------------------
    # Send response.
    # --------------------------------------------------------

    await update.message.reply_text(
        final_reply
    )


# ============================================================
# Telegram error handler
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    error = context.error

    print(
        f"Telegram handler error: {error}"
    )

    try:

        if isinstance(
            update,
            Update
        ):

            if update.effective_chat:

                log_event({
                    "type": "error",
                    "chat_id": update.effective_chat.id,
                    "error": str(error)
                })

    except Exception as logging_error:

        print(
            f"Error while logging error: "
            f"{logging_error}"
        )


# ============================================================
# Start application
# ============================================================

if __name__ == "__main__":

    # --------------------------------------------------------
    # Start public HTTP server.
    # --------------------------------------------------------

    http_thread = threading.Thread(
        target=start_http_server,
        daemon=True
    )

    http_thread.start()

    # --------------------------------------------------------
    # Build Telegram application.
    # --------------------------------------------------------

    app = (
        ApplicationBuilder()
        .token(
            TELEGRAM_BOT_TOKEN
        )
        .build()
    )

    # --------------------------------------------------------
    # Message handler.
    # --------------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    # --------------------------------------------------------
    # Error handler.
    # --------------------------------------------------------

    app.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # Startup messages.
    # --------------------------------------------------------

    print(
        "Bot is running... (Ctrl+C to stop)"
    )

    print(
        f"Public log URL: {LOG_URL}"
    )

    # --------------------------------------------------------
    # Start Telegram polling.
    # --------------------------------------------------------

    app.run_polling()
````
