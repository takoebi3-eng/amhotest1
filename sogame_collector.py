#!/usr/bin/env python3
"""ソシャゲ情報コレクター v2（個人利用・標準ライブラリのみ）

  python sogame_collector.py          ローカルで起動 → http://127.0.0.1:8765
  python sogame_collector.py --build  1回だけ収集して docs/ に静的ページを出力
                                      （GitHub Actions + GitHub Pages 用）

収集元: Bing/Google ニュースRSS（画像つき）、任意のRSS/YouTubeフィード、
        pixiv（非公式の公開エンドポイント。全年齢のみ・低頻度）
設定は config.json（初回に自動作成）。
"""
import json, os, re, sys, threading, time, html, datetime, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "config.json")
DATA = os.path.join(HERE, "docs", "data.json")
INTERVAL_MIN, PORT = 30, 8765
PIXIV_DETAIL_PER_TAG = 12   # タグごとに「いいね数」を取りに行く件数（多いほど遅い）
lock = threading.Lock()
status = {"last": "未実行", "err": []}

DEFAULTS = {  # ゲーム名: pixivタグ
    "原神": ["原神"], "崩壊スターレイル": ["崩壊スターレイル"], "ゼンレスゾーンゼロ": ["ゼンレスゾーンゼロ"],
    "ブルーアーカイブ": ["ブルーアーカイブ"], "ウマ娘": ["ウマ娘プリティーダービー"], "FGO": ["FGO"],
    "プロセカ": ["プロジェクトセカイ"], "学園アイドルマスター": ["学園アイドルマスター"], "NIKKE": ["勝利の女神NIKKE"]}
SUFFIX = ["", " 新衣装 OR コスチューム OR スキン", " 新キャラ OR 実装", " 予告番組 OR イベント OR コラボ"]
CATS = [("衣装", r"衣装|コスチューム|スキン|水着|コスプレ"),
        ("キャラ", r"新キャラ|実装|登場|ピックアップ|復刻"),
        ("番組", r"予告番組|生放送|配信予定|特別番組|PV|映像"),
        ("イベント", r"イベント|コラボ|キャンペーン|アップデート|メンテナンス")]
UA = "Mozilla/5.0 (compatible; sogame-collector personal)"


def now_jst():
    return time.strftime("%Y-%m-%d %H:%M:%S JST", time.gmtime(time.time() + 9 * 3600))


def load_conf():
    if not os.path.exists(CONF):
        c = {"pixiv": True, "exclude_ai": True,
             "titles": {g: {"queries": [g + s for s in SUFFIX], "feeds": [], "pixiv_tags": tags}
                        for g, tags in DEFAULTS.items()}}
        json.dump(c, open(CONF, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return json.load(open(CONF, encoding="utf-8"))


def load_data():
    try:
        return json.load(open(DATA, encoding="utf-8")).get("items", [])
    except Exception:
        return []


def fetch(url, headers=None, limit=None):
    h = {"User-Agent": UA}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=20) as r:
        return r.read(limit) if limit else r.read()


def categorize(text):
    for name, pat in CATS:
        if re.search(pat, text):
            return name
    return "話題"


def loc(tag):
    return tag.split("}")[-1]


def fix_img(u):
    u = (u or "").strip()
    return "https:" + u if u.startswith("//") else u


def real_link(link):
    if "bing.com" in link and "url=" in link:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        if q.get("url"):
            return q["url"][0]
    return link


def parse_feed(xml_bytes, game):
    root, out = ET.fromstring(xml_bytes), []
    for it in root.iter("item"):  # RSS (Google/Bing)
        title = (it.findtext("title") or "").strip()
        link = real_link((it.findtext("link") or "").strip())
        src, img = "", ""
        for ch in it:
            n = loc(ch.tag)
            if n in ("source", "Source") and not src:
                src = (ch.text or "").strip()
            elif n in ("Image", "thumbnail") and not img:
                img = fix_img(ch.get("url") or ch.text)
            elif n == "enclosure" and ch.get("type", "").startswith("image") and not img:
                img = fix_img(ch.get("url"))
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except Exception:
            ts = time.time()
        out.append((title, link, src, ts, img))
    for en in root.iter():  # Atom (YouTube等)
        if loc(en.tag) != "entry":
            continue
        title = link = img = pub = ""
        for ch in en.iter():
            n = loc(ch.tag)
            if n == "title" and not title:
                title = (ch.text or "").strip()
            elif n == "link" and not link:
                link = ch.get("href", "")
            elif n == "thumbnail" and not img:
                img = fix_img(ch.get("url"))
            elif n == "published":
                pub = (ch.text or "")
        try:
            ts = datetime.datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = time.time()
        out.append((title, link, "公式/フィード", ts, img))
    return [{"game": game, "title": t, "link": l, "src": s, "ts": ts, "img": im, "cat": categorize(t)}
            for t, l, s, ts, im in out if t and l]


def og_image(url):
    try:
        t = fetch(url, limit=200000).decode("utf-8", "ignore")
        for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']'):
            m = re.search(pat, t, re.I)
            if m:
                return html.unescape(m.group(1))
    except Exception:
        pass
    return ""


def pixiv_json(url):
    return json.loads(fetch(url, {"Referer": "https://www.pixiv.net/", "Accept-Language": "ja"}))


def collect_pixiv(game, tags, exclude_ai, errs):
    out = []
    for tag in tags:
        try:
            u = ("https://www.pixiv.net/ajax/search/illustrations/%s?word=%s&order=date_d&mode=safe"
                 "&p=1&s_mode=s_tag&type=illust_and_ugoira&lang=ja" % ((urllib.parse.quote(tag),) * 2))
            body = pixiv_json(u)["body"]
            data = (body.get("illust") or body.get("illustManga") or {}).get("data", [])
            data = [d for d in data if d.get("xRestrict", 0) == 0 and not (exclude_ai and d.get("aiType") == 2)]
            for d in data[:PIXIV_DETAIL_PER_TAG]:
                time.sleep(0.8)
                det = pixiv_json("https://www.pixiv.net/ajax/illust/%s?lang=ja" % d["id"])["body"]
                try:
                    ts = datetime.datetime.fromisoformat(d["createDate"]).timestamp()
                except Exception:
                    ts = time.time()
                out.append({"game": game, "title": d.get("title", ""), "link": "https://www.pixiv.net/artworks/%s" % d["id"],
                            "src": d.get("userName", ""), "ts": ts, "img": d.get("url", ""), "cat": "イラスト",
                            "pix": True, "likes": det.get("likeCount", 0), "marks": det.get("bookmarkCount", 0),
                            "views": det.get("viewCount", 0)})
        except Exception as e:
            errs.append("pixiv %s: %s" % (tag, e))
    return out


def collect():
    conf, errs, new = load_conf(), [], []
    for game, cfg in conf["titles"].items():
        urls = []
        for q in cfg.get("queries", []):
            qq = urllib.parse.quote(q + " when:30d")
            urls.append("https://www.bing.com/news/search?q=%s&format=rss&setmkt=ja-JP" % urllib.parse.quote(q))
            urls.append("https://news.google.com/rss/search?q=%s&hl=ja&gl=JP&ceid=JP:ja" % qq)
        urls += cfg.get("feeds", [])
        for u in urls:
            try:
                new += parse_feed(fetch(u), game)
            except Exception as e:
                errs.append("%s: %s" % (game, str(e)[:80]))
        if conf.get("pixiv", True):
            new += collect_pixiv(game, cfg.get("pixiv_tags", [game]), conf.get("exclude_ai", True), errs)
    old = load_data()
    known = {(x["game"], x["link"]) for x in old}
    n = 0  # 画像がない新着記事は本文のog:imageを取得（最大25件/回）
    for it in new:
        if n < 25 and not it.get("img") and not it.get("pix") and (it["game"], it["link"]) not in known \
                and "news.google.com" not in it["link"]:
            it["img"] = og_image(it["link"])
            n += 1
    with lock:
        d = {(x["game"], x["link"]): x for x in old}
        for it in new:
            k = (it["game"], it["link"])
            d[k] = {**d[k], **{a: b for a, b in it.items() if b not in ("", None)}} if k in d else it
        merged = sorted(d.values(), key=lambda x: -x["ts"])[:3000]
        status["last"], status["err"] = now_jst(), errs[:6]
        os.makedirs(os.path.dirname(DATA), exist_ok=True)
        json.dump({"items": merged, "last": status["last"], "err": status["err"], "interval": INTERVAL_MIN},
                  open(DATA, "w", encoding="utf-8"), ensure_ascii=False)


def loop():
    while True:
        try:
            collect()
        except Exception as e:
            status["err"] = [str(e)]
        time.sleep(INTERVAL_MIN * 60)


PAGE = r"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ソシャゲ情報コレクター</title>
<style>
:root{--bg:#f6f7fb;--card:#fff;--tx:#1d2030;--sub:#6a7088;--ln:#e1e4ee;--ac:#5b5bf0;--bgc:#ececff}
@media(prefers-color-scheme:dark){:root{--bg:#14151d;--card:#1e2030;--tx:#eceefa;--sub:#9aa0bd;--ln:#2f3350;--ac:#8c8cff;--bgc:#2b2d52}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,"Hiragino Sans","Noto Sans JP",sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0}.sub{color:var(--sub);font-size:12px;margin:2px 0 12px}
.p{background:var(--card);border:1px solid var(--ln);border-radius:12px;padding:12px;margin-bottom:12px}
.ch{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.c{border:1px solid var(--ln);background:none;color:var(--tx);border-radius:99px;padding:5px 11px;font-size:13px;cursor:pointer}
.c.on{background:var(--bgc);border-color:var(--ac);color:var(--ac);font-weight:600}
input,select{padding:6px 9px;border-radius:8px;border:1px solid var(--ln);background:var(--bg);color:var(--tx);font-size:13px}
.i{display:grid;grid-template-columns:104px 1fr;gap:10px;padding:10px 0;border-top:1px solid var(--ln)}.i:first-child{border-top:0}
.th{width:104px;height:70px;border-radius:8px;object-fit:cover;background:var(--bgc)}.ph{width:104px;height:70px;border-radius:8px;background:var(--bgc)}
.t{font-size:11px;background:var(--bgc);color:var(--ac);border-radius:6px;padding:1px 7px;margin-right:4px}
.g{font-size:11px;color:var(--sub);margin-right:4px}a{color:var(--tx);text-decoration:none}a:hover{color:var(--ac)}.s{font-size:11px;color:var(--sub)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.a img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:10px;background:var(--bgc);display:block}
.a .m{font-size:12px;margin-top:4px;line-height:1.35}.n{color:var(--sub);font-size:11px}
button.r{background:var(--ac);color:#fff;border:0;border-radius:8px;padding:7px 12px;cursor:pointer}
</style></head><body><main>
<h1>ソシャゲ情報コレクター</h1><p class="sub" id="st"></p>
<div class="p"><div class="ch" id="tab"></div></div>
<div class="p"><div class="ch" id="tc"></div></div>
<div class="p"><div class="ch" id="cc"></div><div class="ch" style="margin-top:8px">
<input id="kw" placeholder="キーワード（例：水着）" oninput="draw()"><select id="so" onchange="draw()"></select>
<button class="r" id="rf" style="display:none" onclick="fetch('/refresh').then(()=>setTimeout(load,5000))">今すぐ更新</button></div></div>
<div class="p" id="list"></div></main>
<script>
var D=[],T=[],C="すべて",TAB="news",CATS=["すべて","衣装","キャラ","番組","イベント","話題"];
var local=location.hostname=="127.0.0.1"||location.hostname=="localhost";
function e(s){var d=document.createElement("div");d.textContent=s==null?"":s;return d.innerHTML}
function src(x){if(!x.img)return"";if(x.pix)return local?"/img?u="+encodeURIComponent(x.img):x.img.replace("i.pximg.net","i.pixiv.re");return x.img}
function load(){fetch(local?'/data.json':'data.json').then(r=>r.json()).then(j=>{D=j.items;
 document.getElementById('st').textContent="最終収集: "+j.last+(local?"（"+j.interval+"分ごとに自動更新）":"")+(j.err&&j.err.length?" / 一部取得エラー: "+j.err.join(" | "):"");draw()}).catch(()=>{document.getElementById('list').textContent="データがまだありません"})}
function btn(box,label,on,fn){var b=document.createElement('button');b.className="c"+(on?" on":"");b.textContent=label;b.onclick=fn;box.appendChild(b)}
function draw(){
 var tab=document.getElementById('tab');tab.innerHTML="";
 btn(tab,"ニュース",TAB=="news",()=>{TAB="news";draw()});btn(tab,"イラスト（pixiv）",TAB=="art",()=>{TAB="art";draw()});
 var so=document.getElementById('so'),cur=so.value;so.innerHTML="";
 (TAB=="art"?[["new","新しい順"],["likes","いいね順"],["marks","ブックマーク順"]]:[["new","新しい順"]]).forEach(o=>{var op=document.createElement('option');op.value=o[0];op.textContent=o[1];so.appendChild(op)});
 if([...so.options].some(o=>o.value==cur))so.value=cur;
 if(local)document.getElementById('rf').style.display="";
 document.getElementById('cc').style.display=TAB=="news"?"":"none";
 var games=[...new Set(D.map(x=>x.game))],tc=document.getElementById('tc');tc.innerHTML="";
 games.forEach(g=>btn(tc,g,T.includes(g),()=>{T.includes(g)?T.splice(T.indexOf(g),1):T.push(g);draw()}));
 var cc=document.getElementById('cc');cc.innerHTML="";CATS.forEach(c=>btn(cc,c,C==c,()=>{C=c;draw()}));
 var kw=document.getElementById('kw').value.trim();
 var L=D.filter(x=>(TAB=="art")==!!x.pix&&(!T.length||T.includes(x.game))&&(TAB=="art"||C=="すべて"||x.cat==C)&&(!kw||x.title.includes(kw)));
 var s=so.value;if(s!="new")L.sort((a,b)=>(b[s]||0)-(a[s]||0));
 var box=document.getElementById('list');
 if(!L.length){box.textContent="該当なし（初回は収集に数分かかります）";return}
 if(TAB=="art"){box.innerHTML='<div class="grid">'+L.slice(0,120).map(x=>'<div class="a"><a href="'+e(x.link)+'" target="_blank" rel="noopener"><img loading="lazy" referrerpolicy="no-referrer" src="'+e(src(x))+'" alt=""></a><div class="m">'+e(x.title)+'<div class="n">'+e(x.src)+' ・ '+e(x.game)+'</div><div class="n">♥ '+(x.likes||0)+' ・ ★ '+(x.marks||0)+' ・ 👁 '+(x.views||0)+'</div></div></div>').join("")+'</div>'}
 else{box.innerHTML=L.slice(0,150).map(x=>'<div class="i"><div>'+(x.img?'<img class="th" loading="lazy" referrerpolicy="no-referrer" src="'+e(src(x))+'" onerror="this.className=\'ph\';this.removeAttribute(\'src\')" alt="">':'<div class="ph"></div>')+'</div><div><span class="t">'+e(x.cat)+'</span><span class="g">'+e(x.game)+'</span><a href="'+e(x.link)+'" target="_blank" rel="noopener">'+e(x.title)+'</a><div class="s">'+e(x.src)+' ・ '+new Date(x.ts*1000).toLocaleString('ja-JP',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})+'</div></div></div>').join("")}
}
load();setInterval(load,60000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype, code=200):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/data.json":
            try:
                with lock:
                    body = open(DATA, "rb").read()
            except Exception:
                body = json.dumps({"items": [], "last": status["last"], "err": status["err"], "interval": INTERVAL_MIN}).encode()
            self.send(body, "application/json; charset=utf-8")
        elif u.path == "/refresh":
            threading.Thread(target=collect, daemon=True).start()
            self.send("ok", "text/plain")
        elif u.path == "/img":  # pixiv画像はRefererが必要なので中継する（i.pximg.netのみ許可）
            t = urllib.parse.parse_qs(u.query).get("u", [""])[0]
            p = urllib.parse.urlparse(t)
            if p.scheme == "https" and p.hostname == "i.pximg.net":
                try:
                    self.send(fetch(t, {"Referer": "https://www.pixiv.net/"}), "image/jpeg")
                    return
                except Exception:
                    pass
            self.send("not found", "text/plain", 404)
        else:
            self.send(PAGE, "text/html; charset=utf-8")


def build():
    collect()
    open(os.path.join(HERE, "docs", "index.html"), "w", encoding="utf-8").write(PAGE)
    print("docs/ に出力しました。エラー:", status["err"] or "なし")


if __name__ == "__main__":
    load_conf()
    if "--build" in sys.argv:
        build()
    else:
        threading.Thread(target=loop, daemon=True).start()
        print("起動しました → http://127.0.0.1:%d  （終了: Ctrl+C）" % PORT)
        ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
