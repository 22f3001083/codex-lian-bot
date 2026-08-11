import json
import os
import time
import threading
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

BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

LOG_FILE = "run.jsonl"


# ============================================================
# Validate configuration
# ============================================================

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "Missing Telegram bot token. "
        "Set BOT_TOKEN or TELEGRAM_TOKEN in Railway Variables."
    )

if not AIPIPE_TOKEN:
    raise RuntimeError(
        "Missing AIPIPE_API_KEY. "
        "Set AIPIPE_API_KEY in Railway Variables."
    )

if not BASE_URL:
    raise RuntimeError(
        "Missing BASE_URL. "
        "Set BASE_URL to your public Railway URL."
    )


LOG_URL = f"{BASE_URL}/run.jsonl"


# ============================================================
# AI Pipe OpenRouter client
# ============================================================

client = OpenAI(
    base_url="https://aipipe.org/openrouter/v1",
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

            self.wfile.write(
                b"OK"
            )

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
# Extract JSON object from model response
# ============================================================

def extract_json_object(text: str):

    text = text.strip()

    # First try the complete response directly.
    try:
        return json.loads(text)

    except json.JSONDecodeError:
        pass

    # Remove possible markdown code fences.
    if text.startswith("```"):

        lines = text.splitlines()

        if len(lines) >= 3:

            if lines[0].strip().startswith("```"):
                lines = lines[1:]

            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]

            text = "\n".join(lines).strip()

            try:
                return json.loads(text)

            except json.JSONDecodeError:
                pass

    # Last-resort extraction of the outermost JSON object.
    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
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
    # Log incoming message
    # --------------------------------------------------------

    log_event({
        "type": "incoming",
        "chat_id": chat_id,
        "text": user_text
    })

    # --------------------------------------------------------
    # Get conversation history
    # --------------------------------------------------------

    history = conversation_history.setdefault(
        chat_id,
        []
    )

    history.append({
        "role": "user",
        "content": user_text
    })

    # Keep enough previous messages for multi-turn questions.
    recent_history = history[-12:]

    # --------------------------------------------------------
    # System prompt
    # --------------------------------------------------------

    system_prompt = (
        "You are a careful data analyst and research agent. "

        "Answer the user's LAST data-analysis question accurately. "

        "The conversation may contain multiple messages that provide "
        "context. Use relevant earlier messages when necessary. "

        "Some questions may refer to public datasets, government "
        "reports, websites, or other external sources. "

        "When external information is required, use the available "
        "web search tool to retrieve the necessary information. "

        "Prefer official and primary sources whenever possible, "
        "especially government websites and official datasets. "

        "Do the actual reasoning, comparison, calculation, filtering, "
        "aggregation, or analysis required by the user's question. "

        "The user's message specifies the exact JSON shape required "
        "for the ANSWER. Follow that shape exactly. "

        "Return ONLY the JSON object representing the ANSWER portion. "

        "Do NOT include an outer 'answer' key. "

        "Do NOT include 'log_url'. "

        "Do NOT include citations. "

        "Do NOT include markdown. "

        "Do NOT include explanations. "

        "Do NOT include code fences. "

        "The Python program will add the required outer "
        "'answer' and 'log_url' fields."
    )

    # --------------------------------------------------------
    # Messages sent to the model
    # --------------------------------------------------------

    messages = [
        {
            "role": "system",
            "content": system_prompt
        }
    ] + recent_history

    # --------------------------------------------------------
    # AI Pipe → OpenRouter → GPT-5 Mini
    #
    # OpenRouter's web search is supplied as a server-side tool.
    # AI Pipe proxies the OpenRouter endpoint.
    # --------------------------------------------------------

    response = client.chat.completions.create(

        model="openai/gpt-5-mini",

        messages=messages,

        response_format={
            "type": "json_object"
        },

        extra_body={
            "tools": [
                {
                    "type": "openrouter:web_search"
                }
            ]
        }
    )

    # --------------------------------------------------------
    # Get model response
    # --------------------------------------------------------

    reply_text = response.choices[0].message.content

    if not reply_text:
        raise ValueError(
            "Model returned an empty response."
        )

    reply_text = reply_text.strip()

    # --------------------------------------------------------
    # Save model response in conversation history
    # --------------------------------------------------------

    history.append({
        "role": "assistant",
        "content": reply_text
    })

    # --------------------------------------------------------
    # Parse model's JSON
    # --------------------------------------------------------

    answer = extract_json_object(
        reply_text
    )

    # --------------------------------------------------------
    # Safety normalization
    #
    # If the model accidentally returns:
    #
    # {"answer": {"state": "Assam"}}
    #
    # unwrap it.
    # --------------------------------------------------------

    if (
        isinstance(answer, dict)
        and "answer" in answer
        and len(answer) == 1
    ):
        answer = answer["answer"]

    # --------------------------------------------------------
    # Construct exact grader response
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
    # Log outgoing response
    # --------------------------------------------------------

    log_event({
        "type": "outgoing",
        "chat_id": chat_id,
        "text": final_reply
    })

    # --------------------------------------------------------
    # Send exact JSON to Telegram
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

        if isinstance(update, Update):

            if update.effective_chat:

                log_event({
                    "type": "error",
                    "chat_id": update.effective_chat.id,
                    "error": str(error)
                })

    except Exception as logging_error:

        print(
            f"Error while logging error: {logging_error}"
        )


# ============================================================
# Start application
# ============================================================

if __name__ == "__main__":

    # --------------------------------------------------------
    # Start public HTTP server
    # --------------------------------------------------------

    http_thread = threading.Thread(
        target=start_http_server,
        daemon=True
    )

    http_thread.start()

    # --------------------------------------------------------
    # Build Telegram application
    # --------------------------------------------------------

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # Message handler
    # --------------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    # --------------------------------------------------------
    # Error handler
    # --------------------------------------------------------

    app.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # Startup messages
    # --------------------------------------------------------

    print(
        "Bot is running... (Ctrl+C to stop)"
    )

    print(
        f"Public log URL: {LOG_URL}"
    )

    # --------------------------------------------------------
    # Start Telegram polling
    # --------------------------------------------------------

    app.run_polling()
