"""Registro prospectivo de carteiras da Hyperliquid: o único jeito honesto de testar "seguir quem ganha".

Por que prospectivo: a Hyperliquid só entrega os últimos negócios de cada carteira e não guarda o ranking do
passado. Escolher HOJE quem mais ganhou e olhar o passado dessas mesmas carteiras mostra lucro por construção
(foram escolhidas por terem lucrado). A saída é fixar a regra de escolha ANTES, anotar daqui para a frente o que as
carteiras escolhidas fazem, e só depois comparar com um grupo de controle sorteado.

  python wallets_forward.py --tick      forma o grupo do mês (se ainda não existe) e anota as posições agora
  python wallets_forward.py --verify    confere a cadeia de hash de todos os registros
  python wallets_forward.py --status    contagens (nenhum saldo, lucro ou endereço é impresso)

Arquivo único, só biblioteca padrão do Python: roda igual no PC e numa tarefa agendada do GitHub. Tudo o que ele
grava fica em data/live/wallets/ (ou em WALLETS_LIVE_DIR), NUNCA nos dados da pesquisa; a pesquisa nunca lê daqui.
Só pede o estado ATUAL (ranking, posições, preços de agora): nenhum histórico é baixado, então nada anterior a
2026-10-01 (fim do período lacrado da pesquisa) entra pelos preços. O ranking traz lucro acumulado de cada
carteira em janelas que se sobrepõem a esse período; esses números são usados só pela regra mecânica abaixo.

REGRA DE ESCOLHA (wallets_forward_v1, fixada antes do primeiro registro)
No primeiro registro de cada mês (UTC) baixa-se o ranking público e forma-se um grupo novo ("coorte"):
- elegíveis: saldo da conta >= MIN_ACCOUNT_VALUE; volume negociado no mês > 0; giro do mês (volume / saldo) <=
  MAX_TURNOVER (acima disso é formador de mercado ou alta frequência, que não dá para copiar de hora em hora);
- grupo W, "consistentes": lucro > 0 na semana, no mês E no total; os GROUP_SIZE de maior retorno no mês;
- grupo R, "recentes": fora do W, lucro > 0 na semana; os GROUP_SIZE de maior retorno na semana;
- grupo C, "controle": GROUP_SIZE sorteados entre os elegíveis que sobraram, com semente tirada do sha256 do
  próprio ranking baixado (ninguém escolhe; qualquer um refaz o sorteio a partir do arquivo guardado).
Empates são desfeitos pelo endereço (ordem alfabética). Cada coorte é acompanhada por TRACK_DAYS dias.

O QUE É ANOTADO
No máximo um registro por hora cheia (UTC). A tarefa agendada tenta várias vezes por hora, porque o agendador
gratuito do GitHub atrasa e pula execuções; a tentativa que cai numa hora já anotada não grava nada.
Em cada registro, para cada carteira de uma coorte ativa: saldo da conta, valor total das posições e cada posição
(moeda, tamanho com sinal, preço médio de entrada, valor, resultado não realizado). Para as moedas com posição:
preço médio do livro, preço de marcação, preço do oráculo e funding da hora. Cada registro traz o hash do anterior;
editar ou apagar um registro antigo quebra a cadeia (--verify acusa).

AVALIAÇÃO (pré-registrada aqui, antes de existir qualquer dado)
- Só depois de 90 dias de registros, com pelo menos 80% das horas cobertas.
- Carteira cópia de um grupo: média, entre as carteiras do grupo, dos pesos de cada uma (valor da posição com
  sinal / saldo da conta, com a soma dos módulos limitada a 5 por carteira, o teto de exposição do robô).
- Atraso: os pesos vistos num registro só valem a partir do registro SEGUINTE e até o posterior (o copiador nunca
  opera no instante em que observa). Retorno de cada moeda entre dois registros pelo preço médio do livro anotado.
- Custos: 5,5 bp sobre cada mudança de peso (taxa de ordem a mercado mais derrapagem) e o funding anotado.
- H1: o retorno diário médio da cópia do grupo W é maior que o da cópia do grupo C (reamostragem pareada por dias,
  unilateral) E o retorno líquido médio do W é positivo. H2: o mesmo para o grupo R. Duas hipóteses: cada uma
  precisa de p < 0,025.
- Se nenhuma se confirmar: "seguir carteiras do ranking não funciona nestas condições". Se alguma se confirmar:
  vira candidata a uma SEGUNDA janela de 90 dias, independente, antes de qualquer simulação de operação.
"""
import argparse
import gzip
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RULE_VERSION = "wallets_forward_v1"
SEALED_END = 1790812800                 # 2026-10-01 00:00 UTC: fim do período lacrado da pesquisa
GENESIS = "0" * 64
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"
GROUP_SIZE = 30
MIN_ACCOUNT_VALUE = 100_000.0
MAX_TURNOVER = 50.0
TRACK_DAYS = 90
MAX_WEIGHT = 5.0                        # usado só na avaliação (teto de exposição do robô)
GROUPS = ("W", "R", "C")
DAY = 86400
HOUR = 3600
UA = {"User-Agent": "Mozilla/5.0 (wallets_forward)", "Accept": "application/json"}


# ---------------- rede ----------------
class Net:
    """Acesso à rede (os testes trocam por um falso com os mesmos dois métodos)."""

    def __init__(self, pause_s=0.15, tries=4):
        self.pause_s, self.tries = pause_s, tries

    def _open(self, req, timeout):
        for attempt in range(self.tries):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == self.tries - 1:
                    raise
                time.sleep(5.0 * (attempt + 1))

    def leaderboard_bytes(self):
        return self._open(urllib.request.Request(LEADERBOARD_URL, headers=UA), 120)

    def info(self, body):
        req = urllib.request.Request(INFO_URL, data=json.dumps(body).encode(),
                                     headers={**UA, "Content-Type": "application/json"})
        out = json.loads(self._open(req, 30))
        time.sleep(self.pause_s)
        return out


# ---------------- arquivos e cadeia de hash ----------------
def root_dir(base=None):
    """Pasta dos registros: `base` > WALLETS_LIVE_DIR > data/live/wallets na raiz do repositório (o arquivo fica
    na raiz do repositório público, ou em live/ no repositório do robô). Recusa qualquer caminho dentro dos dados
    da pesquisa."""
    here = Path(__file__).resolve().parent
    default = (here.parent if here.name == "live" else here) / "data" / "live" / "wallets"
    root = Path(base or os.environ.get("WALLETS_LIVE_DIR") or default).resolve()
    parts = [p.lower() for p in root.parts]
    for i in range(len(parts) - 1):
        if parts[i] == "data" and parts[i + 1] == "research":
            raise RuntimeError(f"o registro de carteiras não grava dentro dos dados da pesquisa: {root}")
    return root


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_hash(rec):
    return hashlib.sha256(canonical({k: v for k, v in rec.items() if k != "hash"}).encode("utf-8")).hexdigest()


def _write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    os.replace(tmp, path)


def read_index(root):
    f = root / "index.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_record(root, rel, rec):
    """Grava o registro `rec` em root/rel encadeado ao anterior e acrescenta a linha do índice."""
    index = read_index(root)
    rec = dict(rec, prev_hash=index[-1]["hash"] if index else GENESIS)
    rec["hash"] = record_hash(rec)
    path = root / rel
    if path.exists():
        raise RuntimeError(f"registro já existe: {rel}")
    _write_atomic(path, canonical(rec) + "\n")
    with open(root / "index.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(canonical({"file": rel, "hash": rec["hash"], "kind": rec["kind"], "ts": rec["ts"]}) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return rec


def verify_chain(root):
    """(ok, problemas): cada registro aponta para o hash do anterior, o conteúdo confere com o próprio hash e o
    arquivo do ranking guardado confere com o sha256 anotado na coorte."""
    prev, problems = GENESIS, []
    for n, line in enumerate(read_index(root), 1):
        path = root / line["file"]
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            problems.append(f"registro {n}: arquivo ausente ou ilegível ({line['file']})")
            prev = line.get("hash")
            continue
        if rec.get("prev_hash") != prev:
            problems.append(f"registro {n}: não aponta para o anterior ({line['file']})")
        if record_hash(rec) != rec.get("hash") or rec.get("hash") != line.get("hash"):
            problems.append(f"registro {n}: conteúdo alterado ({line['file']})")
        if rec.get("kind") == "cohort":
            snap = root / rec["snapshot_file"]
            if not snap.exists() or hashlib.sha256(snap.read_bytes()).hexdigest() != rec["snapshot_gz_sha256"]:
                problems.append(f"registro {n}: ranking guardado ausente ou alterado ({rec['snapshot_file']})")
        prev = rec.get("hash")
    return not problems, problems


# ---------------- escolha das coortes ----------------
def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float("nan")
    return v


def parse_leaderboard(raw):
    """Linhas do ranking como {endereço, saldo, janelas: {janela: {pnl, roi, vlm}}} (números; NaN se faltar)."""
    rows = []
    for r in json.loads(raw)["leaderboardRows"]:
        wins = {w[0]: {k: _f(w[1].get(k)) for k in ("pnl", "roi", "vlm")} for w in r.get("windowPerformances", [])}
        rows.append({"address": str(r["ethAddress"]).lower(), "account_value": _f(r.get("accountValue")), "win": wins})
    return rows


def _win(row, window, field):
    return row["win"].get(window, {}).get(field, float("nan"))


def eligible(row):
    av, vlm = row["account_value"], _win(row, "month", "vlm")
    return bool(av >= MIN_ACCOUNT_VALUE and vlm > 0 and vlm / av <= MAX_TURNOVER)


def select_groups(rows, seed_hex):
    """{grupo: [endereços]} pela regra do cabeçalho. Determinístico dado o ranking e a semente."""
    pool = sorted((r for r in rows if eligible(r)), key=lambda r: r["address"])
    seen, uniq = set(), []
    for r in pool:                                   # endereço repetido no ranking conta uma vez só
        if r["address"] not in seen:
            seen.add(r["address"])
            uniq.append(r)

    def top(cands, window):
        ranked = sorted((r for r in cands if _win(r, window, "roi") == _win(r, window, "roi")),
                        key=lambda r: (-_win(r, window, "roi"), r["address"]))
        return [r["address"] for r in ranked[:GROUP_SIZE]]

    w = top([r for r in uniq if _win(r, "week", "pnl") > 0 and _win(r, "month", "pnl") > 0
             and _win(r, "allTime", "pnl") > 0], "month")
    ws = set(w)
    rr = top([r for r in uniq if r["address"] not in ws and _win(r, "week", "pnl") > 0], "week")
    used = ws | set(rr)
    rest = [r["address"] for r in uniq if r["address"] not in used]
    rng = random.Random(int(seed_hex[:16], 16))
    c = sorted(rng.sample(rest, min(GROUP_SIZE, len(rest))))
    return {"W": w, "R": rr, "C": c}, len(uniq)


def month_id(ts):
    return time.strftime("%Y-%m", time.gmtime(ts))


def form_cohort(root, net, now):
    """Baixa o ranking, guarda-o comprimido, escolhe os grupos e grava o registro da coorte do mês."""
    raw = net.leaderboard_bytes()
    seed = hashlib.sha256(raw).hexdigest()
    rows = parse_leaderboard(raw)
    groups, n_eligible = select_groups(rows, seed)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    snap_rel = f"snapshots/leaderboard_{stamp}.json.gz"
    gz = gzip.compress(raw, 6, mtime=0)
    _write_atomic(root / snap_rel, gz)
    rec = {"v": 1, "kind": "cohort", "rule": RULE_VERSION, "cohort": month_id(now), "ts": int(now),
           "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
           "snapshot_file": snap_rel, "snapshot_sha256": seed, "snapshot_gz_sha256": hashlib.sha256(gz).hexdigest(),
           "n_rows": len(rows), "n_eligible": n_eligible, "groups": groups,
           "params": {"group_size": GROUP_SIZE, "min_account_value": MIN_ACCOUNT_VALUE, "max_turnover": MAX_TURNOVER,
                      "track_days": TRACK_DAYS}}
    return append_record(root, f"cohorts/cohort_{month_id(now)}.json", rec)


def cohorts(root):
    out = []
    for line in read_index(root):
        if line["kind"] == "cohort":
            out.append(json.loads((root / line["file"]).read_text(encoding="utf-8")))
    return out


def active_wallets(root, now):
    """{endereço: [(coorte, grupo), ...]} das coortes formadas há no máximo TRACK_DAYS dias."""
    out = {}
    for c in cohorts(root):
        if now - c["ts"] <= TRACK_DAYS * DAY:
            for g in GROUPS:
                for a in c["groups"].get(g, []):
                    out.setdefault(a, []).append([c["cohort"], g])
    return out


# ---------------- registro das posições ----------------
def _position(p):
    pos = p.get("position", p)
    return {"coin": str(pos.get("coin")), "szi": _f(pos.get("szi")), "entry_px": _f(pos.get("entryPx")),
            "value": _f(pos.get("positionValue")), "upnl": _f(pos.get("unrealizedPnl"))}


def _clean(x):
    """NaN/inf viram None (JSON canônico)."""
    if isinstance(x, float) and (x != x or x in (float("inf"), float("-inf"))):
        return None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def take_tick(root, net, now):
    """Anota as posições de todas as carteiras das coortes ativas e o contexto de mercado das moedas com posição."""
    wallets = active_wallets(root, now)
    states, errors = {}, 0
    for addr in sorted(wallets):
        try:
            st = net.info({"type": "clearinghouseState", "user": addr})
            ms = st.get("marginSummary", {})
            states[addr] = {"account_value": _f(ms.get("accountValue")), "total_ntl": _f(ms.get("totalNtlPos")),
                            "positions": [_position(p) for p in st.get("assetPositions", [])]}
        except Exception as e:  # noqa: BLE001 — uma carteira sem resposta não derruba o registro; fica anotado
            states[addr] = {"error": type(e).__name__}
            errors += 1
    held = sorted({p["coin"] for s in states.values() for p in s.get("positions", [])})
    market = {}
    if held:
        meta, ctxs = net.info({"type": "metaAndAssetCtxs"})
        by = {u["name"]: c for u, c in zip(meta["universe"], ctxs)}
        for coin in held:
            c = by.get(coin)
            if c is not None:
                market[coin] = {"mid": _f(c.get("midPx")), "mark": _f(c.get("markPx")),
                                "oracle": _f(c.get("oraclePx")), "funding": _f(c.get("funding"))}
    rec = _clean({"v": 1, "kind": "tick", "rule": RULE_VERSION, "ts": int(now),
                  "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                  "n_wallets": len(wallets), "n_errors": errors, "membership": wallets, "states": states,
                  "market": market})
    rel = time.strftime("ticks/%Y/%m/%d/tick_%Y%m%dT%H%M%SZ.json", time.gmtime(now))
    return append_record(root, rel, rec)


def run_tick(root=None, net=None, now=None):
    """Uma execução: forma a coorte do mês se ainda não existe e anota as posições. Nunca estende uma cadeia
    quebrada. Devolve (None, None), sem gravar nada, se já existe um registro de posições nesta hora cheia (UTC)."""
    root = root_dir(root)
    now = time.time() if now is None else now
    if now < SEALED_END:
        raise RuntimeError("o registro de carteiras só começa depois de 2026-10-01 (período lacrado da pesquisa)")
    ok, problems = verify_chain(root)
    if not ok:
        raise RuntimeError("cadeia de hash quebrada: " + "; ".join(problems[:3]))
    net = net or Net()
    index = read_index(root)
    if index and now <= index[-1]["ts"]:
        raise RuntimeError("relógio andou para trás em relação ao último registro")
    need_cohort = month_id(now) not in {c["cohort"] for c in cohorts(root)}
    last_tick = next((x for x in reversed(index) if x["kind"] == "tick"), None)
    if not need_cohort and last_tick is not None and int(last_tick["ts"] // HOUR) == int(now // HOUR):
        return None, None                         # já existe um registro nesta hora cheia (UTC)
    formed = None
    if need_cohort:
        formed = form_cohort(root, net, now)
        now = max(now, formed["ts"]) + 1          # o registro das posições vem depois da coorte
    tick = take_tick(root, net, now)
    return formed, tick


def status(root=None):
    root = root_dir(root)
    index = read_index(root)
    cs = cohorts(root)
    ticks = [x for x in index if x["kind"] == "tick"]
    out = {"registros": len(index), "coortes": [c["cohort"] for c in cs], "ticks": len(ticks),
           "primeiro_tick": time.strftime("%Y-%m-%d %H:%M", time.gmtime(ticks[0]["ts"])) if ticks else None,
           "ultimo_tick": time.strftime("%Y-%m-%d %H:%M", time.gmtime(ticks[-1]["ts"])) if ticks else None,
           "ultimo_hash": index[-1]["hash"][:16] if index else None}
    if cs:
        out["tamanho_dos_grupos"] = {g: len(cs[-1]["groups"].get(g, [])) for g in GROUPS}
        out["elegiveis_na_ultima_coorte"] = cs[-1]["n_eligible"]
    if ticks:
        days = max((ticks[-1]["ts"] - ticks[0]["ts"]) / DAY, 1e-9)
        out["dias_cobertos"] = round(days, 2)
        out["cobertura_horaria"] = round(min(1.0, len(ticks) / max(days * 24, 1.0)), 3)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Registro prospectivo de carteiras da Hyperliquid")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tick", action="store_true", help="forma a coorte do mês (se preciso) e anota as posições")
    g.add_argument("--verify", action="store_true", help="confere a cadeia de hash")
    g.add_argument("--status", action="store_true", help="contagens")
    a = ap.parse_args(argv)
    if a.verify:
        ok, problems = verify_chain(root_dir())
        print("cadeia íntegra" if ok else "CADEIA QUEBRADA:\n  " + "\n  ".join(problems))
        return 0 if ok else 1
    if a.status:
        print(json.dumps(status(), ensure_ascii=False, indent=1))
        return 0
    formed, tick = run_tick()
    if tick is None:
        print("sem novo registro: já existe um nesta hora (UTC)")
        return 0
    if formed:
        print(f"coorte {formed['cohort']} formada: {formed['n_eligible']} elegíveis em {formed['n_rows']} linhas; grupos "
              + ", ".join(f"{g}={len(formed['groups'][g])}" for g in GROUPS))
    print(f"registro {tick['ts_utc']}: {tick['n_wallets']} carteiras, {tick['n_errors']} sem resposta, "
          f"{len(tick['market'])} moedas com posição | hash {tick['hash'][:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
