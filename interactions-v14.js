
(() => {
  state.markerById = new Map();
  state.selectedId = null;
  state.mapExpanded = false;

  const originalCardHtml = cardHtml;

  function locationLabel(item) {
    const labels = {
      alta: "Localização de alta precisão",
      rua_aproximada: "Localização aproximada — rua",
      bairro_aproximada: "Localização aproximada — bairro",
      nao_localizado: "Localização não disponível"
    };
    return labels[item.localizacao_precisao] || (
      Number.isFinite(item.latitude) && Number.isFinite(item.longitude)
        ? "Localização aproximada"
        : "Localização não disponível"
    );
  }

  window.radarLocationLabel = locationLabel;

  function detailsHtml(item) {
    const located = Number.isFinite(item.latitude) && Number.isFinite(item.longitude);
    return `
      <details class="property-details">
        <summary>Ver dados do anúncio</summary>
        <div class="details-content">
          ${item.descricao ? `<p>${escapeHtml(item.descricao)}</p>` : `<p>Descrição detalhada não disponível.</p>`}
          ${item.telefone_anunciante ? `<p><strong>Contato:</strong> ${escapeHtml(item.telefone_anunciante)}</p>` : ""}
          <p><strong>Localização:</strong> ${escapeHtml(locationLabel(item))}</p>
          ${located && item.endereco_extraido ? `<p><strong>Referência:</strong> ${escapeHtml(item.endereco_extraido)}</p>` : ""}
        </div>
      </details>
    `;
  }

  cardHtml = function(item) {
    let html = originalCardHtml(item);
    html = html.replace(
      `<article class="card" data-id="${escapeHtml(item.id)}">`,
      `<article class="card ${state.selectedId === item.id ? "selected" : ""}" data-id="${escapeHtml(item.id)}" tabindex="0">`
    );

    const locationBadge = Number.isFinite(item.latitude) && Number.isFinite(item.longitude)
      ? `<span class="badge location">📍 ${escapeHtml(locationLabel(item))}</span>`
      : "";

    html = html.replace(
      `<div class="badges">`,
      `<div class="badges">${locationBadge}`
    );

    html = html.replace(
      `${historyHtml(item)}`,
      `${historyHtml(item)}${detailsHtml(item)}`
    );

    return html;
  };

  function findCard(id) {
    return [...document.querySelectorAll(".card[data-id]")].find(card => card.dataset.id === id) || null;
  }

  function highlightCard(id, scrollToCard = false, openDetails = false) {
    document.querySelectorAll(".card.selected").forEach(card => card.classList.remove("selected"));
    const card = findCard(id);
    if (!card) return;

    card.classList.add("selected");

    if (openDetails) {
      const details = card.querySelector("details.property-details");
      if (details) details.open = true;
    }

    if (scrollToCard) {
      const cards = $("cards");
      const targetTop = Math.max(0, card.offsetTop - cards.clientHeight / 2 + card.clientHeight / 2);
      cards.scrollTo({ top: targetTop, behavior: "smooth" });
    }
  }

  function selectProperty(id, options = {}) {
    state.selectedId = id;
    highlightCard(id, !!options.fromMap, !!options.fromMap);

    const item = state.visible.find(x => x.id === id);
    const marker = state.markerById.get(id);

    if (item && marker && Number.isFinite(item.latitude) && Number.isFinite(item.longitude)) {
      const zoom = item.localizacao_precisao === "alta"
        ? 17
        : item.localizacao_precisao === "rua_aproximada"
          ? 15
          : 14;

      state.map.flyTo([item.latitude, item.longitude], zoom, { duration: 0.55 });
      marker.openPopup();
    }
  }

  window.radarSelectProperty = selectProperty;

  renderCards = function() {
    $("cards").innerHTML = state.visible.length
      ? state.visible.map(cardHtml).join("")
      : `<div class="empty">Nenhum imóvel encontrado com esses filtros.</div>`;

    document.querySelectorAll("[data-favorite]").forEach(button => {
      button.addEventListener("click", event => {
        event.stopPropagation();
        toggleFavorite(button.dataset.favorite);
      });
    });

    document.querySelectorAll(".card[data-id]").forEach(card => {
      card.addEventListener("click", event => {
        if (event.target.closest("button, summary, a, details")) return;
        selectProperty(card.dataset.id, { fromMap: false });
      });

      card.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectProperty(card.dataset.id, { fromMap: false });
        }
      });
    });
  };

  renderMap = function() {
    state.markers.clearLayers();
    state.markerById.clear();
    const bounds = [];

    state.visible.forEach(item => {
      if (!Number.isFinite(item.latitude) || !Number.isFinite(item.longitude)) return;

      const selected = state.selectedId === item.id;
      const marker = L.circleMarker([item.latitude, item.longitude], {
        radius: selected ? 13 : 9,
        color: selected ? "#15334f" : "#ffffff",
        weight: selected ? 4 : 2,
        fillColor: markerColor(item),
        fillOpacity: 0.94
      });

      marker.bindPopup(`
        <div class="map-popup">
          <strong>${escapeHtml(item.titulo)}</strong>
          <span>${escapeHtml(item.bairro)} • ${escapeHtml(item.tipo)}</span>
          <b>${money(item.preco)}</b>
          <small>${escapeHtml(locationLabel(item))}</small>
          <em>O anúncio correspondente será destacado na lista.</em>
        </div>
      `);

      marker.on("click", () => selectProperty(item.id, { fromMap: true }));
      marker.addTo(state.markers);
      state.markerById.set(item.id, marker);
      bounds.push([item.latitude, item.longitude]);
    });

    const countText = `${number(bounds.length)} pontos`;
    $("mapCount").textContent = countText;
    const mirror = $("mapCountMirror");
    if (mirror) mirror.textContent = countText;

    if (!state.selectedId) {
      if (bounds.length === 1) state.map.setView(bounds[0], 15);
      if (bounds.length > 1) state.map.fitBounds(bounds, { padding: [24, 24], maxZoom: 14 });
    }
  };

  function addMapControls() {
    const head = document.querySelector(".map-panel .panel-head");
    if (!head || $("mapExpandBtn")) return;

    const oldCount = $("mapCount");
    if (oldCount) oldCount.style.display = "none";

    const actions = document.createElement("div");
    actions.className = "map-head-actions";
    actions.innerHTML = `
      <span id="mapCountMirror" class="pill">0 pontos</span>
      <button id="mapExpandBtn" class="secondary small" type="button">Ver mapa maior</button>
    `;
    head.appendChild(actions);

    $("mapExpandBtn").addEventListener("click", () => {
      state.mapExpanded = !state.mapExpanded;
      document.querySelector(".content-grid").classList.toggle("map-expanded", state.mapExpanded);
      $("mapExpandBtn").textContent = state.mapExpanded ? "Reduzir mapa" : "Ver mapa maior";

      setTimeout(() => {
        state.map.invalidateSize();
        renderMap();
      }, 220);
    });
  }

  addMapControls();

  // Re-renderiza com o comportamento novo, mesmo se a primeira carga já tiver terminado.
  const waitForData = setInterval(() => {
    if (Array.isArray(state.all) && state.all.length) {
      clearInterval(waitForData);
      renderAll();
    }
  }, 120);

  setTimeout(() => clearInterval(waitForData), 10000);
})();
