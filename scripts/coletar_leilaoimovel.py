#!/usr/bin/env python3
"""Coletor nacional experimental — leilaoimovel.com.br.

Objetivos desta primeira versão:
- coletar imóveis de todo o Brasil, sem restrição de cidade/UF;
- gravar apenas em data/fontes/leilaoimovel.json;
- marcar todos os registros como mercado="leilao";
- preservar link direto para o anúncio no Leilão Imóvel;
- capturar, quando disponível: preço, avaliação, desconto, modalidade,
  cidade, UF, endereço, data de encerramento, FGTS e financiamento;
- preservar histórico de preço;
- nunca apagar a base atual se a coleta falhar.

A consolidação final continua a cargo de scripts/processar_base.py.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE = ROOT / "data" / "fontes" / "leilaoimovel.json"
DEBUG_DIR = ROOT / "debug" / "leilaoimovel"

BASE_URL = "https://www.leilaoimovel.com.br/encontre-seu-imovel"
TZ_BR = timezone(timedelta(hours=-3))

MAX_PAGES = max(1, int(os.getenv("LEILAOIMOVEL_MAX_PAGES", "20")))
PAGE_DELAY_SECONDS = max(
    0.8,
    float(os.getenv("LEILAOIMOVEL_PAGE_DELAY_SECONDS", "1.5")),
)


def now_iso() -> str:
    return datetime.now(TZ_BR).isoformat()


def today_iso() -> str:
    return datetime.now(TZ_BR).date().isoformat()


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def fold(text: str | None) -> str:
    raw = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(ch for ch in raw if not unicodedata.combining(ch))


def canonical_url(href: str) -> str:
    absolute = urllib.parse.urljoin("https://www.leilaoimovel.com.br", href)
    parsed = urllib.parse.urlsplit(absolute)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def br_money_to_int(value: str | None) -> int | None:
    if not value:
        return None
    raw = re.sub(r"[^0-9,\.]", "", value)
    if not raw:
        return None
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        parts = raw.split(".")
        if len(parts) > 1 and all(len(x) == 3 for x in parts[1:]):
            raw = "".join(parts)
    try:
        amount = int(round(float(raw)))
        return amount if amount > 1000 else None
    except ValueError:
        return None


def all_prices(text: str) -> list[int]:
    values = []
    for match in re.finditer(r"R\$\s*[\d\.]+(?:,\d{1,2})?", text, re.I):
        amount = br_money_to_int(match.group(0))
        if amount:
            values.append(amount)
    return values


def parse_datetime_br(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(
        r"(\d{2}/\d{2}/\d{4})(?:\s+(?:às|as)?\s*(\d{1,2}:\d{2}))?",
        text,
        re.I,
    )
    if not match:
        return None
    date_part = match.group(1)
    time_part = match.group(2) or "23:59"
    try:
        dt = datetime.strptime(
            f"{date_part} {time_part}",
            "%d/%m/%Y %H:%M",
        ).replace(tzinfo=TZ_BR)
        return dt.isoformat()
    except ValueError:
        return None


def stable_id(url: str, text: str) -> str:
    # O código do imóvel normalmente aparece no título/card: "... - 2923063".
    matches = re.findall(r"\s-\s(\d{5,10})\b", text)
    if matches:
        return f"leilaoimovel-{matches[-1]}"

    # Fallback pelo slug.
    path = urllib.parse.urlsplit(url).path
    nums = re.findall(r"-(\d{5,14})(?:-|$)", path)
    if nums:
        return f"leilaoimovel-{nums[-1]}"

    import hashlib
    return "leilaoimovel-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:18]


def infer_type(text: str) -> str:
    low = fold(text)
    if re.search(r"\bapartamento\b|\bcobertura\b|\bstudio\b|\bflat\b", low):
        return "Apartamento"
    if re.search(r"\bcasa\b|\bsobrado\b", low):
        return "Casa"
    if re.search(r"\bterreno\b|\blote\b", low):
        return "Lote"
    if re.search(r"\barea rural\b|\bimovel rural\b|\bfazenda\b|\bchacara\b|\bsitio\b", low):
        return "Rural"
    if re.search(r"\bcomercial\b|\bgalpao\b|\bloja\b|\bsala comercial\b", low):
        return "Comercial"
    if re.search(r"\bgaragem\b", low):
        return "Garagem"
    return "Imóvel"


def infer_modality(text: str) -> str:
    options = [
        "Leilão SFI Caixa",
        "Licitação Aberta Caixa",
        "Compra Direta",
        "Venda Online",
        "Venda Direta",
        "Judicial",
        "Extrajudicial",
        "Comprei PGFN",
        "Licitação Pública",
        "Leilão SFI",
        "Leilão",
    ]
    low = fold(text)
    for option in options:
        if fold(option) in low:
            return option
    return "Leilão"


def infer_bank(text: str) -> str | None:
    banks = [
        ("Caixa Econômica Federal", ["caixa economica federal", " caixa "]),
        ("Banco do Brasil", ["banco do brasil"]),
        ("Banco Inter", ["banco inter"]),
        ("Santander", ["santander"]),
        ("Bradesco", ["bradesco"]),
        ("Itaú Unibanco", ["itau unibanco", "itau"]),
        ("Banco Safra", ["banco safra", "safra"]),
        ("BRB", ["banco brb", " brb "]),
        ("PGFN", ["pgfn"]),
    ]
    low = f" {fold(text)} "
    for name, needles in banks:
        if any(needle in low for needle in needles):
            return name
    return None


def infer_city_state(text: str) -> tuple[str, str]:
    # Ex.: "Casa Caixa em Cuiabá / MT - 2268803"
    match = re.search(
        r"\b(?:em|no|na)\s+([^/\n]{2,80}?)\s*/\s*([A-Z]{2})\s*-\s*\d{5,10}\b",
        text,
        re.I,
    )
    if match:
        return clean(match.group(1)).title(), match.group(2).upper()

    # Fallback pelo fim de endereço: ", CUIABA - MATO GROSSO"
    states = {
        "ACRE": "AC", "ALAGOAS": "AL", "AMAPA": "AP", "AMAZONAS": "AM",
        "BAHIA": "BA", "CEARA": "CE", "DISTRITO FEDERAL": "DF",
        "ESPIRITO SANTO": "ES", "GOIAS": "GO", "MARANHAO": "MA",
        "MATO GROSSO": "MT", "MATO GROSSO DO SUL": "MS", "MINAS GERAIS": "MG",
        "PARA": "PA", "PARAIBA": "PB", "PARANA": "PR", "PERNAMBUCO": "PE",
        "PIAUI": "PI", "RIO DE JANEIRO": "RJ", "RIO GRANDE DO NORTE": "RN",
        "RIO GRANDE DO SUL": "RS", "RONDONIA": "RO", "RORAIMA": "RR",
        "SANTA CATARINA": "SC", "SAO PAULO": "SP", "SERGIPE": "SE",
        "TOCANTINS": "TO",
    }
    upper = fold(text).upper()
    for state_name, uf in states.items():
        match = re.search(rf",\s*([^,]{{2,70}}?)\s*-\s*{re.escape(state_name)}\b", upper)
        if match:
            return clean(match.group(1)).title(), uf

    return "Não identificada", ""


def infer_address(text: str, property_code: str | None) -> str | None:
    # Pega trecho entre o código do imóvel e "Encerra em"/"1ª Praça".
    if property_code:
        pattern = (
            rf"-\s*{re.escape(property_code)}\s+"
            r"(.+?)\s+(?:Encerra em:|1[ªº°]\s*Praça:|2[ªº°]\s*Praça:)"
        )
        match = re.search(pattern, text, re.I)
        if match:
            address = clean(match.group(1))
            return address[:350] if len(address) >= 5 else None
    return None


def infer_discount(text: str) -> int | None:
    matches = re.findall(r"(?<!\d)(\d{1,2})\s*%", text)
    if not matches:
        return None
    values = [int(x) for x in matches if 0 <= int(x) <= 99]
    return max(values) if values else None


def property_code_from_text(text: str) -> str | None:
    matches = re.findall(r"\s-\s(\d{5,10})\b", text)
    return matches[-1] if matches else None


def card_is_valid(text: str, href: str) -> bool:
    return (
        "/imovel/" in href
        and "R$" in text
        and len(text) >= 45
        and re.search(r"\s-\s\d{5,10}\b", text) is not None
    )


def is_expired(end_at: str | None, modality: str) -> bool:
    if not end_at:
        return False
    # Venda/Compra Direta podem permanecer disponíveis sem depender da data do card.
    direct = {"Venda Direta", "Compra Direta"}
    if modality in direct:
        return False
    try:
        dt = datetime.fromisoformat(end_at)
        return dt < datetime.now(TZ_BR)
    except ValueError:
        return False


def save_diagnostic(page: Page, page_number: int, reason: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        body = clean(page.locator("body").inner_text(timeout=5000))
    except Exception:
        body = ""
    payload = {
        "capturado_em": now_iso(),
        "pagina": page_number,
        "motivo": reason,
        "url_final": page.url,
        "titulo": page.title(),
        "texto_inicial": body[:6000],
        "links_totais": page.locator("a").count(),
    }
    (DEBUG_DIR / f"diagnostico-{page_number}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        page.screenshot(
            path=str(DEBUG_DIR / f"pagina-{page_number}.png"),
            full_page=False,
        )
    except Exception:
        pass


def collect_page(page: Page, page_number: int) -> list[dict[str, Any]]:
    url = BASE_URL if page_number == 1 else f"{BASE_URL}?pag={page_number}"
    print(f"\n[página {page_number}] {url}")

    response = page.goto(url, wait_until="domcontentloaded", timeout=75_000)
    status = response.status if response else None
    print(f"[http] {status} | {page.url}")

    page.wait_for_timeout(1600)

    anchors = page.locator('a[href*="/imovel/"]')
    count = anchors.count()
    print(f"[links candidatos] {count}")

    grouped: dict[str, str] = {}

    for index in range(min(count, 500)):
        anchor = anchors.nth(index)
        try:
            href = anchor.get_attribute("href") or ""
            text = clean(anchor.inner_text(timeout=1500))
            if not card_is_valid(text, href):
                continue

            url_fonte = canonical_url(href)
            current = grouped.get(url_fonte)
            if current is None or len(text) > len(current):
                grouped[url_fonte] = text
        except Exception:
            continue

    print(f"[cards válidos] {len(grouped)}")

    if not grouped:
        save_diagnostic(page, page_number, "nenhum_card_valido")
        return []

    captured_at = now_iso()
    output = []

    for url_fonte, text in grouped.items():
        prices = all_prices(text)
        if not prices:
            continue

        code = property_code_from_text(text)
        modalidade = infer_modality(text)
        encerramento = parse_datetime_br(
            re.search(r"(?:Encerra em:|Data de encerramento:)\s*([^|]+)", text, re.I).group(1)
            if re.search(r"(?:Encerra em:|Data de encerramento:)\s*([^|]+)", text, re.I)
            else text
        )

        if is_expired(encerramento, modalidade):
            continue

        preco = prices[0]
        avaliacao = prices[1] if len(prices) >= 2 and prices[1] >= preco else None
        desconto = infer_discount(text)

        cidade, uf = infer_city_state(text)
        tipo = infer_type(text)
        endereco = infer_address(text, code)

        accepts_fgts = bool(re.search(r"\bFGTS\b", text, re.I))
        accepts_financing = bool(re.search(r"\bFinanciamento\b", text, re.I))

        # Título: até o código do imóvel.
        title_match = re.search(
            r"((?:Agência|Apartamento|Área Industrial|Área Rural|Casa|Comercial|Galpão|Garagem|Imovel Rural|Imóvel Rural|Outros|Terreno|Imóvel).+?\s-\s\d{5,10})\b",
            text,
            re.I,
        )
        titulo = clean(title_match.group(1)) if title_match else f"{tipo} em leilão em {cidade} / {uf}"

        item = {
            "id": stable_id(url_fonte, text),
            "mercado": "leilao",
            "modalidade_leilao": modalidade,
            "titulo": titulo[:220],
            "bairro": "Não identificado",
            "cidade": cidade,
            "uf": uf,
            "tipo": tipo,
            "preco": preco,
            "valor_avaliacao": avaliacao,
            "desconto_percentual": desconto,
            "data_encerramento": encerramento,
            "banco": infer_bank(text),
            "aceita_fgts": accepts_fgts,
            "aceita_financiamento": accepts_financing,
            "area_construida": None,
            "area_terreno": None,
            "quartos": None,
            "banheiros": None,
            "vagas": None,
            "fonte": "Leilão Imóvel",
            "url_fonte": url_fonte,
            "telefone_anunciante": None,
            "descricao": text[:1800],
            "confianca": 82,
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
                {"data": today_iso(), "preco": preco}
            ],
        }
        output.append(item)

    print(f"[normalizados ativos] {len(output)}")
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


def merge_history(
    collected: list[dict[str, Any]],
    existing: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    today = today_iso()
    output = []

    for item in collected:
        old = existing.get(item["id"])
        if old:
            history = list(old.get("historico_precos") or [])
            previous_price = (
                history[-1].get("preco")
                if history
                else old.get("preco")
            )

            if not history or history[-1].get("preco") != item["preco"]:
                history.append({"data": today, "preco": item["preco"]})

            item["historico_precos"] = history[-36:]
            item["preco_reduzido"] = bool(
                previous_price and item["preco"] < previous_price
            )
            item["primeira_captura"] = old.get(
                "primeira_captura",
                item["primeira_captura"],
            )
            item["novo"] = False

            # Preserva enriquecimentos futuros.
            for field in [
                "latitude", "longitude", "localizacao_precisao",
                "geocoding_query", "geocoding_display_name",
                "area_construida", "area_terreno",
                "quartos", "banheiros", "vagas",
                "url_oficial_leiloeiro", "leiloeiro",
                "url_edital", "url_matricula",
            ]:
                if old.get(field) not in (None, "") and item.get(field) in (None, ""):
                    item[field] = old.get(field)

        output.append(item)

    return output


def merge_incremental(
    fresh: list[dict[str, Any]],
    existing: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Mantém registros anteriores e atualiza os encontrados nesta execução.

    Isso permite ampliar a cobertura nacional gradualmente sem apagar imóveis
    válidos que não estavam nas páginas visitadas nesta execução.
    """
    merged = dict(existing)
    for item in merge_history(fresh, existing):
        merged[item["id"]] = item

    records = list(merged.values())
    records.sort(
        key=lambda x: (
            x.get("data_encerramento") or "9999",
            -(x.get("desconto_percentual") or 0),
        )
    )
    return records


def main() -> int:
    SOURCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    existing = load_existing()
    fresh_by_id: dict[str, dict[str, Any]] = {}
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
                records = collect_page(page, page_number)
            except Exception as exc:
                print(
                    f"[erro página {page_number}] "
                    f"{type(exc).__name__}: {exc}"
                )
                records = []

            new_count = 0
            for item in records:
                if item["id"] not in fresh_by_id:
                    fresh_by_id[item["id"]] = item
                    new_count += 1

            print(
                f"[acumulado desta execução] "
                f"{len(fresh_by_id)} anúncios únicos ativos"
            )

            if not records or new_count == 0:
                empty_streak += 1
            else:
                empty_streak = 0

            if empty_streak >= 2:
                print("[fim] duas páginas consecutivas sem novos anúncios válidos")
                break

            time.sleep(PAGE_DELAY_SECONDS)

        browser.close()

    fresh = list(fresh_by_id.values())
    if not fresh:
        print(
            "[falha segura] nenhum anúncio válido coletado; "
            "base anterior preservada.",
            file=sys.stderr,
        )
        return 2

    final_records = merge_incremental(fresh, existing)
    SOURCE_FILE.write_text(
        json.dumps(final_records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\n[sucesso]")
    print(f"  novos/atualizados nesta execução: {len(fresh)}")
    print(f"  total acumulado da fonte: {len(final_records)}")
    print(f"  páginas visitadas: até {MAX_PAGES}")
    print(f"  arquivo: {SOURCE_FILE}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
