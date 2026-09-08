const titles = {
  overview: "运行总览",
  sources: "数据来源",
  tasks: "采集任务",
  trends: "多日趋势",
  visibility: "检索采样",
  quality: "证据与质量",
};

const samples = {
  "AI 笔记工具": {
    "Brave sample": {
      owned: 3,
      position: 1,
      brands: [["OmniNote", 3], ["AtlasNote", 4], ["PaperFlow", 2]],
      results: [
        [1, "OmniNote — Local-first AI notes", "owned", "accepted"],
        [2, "AtlasNote workspace overview", "competitor", "accepted"],
        [3, "Seven practical AI note workflows", "unclassified", "accepted"],
        [4, "PaperFlow knowledge assistant", "competitor", "accepted"],
        [5, "OmniNote evidence-linked search", "owned", "accepted"],
      ],
    },
    "DuckDuckGo sample": {
      owned: 2,
      position: 2,
      brands: [["OmniNote", 2], ["AtlasNote", 3], ["PaperFlow", 3]],
      results: [
        [1, "Independent guide to AI note tools", "unclassified", "accepted"],
        [2, "OmniNote product documentation", "owned", "accepted"],
        [3, "PaperFlow for research teams", "competitor", "accepted"],
        [4, "AtlasNote AI workspace", "competitor", "accepted"],
        [5, "How to compare note assistants", "unclassified", "accepted"],
      ],
    },
  },
  "团队知识库": {
    "Brave sample": {
      owned: 2,
      position: 3,
      brands: [["OmniNote", 2], ["AtlasNote", 5], ["PaperFlow", 1]],
      results: [
        [1, "Team knowledge base patterns", "unclassified", "accepted"],
        [2, "AtlasNote shared workspace", "competitor", "accepted"],
        [3, "OmniNote for small product teams", "owned", "accepted"],
        [4, "Knowledge operations handbook", "unclassified", "accepted"],
        [5, "AtlasNote permissions guide", "competitor", "accepted"],
      ],
    },
    "DuckDuckGo sample": {
      owned: 1,
      position: 4,
      brands: [["OmniNote", 1], ["AtlasNote", 4], ["PaperFlow", 2]],
      results: [
        [1, "Building a searchable team wiki", "unclassified", "accepted"],
        [2, "AtlasNote team hub", "competitor", "accepted"],
        [3, "PaperFlow workspace templates", "competitor", "accepted"],
        [4, "OmniNote shared knowledge demo", "owned", "accepted"],
        [5, "Team documentation systems", "unclassified", "accepted"],
      ],
    },
  },
  "Markdown 知识库": {
    "Brave sample": {
      owned: 4,
      position: 1,
      brands: [["OmniNote", 4], ["AtlasNote", 1], ["PaperFlow", 2]],
      results: [
        [1, "OmniNote Markdown knowledge graph", "owned", "accepted"],
        [2, "Local Markdown search workflows", "unclassified", "accepted"],
        [3, "OmniNote source-linked answers", "owned", "accepted"],
        [4, "PaperFlow Markdown import", "competitor", "accepted"],
        [5, "Plain-text knowledge base guide", "unclassified", "accepted"],
      ],
    },
    "DuckDuckGo sample": {
      owned: 3,
      position: 1,
      brands: [["OmniNote", 3], ["AtlasNote", 2], ["PaperFlow", 2]],
      results: [
        [1, "OmniNote local Markdown workspace", "owned", "accepted"],
        [2, "Markdown knowledge base comparison", "unclassified", "accepted"],
        [3, "AtlasNote plain-text export", "competitor", "accepted"],
        [4, "OmniNote retrieval evidence", "owned", "accepted"],
        [5, "PaperFlow Markdown sync", "competitor", "accepted"],
      ],
    },
  },
};

const trends = {
  7: ["M60 220 L193 205 L326 185 L460 172 L593 140 L726 118 L860 88", "M60 160 L193 153 L326 166 L460 141 L593 148 L726 130 L860 138"],
  30: ["M60 234 L193 216 L326 220 L460 178 L593 166 L726 125 L860 104", "M60 146 L193 160 L326 151 L460 164 L593 142 L726 150 L860 132"],
  90: ["M60 245 L193 230 L326 208 L460 196 L593 170 L726 141 L860 112", "M60 135 L193 143 L326 154 L460 148 L593 158 L726 146 L860 140"],
};

const navButtons = [...document.querySelectorAll("[data-view]")];
const panels = [...document.querySelectorAll("[data-view-panel]")];
const sidebar = document.getElementById("sidebar");
const menuButton = document.getElementById("menuButton");
const pageTitle = document.getElementById("pageTitle");
const toast = document.getElementById("toast");
let toastTimer;

function showView(name) {
  if (!titles[name]) return;
  navButtons.forEach((button) => button.classList.toggle("is-active", button.dataset.view === name));
  panels.forEach((panel) => {
    const active = panel.dataset.viewPanel === name;
    panel.hidden = !active;
    panel.classList.toggle("is-visible", active);
  });
  pageTitle.textContent = titles[name];
  history.replaceState(null, "", `#${name}`);
  sidebar.classList.remove("is-open");
  menuButton.setAttribute("aria-expanded", "false");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("is-visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 1800);
}

function renderVisibility() {
  const keyword = document.getElementById("keywordSelect").value;
  const engine = document.getElementById("engineSelect").value;
  const sample = samples[keyword][engine];
  document.getElementById("sampleDenominator").textContent = "10";
  document.getElementById("ownedHits").textContent = `${sample.owned} / 10`;
  document.getElementById("ownedShare").textContent = `${sample.owned * 10}%`;
  document.getElementById("ownedPosition").textContent = `首次位置 #${sample.position}`;

  const bars = document.getElementById("entityBars");
  bars.replaceChildren();
  sample.brands.forEach(([name, count]) => {
    const item = document.createElement("div");
    item.className = "bar-item";
    const label = document.createElement("div");
    label.className = "bar-item__label";
    const title = document.createElement("span");
    title.textContent = name;
    const value = document.createElement("strong");
    value.textContent = `${count} / 10`;
    label.append(title, value);
    const track = document.createElement("div");
    track.className = "bar-item__track";
    const fill = document.createElement("div");
    fill.className = "bar-item__fill";
    fill.style.width = `${count * 10}%`;
    track.append(fill);
    item.append(label, track);
    bars.append(item);
  });

  const rows = document.getElementById("evidenceRows");
  rows.replaceChildren();
  sample.results.forEach((result) => {
    const row = document.createElement("tr");
    result.forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 0) cell.textContent = `#${value}`;
      else if (index === 3) {
        const chip = document.createElement("span");
        chip.className = "chip chip--success";
        chip.textContent = value;
        cell.append(chip);
      } else cell.textContent = value;
      row.append(cell);
    });
    rows.append(row);
  });
}

navButtons.forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
document.querySelectorAll("[data-jump]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.jump)));
document.getElementById("keywordSelect").addEventListener("change", renderVisibility);
document.getElementById("engineSelect").addEventListener("change", renderVisibility);
document.querySelectorAll("[data-range]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-range]").forEach((item) => item.classList.toggle("is-active", item === button));
    const [owned, competitor] = trends[button.dataset.range];
    document.getElementById("ownedTrend").setAttribute("d", owned);
    document.getElementById("competitorTrend").setAttribute("d", competitor);
  });
});

menuButton.addEventListener("click", () => {
  const open = sidebar.classList.toggle("is-open");
  menuButton.setAttribute("aria-expanded", String(open));
});

document.getElementById("resetDemo").addEventListener("click", () => {
  document.getElementById("keywordSelect").selectedIndex = 0;
  document.getElementById("engineSelect").selectedIndex = 0;
  renderVisibility();
  showView("overview");
  showToast("演示状态已重置 · 未发送任何请求");
});

renderVisibility();
showView(location.hash.slice(1) || "overview");
