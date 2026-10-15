"use strict";

const byId = (id) => document.getElementById(id);
let activeRequest = "";
let activePhone = "";
let polling = false;
let busy = false;
let pending = null;
let lastMessages = "";

function showError(message) {
  byId("error").textContent = message;
  byId("error").hidden = !message;
}

async function api(path, body) {
  const response = await fetch(path, body ? {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-ChatUI-Request": "1" },
    body: JSON.stringify(body),
  } : { cache: "no-store" });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}

function render(detail) {
  byId("status").textContent = detail.status.replaceAll("_", " ");
  const versions = detail.requirement_versions || [];
  byId("requirement").textContent = versions.length
    ? JSON.stringify(versions.at(-1).payload, null, 2)
    : "Waiting for extraction by the worker.";
  const approvals = (detail.approvals || []).filter((item) => item.status === "pending");
  byId("approvals").textContent = approvals.length
    ? `Operator action needed: ${approvals.map((item) => item.approval_type.replaceAll("_", " ")).join(", ")}. Open the dashboard.`
    : "";
  if (detail.status === "NEEDS_CLARIFICATION") {
    byId("approvals").textContent =
      "The backend needs clarification or found no matches. The current webhook does not route clarification answers back into this request. Send a complete corrected requirement as a new request; this UI does not bypass the workflow.";
  }
  const messages = detail.messages || [];
  const serialized = JSON.stringify(messages);
  if (serialized === lastMessages) return;
  lastMessages = serialized;
  const container = byId("messages");
  const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 100;
  container.replaceChildren();
  for (const message of messages) {
    const bubble = document.createElement("article");
    bubble.className = `bubble${message.direction === "inbound" ? " buyer" : ""}`;
    if (message.media_url && message.media_url.startsWith("data:image/")) {
      const image = document.createElement("img");
      image.src = message.media_url;
      image.alt = "Buyer reference image";
      bubble.append(image);
    }
    const text = document.createElement("div");
    text.textContent = message.text || `[${message.message_type} attachment]`;
    bubble.append(text);
    const meta = document.createElement("small");
    meta.textContent = `${message.direction === "inbound" ? "Buyer" : "Assistant"} | ${new Date(message.created_at).toLocaleString()}`;
    if (message.media_url && !message.media_url.startsWith("data:image/")) {
      meta.textContent += " | External media reference (not loaded by test UI)";
    }
    bubble.append(meta);
    container.append(bubble);
  }
  if (nearBottom) container.scrollTop = container.scrollHeight;
}

async function refresh() {
  if (!activeRequest || polling) return;
  polling = true;
  const request = activeRequest;
  const phone = activePhone;
  try {
    const query = new URLSearchParams({ request_id: request, phone });
    const detail = await api(`/chat/conversation?${query}`);
    if (request !== activeRequest || phone !== activePhone) return;
    render(detail);
    showError("");
  } catch (error) {
    if (request === activeRequest && phone === activePhone) showError(error.message);
  } finally {
    polling = false;
  }
}

function fileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Could not read the selected image."));
    reader.readAsDataURL(file);
  });
}

byId("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  busy = true;
  byId("send").disabled = true;
  byId("reset").disabled = true;
  byId("load").disabled = true;
  showError("");
  try {
    const file = byId("image").files[0];
    if (file && (!["image/jpeg", "image/png", "image/webp"].includes(file.type) || file.size > 5 * 1024 * 1024)) {
      throw new Error("Attach a JPEG, PNG, or WebP image no larger than 5 MB.");
    }
    if (file && byId("image-url").value.trim()) throw new Error("Choose a file OR an image URL, not both.");
    const payload = {
      sender_phone: byId("phone").value.trim(),
      profile_name: byId("name").value.trim(),
      text: byId("text").value.trim(),
      media_url: file ? await fileAsDataUrl(file) : byId("image-url").value.trim(),
    };
    if (!payload.text && !payload.media_url) throw new Error("Enter a message or attach an image.");
    // Retain the ID on an ambiguous network failure so retrying cannot duplicate this message.
    const signature = JSON.stringify(payload);
    if (!pending || pending.signature !== signature) {
      pending = { signature, payload: { ...payload, external_message_id: `chatui-${crypto.randomUUID()}` } };
    }
    const result = await api("/chat/send", pending.payload);
    if (!result.request_id && !result.duplicate) {
      throw new Error("No buyer request returned. Check that this phone is not registered as a supplier.");
    }
    if (result.duplicate && !result.request_id) {
      throw new Error("Message was already accepted. The backend does not return request IDs for duplicates. Find the request in the dashboard and use Load / refresh. Do not resend with a new ID.");
    }
    activeRequest = result.request_id;
    activePhone = payload.sender_phone;
    byId("request-id").value = activeRequest;
    byId("text").value = "";
    clearImage();
    pending = null;
    byId("status").textContent = "Accepted by API. Waiting for background worker...";
    await refresh();
  } catch (error) {
    showError(error.message);
  } finally {
    busy = false;
    byId("send").disabled = false;
    byId("reset").disabled = false;
    byId("load").disabled = false;
  }
});

byId("text").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    byId("composer").requestSubmit();
  }
});

byId("load").addEventListener("click", () => {
  activeRequest = byId("request-id").value.trim();
  activePhone = byId("phone").value.trim();
  if (!activeRequest) {
    showError("Enter a request ID from the dashboard or send a message first.");
    return;
  }
  lastMessages = "";
  void refresh();
});

function clearImage() {
  byId("image").value = "";
  byId("image-url").value = "";
  byId("attachment").textContent = "";
}
byId("clear-image").addEventListener("click", clearImage);
byId("image").addEventListener("change", () => {
  byId("attachment").textContent = byId("image").files[0]?.name || "";
});
byId("reset").addEventListener("click", () => {
  activeRequest = "";
  activePhone = "";
  lastMessages = "";
  pending = null;
  byId("phone").value = `+911${crypto.getRandomValues(new Uint32Array(1))[0].toString().padStart(10, "0").slice(-9)}`;
  byId("name").value = "Test Buyer";
  byId("request-id").value = "";
  byId("text").value = "";
  byId("status").textContent = "New buyer identity. Previous database records are not deleted.";
  byId("requirement").textContent = "No extracted requirement yet.";
  byId("approvals").textContent = "";
  byId("messages").replaceChildren();
  clearImage();
  showError("");
});

api("/chat/config").then((config) => {
  byId("backend").textContent = `Connected API target: ${config.api_url}`;
}).catch((error) => showError(error.message));
setInterval(() => { if (!document.hidden && !busy) void refresh(); }, 3000);
