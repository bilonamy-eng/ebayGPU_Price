#!/usr/bin/env python3
"""eBay Browse API 行情扫描（只读查价，不登录、不购买、不出价）。

用法:
  python3 sweep.py                 # 查询全部型号，写 api_sweep_latest.json 并打印摘要
  python3 sweep.py A4000 P2000     # 只查指定型号
  python3 sweep.py --board         # 查询后把结果写进同目录 index.html（行情板）
  python3 sweep.py --board --gate  # 同上，但只在阿姆斯特丹 8:00–01:00 之间运行
  python3 sweep.py --bulk --board  # 高数量板：只留可买数量 > 10 的，价格上限 = 目标 x1.30
凭据: 环境变量 EBAY_CLIENT_ID / EBAY_CLIENT_SECRET（GitHub Actions secrets），
      或本机 ~/.ebay_api.env 里写这两行。只用 Python 标准库。
"""
import base64, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.expanduser("~/.ebay_api.env")
TOKEN_CACHE = os.path.expanduser("~/.ebay_api_token.json")
API = "https://api.ebay.com"
ZIP = "19713"
CAP = 1.15                      # 还价上限 = 目标 x1.15（--bulk 时改为 1.30）
BULK_CAP = 1.30
BULK_MIN = 10                   # 高数量板：可买数量必须 > 10（含 eBay 只显示“超过 10 件”的）
LOW = 0.5                       # 总价低于目标的一半 → 标题加“价格异常低,请核实”
AMS = ZoneInfo("Europe/Amsterdam")
EXCLUDE_IDS = {                 # 用户确认过的永久排除
    "377595218856", "206680618249", "227279680039", "256055431624", "305497274750",
    "366661759506", "267777199999", "278437041334", "128105830282",
    "206726057794"}             # Kingston KTH-PL421LQ/32G = 4Rx4 LRDIMM
GPU_CAT = "27386"
ACC = re.compile(r"bracket|cooler", re.I)   # 便宜且标题带这些词 = 配件；价格正常的是“带挡板”的整卡
BAD = re.compile(
    r"for parts|not working|broken|faulty|defective|as[- ]is|untested|no video|"
    r"laptop|mobile|mxm|fan only|heatsink|heat sink|shroud|backplate|retention|extender|baffle|"
    r"box only|empty box|water ?block|passive|fanless|lrdimm|4rx4|ddr3|read desc|"
    r"load.?reduced|quad.?rank|\bk[a-z]{2}-[a-z]{2}\d{3}lq|-l\b(?=.*pc4)|pc4-\d{4,5}[a-z]?-l\b|"   # LRDIMM 的其他写法（Kingston 料号 …LQ/32G、PC4-2133P-L）
    # 笔记本/迷你工作站显卡模块、改装配件、多件打包
    r"precision ?m?\d{4}|\bdell 7[5-7]\d0\b|thinkpad|zbook|z2 mini|\bp1[5-7]\b|\bn1[7-9][ep]-|\bqn20|"
    r"\bls-[a-z]\d|shunt|resistor|\bmod\b|overclocked|lot of ([2-9]|\d\d)|\d+ ?x nvidia|fury|tjfrk", re.I)

# 型号: (搜索词, 目标价, 最低数量, 必须匹配, 不能匹配, 类目)
MODELS = {
 "RTX 4000":     ("Quadro RTX 4000 8GB",    175, 1, r"rtx ?4000", r"ada|a4000|sff|20 ?gb|16 ?gb", GPU_CAT),
 "RTX 5000":     ("Quadro RTX 5000 16GB",   425, 1, r"rtx ?5000", r"ada|a5000|32 ?gb", GPU_CAT),
 "RTX 6000":     ("Quadro RTX 6000 24GB",   800, 1, r"rtx ?6000", r"ada|a6000|48 ?gb", GPU_CAT),
 "A2000":        ("RTX A2000",              310, 1, r"a2000", r"\b[48] ?gb", GPU_CAT),
 "A4000":        ("RTX A4000 16GB",         900, 1, r"a4000", r"a4000h", GPU_CAT),
 "A5000":        ("RTX A5000 24GB",        2100, 1, r"a5000", r"", GPU_CAT),
 "A6000":        ("RTX A6000 48GB",        3500, 1, r"a6000", r"", GPU_CAT),
 "RTX 4000 Ada": ("RTX 4000 Ada 20GB",     1100, 1, r"4000.*ada|ada.*4000", r"", GPU_CAT),
 "RTX 5000 Ada": ("RTX 5000 Ada 32GB",     3000, 1, r"5000.*ada|ada.*5000", r"", GPU_CAT),
 "RTX 6000 Ada": ("RTX 6000 Ada 48GB",     7000, 1, r"6000.*ada|ada.*6000", r"", GPU_CAT),
 "RTX 5880 Ada": ("RTX 5880 Ada 48GB",     5000, 1, r"5880", r"", GPU_CAT),
 "P4000":        ("Quadro P4000 8GB",       140, 3, r"p4000", r"", GPU_CAT),
 "T1000 4GB":    ("NVIDIA T1000 4GB",       180, 1, r"t1000", r"8 ?gb", GPU_CAT),
 "T1000 8GB":    ("NVIDIA T1000 8GB",       240, 1, r"t1000.*8 ?gb|8 ?gb.*t1000", r"", GPU_CAT),
 "P2000":        ("Quadro P2000 5GB",        70, 5, r"p2000", r"", GPU_CAT),
 "P2200":        ("Quadro P2200 5GB",        80, 5, r"p2200", r"", GPU_CAT),
 "P1000":        ("Quadro P1000 4GB",        40, 5, r"p1000", r"", GPU_CAT),
 "DDR4 ECC 32GB":("32GB DDR4 ECC",        100, 1, r"32 ?gb", r"64 ?gb|16 ?gb|8 ?gb|sodimm|so-dimm|udimm|x ?32 ?gb|32 ?gb ?x", ""),
}
CALLS = {"token": 0, "search": 0, "item": 0}
DIAG = []                       # 每个型号的搜索诊断，打印在日志里


def creds():
    cid, sec = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    if cid and sec:
        return cid.strip(), sec.strip()
    if not os.path.exists(ENV):
        sys.exit(f"没有凭据：请设置环境变量 EBAY_CLIENT_ID / EBAY_CLIENT_SECRET，或新建 {ENV}。")
    d = {}
    for line in open(ENV, encoding="utf-8"):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.strip().split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    if not d.get("EBAY_CLIENT_ID") or not d.get("EBAY_CLIENT_SECRET"):
        sys.exit(f"{ENV} 里缺少 EBAY_CLIENT_ID 或 EBAY_CLIENT_SECRET。")
    return d["EBAY_CLIENT_ID"], d["EBAY_CLIENT_SECRET"]


def http(req, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(2 ** i * 2)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            if i < tries - 1:
                time.sleep(2 ** i)
                continue
            raise RuntimeError(f"网络错误: {e}")


def token():
    """应用令牌有效约 2 小时；本机运行时缓存到文件，避免每轮重新申请。"""
    try:
        c = json.load(open(TOKEN_CACHE))
        if c["exp"] > time.time() + 120:
            return c["tok"]
    except Exception:
        pass
    cid, sec = creds()
    req = urllib.request.Request(
        API + "/identity/v1/oauth2/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials",
                                     "scope": "https://api.ebay.com/oauth/api_scope"}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode()})
    CALLS["token"] += 1
    r = http(req)
    try:
        fd = os.open(TOKEN_CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"tok": r["access_token"], "exp": time.time() + r["expires_in"]}, f)
    except OSError:
        pass
    return r["access_token"]


def get(path, tok):
    return http(urllib.request.Request(API + path, headers={
        "Authorization": "Bearer " + tok, "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country=US,zip={ZIP}"}))


def ship_cost(opts):
    """最低运费；没有任何配送选项（只能自提）返回 None。"""
    costs = [float(o["shippingCost"]["value"]) for o in (opts or [])
             if o.get("shippingCost", {}).get("value") is not None]
    return min(costs) if costs else None


def title_ok(model, title):
    _, _, _, must, mustnot, _ = MODELS[model]
    if BAD.search(title) or not re.search(must, title, re.I):
        return False
    return not (mustnot and re.search(mustnot, title, re.I))


def search(model, tok):
    q, target, need, _, _, cat = MODELS[model]
    flt = (f"buyingOptions:{{FIXED_PRICE|BEST_OFFER}},itemLocationCountry:US,"
           f"price:[..{target * CAP:.2f}],priceCurrency:USD")
    out, offset = [], 0
    while offset < 400:                       # 每型号最多 2 页 x 200 条
        params = {"q": q, "filter": flt, "sort": "price", "limit": 200, "offset": offset}
        if cat:
            params["category_ids"] = cat
        CALLS["search"] += 1
        r = get("/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(params), tok)
        out += r.get("itemSummaries", [])
        offset += 200
        if offset >= r.get("total", 0):
            break
    rows, rejected = [], []
    for it in out:
        iid, title = it.get("legacyItemId", ""), it.get("title", "")
        if iid in EXCLUDE_IDS or str(it.get("conditionId")) == "7000" or not title_ok(model, title):
            if float(it.get("price", {}).get("value", 0)) >= target * LOW:
                rejected.append(f"{iid} ${it['price']['value']} {title[:70]}")
            continue
        if ACC.search(title) and float(it["price"]["value"]) < target * LOW:
            continue
        if "FIXED_PRICE" not in it.get("buyingOptions", []):
            continue
        sc = ship_cost(it.get("shippingOptions"))
        if sc is None:
            continue                          # 无配送 = 只能自提，排除
        price = float(it["price"]["value"])
        total = round(price + sc, 2)
        if total > target * CAP:
            continue
        s = it.get("seller", {})
        rows.append({"model": model, "id": iid, "apiId": it["itemId"], "title": title, "price": price,
                     "shipping": sc, "total": total, "target": target, "need": need,
                     "bestOffer": "BEST_OFFER" in it.get("buyingOptions", []),
                     "condition": it.get("condition"), "seller": s.get("username"),
                     "feedback": s.get("feedbackScore"), "positive": s.get("feedbackPercentage"),
                     "url": f"https://www.ebay.com/itm/{iid}"})
    DIAG.append(f"{model}: 搜到 {len(out)} 条，保留 {len(rows)} 条" +
                ("".join("\n      被过滤: " + x for x in rejected[:6]) if not rows else ""))
    return rows


def detail(row, tok):
    """逐件读取实时库存；已下架/售罄标记 gone。"""
    CALLS["item"] += 1
    try:
        r = get("/buy/browse/v1/item/" + urllib.parse.quote(row["apiId"], safe=""), tok)
    except RuntimeError as e:
        row["qty"], row["qtyText"], row["gone"] = None, "读取失败", "HTTP 404" in str(e)
        row["detailError"] = str(e)[:120]
        return row
    a = (r.get("estimatedAvailabilities") or [{}])[0]
    row["gone"] = a.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK"
    row["sold"] = a.get("estimatedSoldQuantity")
    if a.get("estimatedAvailableQuantity") is not None:
        row["qty"], row["qtyMore"] = a["estimatedAvailableQuantity"], False
        row["qtyText"] = f"{row['qty']} 件可买"
    elif a.get("availabilityThreshold") is not None:
        row["qty"], row["qtyMore"] = a["availabilityThreshold"], True
        row["qtyText"] = f"超过 {row['qty']} 件（eBay 只显示下限）"
    else:
        row["qty"], row["qtyMore"], row["qtyText"] = None, False, "数量未显示"
    sc = ship_cost(r.get("shippingOptions"))
    if sc is not None and r.get("price"):
        row["price"], row["shipping"] = float(r["price"]["value"]), sc
        row["total"] = round(row["price"] + sc, 2)
    row["returns"] = "接受退货" if r.get("returnTerms", {}).get("returnsAccepted") else "不接受退货"
    return row


def status(r):
    if r["total"] < r["target"] * LOW:
        return "价格异常低，需核实"
    if r["total"] <= r["target"]:
        return "可直接买" if (r.get("qty") or 1) >= r["need"] else "价格合格，数量不够"
    return "可还价" if r["bestOffer"] else "略超，不可还价"


def is_bulk(r):
    q = r.get("qty")
    return q is not None and (q > BULK_MIN or (r.get("qtyMore") and q >= BULK_MIN))


def sweep(models, bulk=False):
    t0 = time.time()
    tok = token()
    rows, errors = [], []
    for m in models:
        try:
            rows += search(m, tok)
        except RuntimeError as e:
            errors.append(f"{m}: {e}")
    with ThreadPoolExecutor(4) as ex:
        rows = list(ex.map(lambda r: detail(r, tok), rows))
    errors += [f"{r['id']}: {r['detailError']}" for r in rows if r.get("detailError") and not r.get("gone")]
    rows = [r for r in rows if not r.get("gone") and not r.get("detailError") and r["total"] <= r["target"] * CAP]
    if bulk:
        rows = [r for r in rows if is_bulk(r)]
    for r in rows:
        r["status"] = status(r)
        r.pop("apiId", None)
    order = list(MODELS)
    rows.sort(key=lambda r: (order.index(r["model"]), r["total"]))
    return {"queryTimeAmsterdam": datetime.now(AMS).strftime("%Y-%m-%d %H:%M"), "shipTo": ZIP,
            "apiCalls": dict(CALLS), "seconds": round(time.time() - t0, 1), "errors": errors, "items": rows}


# ---------- 行情板 ----------
def js(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def qty_code(r):
    """模板 q() 认识的数量文本：'7a'、'10+a'、'Last one'、'hiddenbox'，可还价加 ' BO'。"""
    if r.get("qty") is None:
        s = "hiddenbox"
    elif r.get("qtyMore"):
        s = f"{r['qty']}+a"
    elif r["qty"] == 1:
        s = "Last one"
    else:
        s = f"{r['qty']}a"
    return s + (" BO" if r["bestOffer"] else "")


def render_board(res, path):
    html = open(path, encoding="utf-8").read()
    m = re.search(r"^var SM=(\{.*\});?\s*$", html, re.M)
    old_sm = json.loads(m.group(1)) if m else {}
    since = {v[0]: v[1] for v in old_sm.values() if v and v[0] and len(v) > 1 and v[1]}  # 卖家注册时间缓存
    D, PS, SM = [], {}, {}
    for r in res["items"]:
        fb = r.get("feedback")
        pos = r.get("positive")
        fbtxt = "未显示" if fb is None else (f"{fb} / {float(pos):g}%" if pos not in (None, "", "0.0") else f"{fb} / 好评率未显示")
        title = r["title"][:70]
        if r["total"] < r["target"] * LOW:
            title += "(价格异常低,请核实)"
        elif fb is not None and fb < 100:
            title += f"(卖家只有 {fb} 条反馈)"
        D.append([r["model"], r["id"], r["total"], r["target"], qty_code(r), "", fbtxt, title])
        PS[r["id"]] = [f"{r['price']:,.2f}", "Free" if not r["shipping"] else f"US ${r['shipping']:.2f}"]
        SM[r["id"]] = [r.get("seller") or "", since.get(r.get("seller") or "", "")]
    c = res["apiCalls"]
    stat = f"{c['search']} 次搜索 · {c['item']} 次库存读取 · {len(res['errors'])} 个错误 · 用时 {res['seconds']:.0f} 秒"
    subs = [
        (r"^var D=.*$", "var D=" + js(D) + ";"),
        (r"^var PS=.*$", "var PS=" + js(PS) + ";"),
        (r"^var SM=.*$", "var SM=" + js(SM) + ";"),
        (r"^var ORDER=.*$", "var ORDER=" + js(list(MODELS)) + ";"),
        (r"(查询时间（阿姆斯特丹）<b>)[^<]*(</b>)", r"\g<1>" + res["queryTimeAmsterdam"] + r"\g<2>"),
        (r"<span>(金丝雀|数据来源)[^<]*<b>[^<]*</b></span>", "<span>数据来源 eBay 官方 API <b>" + stat + "</b></span>"),
        (r"<li>(散热方式|数据来自)[^<]*</li>",
         "<li>数据来自 eBay 官方 Browse API，每小时自动更新（阿姆斯特丹 8:00–01:00）。散热方式和照片没有逐件核对，"
         "购买前请在页面里确认带风扇；卖家注册时间 API 不提供，只沿用以前查到的。</li>"),
    ]
    for pat, rep in subs:
        html, n = re.subn(pat, lambda _m, rep=rep: _m.expand(rep) if "\\g<" in rep else rep, html, count=1, flags=re.M)
        if n != 1:
            sys.exit(f"行情板模板里找不到要替换的位置: {pat}")
    open(path, "w", encoding="utf-8").write(html)


def fb_text(r):
    fb, pos = r.get("feedback"), r.get("positive")
    if fb is None:
        return "未显示"
    return f"{fb} / {float(pos):g}%" if pos not in (None, "", "0.0") else f"{fb} / 好评率未显示"


def render_bulk(res, path):
    """高数量板：模板结构与行情板不同（D 多行、带增量说明）。"""
    html = open(path, encoding="utf-8").read()
    m = re.search(r"^var SM=(\{.*\});?\s*$", html, re.M)
    old_sm = json.loads(m.group(1)) if m else {}
    since = {re.split(r"[（(]", v[0])[0].strip().lower(): v[1] for v in old_sm.values() if v and v[0] and len(v) > 1 and v[1]}
    dm = re.search(r"^var D=\[.*?\];[ \t]*$", html, re.M | re.S)
    if not dm:
        sys.exit("高数量板模板里找不到 var D")
    old_ids = set(re.findall(r'\["[^"]+",\s*"(\d{12})"', dm.group(0)))
    D, PS, SM = [], {}, {}
    for r in res["items"]:
        D.append([r["model"], r["id"], r["total"], r["target"], qty_code(r),
                  "" if r.get("sold") is None else str(r["sold"]), fb_text(r), r["title"][:80]])
        PS[r["id"]] = [f"{r['price']:,.2f}", "Free" if not r["shipping"] else f"US ${r['shipping']:.2f}"]
        SM[r["id"]] = [r.get("seller") or "", since.get((r.get("seller") or "").lower(), "")]
    ids = [r["id"] for r in res["items"]]
    new, gone = [i for i in ids if i not in old_ids], sorted(old_ids - set(ids))
    inside = sum(1 for r in res["items"] if r["total"] <= r["target"])
    c = res["apiCalls"]
    stat = f"{c['search']} 次搜索 · {c['item']} 次库存读取 · {len(res['errors'])} 个错误 · 用时 {res['seconds']:.0f} 秒（eBay 官方 API）"
    delta = f"合格 {len(ids)} 条（上轮 {len(old_ids)} 条）；新出现 {len(new)} 条；消失 {len(gone)} 条"
    notes = [
        "总价 = 商品价 + 邮费。价格上限 = 目标价 ×1.30；目标价和“超目标 %”按每个型号单独设置。"
        + (f"本轮有 {inside} 条在目标价以内。" if inside else "本轮没有目标价以内的商品。"),
        "数量门槛：可买数量超过 10。eBay 有时只给下限（“超过 10 件”），真实库存看不出来，页面标“10+（上限显示）”。",
        "本轮新出现：" + ("、".join(new) if new else "无") + "。",
        "本轮消失（售出、下架、涨价或数量降到 10 以下）：" + ("、".join(gone) if gone else "无") + "。",
        "数据来自 eBay 官方 Browse API，自动更新。API 不提供“Buy N or more”阶梯价，本页只按单件价判断，阶梯价能进上限的商品可能漏掉。",
        "照片、风扇和卖家注册时间 API 不提供；注册时间只沿用以前查到的。购买前请在商品页确认。",
        "已排除：坏件、配件（支架、挡板、散热片等）、笔记本/MXM 模块、无风扇被动卡、4Rx4 / LRDIMM 内存、只能自提的商品。",
    ]
    subs = [
        (r"^var D=\[.*?\];[ \t]*$", "var D=" + js(D) + ";", re.M | re.S),
        (r"^var PS=.*$", "var PS=" + js(PS) + ";", re.M),
        (r"^var SM=.*$", "var SM=" + js(SM) + ";", re.M),
        (r"^var ORDER=.*$", "var ORDER=" + js(list(MODELS)) + ";", re.M),
        (r"(?<=查询时间（阿姆斯特丹）<b>)[^<]*(?=</b>)", res["queryTimeAmsterdam"], re.M),
        (r"<span>本轮 <b>[^<]*</b></span>", "<span>本轮 <b>" + stat + "</b></span>", re.M),
        (r"<span>增量 <b>[^<]*</b></span>", "<span>增量 <b>" + delta + "</b></span>", re.M),
        (r'<p class="sub">[^<]*</p>', '<p class="sub">美国 eBay，送到 19713，仅美国发货。只列可买数量超过 10 的（含 eBay 只显示“超过 10 件”的）、'
         '总价不超过目标价 130% 的显卡和内存。</p>', re.M),
        (r"(?<=<b>说明</b>\n)\s*<ul>.*?</ul>", "    <ul>\n" + "".join(f"      <li>{n}</li>\n" for n in notes) + "    </ul>", re.S),
    ]
    for pat, rep_, fl in subs:
        html, n = re.subn(pat, lambda _m, rep_=rep_: rep_, html, count=1, flags=fl)
        if n != 1:
            sys.exit(f"高数量板模板里找不到要替换的位置: {pat}")
    open(path, "w", encoding="utf-8").write(html)
    return len(ids), len(new), len(gone)


def main():
    args = sys.argv[1:]
    global CAP
    board, gate, bulk = "--board" in args, "--gate" in args, "--bulk" in args
    if bulk:
        CAP = BULK_CAP
    from_json = next((a.split("=", 1)[1] for a in args if a.startswith("--from-json=")), None)
    names = [a for a in args if not a.startswith("--")]
    bad = [n for n in names if n not in MODELS]
    if bad:
        sys.exit(f"未知型号: {bad}；可选: {list(MODELS)}")
    if gate and datetime.now(AMS).hour in range(1, 8):
        print("阿姆斯特丹 01:00–08:00 之间不查询，本轮跳过。")
        return
    if from_json:                               # 仅用于离线测试渲染
        res = json.load(open(from_json, encoding="utf-8"))
        res["items"] = [r for r in res["items"] if r["id"] not in EXCLUDE_IDS and title_ok(r["model"], r["title"])]
        for r in res["items"]:
            r.setdefault("qtyMore", r.get("qtyText", "").startswith("超过"))
        if bulk:
            res["items"] = [r for r in res["items"] if is_bulk(r)]
    else:
        res = sweep([m for m in MODELS if not names or m in names], bulk)
        with open(os.path.join(HERE, "api_sweep_latest.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    rows = res["items"]
    print(f"查询时间（阿姆斯特丹）{res['queryTimeAmsterdam']} · {len(rows)} 条候选 · 用时 {res['seconds']}s · API 调用 {res['apiCalls']}")
    for e in res["errors"]:
        print("  出错:", e)
    for d in DIAG:
        print("  " + d)
    for r in rows:
        if bulk or r.get("status") in ("可直接买", "可还价"):
            print(f"[{r['status']}] {r['model']} ${r['total']:.2f}（目标 ${r['target']}）{r['qtyText']} "
                  f"{'可还价' if r['bestOffer'] else '不可还价'} {r['seller']} {r['url']}")
    if board:
        if res["errors"] and not from_json:
            sys.exit("本轮有请求失败，结果不完整，不更新行情板。")
        if names:
            sys.exit("只查了部分型号，不更新行情板。")
        if bulk:
            n, new, gone = render_bulk(res, os.path.join(HERE, "index.html"))
            print(f"高数量板已更新: 合格 {n} 条，新出现 {new}，消失 {gone}")
        else:
            render_board(res, os.path.join(HERE, "index.html"))
            print("行情板已更新: index.html")


if __name__ == "__main__":
    main()
