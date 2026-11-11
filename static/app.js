"use strict";

// STAR TeamTalk bot management interface.
// Live values refresh quietly in place; only the results of the user's own
// actions are announced (status region) or reported as errors (alert region).

const REFRESH_MS = 4000;
const page = document.body.dataset.page;
const argument = document.body.dataset.argument;
const content = document.getElementById("content");

// ------------------------------------------------------------- utilities

function el(tag, attributes, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

let announceTimer = null;
function announce(message) {
  const region = document.getElementById("status");
  clearAlert();
  region.textContent = "";
  clearTimeout(announceTimer);
  // A short pause makes screen readers repeat a message that is the same as the last one.
  announceTimer = setTimeout(() => { region.textContent = message; }, 60);
}

function showError(message) {
  const region = document.getElementById("alert");
  region.textContent = "";
  setTimeout(() => { region.textContent = message; }, 60);
}

function clearAlert() {
  document.getElementById("alert").textContent = "";
}

async function api(method, path, body) {
  const options = { method, headers: { "X-STAR-Request": "1" } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new Error("The bot could not be reached. It may be restarting; try again in a few seconds.");
  }
  let data = {};
  try { data = await response.json(); } catch (error) { /* not JSON */ }
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || ("The request failed (" + response.status + ")."));
  }
  return data;
}

// Runs an action: disables the button while it works, then announces the result.
async function act(button, work, workingText) {
  if (button) {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  }
  if (workingText) announce(workingText);
  try {
    const result = await work();
    if (result && result.message) announce(result.message);
    return result;
  } catch (error) {
    showError(error.message);
    return null;
  } finally {
    if (button && button.isConnected) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }
}

function setText(id, text) {
  const node = document.getElementById(id);
  if (node && node.textContent !== text) node.textContent = text;
}

function formatTime(epoch) {
  if (!epoch) return "";
  return new Date(epoch * 1000).toLocaleString();
}

function getAt(object, path) {
  return path.split(".").reduce((value, key) => (value == null ? undefined : value[key]), object);
}

function setAt(object, path, value) {
  const keys = path.split(".");
  let target = object;
  for (const key of keys.slice(0, -1)) {
    if (typeof target[key] !== "object" || target[key] === null) target[key] = {};
    target = target[key];
  }
  target[keys[keys.length - 1]] = value;
}

function fieldId(path) {
  return "field-" + path.replace(/[^A-Za-z0-9_-]/g, "-");
}

// Messages that should survive a page change (e.g. "Server added").
function carryMessage(message) {
  sessionStorage.setItem("star-message", message);
}

function announceCarried() {
  const message = sessionStorage.getItem("star-message");
  if (message) {
    sessionStorage.removeItem("star-message");
    announce(message);
  }
}

function startRefreshing(callback, interval) {
  setInterval(() => { if (!document.hidden) callback(); }, interval || REFRESH_MS);
}

// ------------------------------------------------------------------ forms

function renderField(field, value) {
  const id = fieldId(field.path);
  const helpId = field.help ? id + "-help" : undefined;
  const help = field.help ? el("p", { class: "help", id: helpId, text: field.help }) : null;
  let control;
  if (field.kind === "bool") {
    control = el("input", { type: "checkbox", id, "aria-describedby": helpId });
    control.checked = Boolean(value);
    return el("div", { class: "field checkbox" }, control, el("label", { for: id, text: field.label }), help);
  }
  if (field.kind === "select") {
    control = el("select", { id, "aria-describedby": helpId });
    const values = field.choices.map((choice) => choice.value);
    if (value !== undefined && value !== null && !values.includes(String(value))) {
      control.append(el("option", { value: String(value), text: String(value) }));
    }
    for (const choice of field.choices) {
      control.append(el("option", { value: choice.value, text: choice.label }));
    }
    control.value = String(value ?? "");
  } else if (field.kind === "lines" || field.kind === "json") {
    control = el("textarea", { id, rows: field.kind === "json" ? 4 : 3, "aria-describedby": helpId, spellcheck: "false" });
    control.value = field.kind === "lines" ? (value || []).join("\n") : JSON.stringify(value ?? {}, null, 2);
  } else if (field.kind === "int" || field.kind === "float") {
    control = el("input", {
      type: "number", id, "aria-describedby": helpId,
      step: field.kind === "float" ? "any" : "1",
      min: field.min, max: field.max,
    });
    control.value = value ?? "";
  } else {
    control = el("input", {
      type: field.kind === "password" ? "password" : "text",
      id, "aria-describedby": helpId, autocomplete: "off", spellcheck: "false",
    });
    control.value = value ?? "";
  }
  const parts = [el("label", { for: id, text: field.label }), control, help];
  if (field.kind === "password") {
    const toggleId = id + "-show";
    const toggle = el("input", {
      type: "checkbox", id: toggleId,
      onchange: () => { control.type = toggle.checked ? "text" : "password"; },
    });
    parts.push(el("div", { class: "checkbox small" }, toggle, el("label", { for: toggleId, text: "Show " + field.label.toLowerCase() })));
  }
  return el("div", { class: "field" }, ...parts);
}

function renderSections(sections, values, advancedPaths) {
  const normal = [];
  const advanced = [];
  for (const section of sections) {
    const fieldset = el("fieldset", { id: "section-" + section.path.replace(/\./g, "-") },
      el("legend", { text: section.title }),
      section.description ? el("p", { class: "help", text: section.description }) : null,
      section.fields.map((field) => renderField(field, getAt(values, field.path))),
    );
    (advancedPaths.includes(section.path) ? advanced : normal).push(fieldset);
  }
  const parts = [...normal];
  if (advanced.length) {
    parts.push(el("details", {}, el("summary", { text: "Advanced settings" }), ...advanced));
  }
  return parts;
}

// Reads the form back into a copy of `base`. Throws with the field to focus on errors.
function collectSections(sections, base) {
  const result = JSON.parse(JSON.stringify(base));
  for (const section of sections) {
    for (const field of section.fields) {
      const control = document.getElementById(fieldId(field.path));
      if (!control) continue;
      control.removeAttribute("aria-invalid");
      let value;
      if (field.kind === "bool") value = control.checked;
      else if (field.kind === "int") {
        if (control.value.trim() === "" || !Number.isInteger(Number(control.value))) {
          throw fieldError(control, field.label + " must be a whole number.");
        }
        value = Number(control.value);
      } else if (field.kind === "float") {
        if (control.value.trim() === "" || isNaN(Number(control.value))) {
          throw fieldError(control, field.label + " must be a number.");
        }
        value = Number(control.value);
      } else if (field.kind === "lines") {
        value = control.value.split("\n").map((line) => line.trim()).filter(Boolean);
      } else if (field.kind === "json") {
        try {
          value = control.value.trim() ? JSON.parse(control.value) : {};
        } catch (error) {
          throw fieldError(control, field.label + " is not valid JSON.");
        }
        if (typeof value !== "object" || Array.isArray(value) || value === null) {
          throw fieldError(control, field.label + " must be a JSON object, in curly braces.");
        }
      } else if (field.kind === "int_or_text") {
        value = /^\d+$/.test(control.value.trim()) ? Number(control.value.trim()) : control.value;
      } else {
        value = control.value;
      }
      setAt(result, field.path, value);
    }
  }
  return result;
}

function fieldError(control, message) {
  const error = new Error(message);
  error.control = control;
  return error;
}

function reportFormError(error) {
  showError(error.message);
  if (error.control) {
    error.control.setAttribute("aria-invalid", "true");
    const details = error.control.closest("details");
    if (details) details.open = true;
    error.control.focus();
  }
}

// ------------------------------------------------------- TeamTalk links

const TT_LINK_DEFAULTS = {
  tcpport: 10333, udpport: 10333, encrypted: false,
  username: "", password: "", channel: "", chanpasswd: "",
};
const TT_LINK_PARAMETERS = Object.keys(TT_LINK_DEFAULTS);

// Like Qt's QUrl::fromPercentEncoding: decodes %XX (as UTF-8), leaves "+" and
// a lone "%" as they are.
function percentDecode(text) {
  try {
    return decodeURIComponent(text.replace(/%(?![0-9A-Fa-f]{2})/g, "%25"));
  } catch (error) {
    return text;  // not valid UTF-8 once decoded
  }
}

function parseTeamTalkLink(text) {
  const link = text.trim();
  const match = /^tt:\/+([^?!/]*)\/?\??/i.exec(link);
  if (!/^tt:/i.test(link)) return { error: "That is not a TeamTalk link. TeamTalk links start with tt://." };
  if (!match || !match[1]) return { error: "The TeamTalk link has no server address after tt://." };
  const result = { host: percentDecode(match[1]), given: new Set(), ignored: [], notes: [] };
  const raw = {};
  for (const part of link.slice(match[0].length).split("&")) {
    if (!part) continue;
    const equals = part.indexOf("=");
    const name = (equals < 0 ? part : part.slice(0, equals)).toLowerCase();
    const value = equals < 0 ? "" : part.slice(equals + 1);
    if (!TT_LINK_PARAMETERS.includes(name)) {
      if (!result.ignored.includes(name)) result.ignored.push(name);
    } else if (!(name in raw)) {
      raw[name] = value;  // like the official client, the first one counts
    }
  }
  const values = Object.assign({}, TT_LINK_DEFAULTS);
  for (const name of ["tcpport", "udpport"]) {
    if (!(name in raw)) continue;
    const digits = /^\d+/.exec(raw[name]);
    const port = digits ? Number(digits[0]) : NaN;
    if (port >= 1 && port <= 65535) {
      values[name] = port;
      result.given.add(name);
    } else {
      result.notes.push((name === "tcpport" ? "TCP" : "UDP") + " port " + JSON.stringify(raw[name]) + " is not a valid port, so TeamTalk's default " + TT_LINK_DEFAULTS[name] + " is used.");
    }
  }
  if ("encrypted" in raw) {
    values.encrypted = raw.encrypted.toLowerCase() === "true" || raw.encrypted === "1";
    result.given.add("encrypted");
  }
  for (const name of ["username", "password", "channel", "chanpasswd"]) {
    if (name in raw) {
      values[name] = percentDecode(raw[name]);
      result.given.add(name);
    }
  }
  // Not part of the link format, but common: "tt://host:7777". The official
  // clients would not connect to that address, so the port is split off.
  const hostPort = /^(.*[^:]):(\d{1,5})$/.exec(result.host);
  if (hostPort && !result.host.includes("]") && hostPort[1].indexOf(":") < 0) {
    result.host = hostPort[1];
    if (!result.given.has("tcpport")) {
      values.tcpport = Number(hostPort[2]);
      result.given.add("tcpport");
      result.notes.push("The port " + hostPort[2] + " was written after the address; it is used as the TCP port. TeamTalk links normally give it as tcpport=" + hostPort[2] + ".");
    } else {
      result.notes.push("The :" + hostPort[2] + " after the address was dropped, because the link also gives tcpport.");
    }
  }
  result.values = values;
  return result;
}

function suggestServerName(host, taken) {
  let base = host.replace(/^\[|\]$/g, "").toLowerCase();
  if (/^\d+(\.\d+){3}$/.test(base) || base.includes(":")) {
    base = "server-" + base.replace(/[.:]/g, "-");
  } else {
    const labels = base.split(".").filter((label) => label && !["www", "tt", "teamtalk"].includes(label));
    base = labels[0] || base;
  }
  base = base.replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+/, "").slice(0, 36) || "server";
  let name = base;
  for (let n = 2; taken.includes(name); n++) name = base + "-" + n;
  return name;
}

// -------------------------------------------------------------------- log

function logView(holder, server, limit) {
  let lastId = 0;
  let level = "INFO";
  const tbody = el("tbody");
  const table = el("table", { class: "log" },
    el("caption", { class: "visually-hidden", text: "Log entries, oldest first" }),
    el("thead", {}, el("tr", {}, ["Time", "Server", "Level", "Message"].map((h) => el("th", { scope: "col", text: h })))),
    tbody);
  const empty = el("p", { text: "No log entries yet." });
  holder.replaceChildren(empty, table);
  table.hidden = true;

  async function refresh() {
    const params = new URLSearchParams({ after: String(lastId), level, limit: String(limit) });
    if (server !== null) params.set("server", server === "" ? "_bot" : server);
    let data;
    try { data = await api("GET", "/api/logs?" + params); } catch (error) { return; }
    for (const record of data.records) {
      lastId = Math.max(lastId, record.id);
      tbody.append(el("tr", { class: "level-" + record.level.toLowerCase() },
        el("td", { text: new Date(record.time * 1000).toLocaleTimeString() }),
        el("td", { text: record.server || "Bot" }),
        el("td", { text: record.level.charAt(0) + record.level.slice(1).toLowerCase() }),
        el("td", {}, el("pre", { text: record.message }))));
    }
    while (tbody.rows.length > limit) tbody.deleteRow(0);
    table.hidden = tbody.rows.length === 0;
    empty.hidden = tbody.rows.length !== 0;
  }

  return {
    refresh,
    reset(newServer, newLevel) {
      server = newServer;
      level = newLevel;
      lastId = 0;
      tbody.replaceChildren();
      return refresh();
    },
  };
}

async function showLog() {
  const state = await api("GET", "/api/state");
  const params = new URLSearchParams(window.location.search);
  const serverSelect = el("select", { id: "log-server" },
    el("option", { value: "*", text: "All" }),
    el("option", { value: "", text: "The bot itself" }),
    state.servers.map((server) => el("option", { value: server.name, text: server.name })));
  const levelSelect = el("select", { id: "log-level" },
    [["INFO", "Information and above"], ["WARNING", "Warnings and errors"], ["ERROR", "Errors only"]].map(([value, text]) => el("option", { value, text })));
  const auto = el("input", { type: "checkbox", id: "log-auto" });
  auto.checked = true;
  const holder = el("div", { id: "log-holder" });
  if (params.has("server")) serverSelect.value = params.get("server");
  content.replaceChildren(
    el("form", { class: "filters", onsubmit: (event) => event.preventDefault() },
      el("div", { class: "field" }, el("label", { for: "log-server", text: "Server" }), serverSelect),
      el("div", { class: "field" }, el("label", { for: "log-level", text: "Show" }), levelSelect),
      el("div", { class: "field checkbox" }, auto, el("label", { for: "log-auto", text: "Add new entries automatically" }))),
    el("p", { class: "help", text: "New entries are added quietly at the end of the table." }),
    holder);
  const selectedServer = () => (serverSelect.value === "*" ? null : serverSelect.value);
  const log = logView(holder, selectedServer(), 500);
  const apply = async () => {
    await log.reset(selectedServer(), levelSelect.value);
    announce("Showing " + holder.querySelectorAll("tbody tr").length + " entries.");
  };
  serverSelect.addEventListener("change", apply);
  levelSelect.addEventListener("change", apply);
  await log.reset(selectedServer(), levelSelect.value);
  startRefreshing(() => { if (auto.checked) log.refresh(); }, 3000);
}

// ------------------------------------------------------------ STAR pages

const CONNECTION_FIELDS = [
  { path: "host", label: "Server address", kind: "text", help: "The TeamTalk server's host name or IP address." },
  { path: "tcp_port", label: "TCP port", kind: "int", min: 1, max: 65535 },
  { path: "udp_port", label: "UDP port", kind: "int", min: 1, max: 65535, help: "Usually the same as the TCP port." },
  { path: "encrypted", label: "Encrypted connection", kind: "bool" },
  { path: "nickname", label: "Bot nickname", kind: "text", help: "The name people see in the channel." },
  { path: "username", label: "Username", kind: "text", help: "The bot's account on that server. Leave empty to log in as a guest, if the server allows it." },
  { path: "password", label: "Password", kind: "password" },
  { path: "channel", label: "Channel", kind: "text", help: "The channel path to join, such as /Hangout Room/. A single / is the root channel." },
  { path: "channel_password", label: "Channel password", kind: "password" },
];
const CONNECTION_SECTION = [{ path: "connection", title: "Connection and account", description: "", fields: CONNECTION_FIELDS }];
const SERVER_DEFAULTS = {
  name: "", host: "", tcp_port: 10333, udp_port: 10333, encrypted: false, nickname: "STAR Bot",
  username: "", password: "", channel: "/", channel_password: "", enabled: true,
};

const STATE_LABELS = {
  connected: "In the channel", joining: "Joining the channel", logging_in: "Logging in",
  connecting: "Connecting", reconnecting: "Reconnecting", off: "Turned off", stopped: "Not connected",
};

function stateText(server) {
  return server.state === "stopped" || server.state === "no_channel" ? server.state_text : (STATE_LABELS[server.state] || server.state_text);
}

function othersText(server) {
  if (server.others === null || server.others === undefined) return "";
  if (!server.others.length) return "No one";
  return server.others.length + ": " + server.others.join(", ");
}

function speakingText(server) {
  if (!server.running) return "";
  if (!server.speaking) return server.queue ? server.queue + " waiting" : "Nothing";
  return server.speaking + (server.queue ? " (" + server.queue + " waiting)" : "");
}

function serverUrl(name, suffix) {
  return "/api/servers/" + encodeURIComponent(name) + (suffix || "");
}

let voiceCache = null;
async function loadVoices() {
  if (!voiceCache) voiceCache = await api("GET", "/api/voices");
  return voiceCache;
}

function restartBotButton() {
  const button = el("button", { type: "button", id: "restart-bot", class: "danger", text: "Restart the whole bot" });
  button.addEventListener("click", async () => {
    if (!window.confirm("Restart the whole bot? It leaves every server and connects again a few seconds later; people's voice choices are forgotten.")) return;
    await act(button, () => api("POST", "/api/process/restart"), "Restarting the bot…");
  });
  return button;
}

// --------------------------------------------------------------- overview

async function showOverview() {
  const tableHolder = el("div", { id: "servers-table" });
  const starHolder = el("div", { id: "star-table" });
  const processInfo = el("p", { id: "process-info" });
  content.replaceChildren(
    el("section", { "aria-labelledby": "servers-heading" },
      el("h2", { id: "servers-heading", text: "Server list" }),
      el("p", {}, el("a", { href: "/add", text: "Add a server" })),
      tableHolder),
    el("section", { "aria-labelledby": "star-heading" },
      el("h2", { id: "star-heading", text: "STAR coagulators" }),
      el("p", { class: "help", text: "Where the voices come from. Change them in Bot settings." }),
      starHolder,
      el("p", { id: "voice-count" })),
    el("section", { "aria-labelledby": "process-heading" },
      el("h2", { id: "process-heading", text: "Bot process" }),
      processInfo,
      el("p", { class: "help", text: "Restarting the bot reconnects every server. To reconnect one server, use its Reconnect button; /restart and /quit sent to the bot also affect only the server they are sent on." }),
      restartBotButton()),
  );
  let shownServers = "";
  let shownStar = "";
  async function refresh() {
    let state;
    try {
      state = await api("GET", "/api/state");
    } catch (error) {
      setText("process-info", error.message);
      return;
    }
    const names = state.servers.map((server) => server.name + ":" + server.enabled + ":" + server.running).join(",");
    if (names !== shownServers) {
      const focusedId = document.activeElement && document.activeElement.id;
      tableHolder.replaceChildren(serverTable(state.servers, refresh));
      shownServers = names;
      if (focusedId && document.getElementById(focusedId)) document.getElementById(focusedId).focus();
    }
    for (const server of state.servers) updateServerRow(server);
    const starKey = JSON.stringify(state.star.coagulators);
    if (starKey !== shownStar) {
      starHolder.replaceChildren(starTable(state.star.coagulators));
      shownStar = starKey;
    }
    setText("voice-count", state.star.voices + (state.star.voices === 1 ? " voice" : " voices") + " available.");
    setText("process-info", "Running since " + formatTime(state.process.started_at) + " (process " + state.process.pid + ", TeamTalk SDK " + state.process.sdk + ").");
  }
  await refresh();
  announceCarried();
  startRefreshing(refresh);
}

function serverTable(servers, refresh) {
  if (!servers.length) {
    return el("p", {}, "There are no servers yet. ", el("a", { href: "/add", text: "Add the first one" }), ".");
  }
  const rows = servers.map((server) => {
    const id = "row-" + server.name;
    const toggle = el("button", {
      type: "button", id: "toggle-" + server.name,
      text: server.enabled ? "Turn off " + server.name : "Turn on " + server.name,
      onclick: async (event) => {
        const result = await act(event.target, () => api("POST", serverUrl(server.name, "/enabled"), { enabled: !server.enabled }),
          (server.enabled ? "Turning off " : "Turning on ") + server.name + "…");
        if (result) refresh();
      },
    });
    const reconnectText = server.running ? "Reconnect " : "Connect ";
    const reconnect = server.enabled ? el("button", {
      type: "button", id: "reconnect-" + server.name, text: reconnectText + server.name,
      onclick: async (event) => {
        const result = await act(event.target, () => api("POST", serverUrl(server.name, "/restart")), (server.running ? "Reconnecting " : "Connecting ") + server.name + "…");
        if (result) refresh();
      },
    }) : null;
    return el("tr", { id },
      el("th", { scope: "row" }, el("a", { href: "/servers/" + encodeURIComponent(server.name), text: server.name })),
      el("td", { id: id + "-state" }),
      el("td", { id: id + "-channel" }),
      el("td", { id: id + "-others" }),
      el("td", { id: id + "-speaking" }),
      el("td", { id: id + "-host" }),
      el("td", { class: "actions" }, toggle, " ", reconnect),
    );
  });
  return el("table", {},
    el("caption", { class: "visually-hidden", text: "Servers" }),
    el("thead", {}, el("tr", {},
      ["Server", "State", "Channel", "Others in the channel", "Speaking", "Address", "Actions"].map((h) => el("th", { scope: "col", text: h })))),
    el("tbody", {}, rows));
}

function updateServerRow(server) {
  const id = "row-" + server.name;
  setText(id + "-state", stateText(server));
  setText(id + "-channel", server.channel_path || server.channel);
  setText(id + "-others", othersText(server));
  setText(id + "-speaking", speakingText(server));
  setText(id + "-host", server.host + (server.encrypted ? ", encrypted" : ""));
}

function starTable(coagulators) {
  if (!coagulators.length) return el("p", { text: "No STAR coagulators are set up." });
  return el("table", {},
    el("caption", { class: "visually-hidden", text: "STAR coagulators" }),
    el("thead", {}, el("tr", {}, ["Coagulator", "Address", "State", "Voices", "Requests in progress"].map((h) => el("th", { scope: "col", text: h })))),
    el("tbody", {}, coagulators.map((c) => el("tr", {},
      el("th", { scope: "row", text: c.name }),
      el("td", { text: c.host }),
      el("td", { text: c.connected ? "Connected" : "Not reachable, trying again" }),
      el("td", { text: String(c.voices) }),
      el("td", { text: String(c.pending) }),
    ))));
}

// ----------------------------------------------------------------- server

async function showServer(name) {
  const [data, voices] = await Promise.all([api("GET", serverUrl(name)), loadVoices().catch(() => ({ voices: [], default: "" }))]);
  let config = data.config;

  // Status
  const statusList = el("dl", { class: "status-list" });
  const statusItems = [
    ["state", "State"], ["host", "Address"], ["nickname", "Nickname"], ["account", "Account"],
    ["channel", "Channel"], ["others", "Others in the channel"], ["speaking", "Speaking now"], ["since", "Connected since"],
  ];
  for (const [key, label] of statusItems) statusList.append(el("dt", { text: label }), el("dd", { id: "status-" + key }));
  const toggleButton = el("button", { type: "button", id: "toggle" });
  const reconnectButton = el("button", { type: "button", id: "reconnect" });
  const disconnectButton = el("button", { type: "button", id: "disconnect", text: "Disconnect" });
  const stopButton = el("button", { type: "button", id: "stop-speaking", text: "Stop speaking" });
  const removeButton = el("button", { type: "button", id: "remove", class: "danger", text: "Remove this server" });
  let running = false;

  function updateStatus(status) {
    running = status.running;
    setText("status-state", stateText(status));
    setText("status-host", status.host + (status.encrypted ? ", encrypted" : ", not encrypted"));
    setText("status-nickname", status.nickname);
    setText("status-account", status.username || "Guest (no username)");
    setText("status-channel", status.channel_path || status.channel);
    setText("status-others", othersText(status));
    setText("status-speaking", speakingText(status));
    setText("status-since", status.started_at ? formatTime(status.started_at) : "");
    const toggleText = status.enabled ? "Turn off" : "Turn on";
    if (toggleButton.textContent !== toggleText) toggleButton.textContent = toggleText;
    toggleButton.dataset.enabled = status.enabled ? "1" : "0";
    const reconnectText = status.running ? "Reconnect" : "Connect";
    if (reconnectButton.textContent !== reconnectText) reconnectButton.textContent = reconnectText;
    reconnectButton.hidden = !status.enabled;
    disconnectButton.hidden = !status.running;
    stopButton.hidden = !status.running;
    speakSection.hidden = !status.running;
    peopleSection.hidden = !status.running;
    notRunning.hidden = status.running;
  }

  toggleButton.addEventListener("click", async () => {
    const enable = toggleButton.dataset.enabled !== "1";
    const result = await act(toggleButton, () => api("POST", serverUrl(name, "/enabled"), { enabled: enable }),
      (enable ? "Turning on " : "Turning off ") + name + "…");
    if (result) { updateStatus(result.status); config.enabled = enable; document.getElementById(fieldId("enabled")).checked = enable; }
  });
  reconnectButton.addEventListener("click", async () => {
    const result = await act(reconnectButton, () => api("POST", serverUrl(name, "/restart")), (running ? "Reconnecting " : "Connecting ") + name + "…");
    if (result) updateStatus(result.status);
  });
  disconnectButton.addEventListener("click", async () => {
    const result = await act(disconnectButton, () => api("POST", serverUrl(name, "/disconnect")), "Disconnecting from " + name + "…");
    if (result) { updateStatus(result.status); reconnectButton.focus(); }
  });
  stopButton.addEventListener("click", async () => {
    const result = await act(stopButton, () => api("POST", serverUrl(name, "/stop")), "Stopping…");
    if (result) updateStatus(result.status);
  });
  removeButton.addEventListener("click", async () => {
    if (!window.confirm("Remove " + name + "? The bot leaves that server and its settings are deleted.")) return;
    const result = await act(removeButton, () => api("DELETE", serverUrl(name)), "Removing " + name + "…");
    if (result) { carryMessage(result.message); window.location.href = "/"; }
  });

  // Say something
  const voiceSelect = el("select", { id: "speak-voice", "aria-describedby": "speak-voice-help" },
    el("option", { value: "", text: voices.default ? "The default voice (" + voices.default + ")" : "The default voice (none is set)" }),
    voices.voices.map((voice) => el("option", { value: voice, text: voice })));
  const speakText = el("textarea", { id: "speak-text", rows: 3, maxlength: "4000" });
  const speakButton = el("button", { type: "submit", text: "Speak" });
  const previewButton = el("button", { type: "button", id: "preview", text: "Preview the voice" });
  const speakForm = el("form", { id: "speak-form", novalidate: true },
    el("div", { class: "field" }, el("label", { for: "speak-voice", text: "Voice" }), voiceSelect,
      el("p", { class: "help", id: "speak-voice-help", text: voices.voices.length + " voices. Type the first letters of a name to jump to it; the Voices page can search them." })),
    el("div", { class: "field" }, el("label", { for: "speak-text", text: "What to say" }), speakText),
    el("div", { class: "button-row" }, speakButton, previewButton));
  async function speak(button, text) {
    const result = await act(button, () => api("POST", serverUrl(name, "/speak"), { voice: voiceSelect.value, text }), "Sending…");
    if (result) updateStatus(result.status);
    return result;
  }
  speakForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    speakText.removeAttribute("aria-invalid");
    if (!speakText.value.trim()) { reportFormError(fieldError(speakText, "Type what the bot should say first.")); return; }
    if (await speak(speakButton, speakText.value)) speakText.value = "";
  });
  previewButton.addEventListener("click", async () => {
    const settings = await api("GET", "/api/settings");
    const voice = voiceSelect.value || voices.default;
    if (!voice) { showError("Choose a voice first (there is no default voice)."); voiceSelect.focus(); return; }
    await speak(previewButton, settings.bot.voice_preview_text.split("{voice}").join(voice));
  });
  const speakSection = el("section", { "aria-labelledby": "h-speak" },
    el("h2", { id: "h-speak", text: "Say something" }),
    el("p", { class: "help", text: "The bot speaks it in its channel on this server, like a private message to the bot would." }),
    speakForm);

  // People's choices
  const peopleHolder = el("div", { id: "people" });
  const peopleRefresh = el("button", { type: "button", id: "people-refresh", text: "Refresh the list" });
  let peopleKey = "";
  async function loadPeople(announceIt) {
    let result;
    try { result = await api("GET", serverUrl(name, "/people")); } catch (error) { if (announceIt) showError(error.message); return; }
    const key = JSON.stringify(result.people);
    if (key !== peopleKey) {
      peopleKey = key;
      const focusedId = document.activeElement && document.activeElement.id;
      peopleHolder.replaceChildren(peopleTable(result.people));
      if (focusedId && document.getElementById(focusedId)) document.getElementById(focusedId).focus();
    }
    if (announceIt) announce(result.people.length === 1 ? "1 person." : result.people.length + " people.");
  }
  function peopleTable(people) {
    if (!people.length) return el("p", { text: "No one has chosen a voice, rate, pitch or name announcements here yet. Everyone gets the default voice." });
    return el("table", {},
      el("caption", { class: "visually-hidden", text: "People's choices on this server" }),
      el("thead", {}, el("tr", {}, ["Person", "Voice", "Rate", "Pitch", "Name announced", "Actions"].map((h) => el("th", { scope: "col", text: h })))),
      el("tbody", {}, people.map((person, index) => {
        const forget = el("button", { type: "button", id: "forget-" + index, text: "Forget " + person.key + "'s choices" });
        forget.addEventListener("click", async () => {
          const result = await act(forget, () => api("DELETE", serverUrl(name, "/people/" + encodeURIComponent(person.key))), "Forgetting…");
          if (result) { await loadPeople(false); peopleRefresh.focus(); }
        });
        return el("tr", {},
          el("th", { scope: "row", text: person.key }),
          el("td", { text: person.voice || "Default" }),
          el("td", { text: person.rate || "Default" }),
          el("td", { text: person.pitch || "Default" }),
          el("td", { text: person.names ? "Yes" : "No" }),
          el("td", { class: "actions" }, forget));
      })));
  }
  peopleRefresh.addEventListener("click", () => loadPeople(true));
  const peopleSection = el("section", { "aria-labelledby": "h-people" },
    el("h2", { id: "h-people", text: "People's choices" }),
    el("p", { class: "help", text: "What people chose with the voice, rate, pitch and name commands on this server. The bot forgets these when it reconnects to the server." }),
    peopleRefresh, peopleHolder);
  const notRunning = el("p", { class: "state-note", text: "Say something and people's choices appear while the bot is connected to this server." });

  // Settings
  const enabledField = renderField({ path: "enabled", label: "Connect to this server (turned on)", kind: "bool" }, config.enabled);
  const settingsForm = el("form", { id: "settings-form", novalidate: true },
    ...renderSections(CONNECTION_SECTION, config, []),
    enabledField,
    el("button", { type: "submit", text: "Save settings" }),
    el("p", { class: "help", text: "Saving reconnects this server with the new settings. Other servers are not affected." }));
  settingsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    let updated;
    try { updated = collectSections(CONNECTION_SECTION, config); } catch (error) { reportFormError(error); return; }
    updated.enabled = document.getElementById(fieldId("enabled")).checked;
    const button = settingsForm.querySelector("button[type=submit]");
    const result = await act(button, () => api("PUT", serverUrl(name), { config: updated }), "Saving and reconnecting " + name + "…");
    if (result) { config = updated; updateStatus(result.status); }
  });

  const logHolder = el("div", { id: "server-log" });
  content.replaceChildren(
    el("section", { "aria-labelledby": "h-status" },
      el("h2", { id: "h-status", text: "Status" }), statusList,
      el("div", { class: "button-row" }, toggleButton, reconnectButton, disconnectButton, stopButton),
      el("p", { class: "help", text: "Disconnect leaves this server until the bot restarts or you press Connect, like /quit sent to the bot. Turn off also keeps it off after restarts." })),
    notRunning, speakSection, peopleSection,
    el("section", { "aria-labelledby": "h-settings" },
      el("h2", { id: "h-settings", text: "Settings" }), settingsForm),
    el("section", { "aria-labelledby": "h-log" },
      el("h2", { id: "h-log", text: "Recent log" }),
      el("p", {}, el("a", { href: "/log?server=" + encodeURIComponent(name), text: "Full log for " + name })),
      logHolder),
    el("section", { "aria-labelledby": "h-remove" },
      el("h2", { id: "h-remove", text: "Remove" }),
      el("p", { class: "help", text: "Disconnects the bot from this server and deletes its settings." }),
      removeButton),
  );
  updateStatus(data.status);
  if (data.status.running) await loadPeople(false);
  const log = logView(logHolder, name, 50);
  await log.refresh();
  announceCarried();
  let wasRunning = data.status.running;
  startRefreshing(async () => {
    try {
      const fresh = await api("GET", serverUrl(name));
      updateStatus(fresh.status);
      if (fresh.status.running && !wasRunning) peopleKey = "";
      wasRunning = fresh.status.running;
      if (fresh.status.running) loadPeople(false);
    } catch (error) { /* keep the last values */ }
    log.refresh();
  });
}

// -------------------------------------------------------------------- add

async function showAdd() {
  const state = await api("GET", "/api/state");
  const nameInput = el("input", { type: "text", id: "server-name", required: true, autocomplete: "off", spellcheck: "false", "aria-describedby": "server-name-help", maxlength: "40" });
  const copySelect = el("select", { id: "copy-from", "aria-describedby": "copy-from-help" },
    el("option", { value: "", text: "Nothing, start from the defaults" }),
    state.servers.map((server) => el("option", { value: server.name, text: server.name })));
  const startNow = el("input", { type: "checkbox", id: "start-now" });
  startNow.checked = true;
  const fieldsHolder = el("div", {}, ...renderSections(CONNECTION_SECTION, SERVER_DEFAULTS, []));
  const submit = el("button", { type: "submit", text: "Add server" });
  const linkInput = el("input", { type: "text", id: "tt-link", autocomplete: "off", spellcheck: "false", "aria-describedby": "tt-link-help", inputmode: "url" });
  let appliedLink = null;
  let appliedText = "";

  function applyLink(parsed) {
    const set = (path, value) => {
      const control = document.getElementById(fieldId(path));
      if (!control) return;
      if (control.type === "checkbox") control.checked = Boolean(value);
      else control.value = value;
      control.removeAttribute("aria-invalid");
    };
    const v = parsed.values;
    set("host", parsed.host);
    set("tcp_port", v.tcpport);
    set("udp_port", v.udpport);
    set("encrypted", v.encrypted);
    set("username", v.username);
    set("password", v.password);
    set("channel", v.channel || "/");
    set("channel_password", v.chanpasswd);
    const describe = {
      tcpport: () => "TCP port " + v.tcpport,
      udpport: () => "UDP port " + v.udpport,
      encrypted: () => (v.encrypted ? "encrypted" : "not encrypted"),
      username: () => (v.username ? "username " + v.username : "no username"),
      password: () => (v.password ? "a password" : "no password"),
      channel: () => "channel " + (v.channel || "/ (the root channel)"),
      chanpasswd: () => (v.chanpasswd ? "a channel password" : "no channel password"),
    };
    const given = ["address " + parsed.host].concat(TT_LINK_PARAMETERS.filter((p) => parsed.given.has(p)).map((p) => describe[p]()));
    const defaulted = TT_LINK_PARAMETERS.filter((p) => !parsed.given.has(p)).map((p) => describe[p]());
    let summary = "Filled in from the link: " + given.join(", ") + ".";
    if (defaulted.length) summary += " Not in the link, so set to TeamTalk's defaults: " + defaulted.join(", ") + ".";
    if (parsed.ignored.length) summary += " Ignored unknown link parts: " + parsed.ignored.join(", ") + ".";
    if (parsed.notes.length) summary += " " + parsed.notes.join(" ");
    if (!nameInput.value.trim()) {
      nameInput.value = suggestServerName(parsed.host, state.servers.map((server) => server.name));
      summary += " Suggested server name: " + nameInput.value + ".";
    }
    return summary;
  }

  function fillFromLink(trigger) {
    const text = linkInput.value.trim();
    const wasInvalid = linkInput.getAttribute("aria-invalid") === "true";
    linkInput.removeAttribute("aria-invalid");
    if (!text) {
      if (trigger === "enter") reportFormError(fieldError(linkInput, "Paste a TeamTalk link first."));
      else if (wasInvalid) clearAlert();
      return;
    }
    if (trigger === "leave" && text === appliedText) return;
    const parsed = parseTeamTalkLink(text);
    if (parsed.error) {
      if (trigger === "leave") { linkInput.setAttribute("aria-invalid", "true"); showError(parsed.error); }
      else reportFormError(fieldError(linkInput, parsed.error));
      return;
    }
    appliedLink = parsed;
    appliedText = text;
    announce(applyLink(parsed));
  }
  linkInput.addEventListener("paste", () => setTimeout(() => fillFromLink("paste"), 0));
  linkInput.addEventListener("change", () => fillFromLink("leave"));
  linkInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); fillFromLink("enter"); }
  });

  copySelect.addEventListener("change", async () => {
    let values = SERVER_DEFAULTS;
    if (copySelect.value) {
      try { values = (await api("GET", serverUrl(copySelect.value))).config; } catch (error) { showError(error.message); return; }
    }
    fieldsHolder.replaceChildren(...renderSections(CONNECTION_SECTION, values, []));
    let message = copySelect.value ? "Filled in from " + copySelect.value + ". Change what differs." : "Filled in with the defaults.";
    if (appliedLink) { applyLink(appliedLink); message += " The connection details from the link were kept."; }
    announce(message);
  });

  const addForm = el("form", { id: "add-form", novalidate: true },
    el("div", { class: "field" },
      el("label", { for: "tt-link", text: "TeamTalk link (optional)" }), linkInput,
      el("p", { class: "help", id: "tt-link-help", text: "Paste a tt:// link, such as one from Generate TT URL in TeamTalk, to fill in the address, ports, encryption, account and channel. Pasting fills in the form; after typing or editing a link, press Enter. Anything the link leaves out gets TeamTalk's default." })),
    el("div", { class: "field" },
      el("label", { for: "server-name", text: "Server name (required)" }), nameInput,
      el("p", { class: "help", id: "server-name-help", text: "A short name for this server, such as home. Letters, numbers, dots, dashes and underscores." })),
    el("div", { class: "field" },
      el("label", { for: "copy-from", text: "Copy settings from" }), copySelect,
      el("p", { class: "help", id: "copy-from-help", text: "Starts with another server's settings, such as the bot's account and nickname." })),
    fieldsHolder,
    el("div", { class: "field checkbox" }, startNow, el("label", { for: "start-now", text: "Connect right away" })),
    submit);

  addForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = nameInput.value.trim();
    nameInput.removeAttribute("aria-invalid");
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/.test(name)) {
      reportFormError(fieldError(nameInput, "Please give the server a name using letters, numbers, dots, dashes or underscores, starting with a letter or number."));
      return;
    }
    if (state.servers.some((server) => server.name === name)) {
      reportFormError(fieldError(nameInput, "There is already a server named " + name + ". Choose another name."));
      return;
    }
    let config;
    try { config = collectSections(CONNECTION_SECTION, SERVER_DEFAULTS); } catch (error) { reportFormError(error); return; }
    const host = document.getElementById(fieldId("host"));
    if (!config.host.trim()) { reportFormError(fieldError(host, "Fill in the server address.")); return; }
    config.name = name;
    config.enabled = startNow.checked;
    const result = await act(submit, () => api("POST", "/api/servers", { config }), "Adding " + name + "…");
    if (result) { carryMessage(result.message); window.location.href = "/servers/" + encodeURIComponent(name); }
  });

  content.replaceChildren(addForm);
  linkInput.focus();
}

// ----------------------------------------------------------------- voices

async function showVoices() {
  const search = el("input", { type: "search", id: "voice-search", autocomplete: "off", spellcheck: "false", "aria-describedby": "voice-search-help" });
  const holder = el("div", { id: "voice-holder" });
  const form = el("form", { class: "inline-form", role: "search" },
    el("label", { for: "voice-search", text: "Find voices" }), search,
    el("button", { type: "submit", text: "Search" }),
    el("p", { class: "help", id: "voice-search-help", text: "Part of a voice's name, such as eloquence or en-us. Leave empty to list them all." }));
  async function load(announceCount) {
    let data;
    try { data = await api("GET", "/api/voices?q=" + encodeURIComponent(search.value.trim())); } catch (error) { showError(error.message); return; }
    const intro = data.total
      ? el("p", { text: (search.value.trim() ? data.voices.length + " of " + data.total + " voices match." : data.total + " voices.") + (data.default ? " The default voice is " + data.default + "." : " No default voice is set.") })
      : el("p", { text: "No voices are available. Check the STAR coagulators on the Servers page." });
    holder.replaceChildren(intro, data.voices.length ? el("ol", { class: "voice-list" }, data.voices.map((voice) => el("li", { text: voice }))) : "");
    if (announceCount) announce(data.voices.length === 1 ? "1 voice." : data.voices.length + " voices.");
  }
  form.addEventListener("submit", (event) => { event.preventDefault(); load(true); });
  content.replaceChildren(form,
    el("p", { class: "help", text: "People choose a voice by sending the bot voice and a name or number; the default voice is set in Bot settings." }),
    holder);
  await load(false);
}

// --------------------------------------------------------------- settings

async function showSettings() {
  const [settings, voices] = await Promise.all([api("GET", "/api/settings"), loadVoices().catch(() => ({ voices: [] }))]);
  const sections = [
    { path: "star", title: "STAR coagulators", description: "Where the bot gets its voices.", fields: [
      { path: "star.hosts", label: "Coagulator addresses", kind: "lines", help: "One websocket address per line, such as ws://localhost:7774. Changing them reconnects to the coagulators." },
      { path: "star.request_timeout", label: "Seconds to wait for speech", kind: "float", min: 1, help: "A request that takes longer fails with a message to the person." },
    ] },
    { path: "bot", title: "Bot", description: "Used on every server, right away.", fields: [
      { path: "bot.command_prefix", label: "Command prefix", kind: "text", help: "What private messages start with to be commands, such as / or !. Commands also always work with /." },
      { path: "bot.default_voice", label: "Default voice", kind: "select", help: "For people who have not chosen a voice.",
        choices: [{ value: "", label: "None (people must choose one)" }].concat(voices.voices.map((voice) => ({ value: voice, label: voice }))) },
      { path: "bot.voice_preview_text", label: "Preview text", kind: "text", help: "What preview says. {voice} becomes the voice's name." },
      { path: "bot.max_voice_results", label: "Voices listed in one reply", kind: "int", min: 1 },
      { path: "bot.temp_dir", label: "Folder for speech files", kind: "text", help: "Where speech is kept while it plays. Leave empty for the system's temporary folder." },
    ] },
    { path: "teamtalk", title: "TeamTalk", description: "", fields: [
      { path: "teamtalk.client_name", label: "Client name", kind: "text", help: "What the bot reports as its TeamTalk client. Used the next time each server logs in." },
      { path: "teamtalk.reconnect_seconds", label: "Seconds between reconnection attempts", kind: "float", min: 1 },
      { path: "teamtalk.license_name", label: "SDK license name", kind: "text", help: "Only if your TeamTalk SDK needs a license. Takes effect when the bot restarts." },
      { path: "teamtalk.license_key", label: "SDK license key", kind: "password" },
    ] },
  ];
  const values = { star: settings.star, bot: settings.bot, teamtalk: settings.teamtalk };
  const form = el("form", { id: "bot-settings", novalidate: true },
    ...renderSections(sections, values, ["teamtalk"]),
    el("button", { type: "submit", text: "Save settings" }));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    let updated;
    try { updated = collectSections(sections, values); } catch (error) { reportFormError(error); return; }
    if (!updated.star.hosts.length) { reportFormError(fieldError(document.getElementById(fieldId("star.hosts")), "Give at least one coagulator address.")); return; }
    const button = form.querySelector("button[type=submit]");
    const result = await act(button, () => api("PUT", "/api/settings", updated), "Saving…");
    if (result) { Object.assign(values, updated); voiceCache = null; }
  });
  content.replaceChildren(form,
    el("section", { "aria-labelledby": "h-web" },
      el("h2", { id: "h-web", text: "This page" }),
      el("p", { text: "Served by the bot on " + settings.web.host + ":" + settings.web.port + " and reachable only over Tailscale, without a password. Settings are saved in " + settings.config_path + "." })),
    el("section", { "aria-labelledby": "h-restart" },
      el("h2", { id: "h-restart", text: "Restart" }),
      el("p", { class: "help", text: "Reconnects every server. People's voice choices are forgotten." }),
      restartBotButton()));
}

// ------------------------------------------------------------------- start

const pages = {
  overview: showOverview,
  server: () => showServer(argument),
  add: showAdd,
  voices: showVoices,
  log: showLog,
  settings: showSettings,
  notfound: async () => { content.replaceChildren(el("p", {}, "This page does not exist. ", el("a", { href: "/", text: "Go to the server list" }), ".")); },
};

(pages[page] || pages.notfound)().catch((error) => {
  content.replaceChildren(el("p", { text: "This page could not be loaded: " + error.message }));
  showError(error.message);
});
