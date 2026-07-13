from __future__ import annotations
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import json

ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = ROOT / "data" / "imoveis.json"
META_FILE = ROOT / "data" / "meta.json"

def carregar_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def salvar_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temp.replace(path)

def main() -> None:
    # Nesta primeira versão, preservamos a base de demonstração.
    # Os coletores reais serão adicionados um por vez em scripts/fontes/.
    imoveis = carregar_json(DATA_FILE, [])
    agora = datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds")

    meta = {
        "updated_at": agora,
        "status": "demo",
        "message": "Rotina automática executada. Coletores reais ainda não ativados.",
        "total_registros": len(imoveis)
    }

    salvar_json(DATA_FILE, imoveis)
    salvar_json(META_FILE, meta)
    print(f"Atualização concluída: {len(imoveis)} registros em {agora}")

if __name__ == "__main__":
    main()
