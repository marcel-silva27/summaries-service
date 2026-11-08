/* La web solo consume el gateway del mismo origen. */
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const read = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
const save = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* El chat sigue disponible sin almacenamiento local. */ } };
const state = {users: read("hilo.users", []), user: read("hilo.user", ""), channels: [], channel: read("hilo.channel", ""),
  tab: "messages", messages: [], summaries: [], selectedSummary: "", polls: [], pollIds: read("hilo.polls", {}),
  votes: read("hilo.votes", {}), choices: {}, searchOffset: 0, searchToken: 0, mediaOffset: 0, mediaItems: [], refreshing: false};
const summaryFields = "id channel_id messageState content error created_at structured { overview decisions events { description when } tasks { description assignee } }";
const pollFields = "poll_id channel_id author_id question status total_votes closes_at created_at options { option_id position text vote_count }";
const mediaFields = "id channel_id category created_at uploader_id filename mime_type size_bytes url preview { title description final_url }";
const person = (id) => state.users.find((user) => user.id === id)?.display_name || "Participante";
const time = (value) => new Intl.DateTimeFormat("es-CL", {hour: "2-digit", minute: "2-digit"}).format(new Date(value));
const date = (value) => new Intl.DateTimeFormat("es-CL", {day: "numeric", month: "short"}).format(new Date(value));
const bytes = (value) => value >= 1048576 ? `${(value / 1048576).toFixed(1)} MB` : `${Math.ceil(value / 1024)} KB`;
const empty = (title, text, symbol = "#") => `<div class="empty-state"><span class="empty-symbol">${symbol}</span><h2>${esc(title)}</h2><p>${esc(text)}</p></div>`;
let toastTimer;

function notify(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = error ? "error" : "";
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 6500);
}

async function gql(query, variables = {}) {
  let response;
  try { response = await fetch("/api/v1/graphql", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({query, variables})}); }
  catch { throw new Error("No se pudo conectar. Inténtalo de nuevo."); }
  if (!response.ok) throw new Error("No se pudo completar la solicitud.");
  const result = await response.json();
  if (result.errors?.length) {
    const error = result.errors[0].extensions || {};
    const detail = ({403: "Esta acción corresponde al propietario.", 404: "No se encontró el recurso solicitado.",
      409: "La operación no está disponible: puede que ya hayas votado o que la encuesta esté cerrada.",
      422: "Revisa los datos ingresados.", 502: "Un servicio respondió con un error. Inténtalo más tarde.",
      503: "Este servicio no está disponible por ahora.", 504: "El servicio tardó demasiado. Inténtalo de nuevo."})[error.status];
    throw new Error(detail || error.detail || "No se pudo completar la operación.");
  }
  return result.data;
}

async function busy(button, action) {
  if (button.disabled) return;
  button.disabled = true;
  try { await action(); } catch (error) { notify(error.message, true); }
  finally { button.disabled = false; controls(); }
}

function context() {
  if (!state.channel) throw new Error("Selecciona o crea un canal primero.");
  if (!state.user) throw new Error("Selecciona o crea un usuario primero.");
  return state.channel;
}

function controls() {
  const ready = Boolean(state.user && state.channel);
  for (const id of ["#send-message", "#upload-file", "#new-poll"]) $(id).disabled = !ready;
  const summary = state.summaries.find((item) => item.id === state.selectedSummary) || state.summaries[0];
  $("#request-summary").disabled = !ready || ["pendiente", "procesando"].includes(summary?.messageState);
  $("#message-input").placeholder = ready ? `Mensaje en #${state.channels.find((item) => item.id === state.channel)?.name || "canal"}…` : "Elige un usuario y un canal para conversar…";
}

function renderUsers() {
  $("#user-select").innerHTML = '<option value="">Selecciona un usuario</option>' + state.users.map((user) => `<option value="${esc(user.id)}">${esc(user.display_name)}</option>`).join("");
  $("#user-select").value = state.user;
  $("#search-author").innerHTML = '<option value="">Todos</option>' + state.users.map((user) => `<option value="${esc(user.id)}">${esc(user.display_name)}</option>`).join("");
  save("hilo.users", state.users);
  controls();
}

function renderChannels() {
  $("#channel-list").innerHTML = state.channels.length ? state.channels.map((channel) => `<button class="channel-button ${channel.id === state.channel ? "active" : ""}" data-channel="${esc(channel.id)}"><span>#</span>${esc(channel.name)}</button>`).join("") : '<p class="sidebar-note">Tu equipo todavía no tiene canales. Crea el primero con el botón +.</p>';
}

function selectUser(id) {
  state.user = id;
  save("hilo.user", id);
  renderUsers();
  renderMessages();
  if (state.tab === "files") loadFiles().catch(showViewError);
  if (state.tab === "polls") renderPolls();
}

async function boot() {
  const results = await Promise.allSettled([gql("{ listUsers { id display_name } }"), gql("{ listChannels { id name } }")]);
  if (results[0].status === "fulfilled") state.users = results[0].value.listUsers;
  if (!state.users.some((user) => user.id === state.user)) state.user = state.users[0]?.id || "";
  renderUsers();
  if (results[1].status === "rejected") {
    $("#connection").textContent = "Sin conexión";
    $("#connection-dot").classList.remove("connected");
    $("#channel-list").innerHTML = '<p class="sidebar-note">No pudimos cargar los canales. Usa ↻ para volver a intentarlo.</p>';
    return;
  }
  state.channels = results[1].value.listChannels;
  $("#connection").textContent = "En línea";
  $("#connection-dot").classList.add("connected");
  const selected = state.channels.find((item) => item.id === state.channel)?.id || state.channels[0]?.id;
  renderChannels();
  if (selected) await selectChannel(selected);
}

async function selectChannel(id) {
  state.channel = id;
  state.messages = [];
  state.summaries = [];
  state.selectedSummary = "";
  state.searchToken++;
  state.mediaOffset = 0;
  state.mediaItems = [];
  save("hilo.channel", id);
  const channel = state.channels.find((item) => item.id === id);
  $("#channel-title").textContent = channel?.name || "Canal";
  $("#breadcrumb-channel").textContent = channel?.name || "Canal";
  $("#channel-description").textContent = "Ideas, decisiones y todo lo que nos conecta.";
  $("#sidebar").classList.remove("open");
  $("#search-results").innerHTML = "";
  $("#search-count").textContent = "Los resultados aparecerán aquí";
  renderChannels();
  renderSummary();
  controls();
  await Promise.allSettled([loadMessages(), loadSummaries()]);
  if (state.tab === "files") await loadFiles().catch(showViewError);
  if (state.tab === "polls") await loadPolls().catch(showViewError);
}

function linkText(text) {
  return esc(text).replace(/https?:\/\/[^\s<>]+/g, (url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
}

function renderMessages() {
  const target = $("#messages");
  const nearBottom = target.scrollHeight - target.scrollTop - target.clientHeight < 100;
  if (!state.messages.length) {
    target.innerHTML = empty("Aquí empieza la conversación", "Comparte la primera idea del canal.");
    return;
  }
  let lastDay = "";
  target.innerHTML = state.messages.map((message) => {
    const day = date(message.created_at);
    const separator = day !== lastDay ? `<div class="message-day">${esc(day)}</div>` : "";
    lastDay = day;
    const name = person(message.author_id);
    const tone = [...message.author_id].reduce((total, c) => total + c.charCodeAt(0), 0) % 4;
    return `${separator}<article class="message"><div class="avatar tone-${tone}">${esc(name.slice(0, 2).toUpperCase())}</div><div class="message-body"><div class="message-meta"><strong>${esc(name)}</strong>${message.author_id === state.user ? '<span class="you-tag">tú</span>' : ""}<time datetime="${esc(message.created_at)}">${esc(time(message.created_at))}</time></div><div class="message-text">${linkText(message.content)}</div></div></article>`;
  }).join("");
  if (nearBottom) target.scrollTop = target.scrollHeight;
}

async function loadMessages() {
  const channel = state.channel;
  if (!channel) return;
  try {
    const data = await gql("query($channel: ID!) { listMessages(channel_id: $channel) { id channel_id author_id content created_at } }", {channel});
    if (channel !== state.channel) return;
    const messages = data.listMessages.sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
    if (JSON.stringify(messages) !== JSON.stringify(state.messages) || !$("#messages .message")) {
      state.messages = messages;
      renderMessages();
    }
    // Los anuncios de Polls permiten descubrir encuestas sin inventar un endpoint de listado.
    let discovered = false;
    for (const message of messages) {
      for (const match of message.content.matchAll(/poll[ _]id\s*[:=]\s*([0-9a-f-]{36})/gi)) {
        const ids = state.pollIds[channel] ||= [];
        if (!ids.includes(match[1])) { ids.push(match[1]); discovered = true; }
      }
    }
    if (discovered) save("hilo.polls", state.pollIds);
  } catch (error) {
    if (channel === state.channel && !state.messages.length) $("#messages").innerHTML = `<div class="error-state">${esc(error.message)} Usa ↻ para volver a cargar.</div>`;
  }
}

async function loadSummaries() {
  const channel = state.channel;
  if (!channel) return;
  try {
    const data = await gql(`query($channel: ID!) { listSummaries(channel_id: $channel, limit: 10) { ${summaryFields} } }`, {channel});
    if (channel !== state.channel) return;
    state.summaries = data.listSummaries;
    if (!state.summaries.some((item) => item.id === state.selectedSummary)) state.selectedSummary = state.summaries[0]?.id || "";
    renderSummary();
  } catch (error) {
    if (channel === state.channel && !state.summaries.length) $("#summary-content").innerHTML = `<p>${esc(error.message)}</p>`;
  }
}

function renderSummary() {
  const summary = state.summaries.find((item) => item.id === state.selectedSummary);
  const status = $("#summary-status");
  const labels = {pendiente: "En espera", procesando: "Preparando", completada: "Listo", fallida: "No disponible"};
  status.textContent = labels[summary?.messageState] || "Sin resumen";
  status.className = "status " + ({completada: "done", fallida: "failed", pendiente: "working", procesando: "working"}[summary?.messageState] || "");
  $("#summary-history").hidden = state.summaries.length < 2;
  $("#summary-history").innerHTML = state.summaries.map((item) => `<option value="${esc(item.id)}">${esc(date(item.created_at))} · ${esc(time(item.created_at))} · ${labels[item.messageState]}</option>`).join("");
  $("#summary-history").value = state.selectedSummary;
  if (!summary) $("#summary-content").innerHTML = '<div class="summary-empty"><span class="summary-lines" aria-hidden="true">≡</span><h3>Retoma donde quedaron</h3><p>Reúne las decisiones, eventos y tareas de la conversación en un solo lugar.</p></div>';
  else if (["pendiente", "procesando"].includes(summary.messageState)) $("#summary-content").innerHTML = '<p class="working-dots">Estamos reuniendo el contexto…</p><p>Puedes seguir conversando. El resultado aparecerá aquí.</p>';
  else if (summary.messageState === "fallida") $("#summary-content").innerHTML = `<p>${esc(summary.error || "No se pudo generar el resumen. Inténtalo de nuevo.")}</p>`;
  else if (summary.structured) {
    const data = summary.structured;
    const list = (items) => `<ul>${items.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`;
    $("#summary-content").innerHTML = `<p>${esc(data.overview)}</p>${data.decisions.length ? "<h3>Decisiones</h3>" + list(data.decisions) : ""}${data.events.length ? "<h3>Eventos</h3>" + list(data.events.map((item) => item.description + (item.when ? ` · ${item.when}` : ""))) : ""}${data.tasks.length ? "<h3>Tareas</h3>" + list(data.tasks.map((item) => item.description + (item.assignee ? ` · ${item.assignee}` : ""))) : ""}`;
  } else $("#summary-content").textContent = summary.content;
  controls();
}

function showViewError(error) {
  const target = state.tab === "files" ? $("#gallery") : $("#poll-list");
  target.innerHTML = `<div class="error-state">${esc(error.message)} Puedes seguir usando la conversación.</div>`;
}

async function loadFiles(append = false) {
  const channel = context();
  const user = state.user;
  const input = {limit: 20, offset: state.mediaOffset};
  if ($("#media-category").value) input.category = $("#media-category").value;
  const results = await Promise.allSettled([
    gql(`query($channel: ID!, $user: ID!, $input: GalleryInput) { listAttachments(channel_id: $channel, user_id: $user, input: $input) { items { ${mediaFields} } limit offset } }`, {channel, user, input}),
    gql("query($channel: ID!, $user: ID!) { getQuota(channel_id: $channel, user_id: $user) { available_bytes limit_bytes max_file_bytes } }", {channel, user})
  ]);
  if (channel !== state.channel || user !== state.user) return;
  if (results[0].status === "rejected") throw results[0].reason;
  const items = results[0].value.listAttachments.items;
  state.mediaItems = append ? [...state.mediaItems, ...items] : items;
  $("#files-more").hidden = items.length < 20;
  if (results[1].status === "fulfilled") {
    const quota = results[1].value.getQuota;
    $("#quota").textContent = `${bytes(quota.available_bytes)} disponibles · Máximo ${bytes(Math.min(quota.max_file_bytes, 10485760))} por archivo`;
  }
  $("#gallery").innerHTML = state.mediaItems.length ? state.mediaItems.map((item) => {
    const link = item.category === "LINK";
    const title = link ? item.preview?.title || item.url : item.filename;
    const url = item.preview?.final_url || item.url;
    const safeUrl = /^https?:\/\//i.test(url || "") ? url : "";
    return `<article class="file-card"><div class="file-symbol">${link ? "↗" : item.category === "IMAGE" ? "▧" : "▤"}</div><h3>${esc(title)}</h3><p>${link ? esc(item.preview?.description || item.url) : `${esc(bytes(item.size_bytes))} · ${esc(date(item.created_at))}`}</p><div class="file-actions">${link ? (safeUrl ? `<a href="${esc(safeUrl)}" target="_blank" rel="noopener noreferrer">Abrir enlace ↗</a>` : "") : `<button data-download="${esc(item.id)}">Descargar ↓</button>${item.uploader_id === state.user ? `<button class="delete-file" data-delete="${esc(item.id)}">Eliminar</button>` : ""}`}</div></article>`;
  }).join("") : empty("Un espacio para compartir", "Sube un archivo o comparte un enlace en la conversación.", "▤");
}

async function fileRequest(path, options = {}) {
  const response = await fetch(`/api/v1/media${path}`, {...options, headers: {"X-User-Id": state.user}});
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : "No se pudo completar la operación con el archivo.");
  }
  return response;
}

async function search(attempt = 0, token = ++state.searchToken) {
  const channel = context();
  const input = {limit: 25, offset: state.searchOffset, sort_order: $("#search-order").value};
  if ($("#search-query").value.trim()) input.content = $("#search-query").value.trim();
  if ($("#search-author").value) input.author_id = [$("#search-author").value];
  if ($("#search-links").checked) input.has = ["link"];
  if ($("#search-start").value) input.min_date = new Date($("#search-start").value).toISOString();
  if ($("#search-end").value) input.max_date = new Date($("#search-end").value).toISOString();
  const data = await gql("query($channel: ID!, $input: SearchInput) { searchMessages(channel_id: $channel, input: $input) { total_results indexing retry_after messages { id author_id content created_at highlights } } }", {channel, input});
  if (token !== state.searchToken || channel !== state.channel) return;
  const result = data.searchMessages;
  if (result.indexing) {
    $("#search-count").textContent = "El índice se está preparando. Volveremos a consultar en un momento…";
    if (attempt < 5) setTimeout(() => { if (token === state.searchToken) search(attempt + 1, token).catch((error) => notify(error.message, true)); }, Math.min(result.retry_after, 10) * 1000);
    return;
  }
  $("#search-count").textContent = `${result.total_results} resultado${result.total_results === 1 ? "" : "s"} en este canal`;
  $("#search-results").innerHTML = result.messages.length ? result.messages.map((message) => `<article class="search-result"><div class="message-meta"><strong>${esc(person(message.author_id))}</strong><time>${esc(date(message.created_at))} · ${esc(time(message.created_at))}</time></div><div class="message-text">${esc(message.highlights[0] || message.content).replace(/\*\*(.+?)\*\*/g, "<mark>$1</mark>")}</div></article>`).join("") : empty("Todavía no hay coincidencias", "Prueba otras palabras o cambia los filtros.", "⌕");
  $("#search-prev").hidden = state.searchOffset === 0;
  $("#search-next").hidden = state.searchOffset + 25 >= result.total_results || state.searchOffset + 25 > 9975;
}

async function loadPolls() {
  const channel = context();
  const ids = (state.pollIds[channel] || []).slice(-30).reverse();
  const results = await Promise.allSettled(ids.map((id) => gql(`query($id: ID!) { getPoll(id: $id) { ${pollFields} } }`, {id})));
  if (channel !== state.channel) return;
  state.polls = results.filter((result) => result.status === "fulfilled").map((result) => result.value.getPoll).filter((poll) => poll.channel_id === channel);
  renderPolls();
  if (ids.length && !state.polls.length && results[0]?.status === "rejected") throw results[0].reason;
}

function renderPolls() {
  const labels = {open: "Abierta", closed: "Cerrada", pending_announcement: "Publicando…", creation_failed: "No se pudo publicar"};
  $("#poll-list").innerHTML = state.polls.length ? state.polls.map((poll) => {
    const voted = state.votes[`${poll.poll_id}:${state.user}`];
    const open = poll.status === "open" && Date.parse(poll.closes_at) > Date.now();
    return `<article class="poll-card"><div class="poll-meta"><span>${esc(person(poll.author_id))} preguntó</span><span class="status">${labels[poll.status]}</span></div><h3>${esc(poll.question)}</h3>${poll.options.map((option) => `<label class="poll-option"><span class="bar" style="width:${poll.total_votes ? Math.round(option.vote_count / poll.total_votes * 100) : 0}%"></span><input type="radio" name="poll-${esc(poll.poll_id)}" value="${esc(option.option_id)}" data-poll-choice="${esc(poll.poll_id)}" ${!open || voted ? "disabled" : ""} ${state.choices[poll.poll_id] === option.option_id || voted === option.option_id ? "checked" : ""}><span>${esc(option.text)}</span><span class="votes">${option.vote_count}</span></label>`).join("")}<div class="poll-footer"><span class="muted">${poll.total_votes} voto${poll.total_votes === 1 ? "" : "s"} · Hasta ${esc(time(poll.closes_at))}</span><div class="poll-actions">${poll.author_id === state.user && poll.status === "open" ? `<button class="text-button" data-close-poll="${esc(poll.poll_id)}">Cerrar</button>` : ""}${open ? `<button class="primary" data-vote="${esc(poll.poll_id)}" ${voted ? "disabled" : ""}>${voted ? "Voto registrado" : "Votar"}</button>` : ""}</div></div><p class="poll-id">ID: ${esc(poll.poll_id)}</p></article>`;
  }).join("") : empty("La próxima decisión empieza aquí", "Crea una encuesta o abre una con el ID compartido en el chat.", "◷");
}

function rememberPoll(poll) {
  const ids = state.pollIds[poll.channel_id] ||= [];
  if (!ids.includes(poll.poll_id)) ids.push(poll.poll_id);
  save("hilo.polls", state.pollIds);
}

for (const [button, dialog] of [["#new-user", "#user-dialog"], ["#join-user", "#join-dialog"], ["#new-channel", "#channel-dialog"], ["#new-poll", "#poll-dialog"], ["#import-poll", "#import-poll-dialog"]]) {
  $(button).addEventListener("click", () => $(dialog).showModal());
}
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
$("#mobile-menu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
$("#user-select").addEventListener("change", (event) => selectUser(event.target.value));
$("#channel-list").addEventListener("click", (event) => { const button = event.target.closest("[data-channel]"); if (button) selectChannel(button.dataset.channel); });
$("#retry-connection").addEventListener("click", () => boot().catch((error) => notify(error.message, true)));
$("#reload").addEventListener("click", () => refresh());
$("#summary-history").addEventListener("change", (event) => { state.selectedSummary = event.target.value; renderSummary(); });

$("#tabs").addEventListener("click", async (event) => {
  const tab = event.target.closest("[data-tab]")?.dataset.tab;
  if (!tab) return;
  state.tab = tab;
  document.querySelectorAll("[data-tab]").forEach((button) => button.setAttribute("aria-selected", String(button.dataset.tab === tab)));
  document.querySelectorAll(".view").forEach((view) => { view.hidden = view.id !== `${tab}-view`; });
  try { if (tab === "files") await loadFiles(); if (tab === "polls") await loadPolls(); } catch (error) { showViewError(error); }
});

$("#user-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const data = await gql("mutation($input: UserInput!) { createUser(input: $input) { id display_name } }", {input: {display_name: $("#user-name").value.trim()}});
    state.users.push(data.createUser);
    selectUser(data.createUser.id);
    $("#user-dialog").close(); event.target.reset(); notify("Ya tienes un lugar en la conversación.");
  });
});
$("#join-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const data = await gql("query($id: ID!) { getUser(id: $id) { id display_name } }", {id: $("#existing-user-id").value.trim()});
    if (!state.users.some((user) => user.id === data.getUser.id)) state.users.push(data.getUser);
    selectUser(data.getUser.id); $("#join-dialog").close(); event.target.reset();
  });
});
$("#channel-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const data = await gql("mutation($input: ChannelInput!) { createChannel(input: $input) { id name } }", {input: {name: $("#channel-name").value.trim()}});
    state.channels.push(data.createChannel); $("#channel-dialog").close(); event.target.reset();
    await selectChannel(data.createChannel.id); notify("Canal creado. Dale el primer mensaje.");
  });
});
$("#message-input").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#message-form").requestSubmit($("#send-message")); } });
$("#message-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy($("#send-message"), async () => {
    const channel = context(); const content = $("#message-input").value.trim();
    if (!content) throw new Error("Escribe un mensaje antes de enviarlo.");
    await gql("mutation($channel: ID!, $input: MessageInput!) { sendMessage(channel_id: $channel, input: $input) { id } }", {channel, input: {author_id: state.user, content}});
    $("#message-input").value = ""; await loadMessages(); $("#messages").scrollTop = $("#messages").scrollHeight;
  });
});
$("#request-summary").addEventListener("click", (event) => busy(event.currentTarget, async () => {
  const channel = context(); const input = {user_id: state.user};
  const start = $("#summary-start").value, end = $("#summary-end").value;
  if (Boolean(start) !== Boolean(end)) throw new Error("Completa ambas fechas o deja el intervalo vacío.");
  if (start && end) input.timeInterval = {start: new Date(start).toISOString(), end: new Date(end).toISOString()};
  const data = await gql("mutation($channel: ID!, $input: SummaryInput) { requestSummary(channel_id: $channel, input: $input) { summary_id } }", {channel, input});
  state.selectedSummary = data.requestSummary.summary_id; await loadSummaries(); notify("Resumen solicitado. Te lo mostramos cuando esté listo.");
}));

$("#search-form").addEventListener("submit", (event) => { event.preventDefault(); state.searchOffset = 0; busy(event.submitter, () => search()); });
for (const [id, delta] of [["#search-prev", -25], ["#search-next", 25]]) $(id).addEventListener("click", (event) => busy(event.currentTarget, async () => { state.searchOffset += delta; await search(); }));
$("#upload-file").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", async (event) => {
  const file = event.target.files[0]; if (!file) return;
  await busy($("#upload-file"), async () => {
    const channel = context();
    if (file.size > 10485760) throw new Error("El archivo supera los 10 MiB permitidos.");
    const data = new FormData(); data.append("file", file);
    await fileRequest(`/channels/${encodeURIComponent(channel)}/attachments`, {method: "POST", body: data});
    state.mediaOffset = 0; await loadFiles(); notify("Archivo compartido con el canal.");
  });
  event.target.value = "";
});
$("#media-category").addEventListener("change", () => { state.mediaOffset = 0; loadFiles().catch(showViewError); });
$("#files-more").addEventListener("click", (event) => busy(event.currentTarget, async () => { state.mediaOffset += 20; await loadFiles(true); }));
$("#gallery").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-download], [data-delete]"); if (!button) return;
  await busy(button, async () => {
    if (button.dataset.download) {
      const response = await fileRequest(`/attachments/${encodeURIComponent(button.dataset.download)}/download`);
      const url = URL.createObjectURL(await response.blob()); const link = document.createElement("a");
      link.href = url; link.download = state.mediaItems.find((item) => item.id === button.dataset.download)?.filename || "archivo";
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 10000);
    } else {
      await fileRequest(`/attachments/${encodeURIComponent(button.dataset.delete)}`, {method: "DELETE"});
      state.mediaOffset = 0; await loadFiles(); notify("Archivo eliminado.");
    }
  });
});

let pollRequest = null;
$("#poll-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const input = {channel_id: context(), author_id: state.user, question: $("#poll-question").value.trim(),
      options: $("#poll-options").value.split("\n").map((value) => value.trim()).filter(Boolean), duration_minutes: Number($("#poll-duration").value)};
    if (input.options.length < 2 || input.options.length > 10 || new Set(input.options.map((value) => value.toLocaleLowerCase())).size !== input.options.length) throw new Error("Agrega de 2 a 10 opciones diferentes, una por línea.");
    if (!pollRequest || pollRequest.body !== JSON.stringify(input)) pollRequest = {body: JSON.stringify(input), key: crypto.randomUUID()};
    const data = await gql(`mutation($input: PollInput!, $key: String!) { createPoll(input: $input, idempotency_key: $key) { ${pollFields} } }`, {input, key: pollRequest.key});
    rememberPoll(data.createPoll); pollRequest = null; $("#poll-dialog").close(); event.target.reset();
    await loadPolls(); await loadMessages(); notify("Encuesta creada. Tu equipo ya puede participar.");
  });
});
$("#import-poll-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const data = await gql(`query($id: ID!) { getPoll(id: $id) { ${pollFields} } }`, {id: $("#poll-id").value.trim()});
    if (data.getPoll.channel_id !== context()) throw new Error("Esta encuesta pertenece a otro canal.");
    rememberPoll(data.getPoll); $("#import-poll-dialog").close(); event.target.reset(); await loadPolls();
  });
});
$("#poll-list").addEventListener("change", (event) => { if (event.target.dataset.pollChoice) state.choices[event.target.dataset.pollChoice] = event.target.value; });
$("#poll-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-vote], [data-close-poll]"); if (!button) return;
  busy(button, async () => {
    context();
    if (button.dataset.vote) {
      const id = button.dataset.vote, option = state.choices[id];
      if (!option) throw new Error("Elige una opción antes de votar.");
      await gql("mutation($id: ID!, $input: VoteInput!) { votePoll(id: $id, input: $input) { poll_id } }", {id, input: {voter_id: state.user, option_id: option}});
      state.votes[`${id}:${state.user}`] = option; save("hilo.votes", state.votes); notify("Tu voto quedó registrado.");
    } else {
      await gql("mutation($id: ID!, $input: ClosePollInput!) { closePoll(id: $id, input: $input) { poll_id status } }", {id: button.dataset.closePoll, input: {author_id: state.user}});
      notify("Encuesta cerrada. Los resultados están listos.");
    }
    await loadPolls(); await loadMessages();
  });
});

async function refresh() {
  if (state.refreshing || !state.channel || document.hidden) return;
  state.refreshing = true;
  try {
    await Promise.allSettled([loadMessages(), loadSummaries()]);
    if (state.tab === "polls") await loadPolls().catch(showViewError);
  } finally { state.refreshing = false; }
}
controls();
boot().catch((error) => notify(error.message, true));
setInterval(refresh, 4000);
