const $ = (id) => document.getElementById(id);
const names = {
  queued: "排队中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  stopped: "提前停止",
  interrupted: "已中断",
  passed: "通过",
  error: "执行错误",
  timed_out: "超时",
  not_run: "未运行",
};
const phases = [
  "queued",
  "preparing",
  "baseline",
  "agent",
  "verification",
  "finished",
];
let config,
  selected = new URLSearchParams(location.search).get("task"),
  currentTab = "overview",
  lastList = "";

async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const data = await response.json();
      message =
        typeof data.detail === "string" ? data.detail : "请检查输入内容";
    } catch {}
    throw new Error(message);
  }
  return response;
}
async function json(url, options) {
  return (await request(url, options)).json();
}
function text(id, value) {
  $(id).textContent = value ?? "";
}
function badge(id, status, extra = "") {
  text(id, (names[status] || status || "未运行") + extra);
  $(id).className = "badge " + (status || "not_run");
}
function showError(id, message) {
  text(id, message);
  $(id).hidden = !message;
}
function selectTask(id) {
  selected = id;
  const url = new URL(location);
  url.searchParams.set("task", id);
  history.replaceState(null, "", url);
  lastList = "";
  refresh().catch((error) => showError("connection-error", error.message));
}
function renderList(tasks) {
  const key =
    JSON.stringify(tasks.map((t) => [t.id, t.status, t.phase, t.title])) +
    selected;
  if (key === lastList) return;
  lastList = key;
  text("task-count", tasks.length);
  $("task-list").replaceChildren();
  if (!tasks.length) {
    const p = document.createElement("p");
    p.className = "muted empty-list";
    p.textContent = "还没有任务";
    $("task-list").append(p);
  }
  for (const task of tasks) {
    const button = document.createElement("button");
    button.className = "task-item" + (task.id === selected ? " selected" : "");
    const title = document.createElement("span");
    title.className = "task-name";
    title.textContent = task.title;
    const line = document.createElement("span");
    line.className = "task-line";
    const state = document.createElement("span");
    state.className = "badge " + task.status;
    state.textContent = names[task.status] || task.status;
    const time = document.createElement("span");
    time.textContent = new Date(task.created_at).toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
    line.append(state, time);
    button.append(title, line);
    button.addEventListener("click", () => selectTask(task.id));
    $("task-list").append(button);
  }
}
function renderDetail(task) {
  $("empty-state").hidden = true;
  $("task-detail").hidden = false;
  text("task-id", "TASK / " + task.id.slice(0, 12));
  text("detail-title", task.title);
  badge("task-status", task.status);
  document.querySelectorAll("[data-phase]").forEach((el) => {
    const index = phases.indexOf(el.dataset.phase),
      active = phases.indexOf(task.phase);
    el.className = index === active ? "current" : index < active ? "done" : "";
  });
  text("tool-steps", task.runtime.tool_steps);
  text("changed-count", task.changed_paths.length);
  text("test-status", names[task.test_status] || task.test_status);
  showError("task-error", task.error);
  text("detail-prompt", task.prompt);
  text("final-answer", task.runtime.final_answer || "等待 Agent 返回结果…");
  text("base-commit", task.base_commit);
  text("stop-reason", task.runtime.stop_reason || task.stop_reason || "—");
  text("last-tool", task.runtime.last_tool || "—");
  text("workspace-path", task.workspace);
  text("artifact-path", task.artifact_dir);
  $("changed-files").replaceChildren();
  for (const path of task.changed_paths) {
    const li = document.createElement("li");
    li.textContent = path;
    $("changed-files").append(li);
  }
  if (!task.changed_paths.length) {
    const li = document.createElement("li");
    li.textContent =
      task.status === "running" ? "执行结束后生成文件清单" : "暂无修改";
    $("changed-files").append(li);
  }
  for (const phase of ["before", "after"]) {
    const result = task.tests[phase] || {};
    badge(
      phase + "-status",
      result.status || "not_run",
      result.exit_code == null ? "" : " · exit " + result.exit_code,
    );
  }
}
async function loadArtifacts(id) {
  const files =
    currentTab === "diff"
      ? { diff: "diff-content" }
      : currentTab === "tests"
        ? { before: "before-content", after: "after-content" }
        : currentTab === "logs"
          ? { agent: "agent-content", setup: "setup-content" }
          : {};
  await Promise.all(
    Object.entries(files).map(async ([name, element]) => {
      const value = await (
        await request(`/api/tasks/${id}/artifacts/${name}`)
      ).text();
      if (selected === id) text(element, value || "暂无输出");
    }),
  );
}
async function refresh() {
  const tasks = await json("/api/tasks");
  if (!selected && tasks.length) selected = tasks[0].id;
  renderList(tasks);
  if (selected) {
    const id = selected;
    const task = await json("/api/tasks/" + id);
    if (selected === id) {
      renderDetail(task);
      await loadArtifacts(id);
    }
  }
  showError("connection-error", "");
}
document.querySelectorAll("[data-tab]").forEach((button) =>
  button.addEventListener("click", () => {
    currentTab = button.dataset.tab;
    document
      .querySelectorAll("[data-tab]")
      .forEach((el) => el.setAttribute("aria-selected", String(el === button)));
    document
      .querySelectorAll(".tab-panel")
      .forEach((el) => (el.hidden = el.id !== "panel-" + currentTab));
    if (selected)
      loadArtifacts(selected).catch((error) =>
        showError("connection-error", error.message),
      );
  }),
);
$("task-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("submit-button").disabled = true;
  showError("form-error", "");
  try {
    const task = await json("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        repository_id: Number($("repository").value),
        title: $("title").value,
        prompt: $("prompt").value,
        max_steps: Number($("max-steps").value),
      }),
    });
    selectTask(task.id);
  } catch (error) {
    showError("form-error", error.message);
  } finally {
    $("submit-button").disabled = false;
  }
});
async function init() {
  config = await json("/api/config");
  for (const repo of config.repositories) {
    const option = document.createElement("option");
    option.value = repo.id;
    option.textContent = repo.name;
    $("repository").append(option);
  }
  function path() {
    text(
      "repository-path",
      config.repositories.find((r) => r.id === Number($("repository").value))
        ?.path,
    );
  }
  $("repository").addEventListener("change", path);
  path();
  text(
    "test-command",
    config.test_argv.join(" ") || "未配置 · 结果将标记为未验收",
  );
  if (config.mode === "demo") {
    $("mode-banner").hidden = false;
    text(
      "mode-banner",
      "离线验收模式 · 模型回答预设，真实执行读取、修改和测试，不消耗 API 额度。",
    );
    $("title").value = "修复运费计算的边界条件";
    $("prompt").value = config.demo_prompt;
    $("prompt").readOnly = true;
  } else {
    $("mode-banner").hidden = false;
    text(
      "mode-banner",
      "本地执行模式 · 使用已有模型配置，在任务副本中自动执行代码修改和命令。",
    );
  }
  await refresh();
}
async function poll() {
  try {
    await refresh();
  } catch (error) {
    showError("connection-error", "连接暂时中断：" + error.message);
  } finally {
    setTimeout(poll, 1200);
  }
}
init()
  .then(() => setTimeout(poll, 1200))
  .catch((error) => showError("connection-error", error.message));
