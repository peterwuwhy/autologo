const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

let batch = null;      // { batch_id, labels, errors }
let queue = [];        // 待印的 index 清單
let queuePos = 0;
let isDryRun = false;

// ---------- 分頁 ----------
function showTab(name) {
  $$(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "work" && !work) loadWork();
  if (name === "settings") loadSettings();
  if (name === "calib") loadCalibration();
  if (name === "log") loadLog();
}
$$(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("json") ? await res.json() : { message: await res.text() };
  if (!res.ok && body.ok === undefined) throw new Error(body.error || body.message || res.statusText);
  return body;
}

function postJSON(url, data) {
  return api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
}

// ---------- 工作單 ----------
let work = null;       // /api/work 的回應
let workQueue = [];    // 待印的列號
let workPos = 0;

async function loadWork(opts = {}) {
  const q = new URLSearchParams();
  const tab = opts.tab ?? (work ? work.tab : "");
  if (tab) q.set("tab", tab);
  if (opts.refresh) q.set("refresh", "1");
  flash("#work-msg", true, opts.refresh || !work ? "讀取工作單中…" : "");
  $("#work-refresh").disabled = true;
  let r;
  try {
    r = await api("/api/work?" + q);
  } catch (err) {
    r = { ok: false, message: err.message };
  }
  $("#work-refresh").disabled = false;
  if (!r.ok) {
    flash("#work-msg", false, r.message);
    return;
  }
  work = r;
  flash("#work-msg", true, `${r.fetched} 更新，布標 ${r.library} 個${r.sync ? "（" + r.sync + "）" : ""}`);
  $("#work-tab").innerHTML = r.tabs.map((t) => `<option${t === r.tab ? " selected" : ""}>${esc(t)}</option>`).join("");
  $("#work-title").textContent = r.title;
  $("#work-badge").hidden = !r.dry_run;
  const box = $("#work-errors");
  box.hidden = !r.errors.length;
  box.innerHTML = r.errors.length ? `<ul>${r.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : "";
  $("#work-progress").hidden = true;
  renderWork();
}

function workRow(n) {
  return work.rows.find((r) => r.row === n);
}

function previewUrl(r, file, size) {
  return "/api/work/preview.png?" + new URLSearchParams({ brand: r.folder, file, size, t: Date.now() });
}

function renderWork() {
  const showAll = $("#work-show-all").checked;
  const hidden = work.rows.filter((r) => !r.folder).length;
  $("#work-hidden-count").textContent = hidden;
  $("#work-table").hidden = false;
  const rows = work.rows.filter((r) => showAll || r.folder);
  $("#work-rows").innerHTML = rows.length
    ? rows.map((r) => {
        const base = `<td>${esc(r.date)}</td><td>${esc(r.shop)}</td><td>${esc(r.brand)}</td><td>${esc(r.spec)}</td><td>${esc(r.size)}</td><td>${r.qty}</td>`;
        if (!r.folder) {
          const why = r.brand ? `沒有「${esc(r.brand)}」的布標資料夾` : "沒有品牌";
          return `<tr class="off"><td></td>${base}<td colspan="3">${why}</td><td></td></tr>`;
        }
        const ready = r.file && r.size_text && r.match !== "guess";
        const hint = { guess: "名稱不完全相同，請確認", none: "找不到同名的布標，請選擇", saved: "上次選的" }[r.match] || "";
        const opts = `<option value="">請選擇…</option>` + r.files.map((f) => `<option value="${esc(f)}"${f === r.file ? " selected" : ""}>${esc(f.replace(/\.ezp$/i, ""))}</option>`).join("");
        return `<tr data-row="${r.row}">
          <td><input type="checkbox" class="pick"${ready && !r.printed ? " checked" : ""}${r.file && r.size_text ? "" : " disabled"}></td>${base}
          <td><select class="file${r.file ? "" : " need"}">${opts}</select><span class="hint${r.match === "saved" ? "" : " warn"}">${hint}</span></td>
          <td><input class="size${r.size_text ? "" : " need"}" value="${esc(r.size_text)}" placeholder="${esc(r.size_old || "例：152cm×188cm")}"></td>
          <td>${r.file ? `<img class="thumb" loading="lazy" src="${previewUrl(r, r.file, r.size_text)}" alt="預覽">` : ""}</td>
          <td class="status${r.printed ? " status-ok" : ""}">${r.printed ? "已印 " + esc(r.printed) : ""}</td>
        </tr>`;
      }).join("")
    : `<tr><td colspan="11">這個工作表沒有${showAll ? "" : "對得到布標的"}資料</td></tr>`;
  updateWorkSummary();
}

function workPicked() {
  return [...$$("#work-rows tr[data-row]")].filter((tr) => tr.querySelector(".pick").checked).map((tr) => +tr.dataset.row);
}

function updateWorkSummary() {
  const sel = workPicked();
  const sheets = sel.reduce((n, row) => n + workRow(row).qty, 0);
  $("#work-print").textContent = sel.length ? `列印勾選（${sel.length} 筆、${sheets} 張）` : "列印勾選";
  $("#work-print").disabled = !sel.length;
}

// 換布標或改尺寸：更新預覽，資料齊全才能勾選
async function workRowChanged(tr, fileChanged) {
  const r = workRow(+tr.dataset.row);
  const sel = tr.querySelector(".file");
  const size = tr.querySelector(".size");
  if (fileChanged && sel.value) {
    // 每個布標的尺寸寫法不同（公分或尺），換布標要重算
    const s = await api("/api/work/size?" + new URLSearchParams({ brand: r.folder, file: sel.value, size: r.size }));
    size.value = s.size_text;
    size.placeholder = s.size_old || "例：152cm×188cm";
    tr.querySelector(".hint").textContent = "";
  }
  const file = sel.value;
  const text = size.value.trim();
  sel.classList.toggle("need", !file);
  size.classList.toggle("need", !text);
  const pick = tr.querySelector(".pick");
  pick.disabled = !(file && text);
  if (pick.disabled) pick.checked = false;
  tr.children[9].innerHTML = file ? `<img class="thumb" src="${previewUrl(r, file, text)}" alt="預覽">` : "";
  updateWorkSummary();
}

$("#work-rows").addEventListener("change", (e) => {
  const tr = e.target.closest("tr[data-row]");
  if (!tr) return;
  if (e.target.classList.contains("pick")) updateWorkSummary();
  else workRowChanged(tr, e.target.classList.contains("file"));
});
$("#work-rows").addEventListener("click", (e) => {
  if (e.target.classList.contains("thumb")) {
    $("#preview-big").src = e.target.src;
    $("#preview-dialog").showModal();
  }
});
$("#work-tab").addEventListener("change", (e) => loadWork({ tab: e.target.value }));
$("#work-refresh").addEventListener("click", () => loadWork({ refresh: true }));
$("#work-show-all").addEventListener("change", () => work && renderWork());
$("#work-sync").addEventListener("click", async () => {
  flash("#work-msg", true, "同步布標中…（第一次約需半分鐘）");
  $("#work-sync").disabled = true;
  let r;
  try {
    r = await postJSON("/api/work/sync", {});
  } catch (err) {
    r = { ok: false, message: err.message };
  }
  $("#work-sync").disabled = false;
  if (r.ok) await loadWork();
  flash("#work-msg", r.ok, r.message);
});

$("#work-print").addEventListener("click", () => {
  const sel = workPicked();
  const sheets = sel.reduce((n, row) => n + workRow(row).qty, 0);
  const again = sel.filter((row) => workRow(row).printed).length;
  const where = work.dry_run ? "（測試模式：存成檔案，不會真的印）" : "";
  const warn = again ? `\n其中 ${again} 筆之前印過，會再印一次。` : "";
  if (!confirm(`確定列印 ${sel.length} 筆、共 ${sheets} 張布標？${where}${warn}`)) return;
  workQueue = sel;
  workPos = 0;
  runWorkQueue();
});
$("#work-resume").addEventListener("click", runWorkQueue);

async function runWorkQueue() {
  const bar = () => {
    $("#work-bar").style.width = (workQueue.length ? (workPos / workQueue.length) * 100 : 0) + "%";
    $("#work-progress-text").textContent = `${workPos} / ${workQueue.length}`;
  };
  $("#work-progress").hidden = false;
  $("#work-resume").hidden = true;
  $("#work-print").disabled = true;
  while (workPos < workQueue.length) {
    const row = workQueue[workPos];
    const tr = $(`#work-rows tr[data-row="${row}"]`);
    const status = tr.querySelector(".status");
    status.textContent = "列印中…";
    status.className = "status status-busy";
    bar();
    let r;
    try {
      r = await postJSON("/api/work/print", {
        tab: work.tab, row, file: tr.querySelector(".file").value, size: tr.querySelector(".size").value.trim(),
      });
    } catch (err) {
      r = { ok: false, message: err.message };
    }
    if (!r.ok) {
      status.textContent = "失敗";
      status.className = "status status-bad";
      $("#work-progress-text").textContent = `第 ${workPos + 1} 筆失敗：${r.message}`;
      $("#work-resume").hidden = false;
      $("#work-print").disabled = false;
      return;
    }
    status.textContent = "✓ " + r.message;
    status.className = "status status-ok";
    tr.querySelector(".pick").checked = false;
    if (!work.dry_run) workRow(row).printed = "剛剛";
    workPos++;
  }
  bar();
  $("#work-progress-text").textContent = `完成：${workQueue.length} 筆已送出`;
  updateWorkSummary();
}

// ---------- 上傳 ----------
const drop = $("#drop");
$("#file").addEventListener("change", (e) => e.target.files[0] && upload(e.target.files[0]));
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]));

async function upload(file) {
  $("#file-name").textContent = "讀取中：" + file.name;
  const fd = new FormData();
  fd.append("file", file);
  try {
    batch = await api("/api/upload", { method: "POST", body: fd });
  } catch (err) {
    $("#file-name").textContent = "上傳失敗：" + err.message;
    return;
  }
  $("#file").value = "";
  $("#file-name").textContent = file.name;
  showErrors(batch.errors);
  renderRows();
  const cfg = await api("/api/settings");
  isDryRun = cfg.printer.dry_run;
  $("#mode-badge").hidden = !isDryRun;
}

function showErrors(errors) {
  const box = $("#errors");
  box.hidden = !errors.length;
  box.innerHTML = errors.length
    ? `<h3>有 ${errors.length} 個問題（這些列不會列印，請修正 Excel 後重新上傳）</h3><ul>${errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`
    : "";
}

function renderRows() {
  const labels = batch.labels;
  $("#result").hidden = false;
  $("#progress").hidden = true;
  const stamp = Date.now();
  $("#rows").innerHTML = labels.length
    ? labels
        .map(
          (l, i) => `<tr data-i="${i}">
        <td><input type="checkbox" class="pick" checked></td>
        <td>${l["列"]}</td>
        <td>${esc(l["型號"])}</td>
        <td>${esc(l["寬"])} x ${esc(l["長"])}</td>
        <td>${l["數量"]}</td>
        <td>${esc(l["備註"])}</td>
        <td><img class="thumb" loading="lazy" src="/api/preview/${batch.batch_id}/${i}.png?t=${stamp}" alt="預覽"></td>
        <td class="status"></td>
      </tr>`
        )
        .join("")
    : `<tr><td colspan="8">沒有可列印的資料</td></tr>`;
  $("#check-all").checked = true;
  updateSummary();
}

function picked() {
  return [...$$("#rows tr[data-i]")].filter((tr) => tr.querySelector(".pick").checked).map((tr) => +tr.dataset.i);
}

function updateSummary() {
  const sel = picked();
  const sheets = sel.reduce((n, i) => n + batch.labels[i]["數量"], 0);
  $("#summary").textContent = `共 ${batch.labels.length} 筆可列印，已選 ${sel.length} 筆、合計 ${sheets} 張`;
  $("#print-btn").disabled = !sel.length;
}

$("#rows").addEventListener("change", updateSummary);
$("#check-all").addEventListener("change", (e) => {
  $$("#rows .pick").forEach((c) => (c.checked = e.target.checked));
  updateSummary();
});
$("#rows").addEventListener("click", (e) => {
  if (e.target.classList.contains("thumb")) {
    $("#preview-big").src = e.target.src;
    $("#preview-dialog").showModal();
  }
});

// ---------- 列印 ----------
$("#print-btn").addEventListener("click", () => {
  const sel = picked();
  const sheets = sel.reduce((n, i) => n + batch.labels[i]["數量"], 0);
  const where = isDryRun ? "（測試模式：存成檔案，不會真的印）" : "";
  if (!confirm(`確定列印 ${sel.length} 筆、共 ${sheets} 張標籤？${where}`)) return;
  queue = sel;
  queuePos = 0;
  $$("#rows .status").forEach((td) => (td.textContent = ""));
  runQueue();
});
$("#resume-btn").addEventListener("click", runQueue);

function setStatus(i, text, cls) {
  const td = $(`#rows tr[data-i="${i}"] .status`);
  td.textContent = text;
  td.className = "status " + cls;
}

async function runQueue() {
  $("#progress").hidden = false;
  $("#resume-btn").hidden = true;
  $$(".diag-jump").forEach((el) => (el.hidden = true));
  $("#print-btn").disabled = true;
  while (queuePos < queue.length) {
    const i = queue[queuePos];
    setStatus(i, "列印中…", "status-busy");
    progress();
    let r;
    try {
      r = await postJSON("/api/print", { batch_id: batch.batch_id, index: i });
    } catch (err) {
      r = { ok: false, message: err.message };
    }
    if (!r.ok) {
      setStatus(i, "失敗", "status-bad");
      $("#progress-text").textContent = `第 ${queuePos + 1} 筆失敗：${r.message}`;
      $("#resume-btn").hidden = false;
      $("#diag-jump-btn").hidden = false;
      $("#print-btn").disabled = false;
      return;
    }
    setStatus(i, "✓ " + r.message, "status-ok");
    queuePos++;
  }
  progress();
  $("#progress-text").textContent = `完成：${queue.length} 筆已送出`;
  $("#diag-jump-link").hidden = false;
  $("#print-btn").disabled = false;
}

function progress() {
  $("#progress-bar").style.width = (queue.length ? (queuePos / queue.length) * 100 : 0) + "%";
  $("#progress-text").textContent = `${queuePos} / ${queue.length}`;
}

// ---------- 設定 ----------
function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o ? o[k] : undefined), obj);
}
function setPath(obj, path, v) {
  const keys = path.split(".");
  const last = keys.pop();
  keys.reduce((o, k) => (o[k] ??= {}), obj)[last] = v;
}

async function loadSettings() {
  const cfg = await api("/api/settings");
  $$("[data-path]").forEach((el) => {
    const v = getPath(cfg, el.dataset.path);
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = v ?? "";
  });
  showConnectionFields();
  refreshSample();
  loadLogos();
  loadUsbPrinters();
}

// 依連線方式只顯示需要的欄位（USB：印表機名稱；網路：IP、連接埠）
function showConnectionFields() {
  const usb = $("#connection").value === "usb";
  $$(".usb-only").forEach((el) => (el.hidden = !usb));
  $$(".net-only").forEach((el) => (el.hidden = usb));
}
$("#connection").addEventListener("change", showConnectionFields);

async function loadUsbPrinters() {
  const names = await api("/api/usb-printers");
  $("#usb-printers").innerHTML = names.map((n) => `<option value="${esc(n)}">`).join("");
  // 還沒選過時，自動帶入名稱含 GoDEX 的印表機
  const input = $('[data-path="printer.usb_name"]');
  if (!input.value) input.value = names.find((n) => /godex/i.test(n)) ?? "";
}

function collectSettings() {
  const out = {};
  $$("[data-path]").forEach((el) => {
    // 數字欄位空白時送 null（Number("") 會變成 0），由伺服器回報「不能空白」
    let v = el.type === "checkbox" ? el.checked : el.type === "number" ? (el.value === "" ? null : Number(el.value)) : el.value.trim();
    setPath(out, el.dataset.path, v);
  });
  return out;
}

function flash(sel, ok, text) {
  const el = $(sel);
  el.textContent = text;
  el.className = "msg " + (ok ? "ok" : "bad");
}

async function saveSettings() {
  try {
    const r = await postJSON("/api/settings", collectSettings());
    if (!r.ok) {
      flash("#settings-msg", false, "儲存失敗：" + r.message);
      return false;
    }
    isDryRun = r.settings.printer.dry_run;
    $("#mode-badge").hidden = !isDryRun;
    flash("#settings-msg", true, "已儲存");
    refreshSample();
    if (batch) renderRows(); // 重新產生預覽
    return true;
  } catch (err) {
    flash("#settings-msg", false, "儲存失敗：" + err.message);
    return false;
  }
}
$("#save-settings").addEventListener("click", saveSettings);

function refreshSample() {
  $("#sample-preview").src = "/api/preview/sample/0.png?t=" + Date.now();
}

async function printerAction(url, busyText) {
  if (!(await saveSettings())) return;
  flash("#printer-msg", true, busyText);
  try {
    const r = await postJSON(url, {});
    flash("#printer-msg", r.ok, r.message);
  } catch (err) {
    flash("#printer-msg", false, err.message);
  }
}
$("#test-conn").addEventListener("click", () => printerAction("/api/test-connection", "連線中…"));
$("#test-print").addEventListener("click", () => printerAction("/api/test-print", "送出中…"));

async function loadLogos() {
  const names = await api("/api/logos");
  $("#logo-list").innerHTML = names.length
    ? names.map((n) => `<div class="logo-item"><img src="/api/logos/${encodeURIComponent(n)}.png?t=${Date.now()}" alt="">${esc(n)}</div>`).join("")
    : `<span class="hint">尚無 logo</span>`;
}

$("#logo-upload").addEventListener("click", async () => {
  const file = $("#logo-file").files[0];
  const fd = new FormData();
  if (file) fd.append("file", file);
  fd.append("name", $("#logo-name").value.trim());
  const r = await api("/api/logos", { method: "POST", body: fd });
  flash("#logo-msg", r.ok, r.ok ? "已上傳" : r.message);
  if (r.ok) {
    $("#logo-file").value = "";
    $("#logo-name").value = "";
    loadLogos();
    refreshSample();
  }
});

// ---------- 校正 ----------
async function loadCalibration() {
  const cfg = await api("/api/settings");
  const p = cfg.printer, lc = cfg.label;
  $("#cal-conn").textContent = p.connection === "usb" ? `USB「${p.usb_name}」` : `網路 ${p.ip}:${p.port}`;
  const continuous = Number(lc.gap_mm) === 0;
  $(`[name="cal-paper"][value="${continuous ? "continuous" : "gap"}"]`).checked = true;
  $("#cal-gap").value = continuous ? 3 : lc.gap_mm;
  $("#cal-mode").value = p.print_mode || "D";
  $("#cal-head").value = p.head_width_mm;
  $(`[name="cal-method"][value="${p.image_method}"]`).checked = true;
  $("#cal-invert").checked = !!p.invert;
  $("#cal-dark").value = p.darkness;
  $("#cal-speed").value = p.speed;
  showGapField();
  $("#cal-preview").src = "/api/preview/sample/0.png?t=" + Date.now();
}

function showGapField() {
  $("#cal-gap-wrap").hidden = $('[name="cal-paper"]:checked')?.value === "continuous";
}
$$('[name="cal-paper"]').forEach((r) => r.addEventListener("change", showGapField));

// 送出校正動作：顯示進行中 → 結果；回傳伺服器的回應
async function calAction(msg, busyText, url, data = {}) {
  flash(msg, true, busyText);
  try {
    const r = await postJSON(url, data);
    flash(msg, r.ok, r.message);
    return r;
  } catch (err) {
    flash(msg, false, err.message);
    return { ok: false };
  }
}

async function calSave(msg, changes) {
  const r = await calAction(msg, "儲存中…", "/api/settings", changes);
  if (r.ok) flash(msg, true, "已儲存");
  return r.ok;
}

$("#cal-status").addEventListener("click", () => calAction("#cal1-msg", "檢查中…", "/api/test-connection"));
$("#cal-cancel").addEventListener("click", () => calAction("#cal1-msg", "送出中…", "/api/printer-command", { name: "cancel" }));

$("#cal-media").addEventListener("click", () =>
  calAction("#cal2-msg", "寫入中…", "/api/calibrate/media", {
    paper: $('[name="cal-paper"]:checked')?.value,
    gap_mm: Number($("#cal-gap").value),
    print_mode: $("#cal-mode").value,
  })
);
$("#cal-sensor").addEventListener("click", () => {
  if (!confirm("讓印表機自動偵測標籤紙（會送出幾張空白標籤）？只適用間距紙。")) return;
  calAction("#cal2-msg", "偵測中…", "/api/printer-command", { name: "sensor" });
});

$("#cal-ruler").addEventListener("click", async () => {
  if (!(await calSave("#cal3-msg", { printer: { head_width_mm: Number($("#cal-head").value) } }))) return;
  const r = await calAction("#cal3-msg", "列印中…", "/api/calibrate/ruler");
  if (r.ok) flash("#cal3-msg", true, "已送出：看尺規在紙上最左、最右的數字，填到下面兩格");
});
$("#cal-width").addEventListener("click", async () => {
  const r = await calAction("#cal3-msg", "計算中…", "/api/calibrate/width", {
    left: $("#cal-left").value, right: $("#cal-right").value,
  });
  if (r.ok) $("#cal-preview").src = "/api/preview/sample/0.png?t=" + Date.now();
});
$("#cal-verify").addEventListener("click", () =>
  calAction("#cal3-msg", "列印中…", "/api/calibrate/ruler", { verify: true })
);
$("#cal-offset").addEventListener("click", () =>
  calAction("#cal3-msg", "儲存中…", "/api/calibrate/offset", { left: $("#cal-vleft").value })
);

$("#cal-full").addEventListener("click", () => {
  if (!confirm("連續印 3 張測試標籤？")) return;
  calAction("#cal4-msg", "列印中…", "/api/full-test");
});
$("#cal-method-apply").addEventListener("click", () =>
  calSave("#cal4-msg", {
    printer: { image_method: $('[name="cal-method"]:checked')?.value, invert: $("#cal-invert").checked },
  })
);

$("#cal-sample").addEventListener("click", async () => {
  const changes = { printer: { darkness: Number($("#cal-dark").value), speed: Number($("#cal-speed").value) } };
  if (!(await calSave("#cal5-msg", changes))) return;
  calAction("#cal5-msg", "列印中…", "/api/test-print");
});

// ---------- 紀錄 ----------
async function loadLog() {
  const rows = await api("/api/log");
  $("#log-rows").innerHTML = rows.length
    ? rows
        .map(
          (r) => `<tr><td>${esc(r["時間"])}</td><td>${esc(r["型號"])}</td><td>${esc(r["尺寸"])}</td><td>${esc(r["張數"])}</td>
      <td class="${r["結果"] === "成功" ? "status-ok" : "status-bad"}">${esc(r["結果"])}</td><td>${esc(r["訊息"])}</td></tr>`
        )
        .join("")
    : `<tr><td colspan="6">尚無紀錄</td></tr>`;
}

// ---------- 診斷 ----------
async function runDiag() {
  $("#diag-run").disabled = true;
  $("#diag-copy").disabled = true;
  flash("#diag-msg", true, "檢查中…（約需數秒）");
  try {
    const r = await api("/api/diagnostics");
    $("#diag-text").value = r.text;
    $("#diag-copy").disabled = false;
    flash("#diag-msg", true, "報告已產生，請按「複製報告」");
  } catch (err) {
    flash("#diag-msg", false, "產生失敗：" + err.message);
  }
  $("#diag-run").disabled = false;
}

async function copyReport() {
  const box = $("#diag-text");
  let ok = false;
  // 用其他電腦以 http://IP:8000 開啟時不是安全連線，瀏覽器不允許 clipboard API，改用選取＋複製
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(box.value);
      ok = true;
    } catch (err) {}
  }
  if (!ok) {
    box.focus();
    box.select();
    try {
      ok = document.execCommand("copy");
    } catch (err) {}
  }
  flash("#diag-msg", ok, ok ? "已複製，可直接貼給 AI" : "無法自動複製：文字已全選，請按 Ctrl+C（Mac 按 ⌘+C）");
}

$("#diag-run").addEventListener("click", runDiag);
$("#diag-copy").addEventListener("click", copyReport);
$$(".diag-jump").forEach((el) =>
  el.addEventListener("click", (e) => {
    e.preventDefault();
    showTab("diag");
    runDiag();
  })
);

$$("[data-cmd]").forEach((btn) =>
  btn.addEventListener("click", async () => {
    if (!confirm(btn.dataset.confirm)) return;
    flash("#cmd-msg", true, "送出中…");
    try {
      const cmd = btn.dataset.cmd;
      const url = { basic: "/api/basic-test", full: "/api/full-test" }[cmd];
      const r = url ? await postJSON(url, {}) : await postJSON("/api/printer-command", { name: cmd });
      flash("#cmd-msg", r.ok, `${btn.textContent}：${r.message}`);
    } catch (err) {
      flash("#cmd-msg", false, err.message);
    }
  })
);

// ---------- 程式更新 ----------
let currentVersion = "";

function setUpdateBusy(busy) {
  $("#update-check").disabled = busy;
  $("#update-install").disabled = busy;
}

async function checkUpdate(quiet = false) {
  if (!quiet) flash("#update-msg", true, "檢查中…");
  let r;
  try {
    r = await api("/api/update/check");
  } catch (err) {
    r = { ok: false, message: err.message };
  }
  if (!r.ok) {
    if (!quiet) flash("#update-msg", false, r.message);
    return;
  }
  $("#update-install").hidden = !r.can_install;
  $("#update-banner").hidden = !r.newer;
  $("#update-banner").textContent = `有新版本 ${r.latest}，按此更新`;
  const notes = r.newer && r.notes ? `${r.latest}（${r.published}）更新內容：\n${r.notes}` : "";
  $("#update-notes").textContent = notes;
  $("#update-notes").hidden = !notes;
  flash("#update-msg", !r.newer || r.can_install, r.message);
}

async function installUpdate() {
  if ($("#print-btn").disabled && queuePos < queue.length) {
    flash("#update-msg", false, "正在列印，請等列印完成再更新");
    return;
  }
  if (!confirm("下載並安裝新版？程式會自動關閉再重新開啟，約需 1 分鐘。")) return;
  setUpdateBusy(true);
  flash("#update-msg", true, "下載中…（視網路速度約需數十秒）");
  let r;
  try {
    r = await postJSON("/api/update/install", {});
  } catch (err) {
    r = { ok: false, message: err.message };
  }
  if (!r.ok) {
    flash("#update-msg", false, r.message);
    setUpdateBusy(false);
    return;
  }
  flash("#update-msg", true, r.message + "，請稍候…");
  waitForRestart();
}

// 程式重新啟動後版本號會變；變了就重新整理頁面
function waitForRestart() {
  const start = Date.now();
  const timer = setInterval(async () => {
    try {
      const v = await api("/api/version");
      if (v.version && v.version !== currentVersion) {
        clearInterval(timer);
        flash("#update-msg", true, `已更新到 ${v.version}，重新整理中…`);
        setTimeout(() => location.reload(), 1000);
        return;
      }
    } catch (err) {} // 重新啟動中，連不上是正常的
    if (Date.now() - start > 180000) {
      clearInterval(timer);
      flash("#update-msg", false, "等候逾時：請看電腦上「AutoLogo 更新」視窗的訊息，或手動重新開啟程式");
      setUpdateBusy(false);
    }
  }, 2000);
}

$("#update-check").addEventListener("click", () => checkUpdate());
$("#update-install").addEventListener("click", installUpdate);
$("#update-banner").addEventListener("click", () => {
  showTab("diag");
  $("#update-box").scrollIntoView({ behavior: "smooth" });
});

api("/api/version").then((v) => {
  currentVersion = v.version;
  $("#app-version").textContent = "版本 " + v.version;
  $("#update-current").textContent = v.version;
  checkUpdate(true);
});

api("/api/settings").then((cfg) => (isDryRun = cfg.printer.dry_run));

loadWork();
