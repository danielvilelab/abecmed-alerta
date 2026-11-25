# abecmed-alerta

A cada 15 minutos, das 8h às 22h, o script verifica a ABECMED. **Só chega push quando entra item novo nas condições**, com a **tabela da ABECMED** (flores, da mais
barata para a mais cara, e concentrados), destacando com ✅ o que está nas condições:

- 🌸 **flor abaixo de R$ 60/grama**
- 🧪 **qualquer concentrado disponível**

Quando o push sai:

| Situação | Título | Som |
|---|---|---|
| item **novo** nas condições | 🚨 CORRE: … | **alto** (prioridade 5) |
| itens nas condições, já avisados | — | **sem push** |
| nada nas condições | — | **sem push** |

Se um item esgota e volta, conta como novo e avisa de novo.

Baseado em [guimarques1/abecmed-watch](https://github.com/guimarques1/abecmed-watch) (MIT),
com a autodetecção da versão do bot de [guipolonca-git/abec-notificacoes](https://github.com/guipolonca-git/abec-notificacoes).

## Intervalo

A cada **15 minutos, das 8h às 22h** (Brasília). Cada verificação abre ~4
conversas no bot da associação; 24h a cada 5 min seriam ~35 mil conversas/mês
com o mesmo CPF, o que pesa na cota do Typebot deles e chama atenção. Com a
janela e os 15 min, cai ~80%. Fora da janela o `bot.py` não consulta nada,
mesmo se for chamado (a rodada manual "Run workflow" ignora a trava).

## Passo a passo (GitHub Actions)

### 1. App no celular
Instale o **ntfy** (Play Store / App Store, grátis). Toque em **+** e assine um
nome de tópico difícil de adivinhar, ex.: `abecmed-` + 8 letras/números
aleatórios. Tópicos do ntfy são públicos: quem souber o nome vê seus avisos.

No Android: em **Configurações do app ntfy → Notificações**, permita que
prioridade máxima ignore o "Não perturbe".

### 2. Repositório
Crie um repositório **novo, privado** na sua conta (não use fork: o GitHub não
roda agendamento em fork). Público mostraria a todos que você monitora a
ABECMED. Em privado, cada execução gasta ≥1 min da cota grátis de 2.000
min/mês: a cada 15 min, 8h–22h, dá ~1.700 + a reserva ~200. Não aperte o
intervalo sem conferir a cota em Settings → Billing. Envie estes arquivos
para ele. O `config.ini` (com seu CPF) **nunca** vai junto; já está no `.gitignore`.

### 3. Secrets
**Settings → Secrets and variables → Actions → New repository secret**:

| Nome | Valor |
|---|---|
| `ABECMED_CPF` | seu CPF, só números |
| `NTFY_TOPIC` | o tópico do passo 1 |
| `HEALTHCHECK_URL` | opcional, passo 6 |

Opcional, em **Variables**: `PRECO_MAX_FLOR` para mudar o limite (padrão 60).

### 4. Permissão
**Settings → Actions → General → Workflow permissions → Read and write**.

### 5. Relógio (cron-job.org)
O agendador do próprio GitHub atrasa ou pula execuções. O relógio confiável é
o **cron-job.org** (grátis) chamando o GitHub a cada 15 min, das 8h às 22h:

1. Crie um token em **GitHub → Settings → Developer settings → Fine-grained
   tokens**, com acesso **só a este repositório** e permissão
   **Actions: Read and write** (só dispara o workflow; não mexe no código). Anote a validade.
2. No cron-job.org: **Create cronjob → Import from cURL**, cole e ajuste:

```bash
curl -X POST https://api.github.com/repos/SEU_USUARIO/SEU_REPO/actions/workflows/check.yml/dispatches -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" -H "Authorization: Bearer SEU_TOKEN" -d '{"ref":"main","inputs":{"origem":"cron"}}'
```

3. Agendamento: crontab `*/15 8-21 * * *`, fuso America/Sao_Paulo. **Test run** respondendo `204` = ok.

### 6. Vigia (opcional, recomendado)
No **healthchecks.io**: novo check, período 15 min, tolerância 11 h (cobre a noite sem verificação),
integração **ntfy** no mesmo tópico. Cole a Ping URL no secret
`HEALTHCHECK_URL`. Se o monitor parar (token vencido, GitHub fora), você fica sabendo.

### 7. Primeiro teste
Aba **Actions → check → Run workflow**. Chega no celular
**"abecmed-alerta ativo"** com a situação atual. Depois disso, só os alertas.

## Rodando no computador (para testar)

```bash
./configurar.sh                  # pede CPF e tópico, grava config.ini local
python3 bot.py --teste-push      # celular deve tocar alto
python3 bot.py --dry-run         # mostra o catálogo e marca o que daria alerta
```

## Quando quebrar

Se a ABECMED mudar o fluxo, chega **"abecmed-alerta quebrou"** com as opções que
o script viu (uma vez só, não a cada verificação). Os ajustes ficam no topo do `bot.py`:
`NAV_RX` (botões de navegação), `SEM_ESTOQUE_RX` e `PRECO_RX`.

MIT. Sem vínculo com a ABECMED.
