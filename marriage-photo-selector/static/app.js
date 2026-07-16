const $ = s => document.querySelector(s);
const api = async (url, opts = {}) => {
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || "Something went wrong.");
  return j;
};
const toast = msg => {
  $("#toast").textContent = msg;
  $("#toast").classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => $("#toast").classList.remove("show"), 2600);
};

const state = {
  tab: "photos", filter: "all", page: 1, hasMore: false, loading: false,
  photos: [],            // list currently shown in the Photos grid
  faceMatches: [],       // list currently shown in the Faces grid
  lb: { open: false, list: [], i: 0 },
  refFile: null,
};

/* ---------------- tabs ---------------- */
document.querySelectorAll("nav button").forEach(b => b.onclick = () => showTab(b.dataset.tab));
function showTab(t) {
  state.tab = t;
  document.querySelectorAll("nav button").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === t));
  for (const s of ["photos", "faces", "settings"])
    $("#tab-" + s).style.display = s === t ? "" : "none";
  if (t === "faces") pollIndex();
}

/* ---------------- settings ---------------- */
async function loadSettings() {
  const s = await api("/api/settings");
  $("#inPhotos").value = s.photos_path || "";
  $("#inSelected").value = s.selected_path || "";
  $("#heicNote").textContent = s.heic_supported ? "" :
    "HEIC support is off (pillow-heif not installed) — JPG/PNG/WebP only.";
  pathState("#stPhotos", s.photos_path, s.photos_path_ok);
  pathState("#stSelected", s.selected_path, s.selected_path_ok);

  const banner = $("#banner");
  if (!s.configured) {
    $("#firstRunNote").style.display = "";
    showTab("settings");
    banner.classList.remove("show");
  } else if (!s.photos_path_ok || !s.selected_path_ok) {
    const which = !s.photos_path_ok ? "photos folder" : "selected-photos folder";
    banner.innerHTML = `⚠ Your ${which} isn't reachable right now (drive unplugged or folder moved). ` +
      `Tagging still works, but images and export won't. <a id="goSettings">Open Settings</a>`;
    banner.classList.add("show");
    $("#goSettings").onclick = () => showTab("settings");
  } else {
    banner.classList.remove("show");
  }
  return s;
}
function pathState(el, path, ok) {
  $(el).textContent = !path ? "" : ok ? "✓ Folder found" : "✗ Folder not reachable";
  $(el).className = "pathstate " + (ok ? "ok" : "bad");
}
$("#saveSettings").onclick = async () => {
  const r = await api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ photos_path: $("#inPhotos").value, selected_path: $("#inSelected").value }),
  });
  $("#errPhotos").textContent = r.errors?.photos_path || "";
  $("#errSelected").textContent = r.errors?.selected_path || "";
  if (!r.ok) return;
  $("#firstRunNote").style.display = "none";
  toast("Folders saved. Scanning photos…");
  await loadSettings();
  await rescan();
  showTab("photos");
};

/* ---------------- stats / scan ---------------- */
async function refreshCounts(c) {
  c = c || await api("/api/stats");
  $("#counts").innerHTML =
    `${c.total} photos · <b>${c.selected} selected</b> · ${c.rejected} not selected · ${c.none} untagged`;
  return c;
}
async function rescan() {
  try {
    const c = await api("/api/scan", { method: "POST" });
    await refreshCounts(c);
    resetGrid();
    loadMore();
  } catch (e) { toast(e.message); }
}
$("#rescan").onclick = rescan;

/* ---------------- photo grid ---------------- */
const io = new IntersectionObserver(es => es[0].isIntersecting && loadMore(),
  { rootMargin: "900px" });
io.observe($("#sentinel"));

document.querySelectorAll(".chip").forEach(ch => ch.onclick = () => {
  document.querySelectorAll(".chip").forEach(c => c.classList.remove("active"));
  ch.classList.add("active");
  state.filter = ch.dataset.filter;
  resetGrid(); loadMore();
});
function resetGrid() {
  state.page = 1; state.hasMore = true; state.photos = [];
  $("#grid").innerHTML = ""; $("#gridEmpty").style.display = "none";
}
async function loadMore() {
  if (state.loading || !state.hasMore || state.tab !== "photos") return;
  state.loading = true;
  try {
    const r = await api(`/api/photos?status=${state.filter}&page=${state.page}`);
    state.hasMore = r.has_more; state.page++;
    state.photos.push(...r.photos);
    const frag = document.createDocumentFragment();
    r.photos.forEach(p => frag.appendChild(card(p, state.photos)));
    $("#grid").appendChild(frag);
    if (!state.photos.length) {
      $("#gridEmpty").style.display = "";
      $("#gridEmpty").textContent = state.filter === "all"
        ? "No photos yet. Check the photos folder in Settings, then hit Re-scan folder."
        : "Nothing with this tag yet.";
    }
  } catch (e) { toast(e.message); state.hasMore = false; }
  state.loading = false;
}

function card(p, list) {
  const el = document.createElement("div");
  el.className = "card " + p.status;
  el.dataset.id = p.id;
  el.innerHTML = `
    <img loading="lazy" alt="${p.filename}" title="${p.filename}">
    <span class="tag ${p.status}">${tagLabel(p.status)}</span>
    ${p.score !== undefined ? `<span class="score">${Math.round(p.score * 100)}%</span>` : ""}
    <div class="acts">
      <button class="a-sel">Select</button>
      <button class="a-rej">Reject</button>
      <button class="a-clr">Clear</button>
    </div>`;
  const img = el.querySelector("img");
  img.src = `/api/thumb/${p.id}`;
  img.onload = () => img.classList.add("ready");
  img.onclick = () => openLb(list, list.indexOf(p));
  el.querySelector(".a-sel").onclick = () => setStatus(p, "selected");
  el.querySelector(".a-rej").onclick = () => setStatus(p, "rejected");
  el.querySelector(".a-clr").onclick = () => setStatus(p, "none");
  return el;
}
const tagLabel = s => s === "selected" ? "SELECTED" : s === "rejected" ? "NOT SELECTED" : "";

async function setStatus(p, status) {
  try {
    await api(`/api/photos/${p.id}/status`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    });
  } catch (e) { return toast(e.message); }
  p.status = status;
  // update every card for this photo (it may appear in both grids)
  document.querySelectorAll(`.card[data-id="${p.id}"]`).forEach(el => {
    el.className = "card " + status;
    const t = el.querySelector(".tag");
    t.className = "tag " + status; t.textContent = tagLabel(status);
  });
  [...state.photos, ...state.faceMatches].forEach(o => { if (o.id === p.id) o.status = status; });
  refreshCounts();
  if (state.lb.open) lbTag();
}

/* ---------------- export ---------------- */
$("#export").onclick = async () => {
  try {
    const r = await api("/api/export", { method: "POST" });
    toast(`Copied ${r.copied} photo${r.copied === 1 ? "" : "s"}` +
      (r.already_there ? `, ${r.already_there} already there` : "") +
      (r.missing ? `, ${r.missing} missing on disk` : "") + ".");
  } catch (e) { toast(e.message); }
};

/* ---------------- lightbox ---------------- */
function openLb(list, i) {
  state.lb = { open: true, list, i };
  $("#lb").classList.add("open");
  lbShow();
}
function lbShow() {
  const p = state.lb.list[state.lb.i];
  if (!p) return closeLb();
  $("#lbImg").src = `/api/image/${p.id}`;
  $("#lbName").textContent = `${p.filename}  ·  ${state.lb.i + 1}/${state.lb.list.length}`;
  lbTag();
}
function lbTag() {
  const p = state.lb.list[state.lb.i];
  $("#lbTag").textContent = p.status === "none" ? "no action" : tagLabel(p.status).toLowerCase();
  $("#lbTag").className = p.status;
}
const closeLb = () => { state.lb.open = false; $("#lb").classList.remove("open"); $("#lbImg").src = ""; };
$("#lbClose").onclick = closeLb;
$("#lb").onclick = e => { if (e.target.id === "lb") closeLb(); };

document.addEventListener("keydown", e => {
  if (!state.lb.open) return;
  const p = state.lb.list[state.lb.i];
  if (e.key === "Escape") closeLb();
  else if (e.key === "ArrowRight" && state.lb.i < state.lb.list.length - 1) { state.lb.i++; lbShow(); }
  else if (e.key === "ArrowLeft" && state.lb.i > 0) { state.lb.i--; lbShow(); }
  else if (e.key === "s" || e.key === "S") setStatus(p, "selected");
  else if (e.key === "x" || e.key === "X") setStatus(p, "rejected");
  else if (e.key === "u" || e.key === "U") setStatus(p, "none");
});

/* ---------------- face index ---------------- */
let idxTimer = null;
async function pollIndex() {
  clearTimeout(idxTimer);
  try {
    const s = await api("/api/faces/index/status");
    const pct = s.total_photos ? Math.round(100 * s.indexed_photos / s.total_photos) : 0;
    $("#idxBar").style.width = pct + "%";
    $("#idxText").textContent = s.error ? s.error :
      `${s.indexed_photos}/${s.total_photos} photos indexed · ${s.faces} faces found`;
    $("#idxStart").style.display = s.running ? "none" : "";
    $("#idxStop").style.display = s.running ? "" : "none";
    $("#idxStart").textContent = s.indexed_photos > 0 && s.indexed_photos < s.total_photos
      ? "Resume indexing" : s.indexed_photos >= s.total_photos && s.total_photos > 0
      ? "Index new photos" : "Start indexing";
    if (s.running && state.tab === "faces") idxTimer = setTimeout(pollIndex, 1500);
  } catch (e) { $("#idxText").textContent = e.message; }
}
$("#idxStart").onclick = async () => { await api("/api/faces/index/start", { method: "POST" }); pollIndex(); };
$("#idxStop").onclick = async () => { await api("/api/faces/index/stop", { method: "POST" }); pollIndex(); };

/* ---------------- face search ---------------- */
const drop = $("#drop");
drop.onclick = () => $("#refFile").click();
drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = e => { e.preventDefault(); drop.classList.remove("over");
  if (e.dataTransfer.files[0]) useRef(e.dataTransfer.files[0]); };
$("#refFile").onchange = e => e.target.files[0] && useRef(e.target.files[0]);
$("#thresh").oninput = e => $("#threshVal").textContent = e.target.value;
$("#thresh").onchange = () => state.refFile && faceSearch();
$("#reSearch").onclick = () => faceSearch();

function useRef(file) {
  state.refFile = file;
  $("#refPreview").src = URL.createObjectURL(file);
  $("#refPreview").style.display = "block";
  $("#reSearch").style.display = "";
  faceSearch();
}
async function faceSearch() {
  $("#faceGrid").innerHTML = "";
  $("#faceEmpty").style.display = "";
  $("#faceEmpty").textContent = "Searching…";
  $("#faceTools").style.display = "none";
  const fd = new FormData();
  fd.append("file", state.refFile);
  try {
    const r = await api(`/api/faces/search?threshold=${$("#thresh").value}`, { method: "POST", body: fd });
    state.faceMatches = r.matches;
    if (!r.matches.length) {
      $("#faceEmpty").textContent = r.note ||
        "No matches. Lower the strictness slider, or make sure indexing has run.";
      return;
    }
    $("#faceEmpty").style.display = "none";
    $("#faceTools").style.display = "";
    $("#faceCount").textContent = `${r.matches.length} matching photo${r.matches.length === 1 ? "" : "s"}, best match first`;
    const frag = document.createDocumentFragment();
    r.matches.forEach(m => frag.appendChild(card(m, state.faceMatches)));
    $("#faceGrid").appendChild(frag);
  } catch (e) {
    $("#faceEmpty").textContent = e.message;
  }
}
$("#selAllMatches").onclick = async () => {
  for (const m of state.faceMatches.filter(m => m.status !== "selected")) await setStatus(m, "selected");
  toast("All matches marked as selected.");
};

/* ---------------- boot ---------------- */
(async () => {
  const s = await loadSettings();
  if (s.configured) {
    const c = await refreshCounts();
    if (c.total === 0 && s.photos_path_ok) await rescan(); else loadMore();
  }
})();
