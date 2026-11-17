import hmac
import logging
import os
import re
import threading
import time
import uuid

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

# --- Ollama (LLM local) ---
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "60"))

# Prompt de sistema do assistente. Ele e enviado ao Ollama e tambem ao
# AI Guardrails com role "system" (assim ele nao e confundido com um ataque).
# E o alvo natural dos testes de "system prompt extraction" do AI Red Teaming.
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "").strip()

# --- Check Point AI Guardrails (API Lakera Guard v2) ---
# Endpoints regionais: https://api.lakera.ai (padrao), https://us.api.lakera.ai,
# https://eu.api.lakera.ai, https://ap-southeast-1.api.lakera.ai
LAKERA_API_URL = os.getenv("LAKERA_API_URL", "https://api.lakera.ai/v2/guard")
LAKERA_API_KEY = os.getenv("LAKERA_API_KEY") or os.getenv("LAKERA_GUARD_API_KEY", "")
LAKERA_PROJECT_ID = os.getenv("LAKERA_PROJECT_ID", "")
LAKERA_TIMEOUT = int(os.getenv("LAKERA_TIMEOUT", "10"))
LAKERA_SCREEN_OUTPUT = os.getenv("LAKERA_SCREEN_OUTPUT", "true").lower() == "true"
LAKERA_RETRY_STATUS = {429, 503}

# --- Check Point AI Red Teaming (endpoint dedicado para o scanner) ---
REDTEAM_API_TOKEN = os.getenv("REDTEAM_API_TOKEN", "")
REDTEAM_BLOCK_MESSAGE = os.getenv("REDTEAM_BLOCK_MESSAGE", "Sorry, I cannot help with that request.")
REDTEAM_MIN_TOKEN_LENGTH = 32
REDTEAM_MAX_SESSIONS = 1000
REDTEAM_SESSION_TTL = 3600
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:@;=-]{1,128}$")

MAX_MESSAGE_LENGTH = 8000
MAX_HISTORY_MESSAGES = 20

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = app.logger

# Estado do laboratorio em memoria. Valido apenas para 1 processo/worker
# (ver README: rode com "gunicorn -w 1 --threads N" ou o servidor de dev do Flask).
STATE = {"lakera_enabled": os.getenv("LAKERA_ENABLED", "true").lower() == "true" and bool(LAKERA_API_KEY)}

# Historico das conversas do AI Red Teaming (contrato "Stateful"), por sessionId.
REDTEAM_SESSIONS = {}
REDTEAM_LOCK = threading.Lock()


class GuardUnavailable(Exception):
    pass


def with_system_prompt(conversation):
    if not SYSTEM_PROMPT:
        return conversation
    return [{"role": "system", "content": SYSTEM_PROMPT}] + conversation


def call_ollama(conversation):
    resp = requests.post(
        f"{OLLAMA_HOST}/api/chat",
        json={"model": OLLAMA_MODEL, "messages": with_system_prompt(conversation), "stream": False},
        timeout=OLLAMA_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def call_guardrails(conversation, metadata):
    payload = {
        "messages": with_system_prompt(conversation),
        "breakdown": True,
        "metadata": metadata,
    }
    if LAKERA_PROJECT_ID:
        payload["project_id"] = LAKERA_PROJECT_ID
    headers = {
        "Authorization": f"Bearer {LAKERA_API_KEY}",
        "Content-Type": "application/json",
    }
    for attempt in range(2):
        try:
            resp = requests.post(LAKERA_API_URL, json=payload, headers=headers, timeout=LAKERA_TIMEOUT)
            if resp.status_code in LAKERA_RETRY_STATUS and attempt == 0:
                time.sleep(1)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 0 and isinstance(exc, (requests.ConnectionError, requests.Timeout)):
                continue
            raise GuardUnavailable(str(exc)) from exc
    raise GuardUnavailable("sem resposta valida do AI Guardrails")


def detected_types(guard_result):
    seen = []
    for item in guard_result.get("breakdown") or []:
        if item.get("detected"):
            name = item.get("detector_type") or item.get("detector_id") or "desconhecido"
            if name not in seen:
                seen.append(name)
    return seen


def screen(stage, conversation, metadata):
    """Retorna (bloquear, info). Fail-closed se o AI Guardrails nao responder."""
    try:
        result = call_guardrails(conversation, metadata)
    except GuardUnavailable:
        log.exception("Falha ao chamar o AI Guardrails (%s), bloqueando por seguranca (fail-closed)", stage)
        return True, {"stage": stage, "categories": ["guardrails_indisponivel"], "mode": None, "request_uuid": None}

    info = {
        "stage": stage,
        "categories": detected_types(result),
        "mode": result.get("action"),
        "request_uuid": (result.get("metadata") or {}).get("request_uuid"),
    }
    if info["categories"]:
        log.info(
            "AI Guardrails detectou %s (etapa=%s, modo=%s, request_uuid=%s, origem=%s)",
            info["categories"], stage, info["mode"], info["request_uuid"], metadata.get("user_id"),
        )
    return bool(result.get("flagged")), info


def process_turn(conversation, metadata):
    """Fluxo completo: input -> Guardrails -> Ollama -> Guardrails (output)."""
    guard_active = STATE["lakera_enabled"] and bool(LAKERA_API_KEY)
    detections = []

    if guard_active:
        blocked, info = screen("input", conversation, metadata)
        if blocked:
            return {"blocked": True, **info}
        if info["categories"]:
            detections.append(info)

    reply_text = call_ollama(conversation)

    if guard_active and LAKERA_SCREEN_OUTPUT:
        conversation_with_reply = conversation + [{"role": "assistant", "content": reply_text}]
        blocked, info = screen("output", conversation_with_reply, metadata)
        if blocked:
            return {"blocked": True, **info}
        if info["categories"]:
            detections.append(info)

    return {"blocked": False, "reply": reply_text, "detections": detections}


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


# ---------------------------------------------------------------------------
# Interface web do laboratorio
# ---------------------------------------------------------------------------


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
            "project_configured": bool(LAKERA_PROJECT_ID),
            "redteam_enabled": len(REDTEAM_API_TOKEN) >= REDTEAM_MIN_TOKEN_LENGTH,
        }
    )


@app.post("/api/toggle")
def toggle():
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    if enabled and not LAKERA_API_KEY:
        return jsonify({"error": "LAKERA_API_KEY nao esta configurada no .env"}), 400
    STATE["lakera_enabled"] = enabled
    log.info("AI Guardrails %s via toggle da interface", "ativado" if enabled else "desativado")
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
    metadata = {"user_id": "chat-ui", "internal_request_id": uuid.uuid4().hex}

    try:
        result = process_turn(conversation, metadata)
    except requests.RequestException:
        log.exception("Falha ao chamar o Ollama")
        return jsonify({"error": "Nao foi possivel falar com o modelo local (Ollama)."}), 502
    return jsonify(result)


# ---------------------------------------------------------------------------
# Endpoint para o Check Point AI Red Teaming (target do tipo "Agent", Stateful)
#   Request:  {"message": "...", "sessionId": "..." (opcional na 1a chamada)}
#   Response: {"message": "...", "sessionId": "..."}
# Autenticacao: Authorization: Bearer <REDTEAM_API_TOKEN>
# A protecao segue o mesmo switch da interface: rode um scan com o AI Guardrails
# desligado e outro ligado para comparar os resultados.
# ---------------------------------------------------------------------------


def redteam_authorized():
    if len(REDTEAM_API_TOKEN) < REDTEAM_MIN_TOKEN_LENGTH:
        return False
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):].encode(), REDTEAM_API_TOKEN.encode())


def redteam_get_history(session_id):
    now = time.time()
    with REDTEAM_LOCK:
        expired = [sid for sid, s in REDTEAM_SESSIONS.items() if now - s["ts"] > REDTEAM_SESSION_TTL]
        for sid in expired:
            del REDTEAM_SESSIONS[sid]
        session = REDTEAM_SESSIONS.get(session_id)
        return list(session["history"]) if session else []


def redteam_save_history(session_id, history):
    with REDTEAM_LOCK:
        REDTEAM_SESSIONS[session_id] = {"history": history[-MAX_HISTORY_MESSAGES:], "ts": time.time()}
        if len(REDTEAM_SESSIONS) > REDTEAM_MAX_SESSIONS:
            oldest = min(REDTEAM_SESSIONS, key=lambda sid: REDTEAM_SESSIONS[sid]["ts"])
            del REDTEAM_SESSIONS[oldest]


@app.post("/api/redteam/chat")
def redteam_chat():
    if len(REDTEAM_API_TOKEN) < REDTEAM_MIN_TOKEN_LENGTH:
        return jsonify({"error": "endpoint de red teaming desabilitado (REDTEAM_API_TOKEN ausente ou curto)"}), 503
    if not redteam_authorized():
        return jsonify({"error": "nao autorizado"}), 401

    data = request.get_json(silent=True) or {}
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        return jsonify({"error": "campo 'message' obrigatorio"}), 400
    message = message.strip()[:MAX_MESSAGE_LENGTH]

    session_id = data.get("sessionId")
    if session_id is None or session_id == "":
        session_id = uuid.uuid4().hex
    elif not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        return jsonify({"error": "sessionId invalido"}), 400

    history = redteam_get_history(session_id)
    conversation = history + [{"role": "user", "content": message}]
    metadata = {"user_id": "ai-red-teaming", "session_id": session_id, "internal_request_id": uuid.uuid4().hex}

    try:
        result = process_turn(conversation, metadata)
    except requests.RequestException:
        log.exception("Falha ao chamar o Ollama (red teaming)")
        return jsonify({"error": "modelo local indisponivel"}), 502

    reply = REDTEAM_BLOCK_MESSAGE if result["blocked"] else result["reply"]
    redteam_save_history(session_id, conversation + [{"role": "assistant", "content": reply}])
    return jsonify({"message": reply, "sessionId": session_id})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
