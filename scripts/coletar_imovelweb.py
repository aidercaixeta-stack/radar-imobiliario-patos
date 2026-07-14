#!/usr/bin/env python3
"""Coletor experimental do Imovelweb para Patos de Minas/MG.

Estratégia:
- percorre as páginas públicas de imóveis à venda em Patos de Minas;
- identifica links individuais de anúncios (/propriedades/...);
- extrai os dados já presentes nos cards da listagem;
- NÃO sobrescreve a base consolidada;
- grava apenas data/fontes/imovelweb.json;
- preserva histórico de preços de anúncios já vistos.

O pipeline scripts/processar_base.py é responsável por:
- classificar/excluir anúncios;
- separar leilões e não comparáveis;
- consolidar com TIMTIM e futuras fontes;
- deduplicar anúncios entre fontes.
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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE = ROOT / "data" / "fontes" / "imovelweb.json"
DEBUG_DIR = ROOT / "debug" / "imovelweb"

BASE_URL = "https://www.imovelweb.com.br/imoveis-venda-patos-de-minas-mg.html"
PAGE_URL = "https://www.imovelweb.com.br/imoveis-venda-patos-de-minas-mg-pagina-{page}.html"

TZ_BR = timezone(timedelta(hours=-3))
MAX_PAGES = max(1, int(os.getenv("IMOVELWEB_MAX_PAGES", "60")))
PAGE_DELAY_SECONDS = max(0.8, float(os.getenv("IMOVELWEB_PAGE_DELAY_SECONDS", "1.5")))


def now_iso() -> str:
    return datetime.now(TZ_BR).isoformat()


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def clean_lines(text: str | None) -> list[str]:
    return [clean(line) for line in (text or "").splitlines() if clean(line)]


def brl_to_int(text: str | None) -> int | None:
    if not text:
        return None
    raw = re.sub(r"[^0-9,\.]", "", text)
    if not raw:
        return None
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        # "1.350.000" -> "1350000"; "950.000" -> "950000"
        parts = raw.split(".")
        if len(parts) > 1 and all(len(part) == 3 for part in parts[1:]):
            raw = "".join(parts)
    try:
        return int(round(float(raw)))
    except ValueError:
        return None


def numeric(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def canonical_url(href: str) -> str:
    absolute = urllib.parse.urljoin("https://www.imovelweb.com.br", href)
    parsed = urllib.parse.urlsplit(absolute)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def stable_id(url: str) -> str:
    m = re.search(r"-(\d+)\.html$", urllib.parse.urlsplit(url).path)
    if m:
        return f"imovelweb-{m.group(1)}"
    return "imovelweb-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:18]


def infer_type(url: str, text: str) -> tuple[str, str | None]:
    slug = urllib.parse.urlsplit(url).path.lower()
    blob = clean(text).lower()

    if "apartamento" in slug or "cobertura" in slug or "flat" in slug or "studio" in slug or "kitnet" in slug:
        subtype = "Cobertura" if "cobertura" in slug else None
        return "Apartamento", subtype
    if "casa" in slug or "sobrado" in slug:
        subtype = "Sobrado" if "sobrado" in slug else None
        return "Casa", subtype
    if "terreno" in slug or "lote" in slug:
        return "Lote", None
    if "rurais" in slug or "fazenda" in slug or "chacara" in slug or "sítio" in blob or "sitio" in blob:
        return "Rural", None
    if "comercial" in slug or "loja" in slug or "galpao" in slug or "galpão" in blob:
        return "Comercial", None

    # fallback pelo texto do card
    if re.search(r"\b(apartamento|cobertura|studio|kitnet|flat)\b", blob):
        return "Apartamento", None
    if re.search(r"\b(casa|sobrado)\b", blob):
        return "Casa", None
    if re.search(r"\b(lote|terreno)\b", blob):
        return "Lote", None
    if re.search(r"\b(fazenda|ch[aá]cara|s[ií]tio|rural)\b", blob):
        return "Rural", None
    return "Imóvel", None


def extract_price(text: str) -> int | None:
    # O preço principal aparece antes de condomínio/IPTU nos cards.
    for match in re.finditer(r"R\$\s*([\d\.]+(?:,\d{1,2})?)", text, re.I):
        value = brl_to_int(match.group(0))
        if value and value > 1000:
            return value
    return None


def extract_area(text: str) -> int | None:
    patterns = [
        r"(?<!\d)(\d{2,9}(?:[\.,]\d+)?)\s*m[²2]\s*(?:tot\.?|total|útil|util)?",
        r"(?<!\d)(\d{2,7}(?:[\.,]\d+)?)\s*hectares?\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if not m:
            continue
        raw = m.group(1).replace(".", "").replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            continue
        if "hectare" in m.group(0).lower():
            value *= 10_000
        return int(round(value))
    return None


def extract_feature(text: str, names: str) -> int | None:
    m = re.search(rf"(?<!\d)(\d{{1,2}})\s*(?:{names})\b", text, re.I)
    return int(m.group(1)) if m else None


def extract_location(lines: list[str]) -> tuple[str, str | None]:
    bairro = "Não identificado"
    endereco = None

    for idx, line in enumerate(lines):
        low = line.lower()
        if "patos de minas" not in low:
            continue

        # Ex.: "Centro, Patos de Minas"
        before = re.split(r",\s*Patos de Minas\b", line, flags=re.I)[0].strip(" ,-")
        if before and before.lower() not in {"patos de minas", "minas gerais"}:
            bairro = before

        # A linha anterior costuma ser rua/endereço.
        if idx > 0:
            previous = lines[idx - 1]
            if (
                previous.lower() != "endereço não informado"
                and not previous.startswith("R$")
                and "m²" not in previous
                and len(previous) <= 140
                and not re.search(r"\b(quartos?|ban\.?|banheiros?|vagas?)\b", previous, re.I)
            ):
                endereco = previous
        break

    return bairro, endereco


def best_description(anchor_text: str, card_lines: list[str]) -> str:
    anchor_text = clean(anchor_text)
    if len(anchor_text) >= 35:
        return anchor_text

    ignored = re.compile(
        r"^(R\$|WhatsApp$|Contatar$|Super destaque$|Destaque$|Endereço não informado$)",
        re.I,
    )
    candidates = [
        line for line in card_lines
        if len(line) >= 35 and not ignored.search(line)
    ]
    return max(candidates, key=len) if candidates else anchor_text


def title_from_description(description: str, tipo: str, bairro: str) -> str:
    first = clean(re.split(r"(?<=[.!?])\s+|[;]", description)[0])
    if len(first) >= 8:
        return first[:110].rstrip(" ,-")
    if bairro != "Não identificado":
        return f"{tipo} no {bairro}"
    return f"{tipo} à venda em Patos de Minas"


def find_card_text(anchor) -> str:
    """Sobe na árvore DOM até encontrar um bloco do anúncio sem pegar a página inteira."""
    try:
        return anchor.evaluate(
            """(el) => {
                let node = el;
                let best = "";
                for (let i = 0; i < 10 && node; i++, node = node.parentElement) {
                    const text = (node.innerText || "").trim();
                    if (
                        text.includes("R$") &&
                        text.toLowerCase().includes("patos de minas") &&
                        text.length >= 50 &&
                        text.length <= 10000
                    ) {
                        best = text;
                        if (
                            text.includes("Contatar") ||
                            text.includes("WhatsApp") ||
                            /m[²2]/i.test(text)
                        ) {
                            return text;
                        }
                    }
                }
                return best;
            }"""
        )
    except Exception:
        return ""


def collect_page(page: Page, page_number: int) -> list[dict[str, Any]]:
    url = BASE_URL if page_number == 1 else PAGE_URL.format(page=page_number)
    print(f"\n[página {page_number}] {url}")

    page.goto(url, wait_until="domcontentloaded", timeout=75_000)
    page.wait_for_timeout(1400)

    try:
        page.locator('a[href*="/propriedades/"]').first.wait_for(timeout=25_000)
    except PlaywrightTimeoutError:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"sem-anuncios-pagina-{page_number}.png"), full_page=False)
        print("[aviso] nenhum link de anúncio encontrado")
        return []

    anchors = page.locator('a[href*="/propriedades/"]')
    count = anchors.count()
    print(f"[info] {count} links candidatos encontrados")

    grouped: dict[str, dict[str, Any]] = {}

    for index in range(min(count, 500)):
        anchor = anchors.nth(index)
        try:
            href = anchor.get_attribute("href")
            if not href or "/propriedades/" not in href:
                continue
            url_fonte = canonical_url(href)
            anchor_text = clean(anchor.inner_text(timeout=1200))
            card_text = find_card_text(anchor)

            current = grouped.get(url_fonte)
            quality = len(anchor_text) + min(len(card_text), 5000)
            if current and current["quality"] >= quality:
                continue

            grouped[url_fonte] = {
                "url": url_fonte,
                "anchor_text": anchor_text,
                "card_text": card_text,
                "quality": quality,
            }
        except Exception:
            continue

    results: list[dict[str, Any]] = []
    captured_at = now_iso()

    for payload in grouped.values():
        card_text = payload["card_text"]
        if not card_text or "R$" not in card_text:
            continue

        lines = clean_lines(card_text)
        description = best_description(payload["anchor_text"], lines)
        price = extract_price(card_text)
        if not price or len(description) < 15:
            continue

        tipo, subtipo = infer_type(payload["url"], card_text)
        bairro, endereco = extract_location(lines)
        area = extract_area(card_text)
        quartos = extract_feature(card_text, r"quartos?|dormit[oó]rios?")
        banheiros = extract_feature(card_text, r"ban\.?|banheiros?")
        vagas = extract_feature(card_text, r"vagas?")

        area_built = area if tipo in {"Casa", "Apartamento", "Comercial", "Imóvel"} else None
        area_land = area if tipo in {"Lote", "Rural"} else None

        item = {
            "id": stable_id(payload["url"]),
            "mercado": "tradicional",
            "titulo": title_from_description(description, tipo, bairro),
            "bairro": bairro,
            "tipo": tipo,
            "subtipo": subtipo,
            "preco": price,
            "area_construida": area_built,
            "area_terreno": area_land,
            "quartos": quartos,
            "banheiros": banheiros,
            "vagas": vagas,
            "fonte": "Imovelweb",
            "url_fonte": payload["url"],
            "telefone_anunciante": None,
            "descricao": description,
            "confianca": 70,
            "nota_oportunidade": 50,
            "novo": True,
            "preco_reduzido": False,
            "primeira_captura": captured_at,
            "ultima_captura": captured_at,
            "latitude": None,
            "longitude": None,
            "localizacao_precisao": "nao_localizado",
            "endereco_extraido": endereco,
            "geocoding_query": None,
            "geocoding_display_name": None,
            "historico_precos": [
                {"data": datetime.now(TZ_BR).date().isoformat(), "preco": price}
            ],
        }

        # Confiança simples pela completude.
        completeness = sum(bool(x) for x in [
            bairro != "Não identificado",
            area,
            quartos,
            endereco,
            len(description) >= 80,
        ])
        item["confianca"] = min(92, 56 + completeness * 7)
        results.append(item)

    print(f"[resultado página {page_number}] {len(results)} anúncios normalizados")
    return results


def load_existing() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return {item.get("id"): item for item in data if isinstance(item, dict) and item.get("id")}


def merge_history(items: list[dict[str, Any]], existing: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    today = datetime.now(TZ_BR).date().isoformat()
    merged = []

    for item in items:
        old = existing.get(item["id"])
        if old:
            history = list(old.get("historico_precos") or [])
            previous_price = history[-1].get("preco") if history else old.get("preco")

            if not history or history[-1].get("preco") != item["preco"]:
                history.append({"data": today, "preco": item["preco"]})

            item["historico_precos"] = history[-24:]
            item["preco_reduzido"] = bool(previous_price and item["preco"] < previous_price)
            item["primeira_captura"] = old.get("primeira_captura", item["primeira_captura"])
            item["novo"] = False

            # Reaproveita enriquecimentos futuros.
            for field in [
                "latitude", "longitude", "localizacao_precisao",
                "geocoding_query", "geocoding_display_name"
            ]:
                if old.get(field) is not None:
                    item[field] = old.get(field)

        merged.append(item)

    return merged


def main() -> int:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing = load_existing()
    collected: dict[str, dict[str, Any]] = {}
    empty_streak = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(12_000)

        for page_number in range(1, MAX_PAGES + 1):
            try:
                items = collect_page(page, page_number)
            except Exception as exc:
                print(f"[erro página {page_number}] {type(exc).__name__}: {exc}")
                items = []

            new_count = 0
            for item in items:
                if item["id"] not in collected:
                    collected[item["id"]] = item
                    new_count += 1

            if not items or new_count == 0:
                empty_streak += 1
            else:
                empty_streak = 0

            print(f"[acumulado] {len(collected)} anúncios únicos")

            # Evita loops quando páginas inexistentes repetem conteúdo ou deixam de retornar anúncios.
            if empty_streak >= 2:
                print("[fim] duas páginas consecutivas sem anúncios novos")
                break

            time.sleep(PAGE_DELAY_SECONDS)

        browser.close()

    items = list(collected.values())
    if not items:
        print("[falha segura] nenhum anúncio válido coletado; arquivo da fonte não será alterado.", file=sys.stderr)
        return 2

    items = merge_history(items, existing)
    items.sort(key=lambda x: x.get("preco") or 0, reverse=True)

    SOURCE_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n[sucesso] {len(items)} anúncios do Imovelweb gravados em {SOURCE_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
