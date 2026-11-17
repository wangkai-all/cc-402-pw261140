// 分区留存治理控制台前端（原生 JS，无依赖）
let STATE = null;

const $ = (s) => document.querySelector(s);
const fmtBytes = (n) => {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(n) / Math.log(1024));
  return (n / Math.pow(1024, i)).toFixed(i ? 1 : 0) + " " + u[i];
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function badge(text, cls) { return `<span class="badge b-${cls}">${esc(text)}</span>`; }

function statusBadge(s) {
  const map = {
    RETAINED: ["留存中", "green"], EXPIRED: ["已到期被引用", "amber"],
    PENDING_RECLAIM: ["待回收", "amber"], DELETED: ["已回收", "gray"],
    LIVE: ["在盘", "green"], RUNNING: ["运行中", "green"], COMPLETED: ["已完成", "green"],
    LOST: ["失联", "red"], RESUMED: ["已恢复", "purple"], FAILED: ["失败", "red"],
    ACTIVE: ["有效", "green"], RELEASED: ["已释放", "gray"], REVOKED: ["已撤销", "red"],
    EXPIRED_G: ["已过期", "gray"], RENEWED: ["已续用(旧)", "purple"],
    HOLD: ["延迟回收", "purple"], COPY: ["必要副本", "amber"],
  };
  const [t, c] = map[s] || [s, "gray"];
  return badge(t, c);
}

async function api(path, body) {
  const opt = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opt);
  const data = await r.json();
  if (!r.ok) throw new Error(`${data.error || "Error"}: ${data.message}`);
  return data;
}

let toastTimer;
function toast(msg, isErr) {
  const box = $("#toast");
  box.style.display = "block";
  const el = document.createElement("div");
  el.className = "msg" + (isErr ? " err" : "");
  el.textContent = (isErr ? "✗ " : "✓ ") + msg;
  box.appendChild(el);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.innerHTML = ""; box.style.display = "none"; }, 4500);
}
async function act(path, body, okMsg) {
  try { const r = await api(path, body); toast(okMsg + (r.bytes_freed_this_run ? `（本次释放 ${fmtBytes(r.bytes_freed_this_run)}）` : "")); await refresh(); }
  catch (e) { toast(e.message, true); }
}

// ---------- 渲染 ----------
function renderKpis(s) {
  const t = s.totals;
  $("#kpis").innerHTML = [
    ["在盘存储单元", t.live_units, ""],
    ["在盘占用", fmtBytes(t.live_bytes), ""],
    ["已回收单元", t.deleted_units, ""],
    ["累计真实释放", fmtBytes(t.freed_bytes), "freed"],
    ["有效引用授权", s.grants.filter(g => g.active_now).length, ""],
  ].map(([l, v, c]) => `<div class="kpi ${c}"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");
  $("#virtual-clock").textContent = s.virtual_now_iso;
}

function renderReap(s) {
  const el = $("#tab-reap");
  el.innerHTML = `
    <div class="section-title">
      <h3 style="margin:0">回收器判定（依据：虚拟到期 + grants 台账；不看文件 mtime）</h3>
      <div>
        <button class="ghost" id="btn-preview">刷新预览</button>
        <button class="warn" id="btn-reap-dry">仅预览(dry-run)</button>
        <button class="danger" id="btn-reap">执行真实回收</button>
      </div>
    </div>
    <div id="reap-content"><p class="pill">加载中…</p></div>`;
  $("#btn-preview").onclick = () => refresh();
  $("#btn-reap-dry").onclick = () => act("/api/reap", { dry_run: true }, "dry-run 完成，未删除任何文件");
  $("#btn-reap").onclick = () => {
    if (!confirm("将真实 unlink 可回收文件，确认？")) return;
    act("/api/reap", { dry_run: false }, "回收完成");
  };
  api("/api/preview").then(p => {
    const rc = (items, empty) => items.length ? `<ul class="reasons">${items.map(x =>
      `<li><b>${x.kind === "COPY" ? "副本" : "分区"}#${x.partition_id ?? "-"}</b> 单元${x.unit_id}
       ${fmtBytes(x.size_bytes)} — ${x.reasons.map(esc).join("；")}
       <div class="mono">${esc(x.path)}</div></li>`).join("")}</ul>`
      : `<p class="pill">${empty}</p>`;
    $("#reap-content").innerHTML = `
      <div class="card" style="margin-bottom:12px; border-left:4px solid var(--red)">
        <h3 style="color:var(--red)">可清理范围：${p.reclaimable_count} 个单元，共 ${fmtBytes(p.reclaimable_bytes)}</h3>
        ${rc(p.reclaimable, "当前没有可回收对象")}
      </div>
      <div class="card" style="border-left:4px solid var(--amber)">
        <h3 style="color:var(--amber)">仍保留：${p.retained.length} 个单元（附保留原因）</h3>
        ${rc(p.retained, "无保留对象")}
        <p class="pill">历史累计已真实释放：${fmtBytes(p.total_freed_bytes_historical)}</p>
      </div>`;
  }).catch(e => { $("#reap-content").innerHTML = `<p class="pill">预览失败：${esc(e.message)}</p>`; });
}

function renderPartitions(s) {
  const rows = s.partitions.map(p => `
    <tr>
      <td>${p.id}</td><td>${esc(p.zone)}</td><td>${esc(p.business_day)}</td>
      <td>${statusBadge(p.status)}</td>
      <td>${fmtBytes(p.size_bytes)}<br><span class="mono">${p.sha256.slice(0, 12)}…</span></td>
      <td>${p.expiry_iso}<br><span class="pill">策略 v${p.policy_version}${p.expired ? " · 已过期" : ""}</span></td>
      <td>${p.file_exists ? badge("文件在盘", "green") : badge("文件不存在", "red")}</td>
      <td>${p.reasons.length ? `<ul class="reasons">${p.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>`
        : (p.status === "DELETED" ? '<span class="pill">—</span>' : '<ul class="reasons ok"><li>无引用，到期即可回收</li></ul>')}</td>
    </tr>`).join("");
  $("#tab-partitions").innerHTML = `
    <div class="section-title"><h3 style="margin:0">分区（${s.partitions.length}）</h3>
      <span class="pill">留存到期按分区创建时适用的策略版本计算，而非文件日期</span></div>
    <table><thead><tr><th>id</th><th>zone</th><th>业务日</th><th>状态</th><th>大小/校验</th>
      <th>虚拟到期点</th><th>物理文件</th><th>保留原因</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderTasks(s) {
  const rows = s.tasks.map(t => `
    <tr>
      <td>${t.id}</td><td>${esc(t.name)}</td><td>${statusBadge(t.status)}</td>
      <td>[${t.input_ids.join(", ")}]</td>
      <td>${t.progress.map(pr => {
        const c = pr.state === "DONE" ? "green" : pr.state === "FAILED" ? "red" : "gray";
        return `#${pr.partition_id}:${badge(pr.state, c)}`;
      }).join(" ")}</td>
      <td><span class="pill">心跳距今 ${t.heartbeat_age_s}s（真实墙钟，超时 ${15}s 判失联）</span></td>
      <td>${t.fail_reason ? `<span class="pill" style="color:var(--red)">${esc(t.fail_reason)}</span>` : ""}
        ${t.result_path ? `<div class="mono">${esc(t.result_path)}</div>` : ""}</td>
      <td>
        ${t.status === "RUNNING" ? `<button class="small-btn danger" onclick="lostTask(${t.id})">触发失联</button>` : ""}
        ${t.status === "LOST" ? `<button class="small-btn warn" onclick="resumeTask(${t.id})">恢复（重新鉴权）</button>` : ""}
      </td>
    </tr>`).join("");
  $("#tab-tasks").innerHTML = `
    <div class="section-title"><h3 style="margin:0">加工任务（${s.tasks.length}）</h3>
      <span class="pill">失联后 TASK_LEASE 被撤销，不再阻塞回收；恢复时逐分区重新确认读取权并换发新令牌</span></div>
    <table><thead><tr><th>id</th><th>名称</th><th>状态</th><th>输入分区</th>
      <th>进度</th><th>心跳/租约</th><th>结果/失败原因</th><th>操作</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="8" class="pill">暂无任务</td></tr>'}</tbody></table>`;
}

function renderReports(s) {
  const rows = s.reports.map(r => `
    <tr>
      <td>${r.id}</td><td>${esc(r.title)}</td><td>${statusBadge(r.mode)}</td>
      <td>${r.task_id ?? "-"}</td><td>${fmtBytes(r.size_bytes)} ${r.file_exists ? badge("在盘", "green") : badge("缺失", "red")}</td>
      <td>${r.grants.map(g => {
        const active = g.status === "ACTIVE" && g.expire_vt > s.virtual_now;
        return `<div>${statusBadge(g.status === "EXPIRED" ? "EXPIRED_G" : g.status)}
          <span class="pill">至 ${g.expire_iso}${g.renew_count ? ` · 续${g.renew_count}次` : ""}
          ${g.copy_id ? ` · 副本#${g.copy_id}` : ` · 分区#${g.partition_id}`}</span></div>`;
      }).join("")}</td>
      <td>
        <input id="renew-${r.id}" type="number" value="7" min="1" style="width:64px;padding:3px">
        <button class="small-btn" onclick="renewReport(${r.id})">续用(天)</button>
      </td>
    </tr>`).join("");
  $("#tab-reports").innerHTML = `
    <div class="section-title"><h3 style="margin:0">封存报告（${s.reports.length}）</h3>
      <span class="pill">HOLD 护原件（REPORT_HOLD）；COPY 护独立副本（COPY_BINDING），续用留 RENEWED 痕迹链</span></div>
    <table><thead><tr><th>id</th><th>标题</th><th>模式</th><th>任务</th><th>报告文件</th>
      <th>引用授权与过期点</th><th>续用</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="7" class="pill">暂无报告</td></tr>'}</tbody></table>`;
}

function renderGrants(s) {
  const rows = s.grants.map(g => `
    <tr><td>${g.id}</td><td>${statusBadge(g.kind)}</td>
      <td>${g.partition_id ? "分区#" + g.partition_id : ""}${g.copy_id ? "副本#" + g.copy_id : ""}</td>
      <td>${g.task_id ?? "-"}</td><td>${g.report_id ?? "-"}</td>
      <td>${statusBadge(g.status === "EXPIRED" ? "EXPIRED_G" : g.status)} ${g.active_now ? badge("当前有效", "green") : ""}</td>
      <td>${g.expire_iso}</td><td>${g.renew_count}</td>
      <td>${g.renewed_by_grant ? "→ grant#" + g.renewed_by_grant : ""}</td>
      <td class="mono">${esc(g.note || "")}</td></tr>`).join("");
  $("#tab-grants").innerHTML = `
    <div class="section-title"><h3 style="margin:0">引用授权台账（${s.grants.length}，只追加状态流转）</h3></div>
    <table><thead><tr><th>id</th><th>类型</th><th>对象</th><th>任务</th><th>报告</th>
      <th>状态</th><th>虚拟过期点</th><th>续次</th><th>续用链</th><th>备注</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="10" class="pill">暂无授权</td></tr>'}</tbody></table>`;
}

function renderEvents(s) {
  const rows = s.events.map(e => `
    <tr><td class="mono">${e.id}</td><td>${badge(e.actor, "purple")}</td><td>${esc(e.action)}</td>
      <td>${esc(e.target || "")}</td>
      <td class="mono">${esc(JSON.stringify(e.detail, null, 0))}</td></tr>`).join("");
  $("#tab-events").innerHTML = `
    <div class="section-title"><h3 style="margin:0">审计事件（append-only，最近 ${s.events.length} 条）</h3></div>
    <table><thead><tr><th>#</th><th>执行者</th><th>动作</th><th>对象</th><th>详情</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
}

async function refresh() {
  STATE = await api("/api/state");
  renderKpis(STATE);
  renderReap(STATE); renderPartitions(STATE); renderTasks(STATE);
  renderReports(STATE); renderGrants(STATE); renderEvents(STATE);
}

// ---------- 全局操作（供行内 onclick 调用）----------
window.lostTask = (id) => act("/api/tasks/lost", { task_id: id }, `任务#${id} 已置为失联，等待 supervisor 撤销租约`);
window.resumeTask = (id) => act("/api/tasks/resume", { task_id: id }, `任务#${id} 恢复请求已处理（读取权已重新确认）`);
window.renewReport = (id) => {
  const days = parseInt($("#renew-" + id).value, 10);
  act("/api/reports/renew", { report_id: id, extra_days: days }, `报告#${id} 已续用 ${days} 天`);
};

// ---------- 绑定 ----------
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".tab-body").forEach(x => x.classList.remove("active"));
  t.classList.add("active");
  $("#tab-" + t.dataset.tab).classList.add("active");
});

$("#btn-seed").onclick = () => act("/api/seed", {}, "虚构数据已生成");
document.querySelectorAll("[data-clock]").forEach(b => b.onclick = () =>
  act("/api/clock/advance", { seconds: Number(b.dataset.clock) }, `虚拟时钟已快进 ${b.dataset.clock / 86400} 天`));
$("#btn-policy").onclick = () => act("/api/policy", {
  zone: $("#pol-zone").value, retention_days: Number($("#pol-days").value),
}, "留存策略已更新（旧版本留痕）");
$("#btn-new-partition").onclick = () => act("/api/partitions", {
  zone: $("#np-zone").value, business_day: $("#np-day").value,
}, "新分区已按当前策略版本创建");
$("#btn-task").onclick = () => act("/api/tasks", {
  name: $("#task-name").value,
  partition_ids: $("#task-inputs").value.split(",").map(x => Number(x.trim())).filter(Boolean),
}, "加工任务已创建并签发租约");
$("#btn-report").onclick = () => act("/api/reports", {
  task_id: Number($("#rep-task").value), title: "封存报告-" + new Date().toLocaleTimeString(),
  mode: $("#rep-mode").value,
  partition_ids: $("#rep-parts").value.split(",").map(x => Number(x.trim())).filter(Boolean),
  hold_days: Number($("#rep-days").value),
}, "报告已封存，引用授权已登记");

refresh().catch(e => toast("初始化失败: " + e.message, true));
setInterval(refresh, 2000);
