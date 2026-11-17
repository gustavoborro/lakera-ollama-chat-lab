const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("message-input");
const switchEl = document.getElementById("lakera-switch");
const stateEl = document.getElementById("lakera-state");

let history = [];
let lakeraConfigured = false;

function addMessage(role, text, trace) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = text;
  if (trace) {
    const span = document.createElement("span");
    span.className = "trace";
    span.textContent = trace;
    div.appendChild(span);
  }
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function etapaLabel(stage) {
  return stage === "output" ? "na resposta do modelo" : "na sua mensagem";
}

function traceLabel(info) {
  const parts = [];
  if (info.mode) parts.push(`modo: ${info.mode}`);
  if (info.request_uuid) parts.push(`request_uuid: ${info.request_uuid}`);
  return parts.join(" | ");
}

function renderLakeraState(enabled) {
  switchEl.checked = enabled;
  stateEl.textContent = enabled ? "ATIVO" : "DESATIVADO";
  stateEl.className = `lakera-state ${enabled ? "on" : "off"}`;
}

async function loadStatus() {
  const resp = await fetch("/api/status");
  const data = await resp.json();
  lakeraConfigured = data.lakera_configured;
  renderLakeraState(data.lakera_enabled);
  if (!lakeraConfigured) {
    addMessage("system", "Aviso: LAKERA_API_KEY nao configurada — o toggle nao tera efeito.");
  } else if (!data.project_configured) {
    addMessage("system", "Aviso: LAKERA_PROJECT_ID vazio — usando a AI Guardrails Default Policy (bem restritiva).");
  }
  if (data.redteam_enabled) {
    addMessage("system", "Endpoint do AI Red Teaming habilitado em /api/redteam/chat. O switch acima tambem vale para os scans.");
  }
}

switchEl.addEventListener("change", async () => {
  const desired = switchEl.checked;
  try {
    const resp = await fetch("/api/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: desired }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      addMessage("system", `Nao foi possivel alterar o AI Guardrails: ${data.error}`);
      switchEl.checked = !desired;
      return;
    }
    renderLakeraState(data.enabled);
    addMessage("system", `AI Guardrails ${data.enabled ? "ativado" : "desativado"}.`);
  } catch (err) {
    switchEl.checked = !desired;
    addMessage("system", "Erro de rede ao alterar o AI Guardrails.");
  }
});

formEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return;

  addMessage("user", text);
  inputEl.value = "";
  inputEl.disabled = true;

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, history }),
    });
    const data = await resp.json();

    if (!resp.ok) {
      addMessage("system", data.error || "Erro desconhecido.");
      return;
    }

    if (data.blocked) {
      const categorias = (data.categories || []).join(", ") || "motivo nao especificado";
      addMessage(
        "blocked",
        `Bloqueado pelo Check Point AI Guardrails ${etapaLabel(data.stage)}. Detectores: ${categorias}`,
        traceLabel(data)
      );
      return;
    }

    // Projeto em modo Detect: nada e bloqueado, mas as deteccoes aparecem aqui e no portal.
    for (const det of data.detections || []) {
      addMessage(
        "detected",
        `Detectado ${etapaLabel(det.stage)} (sem bloqueio): ${det.categories.join(", ")}`,
        traceLabel(det)
      );
    }

    history.push({ role: "user", content: text });
    history.push({ role: "assistant", content: data.reply });
    addMessage("assistant", data.reply);
  } catch (err) {
    addMessage("system", "Erro de rede ao falar com o servidor.");
  } finally {
    inputEl.disabled = false;
    inputEl.focus();
  }
});

loadStatus();
