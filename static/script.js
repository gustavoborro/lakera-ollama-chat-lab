const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("message-input");
const switchEl = document.getElementById("lakera-switch");
const stateEl = document.getElementById("lakera-state");

let history = [];
let lakeraConfigured = false;

function addMessage(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = text;
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
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
      addMessage("system", `Nao foi possivel alterar o Lakera Guard: ${data.error}`);
      switchEl.checked = !desired;
      return;
    }
    renderLakeraState(data.enabled);
    addMessage("system", `Lakera Guard ${data.enabled ? "ativado" : "desativado"}.`);
  } catch (err) {
    switchEl.checked = !desired;
    addMessage("system", "Erro de rede ao alterar o Lakera Guard.");
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
      const etapa = data.stage === "output" ? "na resposta do modelo" : "na sua mensagem";
      addMessage("blocked", `Bloqueado pelo Lakera Guard ${etapa}. Categorias: ${categorias}`);
      return;
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
