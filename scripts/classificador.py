import re
import unicodedata
from typing import Dict, Tuple

TERMOS_LEILAO = (
    "leilao", "leilão", "judicial", "extrajudicial", "alienacao fiduciaria",
    "alienação fiduciária", "1º leilao", "2º leilao", "primeiro leilao",
    "segundo leilao", "lance inicial", "edital", "imovel ocupado",
    "imóvel ocupado", "venda direta online", "licitacao aberta",
    "licitação aberta", "propriedade fiduciaria", "propriedade fiduciária"
)

TERMOS_EXCLUSAO_TRADICIONAL = (
    "fracao ideal", "fração ideal", "direitos aquisitivos", "cessao de direitos",
    "cessão de direitos", "cota imobiliaria", "cota imobiliária"
)

def normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", texto.lower()).strip()

def classificar_anuncio(anuncio: Dict) -> Tuple[str, str]:
    campos = " ".join(str(anuncio.get(k, "")) for k in (
        "titulo", "descricao", "fonte", "modalidade", "observacoes", "url"
    ))
    original = campos.lower()
    texto = normalizar(campos)

    if any(normalizar(t) in texto for t in TERMOS_LEILAO):
        return "leilao", "termo_de_leilao"
    if any(normalizar(t) in texto for t in TERMOS_EXCLUSAO_TRADICIONAL):
        return "outros", "direito_ou_fracao"
    if anuncio.get("origem_bancaria") is True:
        return "leilao", "origem_bancaria"
    return "tradicional", "sem_indicio_de_leilao"

def elegivel_para_estatistica_tradicional(anuncio: Dict) -> bool:
    mercado, _ = classificar_anuncio(anuncio)
    return mercado == "tradicional"
