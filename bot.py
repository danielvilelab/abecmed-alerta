#!/usr/bin/env python3
"""
abecmed-alerta — push no celular a CADA verificacao com a tabela da ABECMED,
destacando o que esta nas condicoes:

  1. flor abaixo de R$ 60/grama  (PRECO_MAX_FLOR)
  2. qualquer concentrado disponivel

Item novo nas condicoes toca alto; o resto chega com som normal ou calado.

Baseado em github.com/guimarques1/abecmed-watch (MIT) e na autodeteccao de
versao do bot de github.com/guipolonca-git/abec-notificacoes.

O site (bot.abecmed.com.br) e um Typebot. O script conversa com ele como um
navegador: paciente -> CPF -> menu -> flores (cada tipo: THC, CBD...) e
concentrados. Le os botoes de produto, que vem no formato
"[R$ 95] — La Fanta #2", e aplica as duas regras.


Configuracao (ambiente ganha do arquivo):
    ABECMED_CPF, NTFY_TOPIC, PRECO_MAX_FLOR   ou   config.ini

Uso:
    python3 bot.py --dry-run     # mostra o catalogo e o que daria alerta
    python3 bot.py               # uma verificacao (o GitHub Actions usa isso)
    python3 bot.py --loop        # fica rodando no computador
    python3 bot.py --teste-push  # manda um push de teste pro celular
"""

import argparse
import configparser
import gzip
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

BASE = "https://bot.abecmed.com.br"
FALLBACK_TYPEBOT = "bot-abecmed-2-2"
NTFY_BASE = "https://ntfy.sh"

PRECO_MAX_FLOR = 60.0  # destaca quando o preco/grama for MENOR que isso
BRT = timezone(timedelta(hours=-3))  # horario de Brasilia (sem horario de verao)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:154.0) "
        "Gecko/20100101 Firefox/154.0"
    ),
    "Accept": "*/*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "Origin": BASE,
    "Referer": BASE + "/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

TIMEOUT = 30
RETRIES = 3
INTERVALO_MIN = 10
INTERVALO_MAX = 12
# Janela de verificacao (horario de Brasilia): das 8h ate antes das 22h.
# Fora dela nao consulta o bot - de madrugada ninguem repoe estoque, e cada
# verificacao abre conversas no Typebot da associacao.
HORA_INICIO = 8
HORA_FIM = 22
# O agendador de reserva do GitHub nao roda se a ultima verificacao foi ha
# menos disso: evita verificacao dobrada quando o cron-job.org esta em dia.
MIN_ENTRE_VERIFICACOES = 8
BLOCK_TYPES = {"p", "ul", "ol", "li", "h1", "h2", "h3", "blockquote"}
MAX_MSG = 3500

# Botoes que sao navegacao, nao produto nem subcategoria.
NAV_RX = re.compile(r"voltar|adquirir|menu|in[ií]cio|status|pedido|sair|finalizar", re.I)
SEM_ESTOQUE_RX = re.compile(
    r"sem (estoque|disponibilidade)|esgotad|indispon[ií]vel", re.I
)
PRECO_RX = re.compile(r"R\$\s*([\d.]+(?:,\d{1,2})?)", re.I)
GO_ON = re.compile(r"ciente|entendi|continuar|prosseguir|ok|avan[çc]ar", re.I)
MENU_HINT = re.compile(r"flor|concentrad|[óo]leo", re.I)


class FlowError(Exception):
    """O fluxo do Typebot nao esta onde a gente esperava."""


# ---------------------------------------------------------------- HTTP


def _decode(resp):
    raw = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def post_json(url, payload, headers=None):
    body = json.dumps(payload).encode("utf-8")
    h = dict(BROWSER_HEADERS)
    h.update(headers or {})
    h = {k: v for k, v in h.items() if v is not None}

    last_error = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, data=body, method="POST", headers=h)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                text = _decode(resp)
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code < 500 and exc.code != 429:
                detail = exc.read().decode("utf-8", "replace")[:300]
                raise FlowError("HTTP %s em %s: %s" % (exc.code, url, detail)) from exc
        except Exception as exc:
            last_error = exc
        if attempt < RETRIES - 1:
            time.sleep(2 ** attempt * 2)
    raise FlowError("falhou apos %d tentativas em %s: %s" % (RETRIES, url, last_error))


def detect_typebot_slug():
    """A ABECMED troca a versao do bot (ex.: pix-pagamento -> bot-abecmed-2-2).
    Le o publicId direto da pagina pra nao quebrar a cada troca."""
    req = urllib.request.Request(
        BASE + "/",
        headers={
            "User-Agent": BROWSER_HEADERS["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            html = _decode(resp)
        m = re.search(r'"publishedTypebot"\s*:\s*\{[^}]*"publicId"\s*:\s*"([^"]+)"', html) or \
            re.search(r'\\?"publicId\\?"\s*:\s*\\?"([^"\\]+)', html)
        if m:
            return m.group(1)
    except Exception as exc:
        print("aviso: autodeteccao do bot falhou (%s)" % exc, file=sys.stderr)
    return FALLBACK_TYPEBOT


# ------------------------------------------------------- richText -> texto


def flatten(node):
    if isinstance(node, list):
        return "".join(flatten(n) for n in node)
    if not isinstance(node, dict):
        return ""
    children = node.get("children")
    if children is None:
        return node.get("text", "")
    inner = flatten(children)
    kind = node.get("type")
    if kind == "li":
        return "- " + inner.strip() + "\n"
    if kind in BLOCK_TYPES:
        return inner + "\n"
    return inner


def messages_to_text(messages):
    parts = []
    for msg in messages:
        content = msg.get("content") or {}
        if content.get("type") == "richText":
            parts.append(flatten(content.get("richText", [])))
    return "\n".join(p.strip() for p in parts if p.strip())


# ------------------------------------------------------------- conversa


class Chat:
    def __init__(self, slug):
        self.last = post_json(
            "%s/api/v1/typebots/%s/startChat" % (BASE, slug),
            {"isStreamEnabled": False, "prefilledVariables": {}, "isOnlyRegistering": False},
        )
        self.session_id = self.last.get("sessionId")
        if not self.session_id:
            raise FlowError("startChat nao devolveu sessionId")

    @property
    def items(self):
        payload = self.last.get("input") or {}
        return [i.get("content", "") for i in payload.get("items", [])]

    @property
    def input_type(self):
        return (self.last.get("input") or {}).get("type")

    @property
    def text(self):
        return messages_to_text(self.last.get("messages", []))

    def send(self, text):
        self.last = post_json(
            "%s/api/v1/sessions/%s/continueChat" % (BASE, self.session_id),
            {"message": {"type": "text", "text": text}},
        )
        return self.last

    def pick(self, pattern, required=True):
        rx = re.compile(pattern, re.I)
        for content in self.items:
            if rx.search(content):
                self.send(content)
                return content
        if required:
            raise FlowError("nenhuma opcao casou com /%s/ — opcoes: %r" % (pattern, self.items))
        return None


def abrir(cpf, slug):
    """Sessao nova, identificada, parada no menu principal."""
    chat = Chat(slug)
    for _ in range(5):  # comunicados antes da escolha "paciente"
        if any(re.search(r"paciente", i, re.I) for i in chat.items):
            break
        if chat.pick(GO_ON.pattern, required=False):
            continue
        if len(chat.items) == 1:
            chat.send(chat.items[0])
            continue
        break
    chat.pick(r"paciente")
    if chat.input_type != "text input":
        raise FlowError("esperava campo de CPF, veio %r" % (chat.input_type,))
    chat.send(cpf)
    if re.search(r"inv[áa]lido|n[ãa]o (encontrad|cadastrad)", chat.text, re.I):
        raise FlowError("a ABECMED recusou o CPF (invalido ou nao associado)")
    for _ in range(6):  # receita, avisos, permissoes...
        if any(MENU_HINT.search(i) for i in chat.items):
            return chat
        if chat.pick(GO_ON.pattern, required=False):
            continue
        if len(chat.items) == 1:
            chat.send(chat.items[0])
            continue
        break
    raise FlowError("nao cheguei no menu principal — opcoes: %r" % (chat.items,))


# --------------------------------------------------------------- coleta


def opcoes_reais(items):
    return [i for i in items if i.strip() and not NAV_RX.search(i)]


def explorar(cpf, slug, categoria_rx):
    """Entra numa categoria e devolve (produtos, notas).

    Se a tela ja lista produtos, devolve eles. Se lista subcategorias
    (THC / CBD / CBG...), abre uma sessao nova pra cada uma — sessoes
    isoladas, pra um tipo esgotado nao travar a leitura dos outros.
    """
    chat = abrir(cpf, slug)
    if not chat.pick(categoria_rx, required=False):
        raise FlowError("categoria /%s/ nao esta no menu: %r" % (categoria_rx, chat.items))

    if SEM_ESTOQUE_RX.search(chat.text) and not any(PRECO_RX.search(i) for i in chat.items):
        return [], ["sem estoque"]

    opcoes = opcoes_reais(chat.items)
    if any(PRECO_RX.search(o) for o in opcoes):
        return opcoes, []

    produtos, notas = [], []
    for sub in opcoes[:6]:
        try:
            s = abrir(cpf, slug)
            s.pick(categoria_rx)
            s.pick("^" + re.escape(sub) + "$")
            if SEM_ESTOQUE_RX.search(s.text) and not any(PRECO_RX.search(i) for i in s.items):
                notas.append("%s: sem estoque" % sub)
                continue
            achados = opcoes_reais(s.items)
            produtos += ["%s | %s" % (sub, p) for p in achados]
        except FlowError as exc:
            notas.append("%s: falhou (%s)" % (sub, exc))
    return produtos, notas


def preco(texto):
    m = PRECO_RX.search(texto)
    if not m:
        return None
    v = m.group(1)
    if "," in v:
        v = v.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", v):
        v = v.replace(".", "")
    try:
        return float(v)
    except ValueError:
        return None


def coletar(cpf):
    slug = detect_typebot_slug()
    flores, notas_f = explorar(cpf, slug, r"flor")
    try:
        conc, notas_c = explorar(cpf, slug, r"concentrad")
    except FlowError as exc:
        # Concentrado some do menu quando nao tem? Trata como sem estoque.
        print("aviso: concentrados: %s" % exc, file=sys.stderr)
        conc, notas_c = [], ["fora do menu"]
    return {"slug": slug, "flores": flores, "concentrados": conc,
            "notas": {"flores": notas_f, "concentrados": notas_c}}


def alvos(catalogo, limite):
    """Itens que merecem alerta, como {chave: texto}."""
    out = {}
    for f in catalogo["flores"]:
        p = preco(f)
        if p is not None and p < limite:
            out["flor:" + f] = "🌸 %s" % f
    for c in catalogo["concentrados"]:
        out["conc:" + c] = "🧪 %s" % c
    return out


def catalogo_texto(cat, limite):
    """Tudo que esta disponivel agora, flores da mais barata pra mais cara.
    Marca com ✅ o que esta abaixo do limite."""
    flores = sorted(cat["flores"], key=lambda f: (preco(f) is None, preco(f) or 0))
    linhas = ["DISPONIVEL AGORA"]
    linhas.append("Flores (%d):" % len(flores) if flores else "Flores: sem estoque")
    for f in flores:
        p = preco(f)
        linhas.append("%s %s" % ("✅" if p is not None and p < limite else "•", f))
    if cat["concentrados"]:
        linhas.append("Concentrados (%d):" % len(cat["concentrados"]))
        linhas += ["✅ %s" % c for c in cat["concentrados"]]
    else:
        linhas.append("Concentrados: sem estoque")
    return "\n".join(linhas)


# --------------------------------------------------------------- estado


def load_config(path):
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return {}
    if not parser.has_section("abecmed"):
        return {}
    return {k: v.strip() for k, v in parser["abecmed"].items()}


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def dentro_do_horario():
    return HORA_INICIO <= datetime.now(BRT).hour < HORA_FIM


def minutos_desde_ultima(state_path):
    try:
        ultima = datetime.strptime(load_state(state_path)["updated_at"], "%Y-%m-%dT%H:%M:%S%z")
    except (KeyError, ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - ultima).total_seconds() / 60


def save_state(path, state):
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")


# ------------------------------------------------------------ notificacao


def notify(topic, title, message, tags, priority=3):
    post_json(
        NTFY_BASE,
        {"topic": topic, "title": title, "message": message[:MAX_MSG],
         "tags": tags, "priority": priority, "click": BASE},
        headers={"User-Agent": "abecmed-alerta", "Origin": None, "Referer": None,
                 "Sec-Fetch-Dest": None, "Sec-Fetch-Mode": None, "Sec-Fetch-Site": None},
    )


# ------------------------------------------------------------------ main


def ciclo(cpf, topic, state_path, limite):
    """Uma verificacao. Devolve True se o fluxo quebrou."""
    state = load_state(state_path)
    ja_avisou_quebra = bool(state.get("quebrado"))
    antes = set(state.get("alertados", []))

    try:
        cat = coletar(cpf)
    except FlowError as exc:
        print("FALHA: %s" % exc, file=sys.stderr)
        if not ja_avisou_quebra:
            notify(topic, "abecmed-alerta quebrou",
                   "O fluxo do bot mudou e o script nao conseguiu navegar.\n\n%s" % exc,
                   ["warning"], 4)
        state["quebrado"] = True
        save_state(state_path, state)
        return True

    if ja_avisou_quebra:
        notify(topic, "abecmed-alerta voltou", "Navegacao normalizada.", ["white_check_mark"], 2)

    agora = alvos(cat, limite)
    novos = [agora[k] for k in sorted(agora) if k not in antes]
    disponivel = catalogo_texto(cat, limite)
    hora = datetime.now(BRT).strftime("%H:%M")
    condicoes = "flor < R$ %g/g ou concentrado" % limite

    # Push SO quando entra item NOVO nas condicoes (toca alto), com a tabela.
    # Item que continua disponivel nao repete o aviso; se esgotar e voltar,
    # conta como novo de novo.
    if novos:
        tem_flor = any(n.startswith("🌸") for n in novos)
        tem_conc = any(n.startswith("🧪") for n in novos)
        partes = (["FLOR < R$ %g" % limite] if tem_flor else []) + (["CONCENTRADO"] if tem_conc else [])
        titulo = "🚨 CORRE: " + " + ".join(partes) + " (%s)" % hora
        topo = "⭐ NOVO NAS CONDICOES:\n" + "\n".join(novos)
        outros = [v for k, v in sorted(agora.items()) if v not in novos]
        if outros:
            topo += "\n\nTambem nas condicoes:\n" + "\n".join(outros)
        tags, prioridade = ["rotating_light"], 5
    else:
        titulo = None  # nada novo nas condicoes: verifica, salva o estado e fica quieto

    if titulo:
        notify(topic, titulo, topo + "\n\n" + disponivel, tags, prioridade)
        print("%s  push enviado (prioridade %d): %s" % (hora, prioridade, titulo))
    else:
        print("%s  nada novo nas condicoes (%s; %d ja avisados); sem push. %d flores, %d concentrados" % (
            hora, condicoes, len(agora), len(cat["flores"]), len(cat["concentrados"])))

    # So guarda os alvos ATUAIS: item que esgota e volta gera alerta de novo.
    save_state(state_path, {"alertados": sorted(agora), "quebrado": False, "slug": cat["slug"]})
    return False


def main():
    ap = argparse.ArgumentParser(description="Alerta de flor barata / concentrado na ABECMED")
    ap.add_argument("--config", default=os.path.join(HERE, "config.ini"))
    ap.add_argument("--state", default=os.path.join(HERE, "estado.json"))
    ap.add_argument("--dry-run", action="store_true", help="mostra o catalogo, nao notifica nem salva")
    ap.add_argument("--loop", action="store_true", help="fica rodando no computador")
    ap.add_argument("--teste-push", action="store_true", help="manda um push de teste e sai")
    args = ap.parse_args()

    config = load_config(args.config)
    cpf = re.sub(r"\D", "", os.environ.get("ABECMED_CPF") or config.get("cpf", ""))
    topic = (os.environ.get("NTFY_TOPIC") or config.get("topico", "")).strip()
    bruto = os.environ.get("PRECO_MAX_FLOR") or config.get("preco_max_flor") or ""
    try:
        limite = float(bruto.replace(",", ".")) if bruto.strip() else PRECO_MAX_FLOR
    except ValueError:
        limite = PRECO_MAX_FLOR

    if args.teste_push:
        if not topic:
            sys.exit("erro: topico do ntfy nao configurado")
        notify(topic, "CORRE: teste do abecmed-alerta",
               "Se o celular tocou alto, esta tudo certo.", ["rotating_light"], 5)
        print("push de teste enviado para o topico configurado")
        return

    if len(cpf) != 11:
        sys.exit("erro: CPF nao configurado (ou nao tem 11 digitos). Rode ./configurar.sh")
    if not topic and not args.dry_run:
        sys.exit("erro: topico do ntfy nao configurado. Rode ./configurar.sh")

    if args.dry_run:
        try:
            cat = coletar(cpf)
        except FlowError as exc:
            sys.exit("FALHA: %s" % exc)
        print("versao do bot: %s\n" % cat["slug"])
        print("FLORES (%d):" % len(cat["flores"]))
        for f in cat["flores"]:
            p = preco(f)
            marca = "  <-- ALERTA" if p is not None and p < limite else ""
            print("  %s%s" % (f, marca))
        print("CONCENTRADOS (%d):" % len(cat["concentrados"]))
        for c in cat["concentrados"]:
            print("  %s  <-- ALERTA" % c)
        print("notas:", json.dumps(cat["notas"], ensure_ascii=False))
        return

    if not args.loop:
        # Rodada manual (Run workflow) ignora as travas, para poder testar.
        evento = os.environ.get("GITHUB_EVENT_NAME", "")
        if evento != "workflow_dispatch":
            if not dentro_do_horario():
                print("fora da janela %dh-%dh (Brasilia); sem consulta" % (HORA_INICIO, HORA_FIM))
                return
            desde = minutos_desde_ultima(args.state)
            if evento == "schedule" and desde is not None and desde < MIN_ENTRE_VERIFICACOES:
                print("ultima verificacao ha %.1f min; reserva do GitHub pulando" % desde)
                return
        sys.exit(1 if ciclo(cpf, topic, args.state, limite) else 0)

    print("Monitorando a cada %d-%d min, das %dh as %dh. Ctrl+C para parar.\n"
          % (INTERVALO_MIN, INTERVALO_MAX, HORA_INICIO, HORA_FIM))
    try:
        while True:
            if dentro_do_horario():
                ciclo(cpf, topic, args.state, limite)
            time.sleep(random.uniform(INTERVALO_MIN * 60, INTERVALO_MAX * 60))
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
