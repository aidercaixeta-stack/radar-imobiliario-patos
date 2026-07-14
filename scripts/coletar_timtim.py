#!/usr/bin/env python3
"""Coletor experimental do Classificados TIMTIM.

Estratégia:
- abre as páginas públicas das três categorias imobiliárias;
- percorre os botões "Ver Detalhes" com um navegador Playwright;
- lê o conteúdo do modal;
- normaliza os campos úteis;
- rejeita aluguel e registros sem preço/descrição;
- preserva histórico de preço quando o mesmo anúncio reaparece.

Este coletor evita depender de classes CSS específicas do site sempre que possível.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "imoveis.json"
META_FILE = ROOT / "data" / "meta.json"
DEBUG_DIR = ROOT / "debug" / "timtim"

SOURCES = [
    ("Casa", "https://www.classificadostimtim.com.br/announcements?category=imoveis-venda"),
    ("Lote", "https://www.classificadostimtim.com.br/announcements?category=lotes"),
    ("Apartamento", "https://www.classificadostimtim.com.br/announcements?category=apartamento-venda"),
]

TZ_BR = timezone(timedelta(hours=-3))

RENT_TERMS = re.compile(r"\b(aluguel|aluga-se|alugar|loca[cç][aã]o|loca-se)\b", re.I)
AUCTION_TERMS = re.compile(r"\b(leil[aã]o|hasta|aliena[cç][aã]o fiduci[aá]ria|2[ºo]\s*leil[aã]o)\b", re.I)

# Geocodificação: no teste manual usamos uma cadência conservadora abaixo do limite
# absoluto do serviço público. Antes de ativar rotina periódica, o workflow deverá
# usar um intervalo maior para novas consultas. Resultados já encontrados são
# reaproveitados dos próprios registros existentes.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_DELAY_SECONDS = float(os.getenv("NOMINATIM_DELAY_SECONDS", "1.15"))
NOMINATIM_USER_AGENT = (
    "RadarImobiliarioPatos/0.2 "
    "(+https://github.com/aidercaixeta-stack/radar-imobiliario-patos)"
)
PATOS_BOUNDS = {
    "min_lat": -18.80,
    "max_lat": -18.35,
    "min_lon": -46.70,
    "max_lon": -46.30,
}

STREET_RE = re.compile(
    r"\b((?:Rua|R\.?|Avenida|Av\.?|Alameda|Travessa|Tv\.?|Praça|Pça\.?|Rodovia|BR[- ]?\d{2,3})"
    r"\s+[A-Za-zÁÀÂÃÉÊÍÓÔÕÚÜÇáàâãéêíóôõúüç0-9'ºª.\- ]{2,80}?)"
    r"(?:\s*,?\s*(?:n(?:º|°|o)?\.?\s*)?(\d{1,5}))?"
    r"(?=\s*,|\s*\.|\s*;|\s+-\s+|\s+(?:bairro|pr[oó]xim|com|esquina|lote|área|aceita|tr\.)\b|$)",
    re.I,
)


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def brl_to_int(text: str | None) -> int | None:
    if not text:
        return None
    raw = re.sub(r"[^0-9,\.]", "", text)
    if not raw:
        return None
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        parts = raw.split(".")
        if len(parts) > 1 and len(parts[-1]) == 3:
            raw = "".join(parts)
    try:
        return int(round(float(raw)))
    except ValueError:
        return None


def first_match(patterns: list[str], text: str, flags: int = re.I) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, flags)
        if m:
            return clean(m.group(1))
    return None


def extract_number(patterns: list[str], text: str) -> int | None:
    value = first_match(patterns, text)
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else None


def extract_area(text: str, kind: str) -> int | None:
    if kind == "construida":
        patterns = [
            r"(?:área\s+)?constru[ií]da\s*(?:de|:)?\s*(\d{2,4}(?:[\.,]\d+)?)\s*m[²2]",
            r"(\d{2,4}(?:[\.,]\d+)?)\s*m[²2]\s*(?:de\s+)?(?:área\s+)?constru[ií]da",
        ]
    else:
        patterns = [
            r"(?:lote|terreno)(?:\s+[A-Za-zÁÀÂÃÉÊÍÓÔÕÚÜÇáàâãéêíóôõúüç-]+){0,3}\s*(?:de|:|com)?\s*(\d{2,5}(?:[\.,]\d+)?)\s*m[²2]",
            r"(\d{2,5}(?:[\.,]\d+)?)\s*m[²2]\s*(?:de\s+)?(?:lote|terreno)",
        ]
    value = first_match(patterns, text)
    if not value:
        return None
    try:
        return int(round(float(value.replace(".", "").replace(",", "."))))
    except ValueError:
        return None



def extract_generic_area(text: str) -> int | None:
    """Primeira metragem solta em m², útil para lotes e apartamentos."""
    m = re.search(r"(?<!\d)(\d{2,5}(?:[\.,]\d+)?)\s*m[²2]\b", text, re.I)
    if not m:
        return None
    try:
        return int(round(float(m.group(1).replace(".", "").replace(",", "."))))
    except ValueError:
        return None


def extract_street_and_number(text: str) -> tuple[str | None, str | None]:
    m = STREET_RE.search(text)
    if not m:
        return None, None
    street = clean(m.group(1)).rstrip(" ,.-")
    number = clean(m.group(2)) if m.group(2) else None
    # Evita capturar frases excessivamente genéricas como "Rua tranquila".
    if len(street.split()) < 2:
        return None, None
    return street, number


def build_location_candidate(item: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Retorna (consulta, precisão, endereço extraído)."""
    description = item.get("descricao") or ""
    bairro = item.get("bairro")
    if bairro == "Não identificado":
        bairro = None
    street, number = extract_street_and_number(description)

    if street and number:
        parts = [f"{street}, {number}"]
        if bairro:
            parts.append(bairro)
        parts.extend(["Patos de Minas", "MG", "Brasil"])
        return ", ".join(parts), "alta", f"{street}, {number}"

    if street:
        parts = [street]
        if bairro:
            parts.append(bairro)
        parts.extend(["Patos de Minas", "MG", "Brasil"])
        return ", ".join(parts), "rua_aproximada", street

    if bairro:
        return f"{bairro}, Patos de Minas, MG, Brasil", "bairro_aproximada", bairro

    return None, None, None


def _inside_patos(lat: float, lon: float, display_name: str) -> bool:
    return (
        PATOS_BOUNDS["min_lat"] <= lat <= PATOS_BOUNDS["max_lat"]
        and PATOS_BOUNDS["min_lon"] <= lon <= PATOS_BOUNDS["max_lon"]
        and "patos de minas" in display_name.lower()
    )


def geocode_query(query: str, last_request_at: float) -> tuple[dict[str, Any] | None, float]:
    elapsed = time.monotonic() - last_request_at
    if last_request_at and elapsed < NOMINATIM_DELAY_SECONDS:
        time.sleep(NOMINATIM_DELAY_SECONDS - elapsed)

    params = urllib.parse.urlencode({
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 1,
        "countrycodes": "br",
        "layer": "address",
        "viewbox": "-46.70,-18.35,-46.30,-18.80",
        "bounded": 1,
        "accept-language": "pt-BR,pt",
    })
    request = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}",
        headers={
            "User-Agent": NOMINATIM_USER_AGENT,
            "Accept": "application/json",
        },
    )
    requested_at = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"[geocode] erro em {query!r}: {type(exc).__name__}: {exc}")
        return None, requested_at

    if not payload:
        return None, requested_at
    result = payload[0]
    try:
        lat = float(result["lat"])
        lon = float(result["lon"])
    except (KeyError, TypeError, ValueError):
        return None, requested_at
    display_name = clean(result.get("display_name"))
    if not _inside_patos(lat, lon, display_name):
        return None, requested_at
    return {
        "latitude": lat,
        "longitude": lon,
        "geocoding_display_name": display_name,
    }, requested_at


def _jitter_bairro(lat: float, lon: float, item_id: str) -> tuple[float, float]:
    """Espalha pontos de bairro em ~30–100 m para evitar sobreposição total."""
    seed = int(hashlib.sha1(item_id.encode("utf-8")).hexdigest()[:8], 16)
    angle = (seed % 360) * math.pi / 180
    radius = 0.00028 + ((seed >> 9) % 50) / 100000.0
    return lat + math.sin(angle) * radius, lon + math.cos(angle) * radius


def geocode_items(items: list[dict[str, Any]], existing: dict[str, dict[str, Any]]) -> None:
    """Enriquece itens com coordenadas, reaproveitando resultados já conhecidos."""
    cache: dict[str, dict[str, Any]] = {}
    for old in existing.values():
        query = old.get("geocoding_query")
        if query and old.get("latitude") is not None and old.get("longitude") is not None:
            cache[query] = {
                "latitude": old["latitude"],
                "longitude": old["longitude"],
                "geocoding_display_name": old.get("geocoding_display_name"),
            }

    last_request_at = 0.0
    found = 0
    for index, item in enumerate(items, start=1):
        if item.get("latitude") is not None and item.get("longitude") is not None:
            found += 1
            continue

        query, precision, extracted = build_location_candidate(item)
        item["localizacao_precisao"] = precision or "nao_localizado"
        item["endereco_extraido"] = extracted
        item["geocoding_query"] = query
        if not query:
            continue

        result = cache.get(query)
        if result is None:
            print(f"[geocode {index}/{len(items)}] {precision}: {query}")
            result, last_request_at = geocode_query(query, last_request_at)
            if result:
                cache[query] = result

        if not result:
            item["localizacao_precisao"] = "nao_localizado"
            continue

        lat = float(result["latitude"])
        lon = float(result["longitude"])
        if precision == "bairro_aproximada":
            lat, lon = _jitter_bairro(lat, lon, item["id"])
        item["latitude"] = lat
        item["longitude"] = lon
        item["geocoding_display_name"] = result.get("geocoding_display_name")
        found += 1

    print(f"[geocode] {found}/{len(items)} imóveis com ponto no mapa")

def infer_bairro(text: str) -> str | None:
    patterns = [
        r"\bbairro\s+([A-ZÁÀÂÃÉÊÍÓÔÕÚÜÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÜÇa-záàâãéêíóôõúüç0-9'\- ]{2,45}?)(?=,|\.|\s+-\s+|\s+pr[oó]xim|\s+com\b|\s+em\b|\s+área\b|\s+lote\b|$)",
        r"\b(?:no|na)\s+([A-ZÁÀÂÃÉÊÍÓÔÕÚÜÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÜÇa-záàâãéêíóôõúüç0-9'\- ]{2,35}?)\s*,\s*Patos de Minas",
    ]
    bairro = first_match(patterns, text)
    if bairro:
        bairro = re.sub(r"^(do|da|de)\s+", "", bairro, flags=re.I)
        return bairro.title()
    return None


def parse_date(text: str) -> str:
    m = re.search(r"Publicado\s+em\s+(\d{2}/\d{2}/\d{4})", text, re.I)
    if m:
        try:
            return datetime.strptime(m.group(1), "%d/%m/%Y").replace(tzinfo=TZ_BR).isoformat()
        except ValueError:
            pass
    return datetime.now(TZ_BR).isoformat()


def extract_phone(text: str) -> str | None:
    # Primeiro procura sequências contínuas, como o telefone exibido pelo TIMTIM.
    for item in re.findall(r"(?<!\d)(?:55\d{10,11}|\d{10,11})(?!\d)", text):
        digits = re.sub(r"\D", "", item)
        if digits.startswith("55") and len(digits) in (12, 13):
            digits = digits[2:]
        if len(digits) in (10, 11):
            return digits
    # Fallback para formatos com espaços, parênteses ou hífen.
    for item in re.findall(r"(?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?9?\d{4}[\s-]?\d{4}", text):
        digits = re.sub(r"\D", "", item)
        if digits.startswith("55") and len(digits) in (12, 13):
            digits = digits[2:]
        if len(digits) in (10, 11):
            return digits
    return None


def extract_description(text: str) -> str:
    m = re.search(r"Descrição\s*(.+?)(?=Entrar em Contato|$)", text, re.I | re.S)
    if m:
        return clean(m.group(1))
    # Alguns modais não mostram o rótulo se a descrição estiver fora da área visível.
    # Nesse caso preservamos o texto completo, removendo os rótulos básicos.
    reduced = re.sub(r"Detalhes|Anunciante|Preço:|Tipo:|Categoria:|Localização|Informações|Publicado em \d{2}/\d{2}/\d{4}", " ", text, flags=re.I)
    return clean(reduced)


def title_from_description(description: str, tipo: str, bairro: str | None) -> str:
    desc = re.sub(r"^(vende-se|venda-se|vendo|vende se)\s*", "", description, flags=re.I)
    first = clean(re.split(r"[\.!;]", desc)[0])
    if len(first) > 8 and first.lower() not in {"casa", "apartamento", "lote", "terreno"}:
        return first[:92].rstrip(" ,-")
    if bairro and bairro != "Não identificado":
        return f"{tipo} no {bairro}"
    return f"{tipo} à venda em Patos de Minas"


def stable_id(tipo: str, phone: str | None, description: str) -> str:
    normalized_desc = re.sub(r"\W+", " ", clean(description).lower())[:260]
    basis = f"timtim|{tipo}|{phone or ''}|{normalized_desc}"
    return "timtim-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def find_modal_text(page: Page) -> str:
    # Busca contêineres visíveis que contenham os rótulos típicos do modal.
    candidates = page.locator("div").filter(has_text=re.compile(r"Detalhes", re.I)).filter(has_text=re.compile(r"Anunciante", re.I))
    best_text = ""
    best_key = (2, 10**9)
    for i in range(min(candidates.count(), 120)):
        locator = candidates.nth(i)
        try:
            if not locator.is_visible():
                continue
            text = clean(locator.inner_text(timeout=1500))
        except Exception:
            continue
        if "Preço" not in text or "Categoria" not in text:
            continue
        # Contêiner com descrição tem prioridade; entre equivalentes, prefere o menor
        # para evitar selecionar o body inteiro da página.
        priority = 0 if "Descrição" in text else 1
        key = (priority, len(text))
        if 80 <= len(text) <= 12_000 and key < best_key:
            best_text = text
            best_key = key
    return best_text


def close_modal(page: Page) -> None:
    # Primeiro tenta botão com texto/acessibilidade de fechar; depois Escape.
    for locator in [
        page.get_by_role("button", name=re.compile(r"fechar|close", re.I)),
        page.locator("button").filter(has_text="×"),
    ]:
        try:
            if locator.count() and locator.first.is_visible():
                locator.first.click(timeout=1500)
                page.wait_for_timeout(250)
                return
        except Exception:
            pass
    page.keyboard.press("Escape")
    page.wait_for_timeout(250)


def scroll_all(page: Page) -> None:
    last_height = 0
    stable = 0
    for _ in range(24):
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(450)
        height = page.evaluate("document.body.scrollHeight")
        if height == last_height:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
            last_height = height
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)


def parse_modal(text: str, tipo_hint: str, source_url: str) -> dict[str, Any] | None:
    if not text:
        return None
    if RENT_TERMS.search(text):
        return None

    price_text = first_match([r"Preço:\s*(R\$\s*[\d\.]+(?:,\d{2})?)", r"(R\$\s*[\d\.]+(?:,\d{2})?)"], text)
    price = brl_to_int(price_text)
    description = extract_description(text)
    if not price or price <= 1000 or len(description) < 15:
        return None

    # Segurança: se o texto indicar leilão, classifica separado.
    mercado = "leilao" if AUCTION_TERMS.search(text) else "tradicional"
    phone = extract_phone(text)
    published = parse_date(text)
    area_built = extract_area(description, "construida")
    area_land = extract_area(description, "terreno")
    generic_area = extract_generic_area(description)
    if tipo_hint == "Apartamento" and not area_built:
        area_built = generic_area
    if tipo_hint == "Lote" and not area_land:
        area_land = generic_area
    quartos = extract_number([r"(\d{1,2})\s*(?:quartos?|dormit[oó]rios?)"], description)
    vagas = extract_number([
        r"(?:garagens?|vagas?)(?:\s+para|\s+de|:)?\s*(?:at[eé]\s*)?(\d{1,2})\s*(?:carros?|vagas?)?",
        r"\d{1,2}\s*garagens?\s+para\s+(?:at[eé]\s*)?(\d{1,2})\s*carros?",
        r"(\d{1,2})\s*vagas?"
    ], description)
    bairro = infer_bairro(description) or "Não identificado"
    identifier = stable_id(tipo_hint, phone, description)

    completeness = sum(bool(x) for x in [bairro != "Não identificado", area_built or area_land, quartos, phone])
    confidence = 55 + completeness * 9

    return {
        "id": identifier,
        "mercado": mercado,
        "titulo": title_from_description(description, tipo_hint, bairro),
        "bairro": bairro,
        "tipo": tipo_hint,
        "preco": price,
        "area_construida": area_built,
        "area_terreno": area_land,
        "quartos": quartos,
        "vagas": vagas,
        "fonte": "Classificados TIMTIM",
        "url_fonte": source_url,
        "telefone_anunciante": phone,
        "descricao": description,
        "confianca": min(confidence, 91),
        "nota_oportunidade": 50,
        "novo": True,
        "preco_reduzido": False,
        "primeira_captura": published,
        "ultima_captura": datetime.now(TZ_BR).isoformat(),
        "latitude": None,
        "longitude": None,
        "localizacao_precisao": "nao_localizado",
        "endereco_extraido": None,
        "geocoding_query": None,
        "geocoding_display_name": None,
        "historico_precos": [{"data": datetime.now(TZ_BR).date().isoformat(), "preco": price}],
    }


def collect_category(page: Page, tipo_hint: str, url: str) -> list[dict[str, Any]]:
    print(f"\n[abrindo] {tipo_hint}: {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.get_by_role("button", name=re.compile(r"Ver Detalhes", re.I)).first.wait_for(timeout=25_000)
    except PlaywrightTimeoutError:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"sem-botoes-{tipo_hint.lower()}.png"), full_page=True)
        print(f"[aviso] nenhum botão Ver Detalhes encontrado em {tipo_hint}")
        return []

    scroll_all(page)
    buttons = page.get_by_role("button", name=re.compile(r"Ver Detalhes", re.I))
    count = buttons.count()
    print(f"[info] {count} botões Ver Detalhes encontrados")
    results: list[dict[str, Any]] = []

    # Limite de segurança alto, mas finito, para evitar loop caso o site mude.
    for index in range(min(count, 250)):
        buttons = page.get_by_role("button", name=re.compile(r"Ver Detalhes", re.I))
        if index >= buttons.count():
            break
        button = buttons.nth(index)
        try:
            button.scroll_into_view_if_needed(timeout=5_000)
            button.click(timeout=8_000)
            page.wait_for_timeout(450)
            modal_text = find_modal_text(page)
            item = parse_modal(modal_text, tipo_hint, url)
            if item:
                results.append(item)
                print(f"  [{index+1}/{count}] {item['bairro']} | R$ {item['preco']:,} | {item['titulo'][:55]}")
            else:
                print(f"  [{index+1}/{count}] ignorado: modal sem dados válidos ou aluguel")
        except Exception as exc:
            print(f"  [{index+1}/{count}] erro: {type(exc).__name__}: {exc}")
        finally:
            close_modal(page)

    return results


def load_existing() -> dict[str, dict[str, Any]]:
    if not DATA_FILE.exists():
        return {}
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {item.get("id"): item for item in data if item.get("id")}


def merge_history(items: list[dict[str, Any]], existing: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    today = datetime.now(TZ_BR).date().isoformat()
    merged = []
    for item in items:
        old = existing.get(item["id"])
        if old and old.get("fonte") == "Classificados TIMTIM":
            history = list(old.get("historico_precos") or [])
            previous_price = history[-1]["preco"] if history else old.get("preco")
            if not history or history[-1].get("preco") != item["preco"]:
                history.append({"data": today, "preco": item["preco"]})
            item["historico_precos"] = history[-12:]
            item["preco_reduzido"] = bool(previous_price and item["preco"] < previous_price)
            item["primeira_captura"] = old.get("primeira_captura", item["primeira_captura"])
            for field in [
                "latitude", "longitude", "localizacao_precisao", "endereco_extraido",
                "geocoding_query", "geocoding_display_name"
            ]:
                if old.get(field) is not None:
                    item[field] = old.get(field)
            item["novo"] = False
        merged.append(item)
    return merged


def score_opportunities(items: list[dict[str, Any]]) -> None:
    # Usa mediana por tipo+bairro somente quando há pelo menos 3 comparáveis com área.
    groups: dict[tuple[str, str], list[float]] = {}
    for item in items:
        area = item.get("area_construida") or item.get("area_terreno")
        if item.get("mercado") == "tradicional" and area and item.get("bairro") != "Não identificado":
            groups.setdefault((item["tipo"], item["bairro"]), []).append(item["preco"] / area)

    for item in items:
        area = item.get("area_construida") or item.get("area_terreno")
        values = groups.get((item["tipo"], item["bairro"]), [])
        if not area or len(values) < 3:
            item["nota_oportunidade"] = 50
            continue
        sorted_values = sorted(values)
        n = len(sorted_values)
        med = sorted_values[n // 2] if n % 2 else (sorted_values[n//2 - 1] + sorted_values[n//2]) / 2
        current = item["preco"] / area
        discount = (med - current) / med if med else 0
        # 50 = preço na mediana. Cada 1% abaixo soma 1,5 ponto, limitado a 95.
        item["nota_oportunidade"] = max(20, min(95, round(50 + discount * 150)))


def main() -> int:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    all_items: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1440, "height": 1100},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        )
        page = context.new_page()
        page.set_default_timeout(10_000)
        for tipo, url in SOURCES:
            all_items.extend(collect_category(page, tipo, url))
        browser.close()

    # Deduplicação por ID.
    dedup = {item["id"]: item for item in all_items}
    items = list(dedup.values())
    if not items:
        print("\n[falha segura] nenhum imóvel válido coletado. A base atual NÃO será alterada.", file=sys.stderr)
        return 2

    existing = load_existing()
    items = merge_history(items, existing)
    geocode_items(items, existing)
    score_opportunities(items)
    items.sort(key=lambda x: (x.get("nota_oportunidade", 0), x.get("preco", 0)), reverse=True)

    DATA_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    META_FILE.write_text(json.dumps({
        "updated_at": datetime.now(TZ_BR).isoformat(),
        "status": "dados_reais_timtim",
        "source": "Classificados TIMTIM",
        "records": len(items),
        "mapped_records": sum(1 for item in items if item.get("latitude") is not None and item.get("longitude") is not None),
        "location_precision": {
            "alta": sum(1 for item in items if item.get("localizacao_precisao") == "alta"),
            "rua_aproximada": sum(1 for item in items if item.get("localizacao_precisao") == "rua_aproximada"),
            "bairro_aproximada": sum(1 for item in items if item.get("localizacao_precisao") == "bairro_aproximada"),
            "nao_localizado": sum(1 for item in items if item.get("localizacao_precisao") == "nao_localizado"),
        },
        "note": "Coleta experimental com geocodificação em níveis de precisão; revisar os resultados antes de ativar agendamento diário."
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[sucesso] {len(items)} imóveis válidos gravados em {DATA_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
