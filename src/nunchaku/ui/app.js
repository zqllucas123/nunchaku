const $ = (selector) => document.querySelector(selector);
const state = { services: [], selected: null, editing: null, exporting: null, busy: false, signature: null, gateway: null };
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
const path = (name) => `/api/services/${encodeURIComponent(name)}`;
let toastTimer;

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `show${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = ""; }, 4000);
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (response.status === 401) { location.replace("/login"); throw new Error("登录已过期，请重新登录"); }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `请求失败 (${response.status})`);
  }
  if (response.status === 204) return null;
  return response.json();
}

function statusLabel(service) {
  if (service.state === "running") return "运行中";
  if (service.state === "external") return "外部运行";
  if (service.state === "exited") return `已退出${service.exit_code == null ? "" : ` · ${service.exit_code}`}`;
  return "已停止";
}
// "external": the port is served by a process this dashboard didn't start (e.g. systemd).
const isUp = (x) => x.state === "running" || x.state === "external";

function render() {
  const services = state.services;
  $("#total-count").textContent = services.length;
  $("#running-count").textContent = services.filter(isUp).length;
  $("#engine-count").textContent = new Set(services.map((x) => x.engine)).size;
  if (!services.some((x) => x.name === state.selected)) state.selected = services[0]?.name || null;
  $("#service-list").innerHTML = services.length ? services.map((service) => `
    <button class="service-row ${state.selected === service.name ? "selected" : ""}" data-select="${escapeHtml(service.name)}">
      <span class="service-glyph">${escapeHtml(service.name.slice(0,1).toUpperCase())}</span>
      <span class="service-info"><span class="service-name">${escapeHtml(service.name)}</span><span class="service-description">${escapeHtml(service.description)}</span></span>
      <span class="row-state ${escapeHtml(service.state)}" title="${statusLabel(service)}"></span>
    </button>`).join("") : `<div class="empty-list"><div class="empty-symbol">◇</div>还没有服务<br>点击「新建服务」开始</div>`;
  renderDetail();
}

function renderDetail() {
  const service = state.services.find((x) => x.name === state.selected);
  if (!service) {
    $("#detail-panel").innerHTML = `<div class="detail-placeholder"><div><b>◇</b>选择一项服务后，这里会显示连接配置和运行状态。</div></div>`;
    return;
  }
  const running = service.state === "running";
  const external = service.state === "external";
  const startStop = external
    ? `<button class="button secondary" disabled title="由 systemd 等外部进程管理，请在服务器上启停">外部管理</button>`
    : `<button class="button ${running ? "secondary" : "primary"}" data-action="${running ? "stop" : "start"}">${running ? "■ 停止服务" : "▶ 启动服务"}</button>`;
  $("#detail-panel").innerHTML = `<div class="detail-body">
    <div class="detail-top"><div><div class="eyebrow">SERVICE DETAILS</div><div class="detail-title">${escapeHtml(service.name)}</div><p class="detail-desc">${escapeHtml(service.description)}</p></div><span class="state-badge ${escapeHtml(service.state)}"><i></i>${escapeHtml(statusLabel(service))}</span></div>
    <div class="detail-grid">
      <div class="detail-field"><span>执行引擎</span><strong title="${escapeHtml(service.engine === "custom" ? [service.command, ...(service.args || [])].join(" ") : "")}">${escapeHtml(service.engine === "custom" ? `自定义 · ${service.command}` : service.engine)}</strong></div>
      <div class="detail-field"><span>服务端口</span><strong>${escapeHtml(service.port)}</strong></div>
      <div class="detail-field"><span>私有 Skill 目录</span><strong title="${escapeHtml(service.skill)}">${escapeHtml(service.skill)}</strong></div>
      <div class="detail-field"><span>单次超时</span><strong>${escapeHtml(service.timeout_seconds)} 秒</strong></div>
    </div>
    <div class="endpoint-box"><code title="服务端本机地址">${escapeHtml(service.endpoint)}</code><button class="text-button" data-action="copy">复制地址</button></div>
    <div class="action-row">${startStop}<button class="button secondary" data-action="export">导出连接包</button><span class="spacer"></span><button class="button secondary" data-action="edit" ${running ? "disabled title='请先停止服务'" : ""}>编辑</button><button class="button danger" data-action="delete" ${running ? "disabled title='请先停止服务'" : ""}>删除</button></div>
    ${external ? `<p class="detail-desc external-note">该服务由外部进程（如 systemd）运行。修改配置后需在服务器上重启服务进程才会生效；下方日志读取自 systemd journal。</p>` : ""}
    <div class="logs-head"><span>运行日志 <small class="logs-source" id="logs-source"></small></span><button class="text-button" data-action="logs">刷新日志</button></div><pre class="logs" id="logs">加载中…</pre>
  </div>`;
  loadLogs(service.name);
}

const logSource = (result) => result.source === "journal" ? `来自 systemd journal · ${result.unit}` : result.source === "file" ? "来自管理台日志文件" : "";

async function loadLogs(name) {
  try {
    const result = await request(`${path(name)}/logs`);
    if (state.selected !== name || !$("#logs")) return;
    const node = $("#logs");
    // Keep the reader at the bottom only if they were already there.
    const atBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 24;
    node.textContent = result.logs;
    $("#logs-source").textContent = logSource(result);
    if (atBottom || node.dataset.loaded !== name) node.scrollTop = node.scrollHeight;
    node.dataset.loaded = name;
  } catch (error) { if (state.selected === name && $("#logs")) $("#logs").textContent = error.message; }
}

async function refresh(silent = false) {
  try {
    const [result, gateway] = await Promise.all([request("/api/services"), request("/api/gateway").catch((error) => ({error: error.message}))]);
    const signature = JSON.stringify([result.services, gateway]);
    if (signature === state.signature) return;
    state.signature = signature;
    state.services = result.services;
    state.gateway = gateway;
    render();
    renderGateway();
  } catch (error) { if (!silent) toast(error.message, true); }
}

function renderGateway() {
  const gateway = state.gateway;
  if (!gateway) return;
  if (gateway.error) {
    $("#gateway-panel").innerHTML = `<div class="gateway-body"><div><div class="eyebrow">GATEWAY</div><strong>统一网关</strong><p class="detail-desc">${escapeHtml(gateway.error)}</p></div></div>`;
    return;
  }
  const running = gateway.state === "running";
  const external = gateway.state === "external";
  const online = state.services.filter(isUp).length;
  const shown = gateway.public_endpoint || gateway.endpoint;
  const label = running ? "运行中" : external ? "外部运行" : gateway.state === "exited" ? `已退出 · ${escapeHtml(gateway.exit_code)}` : "已停止";
  const control = external
    ? `<button class="button secondary" disabled title="由 systemd 等外部进程管理">外部管理</button>`
    : `<button class="button ${running ? "secondary" : "primary"}" data-gateway="${running ? "stop" : "start"}">${running ? "■ 停止网关" : "▶ 启动网关"}</button>`;
  $("#gateway-panel").innerHTML = `<div class="gateway-body">
    <div class="gateway-info"><div class="eyebrow">GATEWAY · 客户唯一连接地址</div><strong>统一网关</strong>
      <p class="detail-desc">按工具名转发到各服务进程，只列出在线的服务（当前 ${online} 个）。${gateway.auth ? "需要 Service Key。" : "<b>未启用 Service Key 校验。</b>"}</p></div>
    <span class="state-badge ${running || external ? "running" : escapeHtml(gateway.state)}"><i></i>${label}</span>
    <div class="endpoint-box"><code title="${gateway.public_endpoint ? "员工使用的地址" : "服务端本机地址；跨机器时客户应使用其可访问的地址"}">${escapeHtml(shown)}</code><button class="text-button" data-gateway="copy">复制地址</button><button class="text-button" data-gateway="logs">查看日志</button></div>
    ${control}
  </div><pre class="logs" id="gateway-logs" hidden></pre>`;
}

async function performGateway(action) {
  if (!state.gateway || state.busy) return;
  if (action === "copy") {
    try { await navigator.clipboard.writeText(state.gateway.public_endpoint || state.gateway.endpoint); toast("网关地址已复制"); }
    catch { toast("复制失败，请手动选择地址", true); }
    return;
  }
  if (action === "logs") {
    const node = $("#gateway-logs");
    try {
      const result = await request("/api/gateway/logs");
      node.textContent = result.logs;
      node.title = logSource(result);
      node.hidden = false;
      node.scrollTop = node.scrollHeight;
    } catch (error) { toast(error.message, true); }
    return;
  }
  state.busy = true;
  try {
    await request(`/api/gateway/${action}`, {method:"POST",headers:{"Content-Type":"application/json"}});
    await refresh();
    toast(action === "start" ? "网关已启动" : "网关已停止");
  } catch (error) { toast(error.message, true); await refresh(true); }
  finally { state.busy = false; }
}

function syncEngineFields() {
  const form = $("#service-form");
  const custom = form.elements.namedItem("engine").value === "custom";
  $("#args-field").hidden = !custom;
  form.elements.namedItem("args").required = custom;
  form.elements.namedItem("command").required = custom;
  form.elements.namedItem("command").placeholder = custom ? "例如：/usr/local/bin/my-agent" : "留空使用引擎默认命令";
  $("#command-hint").textContent = custom ? "必填，自定义 Agent 引擎的 CLI 可执行文件路径或名称" : "可选，填写 CLI 可执行文件的路径或名称";
}

function openService(service = null) {
  state.editing = service?.name || null;
  $("#form-title").textContent = service ? "编辑服务" : "新建服务";
  const form = $("#service-form");
  form.reset();
  const values = service || {host:"127.0.0.1",port:8765,engine:"codex",timeout_seconds:180};
  for (const [key, value] of Object.entries(values)) {
    if (form.elements.namedItem(key) && value != null) form.elements.namedItem(key).value = Array.isArray(value) ? value.join("\n") : value;
  }
  syncEngineFields();
  $("#service-dialog").showModal();
  form.elements.namedItem("name").focus();
}

async function saveService(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity() || state.busy) return;
  const data = Object.fromEntries(new FormData(form).entries());
  data.port = Number(data.port);
  data.timeout_seconds = Number(data.timeout_seconds);
  if (!data.command.trim()) delete data.command;
  if (data.engine === "custom") data.args = data.args.split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
  else delete data.args;
  state.busy = true;
  try {
    const isEdit = Boolean(state.editing);
    const saved = await request(isEdit ? path(state.editing) : "/api/services", {
      method: isEdit ? "PUT" : "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(data)
    });
    state.selected = saved.name;
    $("#service-dialog").close();
    await refresh();
    toast(isEdit ? "服务配置已更新" : "服务已创建，可以启动了");
  } catch (error) { toast(error.message, true); }
  finally { state.busy = false; }
}

async function perform(action) {
  const service = state.services.find((x) => x.name === state.selected);
  if (!service || state.busy) return;
  if (action === "copy") {
    try { await navigator.clipboard.writeText(service.endpoint); toast("MCP 地址已复制"); }
    catch { toast("复制失败，请手动选择地址", true); }
    return;
  }
  if (action === "logs") { await loadLogs(service.name); return; }
  if (action === "edit") { openService(service); return; }
  if (action === "export") {
    state.exporting = service.name;
    $("#export-form").reset();
    $("#export-form").elements.namedItem("url").value = service.endpoint.replace("0.0.0.0", "127.0.0.1");
    $("#export-dialog").showModal();
    return;
  }
  if (action === "delete" && !window.confirm(`删除服务「${service.name}」的配置？`)) return;
  state.busy = true;
  try {
    if (action === "delete") {
      await request(path(service.name), {method:"DELETE",headers:{"Content-Type":"application/json"}});
      state.selected = null;
    } else {
      await request(`${path(service.name)}/${action}`, {method:"POST",headers:{"Content-Type":"application/json"}});
    }
    await refresh();
    toast({start:"服务已启动",stop:"服务已停止",delete:"服务已删除"}[action]);
  } catch (error) { toast(error.message, true); await refresh(true); }
  finally { state.busy = false; }
}

async function exportConnector(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity() || state.busy) return;
  state.busy = true;
  try {
    const params = new URLSearchParams(new FormData(form));
    const response = await fetch(`${path(state.exporting)}/connector?${params}`);
    if (response.status === 401) { location.replace("/login"); return; }
    if (!response.ok) { const result = await response.json(); throw new Error(result.error || "导出失败"); }
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${state.exporting}-remote.zip`;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    $("#export-dialog").close();
    toast("公开连接包已下载");
  } catch (error) { toast(error.message, true); }
  finally { state.busy = false; }
}

$("#add-service").addEventListener("click", () => openService());
$("#refresh").addEventListener("click", () => refresh());
$("#service-form").addEventListener("submit", saveService);
$("#service-form").elements.namedItem("engine").addEventListener("change", syncEngineFields);
$("#export-form").addEventListener("submit", exportConnector);
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $(`#${button.dataset.close}`).close()));
$("#service-list").addEventListener("click", (event) => { const row = event.target.closest("[data-select]"); if (row) { state.selected = row.dataset.select; render(); } });
$("#detail-panel").addEventListener("click", (event) => { const button = event.target.closest("[data-action]"); if (button) perform(button.dataset.action); });
$("#gateway-panel").addEventListener("click", (event) => { const button = event.target.closest("[data-gateway]"); if (button) performGateway(button.dataset.gateway); });

// --- views, account, service keys -----------------------------------------

const keys = { list: [], editing: null };
const json = (method, body) => ({method, headers: {"Content-Type": "application/json"}, body: body === undefined ? undefined : JSON.stringify(body)});
const formatTime = (seconds) => seconds ? new Date(seconds * 1000).toLocaleString("zh-CN", {hour12: false}) : "—";
const keyStatus = {active: "有效", disabled: "已停用", expired: "已过期"};

function showView(view) {
  document.querySelectorAll("[data-view]").forEach((link) => link.classList.toggle("active", link.dataset.view === view));
  $("#services").hidden = view !== "services";
  $("#keys").hidden = view !== "keys";
  if (view === "keys") loadKeys();
}

async function loadSession() {
  try {
    const session = await request("/api/session");
    if (!session.admin) { location.replace("/login"); return; }
    $("#admin-name").textContent = session.admin;
  } catch { /* request() already redirected */ }
}

async function loadKeys() {
  try { keys.list = (await request("/api/keys")).keys; renderKeys(); }
  catch (error) { toast(error.message, true); }
}

function grantLabel(key) {
  return key.all_services ? "全部服务" : key.services.map(escapeHtml).join("、");
}

function renderKeys() {
  $("#key-list").innerHTML = keys.list.length ? `<table class="key-table"><thead><tr>
    <th>使用者</th><th>Key</th><th>可访问服务</th><th>状态</th><th>最后使用</th><th>调用次数</th><th>过期时间</th><th></th></tr></thead><tbody>
    ${keys.list.map((key) => `<tr>
      <td><strong>${escapeHtml(key.owner)}</strong><small>${escapeHtml([key.department, key.email].filter(Boolean).join(" · "))}</small>${key.note ? `<small>${escapeHtml(key.note)}</small>` : ""}</td>
      <td><code>${escapeHtml(key.prefix)}…</code></td>
      <td>${grantLabel(key)}</td>
      <td><span class="state-badge ${key.status === "active" ? "running" : "exited"}"><i></i>${keyStatus[key.status]}</span></td>
      <td>${formatTime(key.last_used_at)}</td>
      <td>${key.use_count}</td>
      <td>${key.expires_at ? formatTime(key.expires_at).split(" ")[0] : "永不"}</td>
      <td class="key-actions"><button class="text-button" data-key-edit="${key.id}">编辑</button><button class="text-button" data-key-toggle="${key.id}">${key.enabled ? "停用" : "启用"}</button><button class="text-button danger-text" data-key-delete="${key.id}">删除</button></td>
    </tr>`).join("")}</tbody></table>` : `<div class="empty-list"><div class="empty-symbol">⚿</div>还没有 Service Key<br>点击「生成 Service Key」为使用者创建</div>`;
}

function openKey(key = null) {
  keys.editing = key?.id || null;
  const form = $("#key-form");
  form.reset();
  $("#key-form-title").textContent = key ? "编辑 Service Key" : "生成 Service Key";
  $("#key-submit").textContent = key ? "保存" : "生成";
  const granted = new Set(key?.services || []);
  const names = [...new Set([...state.services.map((s) => s.name), ...granted])];
  $("#grant-services").innerHTML = names.length ? names.map((name) => {
    const service = state.services.find((s) => s.name === name);
    return `<label class="check"><input type="checkbox" name="service" value="${escapeHtml(name)}" ${granted.has(name) ? "checked" : ""}> <span><strong>${escapeHtml(name)}</strong>${service ? ` <small>${escapeHtml(service.description)}</small>` : " <small>（服务已不存在）</small>"}</span></label>`;
  }).join("") : `<p class="muted">还没有已注册的服务，可先勾选「全部服务」。</p>`;
  if (key) {
    for (const field of ["owner", "department", "email", "note"]) form.elements.namedItem(field).value = key[field] || "";
    form.elements.namedItem("all_services").checked = key.all_services;
    if (key.expires_at) form.elements.namedItem("expires_at").value = new Date(key.expires_at * 1000).toISOString().slice(0, 10);
  }
  syncGrant();
  $("#key-dialog").showModal();
  form.elements.namedItem("owner").focus();
}

function syncGrant() {
  const all = $("#key-form").elements.namedItem("all_services").checked;
  $("#grant-services").querySelectorAll("input").forEach((input) => { input.disabled = all; });
}

async function saveKey(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity() || state.busy) return;
  const services = [...form.querySelectorAll("input[name=service]:checked")].map((input) => input.value);
  const allServices = form.elements.namedItem("all_services").checked;
  if (!allServices && !services.length) { toast("请至少选择一个服务，或勾选「全部服务」", true); return; }
  const date = form.elements.namedItem("expires_at").value;
  // End of the chosen day, local time.
  const expires = date ? Math.floor(new Date(`${date}T23:59:59`).getTime() / 1000) : null;
  const payload = {
    owner: form.elements.namedItem("owner").value, department: form.elements.namedItem("department").value,
    email: form.elements.namedItem("email").value, note: form.elements.namedItem("note").value,
    all_services: allServices, services: allServices ? [] : services, expires_at: expires,
  };
  state.busy = true;
  try {
    if (keys.editing) {
      await request(`/api/keys/${keys.editing}`, json("PUT", payload));
      $("#key-dialog").close();
      toast("Service Key 已更新，立即生效");
    } else {
      const created = await request("/api/keys", json("POST", payload));
      $("#key-dialog").close();
      showSecret(created.secret);
    }
    await loadKeys();
  } catch (error) { toast(error.message, true); }
  finally { state.busy = false; }
}

function showSecret(secret) {
  $("#secret-value").textContent = secret;
  const url = state.gateway?.public_endpoint || "https://<网关地址>/mcp";
  $("#secret-config").textContent = JSON.stringify({
    command: "C:\\Users\\<用户名>\\.local\\bin\\nunchaku.exe",
    args: ["bridge", "--url", url, "--root", "D:\\<文档目录>"],
    env: {NUNCHAKU_SERVICE_KEY: secret},
  }, null, 2);
  $("#secret-dialog").showModal();
}

async function keyAction(event) {
  const button = event.target.closest("button");
  if (!button || state.busy) return;
  const id = button.dataset.keyEdit || button.dataset.keyToggle || button.dataset.keyDelete;
  const key = keys.list.find((k) => k.id === id);
  if (!key) return;
  if (button.dataset.keyEdit) { openKey(key); return; }
  if (button.dataset.keyDelete && !window.confirm(`删除「${key.owner}」的 Service Key？删除后立即失效，且无法恢复。`)) return;
  state.busy = true;
  try {
    if (button.dataset.keyDelete) {
      await request(`/api/keys/${id}`, json("DELETE"));
      toast("Service Key 已删除");
    } else {
      await request(`/api/keys/${id}`, json("PUT", {enabled: !key.enabled}));
      toast(key.enabled ? "已停用，立即生效" : "已启用");
    }
    await loadKeys();
  } catch (error) { toast(error.message, true); }
  finally { state.busy = false; }
}

async function changePassword(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const values = Object.fromEntries(new FormData(form).entries());
  if (values.new !== values.repeat) { toast("两次输入的新密码不一致", true); return; }
  try {
    await request("/api/session/password", json("POST", {current: values.current, new: values.new}));
    $("#password-dialog").close();
    toast("密码已修改，其他设备上的登录已失效");
  } catch (error) { toast(error.message === "current password is incorrect" ? "当前密码错误" : error.message, true); }
}

document.querySelectorAll("[data-view]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); history.replaceState(null, "", `#${link.dataset.view}`); showView(link.dataset.view); }));
$("#add-key").addEventListener("click", () => openKey());
$("#refresh-keys").addEventListener("click", loadKeys);
$("#key-form").addEventListener("submit", saveKey);
$("#key-form").elements.namedItem("all_services").addEventListener("change", syncGrant);
$("#key-list").addEventListener("click", keyAction);
$("#copy-secret").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#secret-value").textContent); toast("已复制"); }
  catch { toast("复制失败，请手动选择", true); }
});
$("#change-password").addEventListener("click", () => { $("#password-form").reset(); $("#password-dialog").showModal(); });
$("#password-form").addEventListener("submit", changePassword);
$("#logout").addEventListener("click", async () => { try { await request("/api/session/logout", json("POST", {})); } finally { location.replace("/login"); } });

loadSession();
showView(location.hash === "#keys" ? "keys" : "services");
refresh();
setInterval(() => { if (!document.hidden && !state.busy && !$("#service-dialog").open && !$("#export-dialog").open) refresh(true); }, 5000);
