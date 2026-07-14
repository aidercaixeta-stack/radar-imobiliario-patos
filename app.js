
const state = {
  all: [],
  visible: [],
  market: "tradicional",
  favorites: new Set(JSON.parse(localStorage.getItem("radar-favoritos") || "[]")),
  map: null,
  markers: null,
  installPrompt: null,
  filtersCollapsed: false
};

const $ = (id) => document.getElementById(id);
const money = (n) => new Intl.NumberFormat("pt-BR", {style: "currency", currency: "BRL", maximumFractionDigits: 0}).format(n || 0);
const number = (n) => new Intl.NumberFormat("pt-BR").format(n || 0);
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;").replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function median(values) {
  const arr = values.filter(Number.isFinite).sort((a,b) => a-b);
  if (!arr.length) return null;
  const mid = Math.floor(arr.length / 2);
  return arr.length % 2 ? arr[mid] : (arr[mid-1] + arr[mid]) / 2;
}

function initMap() {
  state.map = L.map("map", { zoomControl: true }).setView([-18.5789, -46.5183], 13);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap"
  }).addTo(state.map);
  state.markers = L.layerGroup().addTo(state.map);
}

async function loadData() {
  try {
    const [dataResponse, metaResponse] = await Promise.all([
      fetch("data/imoveis.json", { cache: "no-store" }),
      fetch("data/meta.json", { cache: "no-store" })
    ]);
    if (!dataResponse.ok) throw new Error("Falha ao carregar imóveis");
    state.all = await dataResponse.json();
    const meta = metaResponse.ok ? await metaResponse.json() : {};
    $("lastUpdate").textContent = meta.updated_at
      ? `Atualizado em ${new Date(meta.updated_at).toLocaleString("pt-BR")}`
      : "Base inicial carregada";
    populateFilters();
    applyFilters();
  } catch (error) {
    $("lastUpdate").textContent = "Não foi possível carregar a base";
    $("cards").innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}

function populateFilters() {
  const bairros = [...new Set(state.all.map(x => x.bairro).filter(Boolean))].sort();
  const tipos = [...new Set(state.all.map(x => x.tipo).filter(Boolean))].sort();
  $("bairro").innerHTML = `<option value="">Todos</option>` + bairros.map(x => `<option>${escapeHtml(x)}</option>`).join("");
  $("tipo").innerHTML = `<option value="">Todos</option>` + tipos.map(x => `<option>${escapeHtml(x)}</option>`).join("");
}

function baseMarketFilter(item) {
  if (state.market === "favoritos") return state.favorites.has(item.id);
  return item.mercado === state.market;
}

function applyFilters() {
  const bairro = $("bairro").value;
  const tipo = $("tipo").value;
  const destaque = $("destaque").value;
  const maxPrice = Number($("maxPrice").value || 0);
  const minArea = Number($("minArea").value || 0);

  let list = state.all.filter(baseMarketFilter).filter(item => {
    if (bairro && item.bairro !== bairro) return false;
    if (tipo && item.tipo !== tipo) return false;
    if (maxPrice && item.preco > maxPrice) return false;
    if (minArea && (item.area_construida || item.area_terreno || 0) < minArea) return false;
    if (destaque === "oportunidade" && item.nota_oportunidade < 80) return false;
    if (destaque === "novo" && !item.novo) return false;
    if (destaque === "reduzido" && !item.preco_reduzido) return false;
    return true;
  });

  const order = $("order").value;
  list.sort((a,b) => {
    if (order === "newest") return new Date(b.primeira_captura) - new Date(a.primeira_captura);
    if (order === "priceAsc") return a.preco - b.preco;
    if (order === "priceDesc") return b.preco - a.preco;
    return b.nota_oportunidade - a.nota_oportunidade;
  });

  state.visible = list;
  renderAll();
}

function renderAll() {
  renderKpis();
  renderCards();
  renderMap();
  const titles = {
    tradicional: "Painel do mercado tradicional",
    leilao: "Painel de leilões",
    favoritos: "Painel de favoritos"
  };
  $("heroTitle").textContent = titles[state.market];
  $("resultTitle").textContent = state.market === "leilao" ? "Leilões encontrados" : state.market === "favoritos" ? "Favoritos encontrados" : "Imóveis encontrados";
  $("resultCount").textContent = number(state.visible.length);
  $("bestScore").textContent = state.visible.length ? Math.max(...state.visible.map(x => x.nota_oportunidade)) : "—";
}

function renderKpis() {
  const valuesM2 = state.visible.map(x => {
    const area = x.area_construida || x.area_terreno;
    return area ? x.preco / area : NaN;
  });
  const total = state.visible.length;
  const novos = state.visible.filter(x => x.novo).length;
  const reduzidos = state.visible.filter(x => x.preco_reduzido).length;
  const opp = state.visible.filter(x => x.nota_oportunidade >= 80).length;
  $("kpiTotal").textContent = number(total);
  $("kpiNew").textContent = number(novos);
  $("kpiReduced").textContent = number(reduzidos);
  $("kpiOpp").textContent = number(opp);
  $("metricNew").textContent = number(novos);
  $("metricReduced").textContent = number(reduzidos);
  $("metricOpp").textContent = number(opp);
  const med = median(valuesM2);
  $("kpiM2").textContent = med ? money(med) : "—";
}

function historyHtml(item) {
  const entries = Array.isArray(item.historico_precos) ? item.historico_precos.slice(-3) : [];
  if (!entries.length) return "";
  return `<div class="history">${entries.map(h => `
    <div>${escapeHtml(new Date(h.data).toLocaleDateString("pt-BR"))}<strong>${money(h.preco)}</strong></div>
  `).join("")}</div>`;
}

function cardPhotoTag(item) {
  if (item.mercado === 'leilao') return item.modalidade_leilao || 'Leilão';
  if (item.nota_oportunidade >= 85) return 'Alta oportunidade';
  if (item.preco_reduzido) return 'Preço reduzido';
  return 'Mercado tradicional';
}

function cardHtml(item) {
  const area = item.area_construida || item.area_terreno || 0;
  const perM2 = area ? item.preco / area : null;
  const isFavorite = state.favorites.has(item.id);
  return `
    <article class="card" data-id="${escapeHtml(item.id)}">
      <div class="card-photo">
        <span class="photo-tag">${escapeHtml(cardPhotoTag(item))}</span>
        <div class="score-bubble" title="Nota de oportunidade">${number(item.nota_oportunidade)}</div>
      </div>
      <div class="card-body">
        <h4>${escapeHtml(item.titulo)}</h4>
        <div class="subtitle-row">${escapeHtml(item.bairro)} • ${escapeHtml(item.tipo)}</div>

        <div class="price-row">
          <div class="price">${money(item.preco)}</div>
          <div class="per-m2">${perM2 ? `${money(perM2)}/m²` : "área não informada"}</div>
        </div>

        <div class="features">
          ${item.area_construida ? `<span class="feature">${number(item.area_construida)} m² construídos</span>` : ""}
          ${item.area_terreno ? `<span class="feature">${number(item.area_terreno)} m² terreno</span>` : ""}
          ${item.quartos ? `<span class="feature">${number(item.quartos)} quartos</span>` : ""}
          ${item.vagas ? `<span class="feature">${number(item.vagas)} vagas</span>` : ""}
        </div>

        <div class="badges">
          ${item.novo ? `<span class="badge new">Novo anúncio</span>` : ""}
          ${item.preco_reduzido ? `<span class="badge down">Preço reduzido</span>` : ""}
          ${item.mercado === "leilao" ? `<span class="badge auction">${escapeHtml(item.modalidade_leilao || "Leilão")}</span>` : ""}
          ${item.ocupado ? `<span class="badge auction">Ocupado</span>` : ""}
        </div>

        ${historyHtml(item)}

        <div class="card-footer">
          <span class="source">Fonte: ${escapeHtml(item.fonte)} • confiança ${number(item.confianca)}%</span>
          <button class="favorite ${isFavorite ? "active" : ""}" data-favorite="${escapeHtml(item.id)}">
            ${isFavorite ? "★ Favorito" : "☆ Favoritar"}
          </button>
        </div>
      </div>
    </article>
  `;
}

function renderCards() {
  $("cards").innerHTML = state.visible.length ? state.visible.map(cardHtml).join("") : `<div class="empty">Nenhum imóvel encontrado com esses filtros.</div>`;
  document.querySelectorAll("[data-favorite]").forEach(button => {
    button.addEventListener("click", () => toggleFavorite(button.dataset.favorite));
  });
}

function markerColor(item) {
  if (item.mercado === "leilao") return "#d68a2c";
  if (item.nota_oportunidade >= 85) return "#2f9f71";
  return "#4f8fd8";
}

function renderMap() {
  state.markers.clearLayers();
  const bounds = [];
  state.visible.forEach(item => {
    if (!Number.isFinite(item.latitude) || !Number.isFinite(item.longitude)) return;
    const marker = L.circleMarker([item.latitude, item.longitude], {
      radius: 9,
      color: "#ffffff",
      weight: 2,
      fillColor: markerColor(item),
      fillOpacity: .92
    });
    marker.bindPopup(`
      <strong>${escapeHtml(item.titulo)}</strong><br>
      ${escapeHtml(item.bairro)}<br>
      ${money(item.preco)}<br>
      Nota: ${number(item.nota_oportunidade)}
    `);
    marker.addTo(state.markers);
    bounds.push([item.latitude, item.longitude]);
  });
  $("mapCount").textContent = `${number(bounds.length)} pontos`;
  if (bounds.length === 1) state.map.setView(bounds[0], 15);
  if (bounds.length > 1) state.map.fitBounds(bounds, { padding: [28,28] });
}

function toggleFavorite(id) {
  if (state.favorites.has(id)) state.favorites.delete(id);
  else state.favorites.add(id);
  localStorage.setItem("radar-favoritos", JSON.stringify([...state.favorites]));
  applyFilters();
}

function clearFilters() {
  ["bairro","tipo","destaque","order"].forEach(id => $(id).selectedIndex = 0);
  $("maxPrice").value = "";
  $("minArea").value = "";
  applyFilters();
}

function toggleFilters() {
  state.filtersCollapsed = !state.filtersCollapsed;
  $("filtersBody").classList.toggle('collapsed', state.filtersCollapsed);
  $("toggleFilters").textContent = state.filtersCollapsed ? 'Expandir' : 'Recolher';
}

document.querySelectorAll(".nav-chip").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".nav-chip").forEach(x => x.classList.remove("active"));
    tab.classList.add("active");
    state.market = tab.dataset.market;
    applyFilters();
  });
});

["bairro","tipo","destaque","maxPrice","minArea","order"].forEach(id => {
  $(id).addEventListener(id === "maxPrice" || id === "minArea" ? "input" : "change", applyFilters);
});
$("clearFilters").addEventListener("click", clearFilters);
$("toggleFilters").addEventListener("click", toggleFilters);

window.addEventListener("beforeinstallprompt", event => {
  event.preventDefault();
  state.installPrompt = event;
  $("installBtn").hidden = false;
});
$("installBtn").addEventListener("click", async () => {
  if (!state.installPrompt) return;
  state.installPrompt.prompt();
  await state.installPrompt.userChoice;
  state.installPrompt = null;
  $("installBtn").hidden = true;
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js"));
}

initMap();
loadData();
