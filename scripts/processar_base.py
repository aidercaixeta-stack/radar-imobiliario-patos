#!/usr/bin/env python3
"""Pipeline multifuente do Radar Imobiliário Patos.

Fluxo:
1. opcionalmente salva a base recém-coletada como uma fonte;
2. lê todos os arquivos data/fontes/*.json;
3. classifica e exclui conteúdo irrelevante;
4. separa mercado tradicional, leilão e itens não comparáveis;
5. deduplica anúncios dentro da mesma fonte e entre fontes;
6. preserva todas as ofertas/URLs originais no imóvel consolidado;
7. grava data/imoveis.json, data/excluidos.json e data/meta.json.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SOURCES_DIR = DATA_DIR / "fontes"
FINAL_FILE = DATA_DIR / "imoveis.json"
EXCLUDED_FILE = DATA_DIR / "excluidos.json"
META_FILE = DATA_DIR / "meta.json"
RULES_FILE = ROOT / "config" / "regras_classificacao.json"
TZ_BR = timezone(timedelta(hours=-3))


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def text_blob(item: dict[str, Any]) -> str:
    values = [
        item.get("titulo"),
        item.get("descricao"),
        item.get("bairro"),
        item.get("tipo"),
        item.get("endereco_extraido"),
        item.get("geocoding_display_name"),
    ]
    return " ".join(str(v) for v in values if v)


def fold(text: str | None) -> str:
    raw = unicodedata.normalize("NFKD", (text or "").lower())
    return re.sub(
        r"\s+",
        " ",
        "".join(ch for ch in raw if not unicodedata.combining(ch)),
    ).strip()


def normalized_phone(item: dict[str, Any]) -> str:
    return re.sub(r"\D", "", str(item.get("telefone_anunciante") or ""))


def normalized_address(item: dict[str, Any]) -> str:
    value = fold(item.get("endereco_extraido") or "")
    value = re.sub(r"\b(rua|r|avenida|av|travessa|tv|praca|pca)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def normalized_description(item: dict[str, Any]) -> str:
    value = fold(item.get("descricao") or item.get("titulo") or "")
    value = re.sub(r"\b\d{2}\s*9?\d{4}[- ]?\d{4}\b", " ", value)
    value = re.sub(r"\br\$\s*[\d\.,]+\b", " ", value)
    return re.sub(r"[^a-z0-9 ]+", " ", value).strip()[:1200]


def normalized_title(item: dict[str, Any]) -> str:
    value = fold(item.get("titulo") or "")
    value = re.sub(r"\br\$\s*[\d\.,]+\b", " ", value)
    return re.sub(r"[^a-z0-9 ]+", " ", value).strip()[:500]


def compile_patterns(patterns: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.I | re.S) for pattern in patterns]


def matches_any(text: str, patterns: list[str]) -> bool:
    return any(rx.search(text) for rx in compile_patterns(patterns))


def classify(
    item: dict[str, Any],
    rules: dict[str, Any],
) -> tuple[str, str | None, str | None]:
    text = text_blob(item)

    try:
        valid_price = float(item.get("preco")) > 1000
    except (TypeError, ValueError):
        valid_price = False

    if not valid_price:
        return "excluir", "dados_invalidos", "Preço ausente ou inválido"

    if len(fold(item.get("descricao") or item.get("titulo") or "")) < 12:
        return "excluir", "dados_invalidos", "Anúncio sem descrição suficiente"

    for rule in rules.get("exclusoes", []):
        if matches_any(text, rule.get("patterns", [])):
            return "excluir", rule.get("id"), rule.get("motivo")

    for rule in rules.get("nao_comparaveis", []):
        if matches_any(text, rule.get("patterns", [])):
            return "nao_comparavel", rule.get("id"), rule.get("categoria")

    if matches_any(text, rules.get("leilao", {}).get("patterns", [])):
        item["mercado"] = "leilao"
    elif item.get("mercado") not in {"tradicional", "leilao"}:
        item["mercado"] = "tradicional"

    return "incluir", None, None


def source_name(item: dict[str, Any], fallback: str) -> str:
    return str(item.get("fonte") or fallback)


def completeness_score(item: dict[str, Any]) -> int:
    fields = [
        "titulo", "descricao", "bairro", "tipo", "preco",
        "area_construida", "area_terreno", "quartos", "vagas",
        "telefone_anunciante", "latitude", "longitude", "endereco_extraido",
    ]
    return sum(
        1 for field in fields
        if item.get(field) not in (None, "", "Não identificado")
    )


def relative_difference(a: Any, b: Any) -> float:
    try:
        x, y = float(a), float(b)
    except (TypeError, ValueError):
        return 1.0
    if x <= 0 or y <= 0:
        return 1.0
    return abs(x - y) / max(x, y)


def item_area(item: dict[str, Any]) -> float | None:
    area = item.get("area_construida") or item.get("area_terreno")
    try:
        return float(area) if area else None
    except (TypeError, ValueError):
        return None


def haversine_meters(
    a: dict[str, Any],
    b: dict[str, Any],
) -> float | None:
    try:
        lat1, lon1 = float(a["latitude"]), float(a["longitude"])
        lat2, lon2 = float(b["latitude"]), float(b["longitude"])
    except (KeyError, TypeError, ValueError):
        return None

    radius = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    value = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * radius * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def text_similarity(a: str, b: str) -> float:
    if len(a) < 20 or len(b) < 20:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def similar_description(a: dict[str, Any], b: dict[str, Any]) -> float:
    return text_similarity(
        normalized_description(a),
        normalized_description(b),
    )


def same_property(
    a: dict[str, Any],
    b: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[bool, int, str]:
    """Deduplicação conservadora dentro da mesma fonte e entre fontes."""

    if (a.get("mercado") or "tradicional") != (b.get("mercado") or "tradicional"):
        return False, 0, "mercado_diferente"

    if fold(str(a.get("tipo"))) != fold(str(b.get("tipo"))):
        return False, 0, "tipo_diferente"

    same_source = source_name(a, "") == source_name(b, "")

    max_price = float(settings.get("diferenca_preco_maxima", 0.07))
    max_area = float(settings.get("diferenca_area_maxima", 0.12))
    max_distance = float(settings.get("distancia_metros_maxima", 40))
    min_desc = float(settings.get("descricao_similaridade_minima", 0.86))

    price_diff = relative_difference(a.get("preco"), b.get("preco"))
    price_ok = price_diff <= max_price

    area_a, area_b = item_area(a), item_area(b)
    area_diff = relative_difference(area_a, area_b) if area_a and area_b else 1.0
    area_ok = bool(area_a and area_b and area_diff <= max_area)

    phone_a, phone_b = normalized_phone(a), normalized_phone(b)
    desc_similarity = similar_description(a, b)
    title_similarity = text_similarity(normalized_title(a), normalized_title(b))

    addr_a, addr_b = normalized_address(a), normalized_address(b)
    same_address = bool(len(addr_a) >= 8 and addr_a == addr_b)

    distance = haversine_meters(a, b)

    if same_source:
        # Dentro da própria fonte, exigimos sinais ainda mais fortes para evitar
        # apagar imóveis distintos que por acaso tenham características parecidas.

        # Mesmo texto praticamente idêntico + mesmo preço.
        if desc_similarity >= 0.96 and price_diff <= 0.02:
            return True, 99, "mesma_fonte_descricao_preco"

        # Título praticamente idêntico + preço + área compatíveis.
        if title_similarity >= 0.97 and price_diff <= 0.02 and area_ok:
            return True, 98, "mesma_fonte_titulo_preco_area"

        # Endereço idêntico + preço e área muito próximos.
        if same_address and price_diff <= 0.02 and area_ok:
            return True, 98, "mesma_fonte_endereco_preco_area"

        # Mesmo telefone + descrição muito semelhante.
        if phone_a and phone_a == phone_b and desc_similarity >= 0.94 and price_diff <= 0.03:
            return True, 97, "mesma_fonte_telefone_descricao"

        return False, 0, "mesma_fonte_sem_evidencia_forte"

    # Regras entre fontes diferentes.
    if phone_a and phone_a == phone_b and desc_similarity >= min_desc:
        return True, 98, "telefone_e_descricao"

    if same_address and price_ok:
        return True, 96 if area_ok else 92, "endereco_e_preco"

    if distance is not None and distance <= max_distance and price_ok and area_ok:
        return True, 94, "localizacao_preco_area"

    if phone_a and phone_a == phone_b and price_ok and area_ok:
        return True, 90, "telefone_preco_area"

    return False, 0, "sem_evidencia_forte"


def offer_from_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "fonte": source_name(item, "Fonte desconhecida"),
        "preco": item.get("preco"),
        "url_fonte": item.get("url_fonte"),
        "id_origem": item.get("id"),
    }


def unique_offers(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    offers = []
    seen = set()
    for item in group:
        offer = offer_from_item(item)
        key = (
            offer.get("fonte"),
            offer.get("url_fonte") or offer.get("id_origem"),
        )
        if key in seen:
            continue
        seen.add(key)
        offers.append(offer)
    return sorted(offers, key=lambda x: x.get("preco") or 10**30)


def merge_records(group: list[dict[str, Any]]) -> dict[str, Any]:
    base = max(group, key=completeness_score)
    result = copy.deepcopy(base)

    offers = unique_offers(group)
    sources = []
    for offer in offers:
        if offer["fonte"] not in sources:
            sources.append(offer["fonte"])

    priced = [
        item for item in group
        if isinstance(item.get("preco"), (int, float))
    ]
    if priced:
        cheapest = min(priced, key=lambda item: item["preco"])
        result["preco"] = cheapest["preco"]
        result["fonte"] = source_name(
            cheapest,
            source_name(result, "Fonte desconhecida"),
        )
        result["url_fonte"] = cheapest.get("url_fonte")

    result["fontes_encontradas"] = sources
    result["ofertas"] = offers
    result["quantidade_ofertas"] = len(offers)
    result["quantidade_fontes"] = len(sources)
    result["duplicado"] = len(group) > 1
    result["duplicado_multifonte"] = len(sources) > 1
    result["duplicado_mesma_fonte"] = len(group) > 1 and len(sources) == 1
    result["quantidade_anuncios_agrupados"] = len(group)

    return result


def deduplicate(
    items: list[dict[str, Any]],
    settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    # Remove IDs exatamente repetidos.
    by_id: dict[str, dict[str, Any]] = {}
    no_id = []
    for item in items:
        if item.get("id"):
            current = by_id.get(item["id"])
            if current is None or completeness_score(item) > completeness_score(current):
                by_id[item["id"]] = item
        else:
            no_id.append(item)

    pool = list(by_id.values()) + no_id
    used = set()
    groups: list[list[dict[str, Any]]] = []

    stats = {
        "total_grupos": 0,
        "mesma_fonte": 0,
        "multifonte": 0,
        "anuncios_agrupados": 0,
    }

    for i, item in enumerate(pool):
        if i in used:
            continue

        group = [item]
        used.add(i)

        # Faz expansão do grupo para capturar relações transitivas conservadoras.
        changed = True
        while changed:
            changed = False
            for j, candidate in enumerate(pool):
                if j in used:
                    continue

                matched = False
                best_confidence = 0
                best_reason = ""

                for member in group:
                    match, confidence, reason = same_property(
                        member,
                        candidate,
                        settings,
                    )
                    if match and confidence > best_confidence:
                        matched = True
                        best_confidence = confidence
                        best_reason = reason

                if matched:
                    current = copy.deepcopy(candidate)
                    current["_dedupe_confidence"] = best_confidence
                    current["_dedupe_reason"] = best_reason
                    group.append(current)
                    used.add(j)
                    changed = True

        if len(group) > 1:
            sources = {source_name(x, "") for x in group}
            stats["total_grupos"] += 1
            stats["anuncios_agrupados"] += len(group)
            if len(sources) > 1:
                stats["multifonte"] += 1
            else:
                stats["mesma_fonte"] += 1

        groups.append(group)

    return [merge_records(group) for group in groups], stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ingest-current-as",
        metavar="NOME",
        help=(
            "Salva o data/imoveis.json atual como data/fontes/NOME.json "
            "antes de consolidar."
        ),
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)

    rules = load_json(RULES_FILE, {})
    if not rules:
        raise SystemExit(f"Regras não encontradas ou inválidas: {RULES_FILE}")

    if args.ingest_current_as:
        current = load_json(FINAL_FILE, [])
        if not isinstance(current, list) or not current:
            raise SystemExit(
                "A base recém-coletada está vazia; consolidação cancelada."
            )
        source_path = SOURCES_DIR / f"{args.ingest_current_as}.json"
        save_json(source_path, current)
        print(f"[ingestão] {len(current)} registros salvos em {source_path}")

    source_files = sorted(SOURCES_DIR.glob("*.json"))
    if not source_files:
        raise SystemExit("Nenhuma fonte disponível em data/fontes/*.json")

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    non_comparable: list[dict[str, Any]] = []

    source_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "capturados": 0,
            "incluidos": 0,
            "excluidos": 0,
            "nao_comparaveis": 0,
        }
    )
    exclusion_reasons: Counter[str] = Counter()

    for path in source_files:
        records = load_json(path, [])
        if not isinstance(records, list):
            print(f"[aviso] ignorando fonte inválida: {path}")
            continue

        fallback_source = path.stem
        print(f"[fonte] {path.name}: {len(records)} registros")

        for raw in records:
            if not isinstance(raw, dict):
                continue

            item = copy.deepcopy(raw)
            src = source_name(item, fallback_source)
            source_stats[src]["capturados"] += 1

            destination, code, detail = classify(item, rules)
            item["classificacao_processada_em"] = datetime.now(TZ_BR).isoformat()

            if destination == "excluir":
                item["motivo_exclusao"] = detail
                item["codigo_exclusao"] = code
                excluded.append(item)
                source_stats[src]["excluidos"] += 1
                exclusion_reasons[detail or code or "Outro"] += 1
                continue

            if destination == "nao_comparavel":
                item["mercado"] = "nao_comparavel"
                item["categoria_nao_comparavel"] = detail
                non_comparable.append(item)
                source_stats[src]["nao_comparaveis"] += 1
                continue

            item["status"] = item.get("status") or "ativo"
            included.append(item)
            source_stats[src]["incluidos"] += 1

    candidates = included + non_comparable
    consolidated, dedupe_stats = deduplicate(
        candidates,
        rules.get("deduplicacao", {}),
    )

    consolidated.sort(
        key=lambda x: (
            x.get("mercado") == "tradicional",
            x.get("nota_oportunidade", 0),
            x.get("preco", 0),
        ),
        reverse=True,
    )

    save_json(FINAL_FILE, consolidated)
    save_json(EXCLUDED_FILE, excluded)

    precision_counter = Counter(
        item.get("localizacao_precisao") or "nao_localizado"
        for item in consolidated
    )
    market_counter = Counter(
        item.get("mercado") or "tradicional"
        for item in consolidated
    )

    meta = {
        "updated_at": datetime.now(TZ_BR).isoformat(),
        "status": "base_multifuente_processada",
        "records": len(consolidated),
        "captured_before_filters": sum(
            v["capturados"] for v in source_stats.values()
        ),
        "excluded_records": len(excluded),
        "duplicate_groups": dedupe_stats["total_grupos"],
        "deduplication": dedupe_stats,
        "markets": {
            "tradicional": market_counter.get("tradicional", 0),
            "leilao": market_counter.get("leilao", 0),
            "nao_comparavel": market_counter.get("nao_comparavel", 0),
        },
        "mapped_records": sum(
            1 for item in consolidated
            if item.get("latitude") is not None
            and item.get("longitude") is not None
        ),
        "location_precision": dict(precision_counter),
        "sources": dict(source_stats),
        "exclusion_reasons": dict(exclusion_reasons),
        "note": (
            "Base processada por pipeline multifuente. Conteúdo irrelevante é "
            "separado em data/excluidos.json; itens não comparáveis ficam fora "
            "do mercado tradicional; duplicidades são agrupadas preservando "
            "todas as ofertas e links originais."
        ),
    }
    save_json(META_FILE, meta)

    print("\n[resultado]")
    print(f"  Capturados: {meta['captured_before_filters']}")
    print(f"  Excluídos: {len(excluded)}")
    print(f"  Base final: {len(consolidated)}")
    print(
        "  Duplicidades: "
        f"{dedupe_stats['total_grupos']} grupos "
        f"({dedupe_stats['mesma_fonte']} mesma fonte; "
        f"{dedupe_stats['multifonte']} multifonte)"
    )

    if exclusion_reasons:
        print("  Motivos de exclusão:")
        for reason, count in exclusion_reasons.most_common():
            print(f"    - {reason}: {count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
