#!/usr/bin/env python3
"""Coletor experimental Chaves na Mão — Patos de Minas/MG.

- Coleta páginas públicas de imóveis à venda.
- Salva exclusivamente em data/fontes/chavesnamao.json.
- Não sobrescreve TIMTIM.
- Marca leilão/licitação/venda direta separadamente do mercado tradicional.
- Preserva histórico de preços.
- O pipeline processar_base.py faz exclusões e deduplicação multifuente.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE = ROOT / "data" / "fontes" / "chavesnamao.json"
DEBUG_DIR = ROOT / "debug" / "chavesnamao"

BASE_URL = "https://www.chavesnamao.com.br/imoveis-a-venda/mg-patos-de-minas/"
TZ_BR = timezone(timedelta(hours=-3))

MAX_PAGES = max(1, int(os.getenv("CHAVES_MAX_PAGES", "30")))
PAGE_DELAY_SECONDS = max(0.8, float(os.getenv("CHAVES_PAGE_DELAY_SECONDS", "1.5")))


def now_iso() -> str:
    return datetime.now(TZ_BR).isoformat()


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def lines(text: str | None) -> list[str]:
    return [clean(x) for x in (text or "").splitlines() if clean(x)]


def canonical_url(href: str) -> str:
    absolute = urllib.parse.urljoin("https://www.chavesnamao.com.br", href)
    parsed = urllib.parse.urlsplit(absolute)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def stable_id(url: str) -> str:
    match = re.search(r"/id-(\d+)/?$", urllib.parse.urlsplit(url).path)
    if match:
        return f"chaves-{match.group(1)}"
    return "chaves-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:18]


def brl_to_int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"R\$\s*([\d\.]+(?:,\d{1,2})?)", text, re.I)
    if not m:
        return None
    raw = m.group(1)
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        parts = raw.split(".")
        if len(parts) > 1 and all(len(x) == 3 for x in parts[1:]):
            raw = "".join(parts)
    try:
        value = int(round(float(raw)))
        return value if value > 1000 else None
    except ValueError:
        return None


def first_number(text: str, pattern: str) -> int | None:
    m = re.search(pattern, text, re.I)
    if not m:
        return None
    try:
        return int(float(m.group(1).replace(".", "").replace(",", ".")))
    except ValueError:
        return None


def extract_area(text: str) -> int | None:
    matches = re.findall(r"(?<!\d)(\d{2,9}(?:[\.,]\d+)?)\s*m[²2]\b", text, re.I)
    values = []
    for raw in matches:
        try:
            values.append(int(round(float(raw.replace(".", "").replace(",", ".")))))
        except ValueError:
            pass
    return max(values) if values else None


def infer_market_and_modality(text: str) -> tuple[str, str | None]:
    low = text.lower()
    if "licitação aberta" in low or "licitacao aberta" in low:
        return "leilao", "Licitação aberta"
    if "venda direta" in low:
        return "leilao", "Venda direta"
    if re.search(r"\bleil[aã]o\b", low):
        return "leilao", "Leilão"
    return "tradicional", None


def infer_type(text: str, url: str) -> str:
    low = (text + " " + url).lower()
    if re.search(r"\b(apartamento|cobertura|studio|kitnet|flat)\b", low):
        return "Apartamento"
    if re.search(r"\b(casa|sobrado)\b", low):
        return "Casa"
    if re.search(r"\b(lote|terreno)\b", low):
        return "Lote"
    if re.search(r"\b(fazenda|ch[aá]cara|s[ií]tio|rural)\b", low):
        return "Rural"
    if re.search(r"\b(loja|sala comercial|galp[aã]o|comercial)\b", low):
        return "Comercial"
    return "Imóvel"


def infer_bairro(text: str) -> str:
    patterns = [
        r"\b(?:no|na|bairro)\s+([A-ZÀ-Ü][A-Za-zÀ-ÿ0-9' -]{2,45}),\s*Patos de Minas",
        r"Endere[cç]o indispon[ií]vel\s+([A-ZÀ-Ü][A-Za-zÀ-ÿ0-9' -]{2,45}),\s*Patos de Minas/MG",
        r"\b([A-ZÀ-Ü][A-Za-zÀ-ÿ0-9' -]{2,45}),\s*Patos de Minas/MG",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text, re.I)
        if matches:
            candidate = clean(matches[-1]).strip(" ,-")
            # Evita capturar frases longas/títulos inteiros.
            if 2 <= len(candidate.split()) <= 7 and len(candidate) <= 55:
                return candidate.title()
    return "Não identificado"


def infer_address(text_lines: list[str]) -> str | None:
    for line in text_lines:
        if "Patos de Minas/MG" not in line:
            continue
        before = line.split(", Patos de Minas/MG")[0].strip()
        if before.lower().startswith("endereço indisponível"):
            return None
        if len(before) <= 140:
            return before
    return None


def best_title(anchor_text: str) -> str:
    ls = lines(anchor_text)
    for line in ls:
        if (
            len(line) >= 15
            and not line.startswith("R$")
            and "Patos de Minas/MG" not in line
            and not re.fullmatch(r"\d+", line)
        ):
            return line[:150]
    return clean(anchor_text)[:150] or "Imóvel à venda em Patos de Minas"


def description_from_card(card_text: str, title: str) -> str:
    ls = lines(card_text)
    candidates = [
        x for x in ls
        if len(x) >= 35
        and x != title
        and "Patos de Minas/MG" not in x
        and not x.startswith("R$")
    ]
    if candidates:
        return max(candidates, key=len)[:1200]
    return title


def get_card_text(anchor) -> str:
    """Retorna somente o texto do próprio link do anúncio.

    Não sobe para elementos-pai, porque isso pode misturar dados de anúncios vizinhos.
    Se o próprio link não trouxer preço e localização suficientes, o anúncio é ignorado
    nesta coleta em vez de arriscar contaminar a base.
    """
    try:
        text = clean(anchor.inner_text(timeout=1800))
    except Exception:
        return ""

    if (
        "R$" not in text
        or "patos de minas" not in text.lower()
        or len(text) < 25
        or len(text) > 3000
    ):
        return ""

    return text


def save_diagnostic(page: Page, page_number: int, reason: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "capturado_em": now_iso(),
        "pagina": page_number,
        "motivo": reason,
        "url_final": page.url,
        "titulo": page.title(),
        "links": page.locator("a").count(),
        "texto_inicial": clean(page.locator("body").inner_text())[:5000],
    }
    (DEBUG_DIR / f"diagnostico-{page_number}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        page.screenshot(path=str(DEBUG_DIR / f"pagina-{page_number}.png"), full_page=False)
    except Exception:
        pass


def collect_page(page: Page, page_number: int) -> list[dict[str, Any]]:
    url = BASE_URL if page_number == 1 else f"{BASE_URL}?pg={page_number}"
    print(f"\n[página {page_number}] {url}")

    response = page.goto(url, wait_until="domcontentloaded", timeout=75_000)
    status = response.status if response else None
    print(f"[http] {status} | {page.url}")

    page.wait_for_timeout(1800)

    anchors = page.locator('a[href*="/imovel/"]')
    count = anchors.count()
    print(f"[links candidatos] {count}")

    grouped: dict[str, dict[str, Any]] = {}

    for i in range(min(count, 500)):
        a = anchors.nth(i)
        try:
            href = a.get_attribute("href")
            if not href or "/imovel/" not in href:
                continue
            url_fonte = canonical_url(href)
            card_text = get_card_text(a)
            if not card_text:
                continue

            quality = len(card_text)
            old = grouped.get(url_fonte)
            if old and old["quality"] >= quality:
                continue

            grouped[url_fonte] = {
                "url": url_fonte,
                "text": card_text,
                "quality": quality,
            }
        except Exception:
            continue

    print(f"[anúncios únicos na página] {len(grouped)}")

    if not grouped:
        save_diagnostic(page, page_number, "nenhum_anuncio_encontrado")
        return []

    captured_at = now_iso()
    output = []

    for payload in grouped.values():
        text = clean(payload["text"])
        text_lines = lines(payload["text"])
        price = brl_to_int(text)
        title = best_title(payload["text"])

        if not price:
            continue

        mercado, modalidade = infer_market_and_modality(text)
        tipo = infer_type(text, payload["url"])
        bairro = infer_bairro(text)
        endereco = infer_address(text_lines)
        area = extract_area(text)
        quartos = first_number(text, r"(?<!\d)(\d{1,2})\s*(?:quartos?|dormit[oó]rios?)\b")
        banheiros = first_number(text, r"(?<!\d)(\d{1,2})\s*(?:banheiros?|ban\.?)\b")
        vagas = first_number(text, r"(?<!\d)(\d{1,2})\s*vagas?\b")
        description = description_from_card(payload["text"], title)

        # Guarda de integridade: quando o URL informa o preço, ele deve ser
        # compatível com o preço lido no próprio link do anúncio.
        url_price_match = re.search(r"-RS(\d+)(?:/|$)", payload["url"], re.I)
        if url_price_match:
            try:
                url_price = int(url_price_match.group(1))
                if url_price > 1000:
                    diff = abs(url_price - price) / max(url_price, price)
                    if diff > 0.08:
                        print(
                            f"[descartado por inconsistência] {payload['url']} "
                            f"url={url_price} texto={price}"
                        )
                        continue
            except ValueError:
                pass

        item = {
            "id": stable_id(payload["url"]),
            "mercado": mercado,
            "modalidade_leilao": modalidade,
            "titulo": title,
            "bairro": bairro,
            "tipo": tipo,
            "preco": price,
            "area_construida": area if tipo in {"Casa", "Apartamento", "Comercial", "Imóvel"} else None,
            "area_terreno": area if tipo in {"Lote", "Rural"} else None,
            "quartos": quartos,
            "banheiros": banheiros,
            "vagas": vagas,
            "fonte": "Chaves na Mão",
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

        completeness = sum(bool(x) for x in [
            bairro != "Não identificado",
            area,
            quartos,
            endereco,
            len(description) >= 60,
        ])
        item["confianca"] = min(94, 58 + completeness * 7)
        output.append(item)

    print(f"[normalizados] {len(output)}")
    return output


def load_existing() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return {
        item["id"]: item
        for item in data
        if isinstance(item, dict) and item.get("id")
    }


def merge_history(items: list[dict[str, Any]], existing: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    today = datetime.now(TZ_BR).date().isoformat()

    for item in items:
        old = existing.get(item["id"])
        if not old:
            continue

        history = list(old.get("historico_precos") or [])
        previous_price = history[-1].get("preco") if history else old.get("preco")

        if not history or history[-1].get("preco") != item["preco"]:
            history.append({"data": today, "preco": item["preco"]})

        item["historico_precos"] = history[-24:]
        item["preco_reduzido"] = bool(previous_price and item["preco"] < previous_price)
        item["primeira_captura"] = old.get("primeira_captura", item["primeira_captura"])
        item["novo"] = False

        for field in [
            "latitude", "longitude", "localizacao_precisao",
            "geocoding_query", "geocoding_display_name"
        ]:
            if old.get(field) is not None:
                item[field] = old.get(field)

    return items


def main() -> int:
    SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

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
                page_items = collect_page(page, page_number)
            except Exception as exc:
                print(f"[erro página {page_number}] {type(exc).__name__}: {exc}")
                page_items = []

            new_count = 0
            for item in page_items:
                if item["id"] not in collected:
                    collected[item["id"]] = item
                    new_count += 1

            print(f"[acumulado] {len(collected)} anúncios únicos")

            if not page_items or new_count == 0:
                empty_streak += 1
            else:
                empty_streak = 0

            if empty_streak >= 2:
                print("[fim] duas páginas consecutivas sem anúncios novos")
                break

            time.sleep(PAGE_DELAY_SECONDS)

        browser.close()

    items = list(collected.values())
    if not items:
        print("[falha segura] nenhum anúncio válido coletado; base existente preservada.", file=sys.stderr)
        return 2

    items = merge_history(items, existing)
    items.sort(
        key=lambda x: (
            x.get("mercado") == "tradicional",
            x.get("preco") or 0,
        ),
        reverse=True,
    )

    SOURCE_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    tradicionais = sum(1 for x in items if x.get("mercado") == "tradicional")
    especiais = len(items) - tradicionais
    print(f"\n[sucesso] {len(items)} anúncios gravados em {SOURCE_FILE}")
    print(f"  tradicionais: {tradicionais}")
    print(f"  leilão/licitação/venda direta: {especiais}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
