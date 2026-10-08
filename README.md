# Registro prospectivo de carteiras da Hyperliquid

Este repositório anota, uma vez por hora, as posições de três grupos de carteiras do ranking público da
Hyperliquid. O objetivo é responder com dados a uma pergunta só: **copiar quem aparece ganhando no ranking dá
resultado depois dos custos?**

Nada aqui é recomendação de investimento, e nenhuma ordem é enviada por este código. Todos os dados vêm de páginas
públicas da Hyperliquid; os endereços são endereços públicos de blockchain.

## Por que anotar daqui para a frente
Escolher hoje quem mais ganhou e olhar o passado dessas mesmas carteiras mostra lucro por construção: elas foram
escolhidas por terem lucrado. O teste honesto é fixar a regra de escolha antes, anotar o que as carteiras
escolhidas fazem depois, e comparar com um grupo de controle sorteado.

## Regra de escolha (`wallets_forward_v1`, fixada antes do primeiro registro)
No primeiro registro de cada mês (UTC) o ranking é baixado e forma-se um grupo novo ("coorte"):

- **Elegíveis:** saldo da conta de pelo menos US$ 100 mil; volume negociado no mês maior que zero; giro do mês
  (volume / saldo) de no máximo 50 vezes. Acima disso é formador de mercado ou alta frequência, que não dá para
  copiar de hora em hora.
- **Grupo W, "consistentes":** lucro maior que zero na semana, no mês e no total; as 30 de maior retorno no mês.
- **Grupo R, "recentes":** fora do W, lucro maior que zero na semana; as 30 de maior retorno na semana.
- **Grupo C, "controle":** 30 sorteadas entre as elegíveis que sobraram. A semente do sorteio é o sha256 do próprio
  ranking baixado, que fica guardado: qualquer pessoa refaz o sorteio.

Empates são desfeitos pelo endereço. Cada coorte é acompanhada por 90 dias.

## O que é anotado
Para cada carteira de uma coorte ativa: saldo da conta, valor total das posições e cada posição (moeda, tamanho com
sinal, preço médio de entrada, valor, resultado não realizado). Para as moedas com posição: preço médio do livro,
preço de marcação, preço do oráculo e funding da hora. Só o estado do momento é pedido; nenhum histórico é baixado.

## Avaliação (escrita antes de existir qualquer dado)
- Só depois de 90 dias de registros, com pelo menos 80% das horas cobertas.
- **Carteira cópia de um grupo:** média, entre as carteiras do grupo, dos pesos de cada uma (valor da posição com
  sinal dividido pelo saldo, com a soma dos módulos limitada a 5 por carteira).
- **Atraso:** os pesos vistos num registro só valem a partir do registro seguinte. O copiador nunca opera no
  instante em que observa.
- **Custos:** 5,5 bp sobre cada mudança de peso, mais o funding anotado.
- **H1:** o retorno diário médio da cópia do grupo W é maior que o da cópia do grupo C (reamostragem pareada por
  dias, unilateral) e o retorno líquido médio do W é positivo. **H2:** o mesmo para o grupo R. São duas hipóteses,
  então cada uma precisa de p < 0,025.
- Se nenhuma se confirmar, a conclusão é que seguir carteiras do ranking não funciona nestas condições. Se alguma
  se confirmar, ela vira candidata a uma segunda janela de 90 dias, independente, antes de qualquer simulação.

## Arquivos
- `wallets_forward.py`: o programa inteiro (só biblioteca padrão do Python).
- `data/live/wallets/index.jsonl`: a ordem dos registros e o hash de cada um.
- `data/live/wallets/cohorts/`: a formação de cada coorte.
- `data/live/wallets/snapshots/`: o ranking baixado na formação, comprimido.
- `data/live/wallets/ticks/`: um arquivo por registro de posições.

Cada registro traz o hash do anterior. Editar ou apagar um registro antigo quebra a cadeia:

```
python wallets_forward.py --verify
python wallets_forward.py --status
```

## Limites conhecidos
- A tarefa agendada pode atrasar ou falhar; as horas sem registro ficam como lacunas e contam na cobertura.
- O endereço do ranking não é uma página oficial documentada da Hyperliquid e pode mudar.
- Uma carteira pode ter proteção em outra bolsa; o que aparece aqui é só a posição na Hyperliquid.

## Mudanças na coleta
- **2026-10-08, primeiro dia:** a tarefa agendada rodava uma vez por hora e o agendador gratuito do GitHub pulou
  cinco das sete primeiras horas. A partir de agora ela tenta quatro vezes por hora, e o programa grava no máximo um
  registro por hora cheia (UTC). A regra de escolha e a de avaliação não mudaram; as horas já perdidas continuam
  contando como lacunas.
