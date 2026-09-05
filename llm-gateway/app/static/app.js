const $ = (id) => document.getElementById(id);
let models = [], activeSubscription = null, refreshTimer = null;
let conversations = [], activeConversationId = null;
let typewriterSettings = {intervalMs:35, charsPerTick:1};
let deepseekKeyConfigured = null;

async function api(url, options = {}) {
  const response = await fetch(url, {headers: {"content-type": "application/json", ...(options.headers || {})}, ...options});
  const body = response.status === 204 ? null : await response.json();
  if (!response.ok) throw new Error(body?.error?.message || `HTTP ${response.status}`);
  return body;
}
function pretty(value) { return JSON.stringify(value, null, 2); }
function parseJson(id, fallback = {}) { const value = $(id).value.trim(); return value ? JSON.parse(value) : fallback; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c])); }
function setStatus(id, message, error = false) { $(id).textContent = message; $(id).style.color = error ? "#f85149" : ""; }

// 每一次 SSE 订阅都拥有自己的控制器。旧订阅结束时只能清理它自己，
// 不能把用户刚建立的重新订阅一并置空。
function startSubscription(conversation) {
  const subscription = {controller: new AbortController(), conversation};
  activeSubscription = subscription;
  return subscription;
}
function finishSubscription(subscription) {
  if (activeSubscription === subscription) activeSubscription = null;
}
function isSubscribed(conversation) { return activeSubscription?.conversation === conversation; }

function newConversation() {
  const conversation = {id:crypto.randomUUID(), title:"新对话", status:"idle", requestId:"", lastEventId:0, followOutput:true, messages:[]};
  conversations.unshift(conversation); activeConversationId = conversation.id; renderConversations(); renderChat(); return conversation;
}
function activeConversation() { return conversations.find(c => c.id === activeConversationId) || newConversation(); }
function renderConversations() {
  $("conversation-list").innerHTML = conversations.map(c => `<button class="conversation ${c.id === activeConversationId ? "active" : ""}" data-conversation="${c.id}"><strong>${escapeHtml(c.title)}</strong><small>${escapeHtml(c.status)}${c.requestId ? ` · ${escapeHtml(c.requestId.slice(0,12))}` : ""}</small></button>`).join("");
  document.querySelectorAll("[data-conversation]").forEach(button => button.onclick = () => { activeConversationId = button.dataset.conversation; renderConversations(); renderChat(); });
}
function renderChat() {
  const conversation = activeConversation(); $("conversation-title").textContent = conversation.title;
  const subscribed = isSubscribed(conversation);
  const localPlayback = conversation.status === "streaming" && !subscribed && conversation.messages.some(message => message.typing?.queue || message.typing?.timer);
  $("connection-status").textContent = localPlayback ? "模型输出已接收完，正在本地逐字展示" : ({idle:"空闲：尚未发送请求",streaming:"连接中：正在接收 SSE 文本",completed:"已完成：终态指标已收到",disconnected:"订阅已断开：已接收文字已完整显示；后台 Run 仍可能继续生成",cancelled:"已取消：上游生成已停止",failed:"失败：请查看终态错误"}[conversation.status] || conversation.status);
  $("connection-badge").textContent = conversation.status; $("history-request-id").value = conversation.requestId || "";
  const chatLog = $("chat-log");
  chatLog.innerHTML = conversation.messages.length ? conversation.messages.map(message => message.type === "event" ? `<div class="connection-event">${escapeHtml(message.content)}</div>` : `<div class="bubble ${message.role}">${escapeHtml(message.content || "正在生成…")}<div class="meta">${escapeHtml(message.meta || "")}</div></div>`).join("") : `<div class="empty-chat">选择模型并输入问题，开始一次独立的调试对话。</div>`;
  // 只有用户仍停留在底部时，增量输出才自动跟随；向上滚动阅读历史不会被打字机拉回。
  if (conversation.followOutput !== false) chatLog.scrollTop = chatLog.scrollHeight;
  $("abort-request").disabled = !subscribed;
  $("reconnect-request").disabled = !conversation.requestId || conversation.status !== "disconnected";
  $("cancel-request").disabled = !conversation.requestId || ["completed", "failed", "cancelled"].includes(conversation.status);
}
function appendChat(role, content, meta = "") { const conversation = activeConversation(); conversation.messages.push({role, content, meta}); renderChat(); return conversation.messages.at(-1); }
function appendEvent(content) { activeConversation().messages.push({type:"event", content}); renderChat(); }

function capabilityTag(label, enabled = true) {
  return `<span class="capability-tag ${enabled ? "" : "off"}">${escapeHtml(label)}</span>`;
}
function structuredOutputLabel(capability) {
  return {native_schema:"原生 Schema", json_mode:"JSON 模式", prompt_only:"仅提示词约束"}[capability] || "不支持";
}
function formatMetrics(metrics = {}) {
  const value = field => metrics[field] ?? "—";
  return `耗时 ${value("latency_ms")} ms · 首 Token ${value("first_token_latency_ms")} ms · Token ${value("input_tokens")}/${value("output_tokens")}/${value("total_tokens")} · 重试 ${value("retry_count")}`;
}

function enqueueTypewriter(message, text) {
  if (!text) return;
  message.typing ||= {queue:"", timer:null, terminal:null};
  message.typing.queue += text;
  if (message.typing.timer) return;
  const tick = () => {
    const state = message.typing;
    if (!state.queue) {
      state.timer = null;
      if (state.terminal) { const terminal = state.terminal; state.terminal = null; terminal(); }
      return;
    }
    message.content += state.queue.slice(0, typewriterSettings.charsPerTick);
    state.queue = state.queue.slice(typewriterSettings.charsPerTick);
    renderChat();
    state.timer = setTimeout(tick, typewriterSettings.intervalMs);
  };
  message.typing.timer = setTimeout(tick, typewriterSettings.intervalMs);
}

function afterTypewriter(message, callback) {
  message.typing ||= {queue:"", timer:null, terminal:null};
  if (message.typing.queue || message.typing.timer) message.typing.terminal = callback;
  else callback();
}

// 断开时立即展示浏览器已经收到、但仍在打字机队列中的文字。
// 断开后的新 Token 不会进入这个队列，必须重新订阅才能接收。
function flushTypewriter(message) {
  const state = message.typing;
  if (!state) return;
  if (state.timer) clearTimeout(state.timer);
  message.content += state.queue;
  state.queue = "";
  state.timer = null;
  state.terminal = null;
}

function populateModels() {
  const options = models.map(m => `<option value="${escapeHtml(m.model)}">${escapeHtml(m.model)} — ${escapeHtml(m.protocol)}</option>`).join("");
  $("generate-model").innerHTML = options;
  $("usage-model").innerHTML = `<option value="">全部</option>${options}`;
  $("model-cards").innerHTML = models.map(m => `<article class="card model-card"><h2>${escapeHtml(m.model)}</h2><p class="model-protocol">${escapeHtml(m.protocol)}</p><p class="muted">支持能力</p><div class="capability-tags">${m.capabilities.streaming ? capabilityTag("流式输出") : ""}${m.capabilities.chat_completions ? capabilityTag("多轮对话") : ""}${m.capabilities.structured_output !== "unsupported" ? capabilityTag(`结构化输出：${structuredOutputLabel(m.capabilities.structured_output)}`) : ""}${m.capabilities.supports_temperature ? capabilityTag("温度参数") : ""}${m.capabilities.supports_top_p ? capabilityTag("Top P 参数") : ""}</div></article>`).join("");
}

async function loadGateway() {
  const [config, registered] = await Promise.all([api("/admin/ui/config"), api("/admin/ui/models")]);
  models = registered; populateModels();
  $("health-badge").textContent = "Gateway 正常"; $("health-badge").className = "badge ok";
  $("config-status").innerHTML = `<dt>健康状态</dt><dd>${escapeHtml(config.health)}</dd><dt>数据库</dt><dd>${escapeHtml(config.database_path)}</dd><dt>DeepSeek Key</dt><dd>${config.deepseek_key.configured ? "已配置" : "未配置"}（${escapeHtml(config.deepseek_key.source)}）</dd>`;
  // 首次加载绝不请求 Key 明文；只有用户点击“从本地环境读取”才会填入。
  $("runtime-key").value = "";
  deepseekKeyConfigured = config.deepseek_key.configured;
  typewriterSettings = {
    intervalMs: config.typewriter?.interval_ms || 35,
    charsPerTick: config.typewriter?.chars_per_tick || 1,
  };
  setStatus("key-status", config.deepseek_key.configured ? "环境变量已配置；点击“从本地环境读取”后才会显示明文。" : "未读取到 DEEPSEEK_API_KEY。", !config.deepseek_key.configured);
}

async function loadPrompts() {
  const prompts = await api("/v1/prompts");
  const ids = [...new Set(prompts.map(p => p.id))];
  const opts = ids.map(id => `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join("");
  $("prompt-id").innerHTML = `<option value="">不使用</option>${opts}`;
  $("preview-prompt-id").innerHTML = opts;
  $("prompt-list").innerHTML = `<table><thead><tr><th>ID</th><th>版本</th><th>Role</th><th>Active</th><th>创建时间</th><th>操作</th></tr></thead><tbody>${prompts.map(p => `<tr><td>${escapeHtml(p.id)}</td><td>${p.version}</td><td>${p.role}</td><td>${p.active}</td><td>${escapeHtml(p.created_at)}</td><td><button data-view="${escapeHtml(p.id)}" data-version="${p.version}">查看</button> <button data-activate="${escapeHtml(p.id)}" data-version="${p.version}">设为 active</button></td></tr>`).join("")}</tbody></table>`;
  document.querySelectorAll("[data-activate]").forEach(button => button.onclick = async () => { try { await api(`/v1/prompts/${encodeURIComponent(button.dataset.activate)}/activate`, {method:"POST", body: pretty({version:Number(button.dataset.version)})}); await loadPrompts(); } catch (error) { setStatus("prompt-status", error.message, true); } });
  document.querySelectorAll("[data-view]").forEach(button => button.onclick = async () => { try { const prompt=await api(`/v1/prompts/${encodeURIComponent(button.dataset.view)}?version=${button.dataset.version}`); $("prompt-create-id").value=prompt.id; $("prompt-role").value=prompt.role; $("prompt-template").value=prompt.template; setStatus("prompt-status",`已加载 ${prompt.id} v${prompt.version}；保存会创建新版本。`); } catch(error) { setStatus("prompt-status",error.message,true); } });
}

function addMessage(role = "user", content = "") {
  const row = document.createElement("div"); row.className = "grid three";
  row.innerHTML = `<label>Role <select class="message-role"><option value="user">user</option><option value="assistant">assistant</option><option value="developer">developer</option></select></label><label>Content <input class="message-content" value="${escapeHtml(content)}"></label><button type="button" class="remove-message">删除</button>`;
  row.querySelector(".message-role").value = role; row.querySelector(".remove-message").onclick = () => row.remove(); $("message-list").append(row);
}

function currentConversationHistory(conversation) {
  // 只携带已经形成的正常对话。失败/中断的 assistant 半截文本不能作为模型事实继续注入。
  return conversation.messages
    .filter(message => ["user", "assistant", "developer"].includes(message.role))
    .filter(message => typeof message.content === "string" && message.content.trim())
    .filter(message => !String(message.meta || "").startsWith("失败："))
    .map(message => ({role: message.role, content: message.content}));
}

function buildMultiTurnMessages(conversation, configuredHistory, userInput) {
  return [
    ...configuredHistory.filter(message => message.content.trim()),
    ...currentConversationHistory(conversation),
    {role: "user", content: userInput},
  ];
}

function requestPayload() {
  const payload = {model: $("generate-model").value, stream: $("stream-request").checked};
  const userInput = $("user-input").value.trim();
  if (!userInput) throw new Error("请输入需要发送的用户消息。");
  if ($("input-mode").value === "simple") { payload.system = $("system-input").value || null; payload.user = userInput; }
  else {
    const configuredHistory = [...document.querySelectorAll("#message-list > div")].map(row => ({role:row.querySelector(".message-role").value, content:row.querySelector(".message-content").value}));
    // 手动 messages 是额外上下文；当前聊天窗口的成功历史也会自动透传。
    payload.messages = buildMultiTurnMessages(activeConversation(), configuredHistory, userInput);
  }
  for (const [element, field] of [["max-tokens","max_output_tokens"],["temperature","temperature"],["top-p","top_p"]]) if ($(element).value !== "") payload[field] = Number($(element).value);
  const format = $("response-format").value;
  if (format !== "text") { payload.response_format = {type: format}; if (format === "json_schema") payload.response_format = {type:format, name:"ui_schema", json_schema:parseJson("schema-json")}; }
  if ($("prompt-id").value) payload.prompt = {id:$("prompt-id").value, version:$("prompt-version").value ? Number($("prompt-version").value) : null, variables:parseJson("prompt-variables"), position:$("prompt-position").value};
  return payload;
}

async function consumeSse(payload, assistantMessage) {
  const conversation = activeConversation(); conversation.status = "streaming"; renderConversations(); renderChat();
  const subscription = startSubscription(conversation); renderChat();
  try {
    const response = await fetch("/v1/generate", {method:"POST", headers:{"content-type":"application/json"}, body:JSON.stringify(payload), signal:subscription.controller.signal});
    await readSseResponse(response, assistantMessage, conversation);
  } catch (error) { if (error.name === "AbortError") { flushTypewriter(assistantMessage); conversation.status="disconnected"; $("history-output").textContent="订阅已断开：已接收文字已立即显示。后台模型仍可能生成，可在宽限时间内重新订阅。"; } else { conversation.status="failed"; assistantMessage.content = error.message; } renderConversations(); renderChat(); }
  finally { finishSubscription(subscription); renderChat(); }
}

async function readSseResponse(response, assistantMessage, conversation) {
  const responseRequestId = response.headers.get("X-Request-ID");
  if (responseRequestId) conversation.requestId = responseRequestId;
  if (!response.ok) { const errorBody = await response.json(); assistantMessage.content = errorBody?.error?.message || "请求失败"; assistantMessage.meta = "请求失败"; conversation.status="failed"; renderConversations(); renderChat(); return; }
  const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "";
  while (true) { const {done, value} = await reader.read(); if (done) break; buffer += decoder.decode(value, {stream:true}); const frames = buffer.split("\n\n"); buffer = frames.pop(); for (const frame of frames) renderSseFrame(frame, assistantMessage, conversation); }
}
function renderSseFrame(frame, assistantMessage, conversation) {
  if (frame.startsWith(": ping")) return;
  const eventId = Number((frame.match(/^id: (\d+)$/m) || [])[1]);
  if (Number.isFinite(eventId)) conversation.lastEventId = Math.max(conversation.lastEventId || 0, eventId);
  const event = (frame.match(/^event: (.+)$/m) || [])[1] || "message";
  const dataLine = (frame.match(/^data: (.+)$/m) || [])[1]; let data; try { data = JSON.parse(dataLine); } catch { data = dataLine; }
  const requestId = data?.gateway_request_id || data?.gateway_metrics?.request_id;
  if (requestId) conversation.requestId = requestId;
  if (event === "response.output_text.delta") { enqueueTypewriter(assistantMessage, data.delta || ""); }
  else if (event === "response.completed") { afterTypewriter(assistantMessage, () => { conversation.status="completed"; assistantMessage.meta = formatMetrics(data.gateway_metrics); renderConversations(); renderChat(); }); }
  else if (event === "response.failed") { afterTypewriter(assistantMessage, () => { conversation.status="failed"; assistantMessage.meta = `失败：${data?.error?.message || "上游流请求失败"}`; renderConversations(); renderChat(); }); }
  // response.created、心跳及当前尚未统一展示的上游事件只更新内部状态，不向对话区倾倒原始 JSON。
  renderConversations(); renderChat();
}

async function sendRequest() {
  try {
    const conversation = activeConversation();
    if (deepseekKeyConfigured === false) {
      conversation.status = "failed";
      appendChat("assistant", "你没有配置 API key，请在第一个页面配置后进行对话。", "未配置 API key");
      renderConversations(); renderChat();
      return;
    }
    const payload = requestPayload();
    $("user-input").value=""; conversation.followOutput=true; conversation.requestId=""; conversation.lastEventId=0;
    const userText = payload.user || payload.messages?.at(-1)?.content || "";
    conversation.title = userText.slice(0, 22) || "新对话";
    appendChat("user", userText);
    const assistantMessage = appendChat("assistant", "");
    if (payload.stream) await consumeSse(payload, assistantMessage);
    else {
      conversation.status="streaming"; renderConversations(); renderChat();
      const result = await api("/v1/generate", {method:"POST", body:pretty(payload)});
      assistantMessage.content = result.output?.flatMap(item => item.content || []).filter(item => item.type === "output_text").map(item => item.text).join("") || "模型未返回文本内容。";
      assistantMessage.meta = formatMetrics(result.metrics); conversation.requestId=result.metrics?.request_id || "";
      conversation.status="completed"; renderConversations(); renderChat();
    }
  }
  catch (error) { const conversation=activeConversation(); conversation.status="failed"; appendChat("assistant", error.message, "请求失败"); renderConversations(); renderChat(); }
}

function summary(records) { const numeric = field => records.reduce((sum, r) => sum + (r[field] ?? 0), 0); const average = field => { const values = records.map(r => r[field]).filter(v => v !== null); return values.length ? (values.reduce((a,b)=>a+b,0)/values.length).toFixed(2) : "—"; }; return [{label:"请求",value:records.length},{label:"输入 Token",value:numeric("input_tokens")},{label:"输出 Token",value:numeric("output_tokens")},{label:"总 Token",value:numeric("total_tokens")},{label:"完成 / 失败 / 取消",value:`${records.filter(r=>r.status==="completed").length} / ${records.filter(r=>r.status==="failed").length} / ${records.filter(r=>r.status==="cancelled").length}`},{label:"平均延迟 / TTFT",value:`${average("latency_ms")} / ${average("first_token_latency_ms")} ms`}]; }
async function loadUsage() { try { const params = new URLSearchParams({limit:$("usage-limit").value || "100"}); for (const [id,key] of [["usage-model","model"],["usage-status","status"],["usage-request-id","request_id"]]) if ($(id).value) params.set(key,$(id).value); const records = await api(`/admin/usage?${params}`); $("usage-summary").innerHTML = summary(records).map(c=>`<article class="card"><p class="muted">${c.label}</p><h2>${c.value}</h2></article>`).join(""); const fields=["request_id","model","status","input_tokens","output_tokens","total_tokens","latency_ms","first_token_latency_ms","retry_count","error_type","error_code","prompt_id","prompt_version","created_at"]; $("usage-table").innerHTML = `<table><thead><tr>${fields.map(f=>`<th>${f}</th>`).join("")}</tr></thead><tbody>${records.map(r=>`<tr>${fields.map(f=>`<td>${escapeHtml(r[f])}</td>`).join("")}</tr>`).join("")}</tbody></table>`; return records; } catch(error) { $("usage-table").textContent = error.message; return []; } }

document.querySelectorAll(".tab").forEach(tab => tab.onclick = () => { document.querySelectorAll(".tab,.panel").forEach(el=>el.classList.remove("active")); tab.classList.add("active"); $(tab.dataset.tab).classList.add("active"); if (tab.dataset.tab === "usage") loadUsage(); if (tab.dataset.tab === "prompts") loadPrompts(); });
$("input-mode").onchange = () => { $("simple-inputs").hidden=$("input-mode").value!=="simple"; $("messages-inputs").hidden=!$("simple-inputs").hidden; };
$("add-message").onclick=()=>addMessage();
$("response-format").onchange = () => $("schema-wrap").hidden=$("response-format").value!=="json_schema";
$("new-conversation").onclick=()=>newConversation();
$("load-environment-key").onclick=async()=>{ try { const key=await api("/admin/ui/runtime-key/load-environment",{method:"POST"}); $("runtime-key").value=key.value; setStatus("key-status","已从本地环境读取；此字段不可编辑。"); await loadGateway(); $("runtime-key").value=key.value; } catch(error) { $("runtime-key").value=""; setStatus("key-status",error.message,true); } };
$("send-request").onclick=sendRequest; $("abort-request").onclick=()=>activeSubscription?.controller.abort();
$("chat-log").onscroll=()=>{ const conversation=activeConversation(), log=$("chat-log"); conversation.followOutput=log.scrollHeight-log.scrollTop-log.clientHeight<24; };
$("user-input").onkeydown=(event)=>{ if(event.key==="Enter" && !event.shiftKey){ event.preventDefault(); sendRequest(); } };
$("reconnect-request").onclick=async()=>{ const conversation=activeConversation(), id=$("history-request-id").value.trim(); if(!id)return; conversation.requestId=id; conversation.status="streaming"; const assistantMessage=[...conversation.messages].reverse().find(message=>message.role==="assistant") || appendChat("assistant",""); const subscription=startSubscription(conversation); renderConversations(); renderChat(); try { const response=await fetch(`/v1/streams/${encodeURIComponent(id)}/events?after_event_id=${conversation.lastEventId || 0}`,{signal:subscription.controller.signal}); await readSseResponse(response,assistantMessage,conversation); } catch(error) { if(error.name==="AbortError") { flushTypewriter(assistantMessage); conversation.status="disconnected"; $("history-output").textContent="订阅已断开：已接收文字已立即显示。可在宽限时间内再次重新订阅。"; } else { conversation.status="failed"; assistantMessage.meta=error.message; } } finally { finishSubscription(subscription); renderConversations(); renderChat(); } };
$("cancel-request").onclick=async()=>{ const conversation=activeConversation(), id=conversation.requestId || $("history-request-id").value.trim(); if(!id)return; try { const result=await api(`/v1/streams/${encodeURIComponent(id)}/cancel`,{method:"POST"}); activeSubscription?.controller.abort(); conversation.status=result.status; $("history-output").textContent="后台模型生成已取消。"; renderConversations(); renderChat(); } catch(error) { $("history-output").textContent=error.message; } };
$("history-request-id").oninput=()=>{ const conversation=activeConversation(); conversation.requestId=$("history-request-id").value.trim(); $("reconnect-request").disabled=!conversation.requestId; };
$("create-prompt").onclick=async()=>{ try { const payload={id:$("prompt-create-id").value||null,template:$("prompt-template").value,role:$("prompt-role").value,activate:$("prompt-activate").checked}; const record=await api("/v1/prompts",{method:"POST",body:pretty(payload)}); setStatus("prompt-status",`已创建 ${record.id} v${record.version}`); await loadPrompts(); } catch(error){setStatus("prompt-status",error.message,true);} };
$("preview-prompt").onclick=async()=>{ try { const id=$("preview-prompt-id").value; const body={version:$("preview-version").value?Number($("preview-version").value):null,variables:parseJson("preview-variables")}; $("preview-output").textContent=pretty(await api(`/v1/prompts/${encodeURIComponent(id)}/render`,{method:"POST",body:pretty(body)})); }catch(error){$("preview-output").textContent=error.message;} };
$("load-usage").onclick=loadUsage; $("usage-auto-refresh").onchange=()=>{ clearInterval(refreshTimer); refreshTimer=$("usage-auto-refresh").checked?setInterval(loadUsage,10000):null; };
newConversation(); Promise.all([loadGateway(),loadPrompts()]).catch(error=>{$("health-badge").textContent=error.message;});
