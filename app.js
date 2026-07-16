const state = {
  all: [],
  visible: [],
  market: "tradicional",
  favorites: new Set(JSON.parse(localStorage.getItem("radar-favoritos") || "[]")),
  map: null,
  markers: null,
  installPrompt: null,
  filtersCollapsed: false,
  meta: {},
  auctionMeta: {}
};

const $ = (id) => document.getElementById(id);
const money = (n) => new Intl.NumberFormat("pt-BR", {style: "currency", currency: "BRL", maximumFractionDigits: 0}).format(n || 0);
const number = (n) => new Intl.NumberFormat("pt-BR").format(n || 0);
const percent = (n) => Number.isFinite(Number(n)) ? `${number(Number(n))}%` : "—";
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

function parseDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function auctionIsClosed(item) {
  if (item.mercado !== "leilao") return false;
  const end = parseDate(item.data_encerramento);
  return end ? end.getTime() < Date.now() : item.status === "encerrado";
}

function endsWithinDays(item, days = 7) {
  const end = parseDate(item.data_encerramento);
  if (!end || auctionIsClosed(item)) return false;
  const diff = end.getTime() - Date.now();
  return diff >= 0 && diff <= days * 86400000;
}

function normalizeItem(raw) {
  const market = raw.mercado || "tradicional";
  const city = raw.cidade || (market === "tradicional" ? "Patos de Minas" : null);
  const uf = raw.uf || (market === "tradicional" ? "MG" : null);
  const sourceOffers = Array.isArray(raw.ofertas) && raw.ofertas.length
    ? raw.ofertas
    : (raw.url_fonte ? [{
        fonte: raw.fonte,
        preco: raw.preco,
        url_fonte: raw.url_fonte,
        id_origem: raw.id
      }] : []);

  return {
    area_construida: null,
    area_terreno: null,
    quartos: null,
    banheiros: null,
    vagas: null,
    telefone_anunciante: null,
    confianca: null,
    nota_oportunidade: 50,
    novo: false,
    preco_reduzido: false,
    latitude: null,
    longitude: null,
    localizacao_precisao: "nao_localizado",
    historico_precos: [],
    status: "ativo",
    ...raw,
    mercado: market,
    cidade: city,
    uf,
    ofertas: sourceOffers,
    fontes_encontradas: raw.fontes_encontradas || (raw.fonte ? [raw.fonte] : []),
    quantidade_ofertas: raw.quantidade_ofertas || sourceOffers.length || 1,
    quantidade_fontes: raw.quantidade_fontes || new Set(sourceOffers.map(x => x.fonte).filter(Boolean)).size || 1
  };
}

function initMap() {
  state.map = L.map("map", { zoomControl: true }).setView([-18.5789, -46.5183], 13);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap"
  }).addTo(state.map);
  state.markers = L.layerGroup().addTo(state.map);
}

async function fetchOptional(url, fallback) {
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) return fallback;
    return await response.json();
  } catch {
    return fallback;
  }
}

async function loadAuctionData() {
  const index = await fetchOptional("data/fontes/leilaoimovel_index.json", null);
  if (index && Array.isArray(index.files) && index.files.length) {
    const parts = await Promise.all(index.files.map(file => fetchOptional(file.path, [])));
    return parts.flat();
  }
  return fetchOptional("data/fontes/leilaoimovel.json", []);
}

async function loadData() {
  try {
    const [base, auctions, meta, auctionMeta] = await Promise.all([
      fetchOptional("data/imoveis.json", []),
      loadAuctionData(),
      fetchOptional("data/meta.json", {}),
      fetchOptional("data/fontes/leilaoimovel_meta.json", {})
    ]);

    const merged = [...base, ...auctions].map(normalizeItem);
    const seen = new Set();
    state.all = merged.filter(item => {
      const key = item.url_fonte || item.id;
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    state.meta = meta;
    state.auctionMeta = auctionMeta;

    const dates = [meta.updated_at, auctionMeta.updated_at]
      .map(parseDate).filter(Boolean).sort((a,b) => b-a);
    $("lastUpdate").textContent = dates.length
      ? `Atualizado em ${dates[0].toLocaleString("pt-BR")}`
      : "Base inicial carregada";
    $("dataStatus").textContent = "Dados reais multifuente";

    configureMarketUi();
    populateFilters();
    applyFilters();
  } catch (error) {
    $("lastUpdate").textContent = "Não foi possível carregar a base";
    $("cards").innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  }
}

function marketItems() {
  if (state.market === "favoritos") {
    return state.all.filter(x => state.favorites.has(x.id));
  }
  return state.all.filter(x => x.mercado === state.market);
}

function optionHtml(values) {
  return `<option value="">Todos</option>` + values
    .filter(Boolean)
    .sort((a,b) => String(a).localeCompare(String(b), "pt-BR"))
    .map(x => `<option value="${escapeHtml(x)}">${escapeHtml(x)}</option>`).join("");
}

function populateFilters() {
  const current = {
    uf: $("uf").value,
    cidade: $("cidade").value,
    bairro: $("bairro").value,
    tipo: $("tipo").value
  };
  const items = marketItems();
  const cityItems = current.uf ? items.filter(x => x.uf === current.uf) : items;
  const bairroItems = cityItems.filter(x => !current.cidade || x.cidade === current.cidade);

  $("uf").innerHTML = optionHtml([...new Set(items.map(x => x.uf))]);
  $("cidade").innerHTML = optionHtml([...new Set(cityItems.map(x => x.cidade))]);
  $("bairro").innerHTML = optionHtml([...new Set(bairroItems.map(x => x.bairro))]);
  $("tipo").innerHTML = optionHtml([...new Set(items.map(x => x.tipo))]);

  Object.entries(current).forEach(([id, value]) => {
    if ([...$(id).options].some(option => option.value === value)) $(id).value = value;
  });
}

function configureMarketUi() {
  const isAuction = state.market === "leilao";
  $("auctionSituationLabel").hidden = !isAuction;
  $("bairroLabel").hidden = isAuction;
  $("minAreaLabel").hidden = isAuction;

  $("destaque").innerHTML = isAuction
    ? `<option value="">Todos</option><option value="desconto40">Desconto de 40% ou mais</option><option value="encerra7">Encerra em até 7 dias</option><option value="novo">Novos anúncios</option>`
    : `<option value="">Todos</option><option value="oportunidade">Oportunidades</option><option value="novo">Novos anúncios</option><option value="reduzido">Preço reduzido</option>`;

  const titles = {
    tradicional: "Painel do mercado tradicional",
    leilao: "Painel nacional de leilões",
    favoritos: "Painel de favoritos"
  };
  $("heroTitle").textContent = titles[state.market];
  $("heroSubtitle").textContent = isAuction
    ? "Ofertas de leilão separadas do mercado tradicional, com desconto, encerramento e acesso direto à fonte."
    : "Mapa, cards visuais, indicadores e filtros em uma interface limpa e profissional.";

  $("mainMetricLabel").textContent = isAuction ? "Desconto mediano" : "Mediana R$/m²";
  $("mainMetricHelp").textContent = isAuction
    ? "Mediana do desconto informado pela fonte entre os leilões visíveis."
    : "Referência do grupo visível, sem misturar leilões ou imóveis não comparáveis.";
  $("metricNewLabel").textContent = isAuction ? "Leilões ativos" : "Novos anúncios";
  $("metricReducedLabel").textContent = isAuction ? "Encerram em 7 dias" : "Quedas de preço";
  $("metricOppLabel").textContent = isAuction ? "Desconto ≥ 40%" : "Oportunidades";
  $("bestScoreLabel").textContent = isAuction ? "Maior desconto visível" : "Melhor nota visível";
}

function baseMarketFilter(item) {
  if (state.market === "favoritos") return state.favorites.has(item.id);
  return item.mercado === state.market;
}

function applyFilters() {
  const uf = $("uf").value;
  const cidade = $("cidade").value;
  const bairro = $("bairro").value;
  const tipo = $("tipo").value;
  const destaque = $("destaque").value;
  const situacao = $("situacao").value;
  const maxPrice = Number($("maxPrice").value || 0);
  const minArea = Number($("minArea").value || 0);

  let list = state.all.filter(baseMarketFilter).filter(item => {
    if (uf && item.uf !== uf) return false;
    if (cidade && item.cidade !== cidade) return false;
    if (bairro && item.bairro !== bairro) return false;
    if (tipo && item.tipo !== tipo) return false;
    if (maxPrice && item.preco > maxPrice) return false;
    if (minArea && (item.area_construida || item.area_terreno || 0) < minArea) return false;

    if (item.mercado === "leilao") {
      if (situacao === "ativos" && auctionIsClosed(item)) return false;
      if (situacao === "encerrados" && !auctionIsClosed(item)) return false;
      if (destaque === "desconto40" && Number(item.desconto_percentual || 0) < 40) return false;
      if (destaque === "encerra7" && !endsWithinDays(item, 7)) return false;
      if (destaque === "novo" && !item.novo) return false;
    } else {
      if (destaque === "oportunidade" && item.nota_oportunidade < 80) return false;
      if (destaque === "novo" && !item.novo) return false;
      if (destaque === "reduzido" && !item.preco_reduzido) return false;
    }
    return true;
  });

  const order = $("order").value;
  list.sort((a,b) => {
    if (order === "newest") return new Date(b.primeira_captura || 0) - new Date(a.primeira_captura || 0);
    if (order === "priceAsc") return (a.preco || 0) - (b.preco || 0);
    if (order === "priceDesc") return (b.preco || 0) - (a.preco || 0);
    if (order === "ending") return (parseDate(a.data_encerramento)?.getTime() || Infinity) - (parseDate(b.data_encerramento)?.getTime() || Infinity);
    if (state.market === "leilao") return Number(b.desconto_percentual || 0) - Number(a.desconto_percentual || 0);
    return (b.nota_oportunidade || 0) - (a.nota_oportunidade || 0);
  });

  state.visible = list;
  renderAll();
}

function renderAll() {
  renderKpis();
  renderCards();
  renderMap();
  $("resultTitle").textContent = state.market === "leilao" ? "Leilões encontrados" : state.market === "favoritos" ? "Favoritos encontrados" : "Imóveis encontrados";
  $("resultCount").textContent = number(state.visible.length);
  if (state.market === "leilao") {
    const maxDiscount = Math.max(0, ...state.visible.map(x => Number(x.desconto_percentual || 0)));
    $("bestScore").textContent = maxDiscount ? percent(maxDiscount) : "—";
  } else {
    $("bestScore").textContent = state.visible.length ? Math.max(...state.visible.map(x => Number(x.nota_oportunidade || 0))) : "—";
  }
}

function renderKpis() {
  const total = state.visible.length;
  const isAuction = state.market === "leilao";

  if (isAuction) {
    const discounts = state.visible.map(x => Number(x.desconto_percentual)).filter(Number.isFinite);
    const active = state.visible.filter(x => !auctionIsClosed(x)).length;
    const ending = state.visible.filter(x => endsWithinDays(x, 7)).length;
    const highDiscount = state.visible.filter(x => Number(x.desconto_percentual || 0) >= 40).length;
    $("kpiM2").textContent = discounts.length ? percent(median(discounts)) : "—";
    $("metricNew").textContent = number(active);
    $("metricReduced").textContent = number(ending);
    $("metricOpp").textContent = number(highDiscount);
    $("kpiNew").textContent = number(active);
    $("kpiReduced").textContent = number(ending);
    $("kpiOpp").textContent = number(highDiscount);
  } else {
    const valuesM2 = state.visible.map(x => {
      const area = x.area_construida || x.area_terreno;
      return area ? x.preco / area : NaN;
    });
    const novos = state.visible.filter(x => x.novo).length;
    const reduzidos = state.visible.filter(x => x.preco_reduzido).length;
    const opp = state.visible.filter(x => x.nota_oportunidade >= 80).length;
    const med = median(valuesM2);
    $("kpiM2").textContent = med ? money(med) : "—";
    $("metricNew").textContent = number(novos);
    $("metricReduced").textContent = number(reduzidos);
    $("metricOpp").textContent = number(opp);
    $("kpiNew").textContent = number(novos);
    $("kpiReduced").textContent = number(reduzidos);
    $("kpiOpp").textContent = number(opp);
  }
  $("kpiTotal").textContent = number(total);
}

function historyHtml(item) {
  const entries = Array.isArray(item.historico_precos) ? item.historico_precos.slice(-3) : [];
  if (!entries.length) return "";
  return `<div class="history">${entries.map(h => `
    <div>${escapeHtml(new Date(h.data).toLocaleDateString("pt-BR"))}<strong>${money(h.preco)}</strong></div>
  `).join("")}</div>`;
}

function cardPhotoTag(item) {
  if (item.mercado === "leilao") return item.modalidade_leilao || "Leilão";
  if (item.nota_oportunidade >= 85) return "Alta oportunidade";
  if (item.preco_reduzido) return "Preço reduzido";
  return "Mercado tradicional";
}

function displayLocation(item) {
  if (item.mercado === "leilao") {
    return [item.cidade, item.uf].filter(Boolean).join(" / ") || item.bairro || "Local não informado";
  }
  return item.bairro || [item.cidade, item.uf].filter(Boolean).join(" / ") || "Local não informado";
}

function formatEndDate(value) {
  const date = parseDate(value);
  return date ? date.toLocaleString("pt-BR", {dateStyle: "short", timeStyle: "short"}) : "Não informado";
}

function auctionFactsHtml(item) {
  if (item.mercado !== "leilao") return "";
  return `<div class="auction-facts">
    <div><span>Valor de avaliação</span><strong>${item.valor_avaliacao ? money(item.valor_avaliacao) : "—"}</strong></div>
    <div><span>Desconto informado</span><strong>${percent(Number(item.desconto_percentual))}</strong></div>
    <div><span>Encerramento</span><strong>${escapeHtml(formatEndDate(item.data_encerramento))}</strong></div>
  </div>`;
}

function cardHtml(item) {
  const area = item.area_construida || item.area_terreno || 0;
  const perM2 = area ? item.preco / area : null;
  const isFavorite = state.favorites.has(item.id);
  const closed = auctionIsClosed(item);
  const scoreText = item.mercado === "leilao" && Number.isFinite(Number(item.desconto_percentual))
    ? percent(Number(item.desconto_percentual))
    : number(item.nota_oportunidade);
  const scoreTitle = item.mercado === "leilao" ? "Desconto informado" : "Nota de oportunidade";
  const confidence = Number.isFinite(Number(item.confianca)) ? ` • confiança ${number(item.confianca)}%` : "";

  return `
    <article class="card" data-id="${escapeHtml(item.id)}">
      <div class="card-photo">
        <span class="photo-tag">${escapeHtml(cardPhotoTag(item))}</span>
        <div class="score-bubble" title="${escapeHtml(scoreTitle)}">${escapeHtml(scoreText)}</div>
      </div>
      <div class="card-body">
        <h4>${escapeHtml(item.titulo)}</h4>
        <div class="subtitle-row">${escapeHtml(displayLocation(item))} • ${escapeHtml(item.tipo || "Imóvel")}</div>

        <div class="price-row">
          <div class="price">${money(item.preco)}</div>
          <div class="per-m2">${item.mercado === "leilao" ? escapeHtml(item.modalidade_leilao || "Leilão") : perM2 ? `${money(perM2)}/m²` : "área não informada"}</div>
        </div>

        ${auctionFactsHtml(item)}

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
          ${item.mercado === "leilao" ? `<span class="badge ${closed ? "closed" : "active-auction"}">${closed ? "Encerrado" : "Ativo"}</span>` : ""}
          ${item.ocupado ? `<span class="badge auction">Ocupado</span>` : ""}
        </div>

        ${historyHtml(item)}

        <div class="card-footer">
          <span class="source">Fonte: ${escapeHtml(item.fonte || "Fonte original")}${confidence}</span>
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
      ${escapeHtml(displayLocation(item))}<br>
      ${money(item.preco)}<br>
      ${item.mercado === "leilao" ? `Desconto: ${percent(Number(item.desconto_percentual))}` : `Nota: ${number(item.nota_oportunidade)}`}
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
  ["uf","cidade","bairro","tipo","destaque","order"].forEach(id => $(id).selectedIndex = 0);
  $("situacao").value = "ativos";
  $("maxPrice").value = "";
  $("minArea").value = "";
  applyFilters();
}

function toggleFilters() {
  state.filtersCollapsed = !state.filtersCollapsed;
  $("filtersBody").classList.toggle("collapsed", state.filtersCollapsed);
  $("toggleFilters").textContent = state.filtersCollapsed ? "Expandir" : "Recolher";
}

document.querySelectorAll(".nav-chip").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".nav-chip").forEach(x => x.classList.remove("active"));
    tab.classList.add("active");
    state.market = tab.dataset.market;
    ["uf","cidade","bairro","tipo","destaque"].forEach(id => $(id).value = "");
    $("situacao").value = "ativos";
    $("order").value = state.market === "leilao" ? "score" : "score";
    configureMarketUi();
    populateFilters();
    applyFilters();
  });
});

["uf","cidade","bairro","tipo","destaque","situacao","maxPrice","minArea","order"].forEach(id => {
  $(id).addEventListener(id === "maxPrice" || id === "minArea" ? "input" : "change", () => {
    if (id === "uf") {
      $("cidade").value = "";
      $("bairro").value = "";
      populateFilters();
    } else if (id === "cidade") {
      $("bairro").value = "";
      populateFilters();
    }
    applyFilters();
  });
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
