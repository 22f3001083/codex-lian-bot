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

# Railway's public domain should be stored in BASE_URL.
# Example:
# https://your-app.up.railway.app
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
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


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
                self.end_headers()
                self.wfile.write(b"run.jsonl not found")

        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Keep Railway logs clean.
        pass


def start_http_server():
    server = ThreadingHTTPServer(
        ("0.0.0.0", PORT),
        LogRequestHandler
    )

    print(f"HTTP server running on port {PORT}")
    server.serve_forever()


# ============================================================
# Telegram message handler
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    user_text = update.message.text

    # Log incoming message.
    log_event({
        "type": "incoming",
        "chat_id": chat_id,
        "text": user_text
    })

    # Get conversation history for this Telegram chat.
    history = conversation_history.setdefault(chat_id, [])

    history.append({
        "role": "user",
        "content": user_text
    })

    # ========================================================
    # System prompt
    # ========================================================

    system_prompt = (
        "You are a careful data analyst. "
        "Answer the user's LAST data-analysis question accurately. "
        "The conversation may contain multiple messages that provide context. "
        "Use relevant earlier messages when necessary. "
        "The grader's message specifies the exact JSON shape required for "
        "the answer. Follow that requested answer shape exactly, even if "
        "the latest message does not repeat it. "
        "Return ONLY the JSON object representing the ANSWER portion. "
        "Do NOT include 'log_url'. "
        "Do NOT include an outer 'answer' key. "
        "Do NOT include markdown, explanations, or code fences."
    )

    # ========================================================
    # Ask AI Pipe / GPT
    # ========================================================

    response = client.chat.completions.create(
        model="gpt-5-mini",
        messages=[
            {
                "role": "system",
                "content": system_prompt
            }
        ] + history[-6:]
    )

    reply_text = response.choices[0].message.content.strip()

    # Keep AI response in conversation history.
    history.append({
        "role": "assistant",
        "content": reply_text
    })

    # ========================================================
    # Parse AI's answer JSON
    # ========================================================

    try:
        answer = json.loads(reply_text)

    except json.JSONDecodeError:

        start = reply_text.find("{")
        end = reply_text.rfind("}")

        if start == -1 or end == -1:
            raise ValueError(
                "Model did not return a JSON object."
            )

        answer = json.loads(
            reply_text[start:end + 1]
        )

    # If the model accidentally included an outer "answer",
    # remove it so Python can construct the official wrapper.
    if (
        isinstance(answer, dict)
        and "answer" in answer
        and len(answer) == 1
    ):
        answer = answer["answer"]

    # ========================================================
    # Construct EXACT grader-required response
    # ========================================================

    final_reply = json.dumps(
        {
            "answer": answer,
            "log_url": LOG_URL
        },
        ensure_ascii=False,
        separators=(",", ":")
    )

    # Log final response.
    log_event({
        "type": "outgoing",
        "chat_id": chat_id,
        "text": final_reply
    })

    # Send exact JSON to Telegram.
    await update.message.reply_text(final_reply)


# ============================================================
# Start application
# ============================================================

if __name__ == "__main__":

    # Start HTTP server in background.
    http_thread = threading.Thread(
        target=start_http_server,
        daemon=True
    )

    http_thread.start()

    # Build Telegram application.
    app = ApplicationBuilder().token(
        TELEGRAM_BOT_TOKEN
    ).build()

    # Handle normal text messages.
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    print("Bot is running... (Ctrl+C to stop)")
    print(f"Public log URL: {LOG_URL}")

    # Start Telegram polling.
    app.run_polling()
