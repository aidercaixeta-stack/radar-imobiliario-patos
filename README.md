# Radar Imobiliário Patos

Versão inicial do radar de imóveis de Patos de Minas.

## O que já funciona

- Interface responsiva para computador e celular
- Mapa interativo
- Filtros por bairro, tipo, preço, área e destaque
- Separação obrigatória entre mercado tradicional e leilões
- Favoritos salvos no navegador
- Histórico de preços
- Nota de oportunidade
- Estrutura PWA para instalação no celular
- Rotina automática diária configurada para 07:12 em `America/Sao_Paulo`

## Estado atual

Os imóveis exibidos são **dados de demonstração**. Os coletores reais serão implantados por fonte, depois da validação técnica e das condições de acesso de cada site.

## Regra de qualidade

Anúncios de leilão encontrados em portais mistos, como o Imovelweb, não entram nos cálculos do mercado tradicional.

## Publicação

Depois de enviar os arquivos ao GitHub:

1. Abra `Settings`
2. Entre em `Pages`
3. Em `Build and deployment`, escolha `Deploy from a branch`
4. Selecione `main` e a pasta `/ (root)`
5. Clique em `Save`
