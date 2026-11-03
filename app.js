"use strict";

const $ = (id) => document.getElementById(id);
let token = "";
let me = null;
let contacts = [];
let activePeer = "";
let lastMessageId = 0;
let socket = null;
let reconnectTimer = null;
let peerConnection = null;
let localStream = null;
let pendingOffer = null;
let callType = "audio";
let remoteDescriptionReady = false;
let pendingCandidates = [];
let allMessages = [];
let messagesExpanded = false;
let allAuditEvents = [];
let auditExpanded = false;

function setStatus(message) { $("status").textContent = message; }
function authHeaders(extra = {}) {
  return Object.assign({"Authorization": `Bearer ${token}`}, extra);
}
async function api(url, options = {}) {
  const headers = authHeaders(options.headers || {});
  const response = await fetch(url, {...options, headers});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function safeText(tag, text) {
  const el = document.createElement(tag);
  el.textContent = text == null ? "" : String(text);
  return el;
}
function showApp() {
  $("loginPanel").hidden = Boolean(me);
  $("appPanel").hidden = !me;
  if (me) {
    $("currentUser").textContent = me.username;
    $("currentRole").textContent = me.role;
    $("adminPanel").hidden = me.role !== "admin";
  }
}
function showError(error) { setStatus(error.message || String(error)); }
function showWorkspaceTab(name) {
  const home = name === "home";
  $("homeTab").hidden = !home;
  $("folderTab").hidden = home;
  $("homeTabButton").classList.toggle("primary", home);
  $("folderTabButton").classList.toggle("primary", !home);
  if (home) $("homeTabButton").setAttribute("aria-current", "page");
  else $("folderTabButton").setAttribute("aria-current", "page");
  if (home) $("folderTabButton").removeAttribute("aria-current");
  else $("homeTabButton").removeAttribute("aria-current");
  if (!home && token) loadFiles().catch(showError);
}
$("homeTabButton").addEventListener("click", () => showWorkspaceTab("home"));
$("folderTabButton").addEventListener("click", () => showWorkspaceTab("folders"));
$("toggleMessages").addEventListener("click", () => {
  messagesExpanded = !messagesExpanded;
  $("toggleMessages").textContent = messagesExpanded ? "Show latest 20" : "Show all";
  renderMessages();
});
$("toggleAudit").addEventListener("click", () => {
  auditExpanded = !auditExpanded;
  $("toggleAudit").textContent = auditExpanded ? "Show latest 8" : "Show all";
  renderAudit();
});

$("loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({username: $("loginUsername").value.trim(), password: $("loginPassword").value})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Sign in failed.");
    token = data.token;
    me = {username: data.username, role: data.role};
    $("loginPassword").value = "";
    showApp();
    showWorkspaceTab("home");
    setStatus("Signed in.");
    connectSocket();
    await Promise.all([loadContacts(), loadFiles()]);
    if (me.role === "admin") await Promise.all([loadUsers(), loadStats(), loadAudit()]);
    startRefreshLoop();
  } catch (error) { showError(error); }
});

$("logoutButton").addEventListener("click", async () => {
  stopCall();
  if (socket) socket.close();
  try { await api("/api/logout", {method: "POST"}); } catch (_) {}
  token = ""; me = null; activePeer = ""; contacts = [];
  $("contactSelect").replaceChildren(new Option("Choose an employee", ""));
  $("messageHistory").replaceChildren();
  showApp(); setStatus("Signed out.");
});

$("refreshContacts").addEventListener("click", () => loadContacts().catch(showError));
$("contactSelect").addEventListener("change", async () => {
  activePeer = $("contactSelect").value;
  lastMessageId = 0;
  await loadMessages().catch(showError);
});
$("messageForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!activePeer) return setStatus("Choose an employee first.");
  const body = $("messageInput").value.trim();
  if (!body) return;
  try {
    await api("/api/messages", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({recipient:activePeer, body})});
    $("messageInput").value = "";
    await loadMessages();
  } catch (error) { showError(error); }
});
$("uploadForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("fileInput").files[0];
  if (!file) return;
  const form = new FormData(); form.append("file", file);
  try {
    const response = await fetch("/api/files", {method:"POST", headers:authHeaders(), body:form});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Upload failed.");
    $("fileInput").value = "";
    setStatus(`Uploaded ${file.name} into the database.`);
    await loadFiles();
    if (me.role === "admin") await loadStats();
  } catch (error) { showError(error); }
});

$("createEmployeeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const data = await api("/api/admin/users", {method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({username:$("newUsername").value.trim(), password:$("newPassword").value})});
    $("newUsername").value = ""; $("newPassword").value = "";
    setStatus(data.message);
    await Promise.all([loadUsers(), loadStats(), loadContacts()]);
  } catch (error) { showError(error); }
});
$("refreshUsers").addEventListener("click", () => loadUsers().catch(showError));
$("refreshAudit").addEventListener("click", () => loadAudit().catch(showError));

async function loadContacts() {
  const data = await api("/api/contacts");
  contacts = data.contacts;
  const previous = activePeer;
  const select = $("contactSelect");
  select.replaceChildren(new Option("Choose an employee", ""));
  for (const contact of contacts) {
    const label = `${contact.username} (${contact.online ? "online" : "offline"})`;
    select.add(new Option(label, contact.username));
  }
  if (contacts.some(c => c.username === previous)) select.value = previous;
  activePeer = select.value;
  $("onlineList").textContent = contacts.filter(c => c.online).map(c => c.username).join(", ") || "No other employees online";
}
async function loadMessages() {
  if (!activePeer) { allMessages = []; renderMessages(); return; }
  const data = await api(`/api/messages?with_user=${encodeURIComponent(activePeer)}`);
  allMessages = data.messages || [];
  lastMessageId = allMessages.reduce((max, message) => Math.max(max, message.id), 0);
  renderMessages();
}
function renderMessages() {
  const history = $("messageHistory");
  history.replaceChildren();
  const visible = messagesExpanded ? allMessages : allMessages.slice(-20);
  if (!visible.length) history.append(safeText("p", "No messages yet. Start the conversation."));
  for (const message of visible) {
    const line = document.createElement("p");
    const strong = safeText("strong", message.sender === me.username ? "You" : message.sender);
    const time = safeText("small", ` · ${new Date(message.created_at).toLocaleString()}`);
    line.append(strong, time, document.createElement("br"), safeText("span", message.body));
    history.append(line);
  }
  history.scrollTop = history.scrollHeight;
  $("toggleMessages").textContent = messagesExpanded ? "Show latest 20" : "Show all";
  $("toggleMessages").hidden = allMessages.length <= 20;
}

async function loadFiles() {
  const data = await api("/api/files");
  const files = data.files || [];
  const folders = $("fileFolders");
  const list = $("fileList");
  folders.replaceChildren(); list.replaceChildren();
  const categories = ["Documents", "Images", "Videos", "Audio", "Archives", "Code", "Other"];
  const grouped = Object.fromEntries(categories.map(category => [category, []]));
  for (const file of files) (grouped[file.category] || grouped.Other).push(file);
  const icon = {Documents:"DOC", Images:"IMG", Videos:"VID", Audio:"AUD", Archives:"ZIP", Code:"CODE", Other:"FILE"};
  for (const category of categories) {
    const items = grouped[category];
    const card = document.createElement("section");
    card.className = "folder-card";
    const heading = safeText("h3", `${icon[category]}  ${category}`);
    const count = safeText("p", `${items.length} file${items.length === 1 ? "" : "s"}`);
    count.className = "file-meta";
    const open = document.createElement("button"); open.type = "button"; open.textContent = "View files";
    open.disabled = items.length === 0;
    open.addEventListener("click", () => {
      list.replaceChildren();
      const title = safeText("h3", category + " folder");
      list.append(title);
      for (const file of items) list.append(makeFileRow(file));
      list.scrollIntoView({behavior:"smooth", block:"start"});
    });
    card.append(heading, count, open); folders.append(card);
  }
  if (!files.length) list.append(safeText("li", "No files yet. Upload the first team file above."));
  else {
    const recent = safeText("h3", "Recently uploaded");
    list.append(recent);
    for (const file of files.slice(0, 8)) list.append(makeFileRow(file));
  }
  if (me && me.role === "admin") await loadStats();
}
function makeFileRow(file) {
  const li = document.createElement("li");
  const row = document.createElement("div"); row.className = "file-row";
  const details = document.createElement("div");
  details.append(safeText("strong", file.name), safeText("div", `${file.category || "Other"} · ${formatBytes(file.size)} · uploaded by ${file.uploader} · ${new Date(file.uploaded_at).toLocaleString()}`));
  details.lastChild.className = "file-meta";
  const actions = document.createElement("div"); actions.className = "file-actions";
  const download = document.createElement("button"); download.type = "button"; download.textContent = "Download";
  download.addEventListener("click", async () => {
    try {
      const response = await fetch(`/api/files/${encodeURIComponent(file.id)}`, {headers:authHeaders()});
      if (!response.ok) { const err = await response.json().catch(() => ({})); throw new Error(err.error || "Download failed."); }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = file.name; document.body.append(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { showError(error); }
  });
  actions.append(download);
  if (me.role === "admin" || file.uploader === me.username) {
    const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "Delete"; remove.className = "danger";
    remove.addEventListener("click", async () => {
      if (!confirm(`Delete ${file.name}?`)) return;
      try { await api(`/api/files/${encodeURIComponent(file.id)}`, {method:"DELETE"}); await loadFiles(); }
      catch (error) { showError(error); }
    });
    actions.append(remove);
  }
  row.append(details, actions); li.append(row); return li;
}

async function loadUsers() {
  const data = await api("/api/admin/users");
  $("employeeCount").textContent = data.employee_count;
  $("accountCount").textContent = data.account_count;
  const list = $("userList"); list.replaceChildren();
  for (const user of data.users) {
    const li = document.createElement("li");
    li.append(safeText("span", `${user.username} — ${user.role} — created ${new Date(user.created_at).toLocaleString()} `));
    if (user.role === "employee") {
      const button = document.createElement("button"); button.textContent = "Delete employee";
      button.addEventListener("click", async () => {
        if (!confirm(`Permanently delete employee account "${user.username}"?`)) return;
        try {
          await api(`/api/admin/users/${encodeURIComponent(user.username)}`, {method:"DELETE"});
          setStatus(`Deleted ${user.username}.`);
          await Promise.all([loadUsers(), loadStats(), loadContacts()]);
        } catch (error) { showError(error); }
      });
      li.append(button);
    }
    list.append(li);
  }
}
async function loadStats() {
  const data = await api("/api/admin/stats");
  $("employeeCount").textContent = data.employee_count;
  $("accountCount").textContent = data.account_count;
  $("activeSessions").textContent = data.active_sessions;
  $("messageCount").textContent = data.message_count;
  $("fileCount").textContent = data.file_count;
}
async function loadAudit() {
  const data = await api("/api/admin/audit");
  allAuditEvents = data.events || [];
  renderAudit();
}
function renderAudit() {
  const list = $("auditList"); list.replaceChildren();
  const visible = auditExpanded ? allAuditEvents : allAuditEvents.slice(0, 8);
  for (const event of visible) {
    list.append(safeText("li", `${new Date(event.created_at).toLocaleString()} — ${event.username}: ${event.action}${event.detail ? " (" + event.detail + ")" : ""}`));
  }
  if (!visible.length) list.append(safeText("li", "No activity recorded yet."));
  $("toggleAudit").hidden = allAuditEvents.length <= 8;
  $("toggleAudit").textContent = auditExpanded ? "Show latest 8" : "Show all";
}

function formatBytes(size) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size/1024).toFixed(1)} KB`;
  return `${(size/(1024*1024)).toFixed(1)} MB`;
}
function startRefreshLoop() {
  if (window.sfxRefreshLoop) clearInterval(window.sfxRefreshLoop);
  window.sfxRefreshLoop = setInterval(async () => {
    if (!token) return;
    try {
      if (activePeer) await loadMessages();
      await loadContacts();
      await loadFiles();
      if (me && me.role === "admin") await loadStats();
    } catch (_) {}
  }, 4000);
}

function connectSocket() {
  if (socket) socket.close();
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${protocol}//${location.host}/ws`);
  socket.addEventListener("open", () => socket.send(JSON.stringify({type:"auth", token})));
  socket.addEventListener("message", async (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch (_) { return; }
    if (data.type === "presence") {
      $("onlineList").textContent = data.online.filter(n => n !== me.username).join(", ") || "No other employees online";
      await loadContacts().catch(() => {});
    } else if (data.type === "message") {
      if (activePeer === data.message.sender || activePeer === data.message.recipient) await loadMessages().catch(() => {});
    } else if (data.type === "call-offer") {
      pendingOffer = data;
      activePeer = data.from;
      $("contactSelect").value = activePeer;
      callType = data.callType || "audio";
      $("callStatus").textContent = `Incoming ${callType} call from ${data.from}.`;
      $("answerCallButton").hidden = false;
      $("rejectCallButton").hidden = false;
    } else if (data.type === "call-answer") {
      if (peerConnection && data.sdp) {
        await peerConnection.setRemoteDescription(new RTCSessionDescription(data.sdp));
        remoteDescriptionReady = true;
        for (const candidate of pendingCandidates) await peerConnection.addIceCandidate(candidate).catch(()=>{});
        pendingCandidates = [];
        $("callStatus").textContent = `Connected with ${data.from}.`;
      }
    } else if (data.type === "ice-candidate" && data.candidate && peerConnection) {
      const candidate = new RTCIceCandidate(data.candidate);
      if (remoteDescriptionReady) await peerConnection.addIceCandidate(candidate).catch(()=>{});
      else pendingCandidates.push(candidate);
    } else if (data.type === "call-end" || data.type === "call-reject") {
      $("callStatus").textContent = data.type === "call-reject" ? `${data.from} rejected the call.` : `Call ended by ${data.from}.`;
      stopCall(false);
    } else if (data.type === "peer-unavailable") {
      $("callStatus").textContent = `${data.to} is offline or unavailable.`;
      stopCall(false);
    } else if (data.type === "error") setStatus(data.message);
  });
  socket.addEventListener("close", () => {
    if (token) reconnectTimer = setTimeout(() => { if (token) connectSocket(); }, 2000);
  });
}
function socketSend(payload) {
  if (!socket || socket.readyState !== WebSocket.OPEN) throw new Error("Realtime connection is not ready. Try again in a moment.");
  socket.send(JSON.stringify(payload));
}
async function createPeerConnection(target) {
  peerConnection = new RTCPeerConnection({
    iceServers: [{urls:"stun:stun.l.google.com:19302"}]
  });
  remoteDescriptionReady = false;
  pendingCandidates = [];
  peerConnection.onicecandidate = (event) => {
    if (event.candidate && socket && socket.readyState === WebSocket.OPEN)
      socketSend({type:"ice-candidate", to:target, candidate:event.candidate});
  };
  peerConnection.ontrack = (event) => {
    const stream = event.streams[0];
    if (callType === "video") $("remoteVideo").srcObject = stream;
    else $("remoteAudio").srcObject = stream;
  };
  peerConnection.onconnectionstatechange = () => {
    if (peerConnection && ["failed","disconnected","closed"].includes(peerConnection.connectionState))
      $("callStatus").textContent = `Call connection: ${peerConnection.connectionState}.`;
  };
  localStream = await navigator.mediaDevices.getUserMedia({audio:true, video:callType === "video"});
  $("localVideo").srcObject = callType === "video" ? localStream : null;
  for (const track of localStream.getTracks()) peerConnection.addTrack(track, localStream);
}
async function startCall(type) {
  if (!activePeer) return setStatus("Choose an employee first.");
  if (activePeer === me.username) return setStatus("You cannot call your own account.");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return setStatus("Calls need HTTPS or localhost and browser microphone permission.");
  try {
    callType = type;
    $("callStatus").textContent = `Calling ${activePeer}…`;
    await createPeerConnection(activePeer);
    const offer = await peerConnection.createOffer();
    await peerConnection.setLocalDescription(offer);
    socketSend({type:"call-offer", to:activePeer, callType, sdp:peerConnection.localDescription});
    $("endCallButton").hidden = false;
  } catch (error) { $("callStatus").textContent = error.message; stopCall(false); }
}
$("audioCallButton").addEventListener("click", () => startCall("audio"));
$("videoCallButton").addEventListener("click", () => startCall("video"));
$("answerCallButton").addEventListener("click", async () => {
  if (!pendingOffer) return;
  try {
    callType = pendingOffer.callType || "audio";
    await createPeerConnection(pendingOffer.from);
    await peerConnection.setRemoteDescription(new RTCSessionDescription(pendingOffer.sdp));
    remoteDescriptionReady = true;
    const answer = await peerConnection.createAnswer();
    await peerConnection.setLocalDescription(answer);
    socketSend({type:"call-answer", to:pendingOffer.from, sdp:peerConnection.localDescription});
    $("callStatus").textContent = `Connecting to ${pendingOffer.from}…`;
    $("endCallButton").hidden = false;
  } catch (error) { $("callStatus").textContent = error.message; stopCall(false); }
  $("answerCallButton").hidden = true; $("rejectCallButton").hidden = true; pendingOffer = null;
});
$("rejectCallButton").addEventListener("click", () => {
  if (pendingOffer) socketSend({type:"call-reject", to:pendingOffer.from});
  pendingOffer = null; $("answerCallButton").hidden = true; $("rejectCallButton").hidden = true;
  $("callStatus").textContent = "Call rejected.";
});
$("endCallButton").addEventListener("click", () => {
  if (activePeer) { try { socketSend({type:"call-end", to:activePeer}); } catch (_) {} }
  stopCall();
  $("callStatus").textContent = "Call ended.";
});
function stopCall(closePeer = true) {
  if (closePeer && peerConnection) { try { peerConnection.close(); } catch (_) {} }
  peerConnection = null;
  if (localStream) localStream.getTracks().forEach(track => track.stop());
  localStream = null;
  $("localVideo").srcObject = null; $("remoteVideo").srcObject = null; $("remoteAudio").srcObject = null;
  $("answerCallButton").hidden = true; $("rejectCallButton").hidden = true; $("endCallButton").hidden = true;
  pendingOffer = null; remoteDescriptionReady = false; pendingCandidates = [];
}
