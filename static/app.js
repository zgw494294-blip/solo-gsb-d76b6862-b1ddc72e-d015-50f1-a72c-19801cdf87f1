"use strict";

const MAX_LINES = 80;
const TERMINAL = new Set(["finished", "exception", "terminated"]);
const STATUS_TEXT = {
  idle: "空闲",
  starting: "启动中",
  paused: "已暂停",
  running: "运行中",
  finished: "正常结束",
  exception: "异常结束",
  terminated: "已终止",
};

const EXAMPLES = {
  assign: [
    "# 赋值示例",
    'name = "world"',
    "a = 1",
    "b = 2",
    "c = a + b",
    'message = "hello " + name',
    "print(message)",
    'print("c =", c)',
  ].join("\n"),
  func: [
    "# 函数调用示例：单步会进入函数内部",
    "def add(x, y):",
    "    total = x + y",
    "    return total",
    "",
    "result = add(3, 4)",
    'print("result =", result)',
  ].join("\n"),
  loop: [
    "# 循环示例：每一轮迭代都会暂停",
    "total = 0",
    "for i in range(5):",
    "    total += i",
    '    print("i =", i, "total =", total)',
    'print("done:", total)',
  ].join("\n"),
  exc: [
    "# 异常示例：观察未捕获异常",
    "def div(a, b):",
    "    return a / b",
    "",
    "x = div(10, 0)",
    "print(x)",
  ].join("\n"),
};

let currentRunId = null;   // 当前运行标识；响应里的 run_id 不匹配则丢弃
let pauseSeq = null;       // 当前停顿序号
let status = "idle";
let pollTimer = null;

const $ = (id) => document.getElementById(id);

function setMessage(text) { $("message").textContent = text || ""; }

function refreshLineCount() {
  const lines = $("editor").value.split("\n");
  $("lineCount").textContent = lines.length + " / " + MAX_LINES + " 行";
  $("lineCount").style.color = lines.length > MAX_LINES ? "#cf222e" : "";
}

function setStatus(newStatus) {
  status = newStatus;
  const badge = $("statusBadge");
  badge.textContent = STATUS_TEXT[newStatus] || newStatus;
  badge.className = "badge " + newStatus;
  refreshButtons();
}

function refreshButtons() {
  const active = currentRunId !== null && !TERMINAL.has(status);
  $("btnRun").disabled = active;
  $("btnStep").disabled = !(active && status === "paused");
  $("btnContinue").disabled = !(active && status === "paused");
  $("btnTerminate").disabled = !active;
  document.querySelectorAll("#examples button").forEach((b) => (b.disabled = active));
}

function enterRunMode() {
  buildCodeView($("editor").value);
  $("editor").classList.add("hidden");
  $("codeView").classList.remove("hidden");
}

function exitRunMode() {
  $("codeView").classList.add("hidden");
  $("editor").classList.remove("hidden");
}

function buildCodeView(code) {
  const view = $("codeView");
  view.textContent = "";
  code.split("\n").forEach((text, i) => {
    const line = document.createElement("div");
    line.className = "line";
    line.id = "src-line-" + (i + 1);
    const num = document.createElement("span");
    num.className = "lineno";
    num.textContent = i + 1;
    const content = document.createElement("span");
    content.textContent = text || " ";
    line.appendChild(num);
    line.appendChild(content);
    view.appendChild(line);
  });
}

function highlightLine(lineNo, isError) {
  document.querySelectorAll("#codeView .line.current, #codeView .line.error-line")
    .forEach((el) => el.classList.remove("current", "error-line"));
  if (!lineNo) return;
  const el = $("src-line-" + lineNo);
  if (el) {
    el.classList.add(isError ? "error-line" : "current");
    el.scrollIntoView({ block: "nearest" });
  }
}

function renderState(state) {
  // 旧运行的迟到响应不得覆盖新运行
  if (!state || state.run_id !== currentRunId) return;
  pauseSeq = state.pause_seq;
  setStatus(state.status);
  $("runInfo").textContent =
    "run_id=" + state.run_id + "　停顿序号=" + state.pause_seq +
    (state.line ? "　待执行行=" + state.line : "");

  highlightLine(state.line, state.status === "exception");

  const tbody = document.querySelector("#localsTable tbody");
  tbody.textContent = "";
  const names = Object.keys(state.locals || {});
  if (names.length === 0) {
    tbody.innerHTML = '<tr class="empty"><td colspan="2">（无）</td></tr>';
  } else {
    for (const name of names) {
      const tr = document.createElement("tr");
      const tdName = document.createElement("td");
      tdName.textContent = name;
      const tdValue = document.createElement("td");
      tdValue.textContent = state.locals[name];
      tr.appendChild(tdName);
      tr.appendChild(tdValue);
      tbody.appendChild(tr);
    }
  }

  const out = $("stdout");
  out.textContent = state.stdout || "";
  out.scrollTop = out.scrollHeight;

  const exc = $("exception");
  exc.textContent = state.exception || "无";
  exc.classList.toggle("has-error", !!state.exception);

  if (TERMINAL.has(state.status)) {
    stopPolling();
    currentRunId = null;
    exitRunMode();  // 结束后可改稿重跑
    setStatus("idle");
    $("runInfo").textContent = "上一次运行 run_id=" + state.run_id +
      " 已结束（" + (STATUS_TEXT[state.status] || state.status) + "）";
  }
}

async function api(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await resp.json(); } catch (e) { /* ignore */ }
  return { ok: resp.ok, status: resp.status, data };
}

async function startRun() {
  setMessage("");
  const code = $("editor").value;
  if (!code.trim()) { setMessage("代码不能为空"); return; }
  if (code.split("\n").length > MAX_LINES) {
    setMessage("代码最多 " + MAX_LINES + " 行");
    return;
  }
  const { ok, data } = await api("/api/run", { code });
  if (!ok) { setMessage((data && data.error) || "启动失败"); return; }
  currentRunId = data.run_id;
  pauseSeq = data.pause_seq;
  enterRunMode();
  renderState(data);
  startPolling();
}

async function step() {
  if (currentRunId === null) return;
  $("btnStep").disabled = true;  // 防止同次停顿重复点击
  const { ok, data } = await api("/api/step", {
    run_id: currentRunId,
    pause_seq: pauseSeq,
  });
  if (!ok) {
    setMessage((data && data.error) || "请求被拒绝");
    if (data && data.state) renderState(data.state);
    return;
  }
  setMessage("");
  renderState(data);
}

async function continueRun() {
  if (currentRunId === null) return;
  $("btnContinue").disabled = true;
  const { ok, data } = await api("/api/continue", {
    run_id: currentRunId,
    pause_seq: pauseSeq,
  });
  if (!ok) {
    setMessage((data && data.error) || "请求被拒绝");
    if (data && data.state) renderState(data.state);
    return;
  }
  setMessage("");
  renderState(data);
}

async function terminate() {
  if (currentRunId === null) return;
  const { ok, data } = await api("/api/terminate", { run_id: currentRunId });
  if (!ok) { setMessage((data && data.error) || "终止失败"); return; }
  setMessage("");
  renderState(data);
}

async function poll() {
  if (currentRunId === null) return;
  const runId = currentRunId;
  try {
    const resp = await fetch("/api/state?run_id=" + runId);
    if (!resp.ok) return;  // 运行已过期等情况：忽略本次轮询
    const state = await resp.json();
    if (runId !== currentRunId) return;  // 等待响应期间已切换运行
    renderState(state);
  } catch (e) { /* 网络抖动，下一轮再试 */ }
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(poll, 400);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

$("btnRun").addEventListener("click", startRun);
$("btnStep").addEventListener("click", step);
$("btnContinue").addEventListener("click", continueRun);
$("btnTerminate").addEventListener("click", terminate);
$("editor").addEventListener("input", refreshLineCount);
document.querySelectorAll("#examples button").forEach((btn) => {
  btn.addEventListener("click", () => {
    $("editor").value = EXAMPLES[btn.dataset.example] || "";
    refreshLineCount();
    setMessage("");
  });
});

$("editor").value = EXAMPLES.assign;
refreshLineCount();
refreshButtons();
