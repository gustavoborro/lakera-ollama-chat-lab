import logging
import os

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "60"))

LAKERA_API_URL = os.getenv("LAKERA_API_URL", "https://api.lakera.ai/v2/guard")
LAKERA_API_KEY = os.getenv("LAKERA_API_KEY", "")
LAKERA_PROJECT_ID = os.getenv("LAKERA_PROJECT_ID", "")
LAKERA_TIMEOUT = int(os.getenv("LAKERA_TIMEOUT", "10"))
LAKERA_SCREEN_OUTPUT = os.getenv("LAKERA_SCREEN_OUTPUT", "true").lower() == "true"

MAX_MESSAGE_LENGTH = 4000
MAX_HISTORY_MESSAGES = 20

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = app.logger

# Estado do laboratorio em memoria. Valido apenas para 1 processo/worker
# (ver README: rode com "gunicorn -w 1" ou o servidor de dev do Flask).
STATE = {"lakera_enabled": os.getenv("LAKERA_ENABLED", "true").lower() == "true" and bool(LAKERA_API_KEY)}


def call_ollama(messages):
    resp = requests.post(
        f"{OLLAMA_HOST}/api/chat",
        json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
        timeout=OLLAMA_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def call_lakera_guard(messages):
    payload = {"messages": messages, "breakdown": True}
    if LAKERA_PROJECT_ID:
        payload["project_id"] = LAKERA_PROJECT_ID
    headers = {
        "Authorization": f"Bearer {LAKERA_API_KEY}",
        "Content-Type": "application/json",
    }
    resp = requests.post(LAKERA_API_URL, json=payload, headers=headers, timeout=LAKERA_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def flagged_categories(guard_result):
    breakdown = guard_result.get("breakdown") or []
    return [item.get("category", item.get("detector", "desconhecido")) for item in breakdown if item.get("flagged")]


def sanitize_history(raw_history):
    if not isinstance(raw_history, list):
        return []
    cleaned = []
    for item in raw_history[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            cleaned.append({"role": role, "content": content[:MAX_MESSAGE_LENGTH]})
    return cleaned


@app.get("/")
def index():
    return render_template("index.html", model=OLLAMA_MODEL)


@app.get("/api/status")
def status():
    return jsonify(
        {
            "model": OLLAMA_MODEL,
            "lakera_enabled": STATE["lakera_enabled"],
            "lakera_configured": bool(LAKERA_API_KEY),
        }
    )


@app.post("/api/toggle")
def toggle():
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    if enabled and not LAKERA_API_KEY:
        return jsonify({"error": "LAKERA_API_KEY nao esta configurada no .env"}), 400
    STATE["lakera_enabled"] = enabled
    log.info("Lakera Guard %s via toggle da interface", "ativado" if enabled else "desativado")
    return jsonify({"enabled": STATE["lakera_enabled"]})


@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "mensagem vazia"}), 400
    if len(message) > MAX_MESSAGE_LENGTH:
        return jsonify({"error": "mensagem muito longa"}), 400

    history = sanitize_history(data.get("history"))
    conversation = history + [{"role": "user", "content": message}]

    lakera_active = STATE["lakera_enabled"] and bool(LAKERA_API_KEY)

    if lakera_active:
        try:
            guard_result = call_lakera_guard(conversation)
        except requests.RequestException:
            log.exception("Falha ao chamar o Lakera Guard, bloqueando por seguranca (fail-closed)")
            return jsonify({"blocked": True, "stage": "input", "categories": ["lakera_indisponivel"]})

        if guard_result.get("flagged"):
            return jsonify({"blocked": True, "stage": "input", "categories": flagged_categories(guard_result)})

    try:
        reply_text = call_ollama(conversation)
    except requests.RequestException:
        log.exception("Falha ao chamar o Ollama")
        return jsonify({"error": "Nao foi possivel falar com o modelo local (Ollama)."}), 502

    if lakera_active and LAKERA_SCREEN_OUTPUT:
        conversation_with_reply = conversation + [{"role": "assistant", "content": reply_text}]
        try:
            guard_result_out = call_lakera_guard(conversation_with_reply)
        except requests.RequestException:
            log.exception("Falha ao chamar o Lakera Guard na saida, bloqueando por seguranca (fail-closed)")
            return jsonify({"blocked": True, "stage": "output", "categories": ["lakera_indisponivel"]})

        if guard_result_out.get("flagged"):
            return jsonify({"blocked": True, "stage": "output", "categories": flagged_categories(guard_result_out)})

    return jsonify({"blocked": False, "reply": reply_text})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
