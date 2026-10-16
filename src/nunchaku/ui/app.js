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
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `请求失败 (${response.status})`);
  }
  if (response.status === 204) return null;
  return response.json();
}

function statusLabel(service) {
  if (service.state === "running") return "运行中";
  if (service.state === "exited") return `已退出${service.exit_code == null ? "" : ` · ${service.exit_code}`}`;
  return "已停止";
}

function render() {
  const services = state.services;
  $("#total-count").textContent = services.length;
  $("#running-count").textContent = services.filter((x) => x.state === "running").length;
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
  $("#detail-panel").innerHTML = `<div class="detail-body">
    <div class="detail-top"><div><div class="eyebrow">SERVICE DETAILS</div><div class="detail-title">${escapeHtml(service.name)}</div><p class="detail-desc">${escapeHtml(service.description)}</p></div><span class="state-badge ${escapeHtml(service.state)}"><i></i>${escapeHtml(statusLabel(service))}</span></div>
    <div class="detail-grid">
      <div class="detail-field"><span>执行引擎</span><strong title="${escapeHtml(service.engine === "custom" ? [service.command, ...(service.args || [])].join(" ") : "")}">${escapeHtml(service.engine === "custom" ? `自定义 · ${service.command}` : service.engine)}</strong></div>
      <div class="detail-field"><span>服务端口</span><strong>${escapeHtml(service.port)}</strong></div>
      <div class="detail-field"><span>私有 Skill 目录</span><strong title="${escapeHtml(service.skill)}">${escapeHtml(service.skill)}</strong></div>
      <div class="detail-field"><span>单次超时</span><strong>${escapeHtml(service.timeout_seconds)} 秒</strong></div>
    </div>
    <div class="endpoint-box"><code title="服务端本机地址">${escapeHtml(service.endpoint)}</code><button class="text-button" data-action="copy">复制地址</button></div>
    <div class="action-row"><button class="button ${running ? "secondary" : "primary"}" data-action="${running ? "stop" : "start"}">${running ? "■ 停止服务" : "▶ 启动服务"}</button><button class="button secondary" data-action="export">导出连接包</button><span class="spacer"></span><button class="button secondary" data-action="edit" ${running ? "disabled title='请先停止服务'" : ""}>编辑</button><button class="button danger" data-action="delete" ${running ? "disabled title='请先停止服务'" : ""}>删除</button></div>
    <div class="logs-head"><span>运行日志</span><button class="text-button" data-action="logs">刷新日志</button></div><pre class="logs" id="logs">加载中…</pre>
  </div>`;
  loadLogs(service.name);
}

async function loadLogs(name) {
  try {
    const result = await request(`${path(name)}/logs`);
    if (state.selected === name && $("#logs")) $("#logs").textContent = result.logs;
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
  const online = state.services.filter((x) => x.state === "running").length;
  $("#gateway-panel").innerHTML = `<div class="gateway-body">
    <div class="gateway-info"><div class="eyebrow">GATEWAY · 客户唯一连接地址</div><strong>统一网关</strong>
      <p class="detail-desc">按工具名把请求转发到各服务进程。只会列出正在运行的服务（当前 ${online} 个）。</p></div>
    <span class="state-badge ${escapeHtml(gateway.state)}"><i></i>${running ? "运行中" : gateway.state === "exited" ? `已退出 · ${escapeHtml(gateway.exit_code)}` : "已停止"}</span>
    <div class="endpoint-box"><code title="服务端本机地址；跨机器时客户应使用其可访问的地址">${escapeHtml(gateway.endpoint)}</code><button class="text-button" data-gateway="copy">复制地址</button><button class="text-button" data-gateway="logs">查看日志</button></div>
    <button class="button ${running ? "secondary" : "primary"}" data-gateway="${running ? "stop" : "start"}">${running ? "■ 停止网关" : "▶ 启动网关"}</button>
  </div><pre class="logs" id="gateway-logs" hidden></pre>`;
}

async function performGateway(action) {
  if (!state.gateway || state.busy) return;
  if (action === "copy") {
    try { await navigator.clipboard.writeText(state.gateway.endpoint); toast("网关地址已复制"); }
    catch { toast("复制失败，请手动选择地址", true); }
    return;
  }
  if (action === "logs") {
    const node = $("#gateway-logs");
    try { node.textContent = (await request("/api/gateway/logs")).logs; node.hidden = false; }
    catch (error) { toast(error.message, true); }
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
refresh();
setInterval(() => { if (!document.hidden && !state.busy && !$("#service-dialog").open && !$("#export-dialog").open) refresh(true); }, 5000);
