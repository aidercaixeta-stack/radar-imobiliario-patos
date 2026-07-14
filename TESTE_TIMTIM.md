# Teste do primeiro coletor real — TIMTIM

Esta versão ainda não ativa coleta diária.

## O que o teste faz

1. Abre as três páginas públicas:
   - Imóveis Venda
   - Lotes
   - Apartamento Venda
2. Procura os botões **Ver Detalhes**.
3. Abre cada modal e extrai os dados disponíveis.
4. Rejeita textos identificados como aluguel.
5. Mantém leilões fora do mercado tradicional quando palavras de leilão forem encontradas.
6. Só substitui `data/imoveis.json` se pelo menos um imóvel válido for coletado.

## Como executar

No GitHub, abra **Actions** → **Testar coletor TIMTIM** → **Run workflow**.

Depois aguarde o resultado. Se a execução ficar verde, o workflow cria um commit com os dados coletados. Se falhar, a base atual permanece intacta.

## Limitações desta primeira versão

- O site TIMTIM concentra muitos dados em texto livre; bairro, área, quartos e vagas são extraídos por padrões de texto e precisam ser validados com dados reais.
- O mapa só exibirá anúncios com coordenadas. Nesta primeira coleta, coordenadas não são inventadas: ficam vazias quando não houver endereço preciso.
- A nota de oportunidade só se torna comparativa quando houver pelo menos três imóveis comparáveis do mesmo tipo e bairro com área identificada.
