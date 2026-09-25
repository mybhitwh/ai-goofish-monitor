#!/usr/bin/env python3
"""推荐流采集探头（方案甲瘦身版）。

职责边界：只做上游平台没有的能力——扫首页个性化信息流 + seen 去重；
候选商品按平台 result_items 的 record 格式注入平台库（data/app.sqlite3），
评估展示、通知、去重视图全部由平台单源负责。

与旧版（工作区根 feed_scan.py）的差异：
- 本地评估只保留"廉价初筛"（话题/硬排除/型号正则），价格区间不再硬编码，
  改读平台 tasks 表（取所有启用任务价格带的交集，最严格口径）；
- PASS/CAND 商品注入平台 result_items（result_filename=feed_scan_full_data.jsonl），
  Web UI 结果页可直接查看，SKIP 不入库；
- 路径不再硬编码 E:/，跟随仓库位置自适应（u12 可直接用）；
- findings.json / findings.log 仅作为运行留痕保留。

用法:
  python tools/feed_scan.py              # 正式跑（有头 Chrome + 登录态）
  python tools/feed_scan.py --limit 6    # 本次最多抓 6 个详情页
  python tools/feed_scan.py --dry-run    # 只读配置与库，不连闲鱼、不写库

环境变量:
  GOOFISH_STATE_DIR   seen/findings 目录，默认 <仓库上级>/feed_state
  APP_DATABASE_FILE   平台库路径，默认 <仓库>/data/app.sqlite3
"""
import asyncio
import json
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(__import__("os").environ.get("APP_DATABASE_FILE", str(REPO_ROOT / "data" / "app.sqlite3")))
STATE = Path(__import__("os").environ.get("GOOFISH_STATE_DIR", str(REPO_ROOT.parent / "feed_state")))
SEEN_FILE = STATE / "seen_items.json"

RESULT_FILENAME = "feed_scan_full_data.jsonl"   # 平台结果页按此分组，关键词=feed_scan
KEYWORD = "feed_scan"
TASK_NAME = "推荐流信息流发现"

# ---------- 廉价初筛规则（非最终判定；最终评估以平台 criteria/AI 为准） ----------
TOPIC_PAT = re.compile(r"(ipad|air|平板)", re.I)
HARD_REJECT = re.compile(r"(128G|64G|512G|1TB|2TB|港版|美版|日版|韩版|教育版|资源机|官换|监管机|扩容|回收|上门回收|以旧换新)", re.I)
MODEL_OK = re.compile(r"(M4|Air\s*8|Air8|8代|2026)", re.I)
DEFECT_PAT = re.compile(r"(磕碰|划痕|破损|碎裂|裂纹|凹陷|掉漆|磨损|瑕疵|变形|亮点|坏点|漏光|发黄|进水|拆修|拆机|维修)", re.I)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_price_band(conn) -> tuple[float, float, list[str]]:
    """价格带单源：读平台启用任务的 min/max，取交集（最严格口径）。"""
    rows = conn.execute(
        "SELECT task_name, min_price, max_price FROM tasks WHERE enabled = 1"
    ).fetchall()
    mins, maxs, names = [], [], []
    for r in rows:
        try:
            lo = float(str(r["min_price"])) if r["min_price"] is not None else None
            hi = float(str(r["max_price"])) if r["max_price"] is not None else None
        except (TypeError, ValueError):
            continue
        if lo is not None:
            mins.append(lo)
        if hi is not None:
            maxs.append(hi)
        names.append(r["task_name"])
    if not mins or not maxs:
        raise SystemExit("[fatal] 平台启用任务中没有可用的价格区间配置，拒绝使用硬编码兜底")
    return max(mins), min(maxs), names


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def known_item_ids(conn) -> set[str]:
    return {str(r[0]) for r in conn.execute(
        "SELECT DISTINCT item_id FROM result_items WHERE item_id IS NOT NULL"
    ).fetchall()}


def find_defects(text: str) -> list[str]:
    """瑕疵词提取；否定仅在其紧邻瑕疵词（前 6 字符内以 无/没/非 结尾）时成立。"""
    defects = set()
    for m in DEFECT_PAT.finditer(text):
        before = text[max(0, m.start() - 6):m.start()]
        if re.search(r"(无|没|非)\s*(明显|任何)?\s*$", before):
            continue
        defects.add(m.group())
    return sorted(defects)


def evaluate(d: dict, price_min: float, price_max: float) -> tuple[str, list[str], str]:
    """本地规则初筛，返回 (verdict, defects, note)。"""
    text = f'{d.get("title", "")} {d.get("desc", "")}'
    defects = find_defects(text)
    try:
        p = float(str(d.get("price", "")).replace("¥", "").strip())
    except (TypeError, ValueError):
        return "SKIP_无价格", defects, ""
    if not (price_min <= p <= price_max):
        return "SKIP_价格区间外", defects, f"price={p}"
    if not TOPIC_PAT.search(text):
        return "SKIP_非平板", defects, ""
    if HARD_REJECT.search(text):
        return "SKIP_硬排除项", defects, HARD_REJECT.search(text).group()
    if not MODEL_OK.search(text):
        return "CAND_型号未标明", defects, "标题描述未含 M4/Air8/2026，需人工看图"
    return "PASS_候选", defects, ""


def build_record(d: dict, feed_title: str, feed_price, verdict: str,
                 defects: list[str], note: str) -> dict:
    """构造与平台 save_result_record 口径一致的中文键 record。"""
    item_id = str(d.get("item_id") or "")
    link = f"https://www.goofish.com/item?id={item_id}"
    return {
        "爬取时间": now_str(),
        "搜索关键字": KEYWORD,
        "任务名称": TASK_NAME,
        "商品信息": {
            "商品标题": d.get("title") or feed_title,
            "当前售价": str(d.get("price") or feed_price or ""),
            "商品原价": "",
            "“想要”人数": d.get("wantCnt") or "",
            "商品标签": "",
            "发货地区": "",
            "卖家昵称": "",
            "商品链接": link,
            "发布时间": str(d.get("gmtCreate") or ""),
            "商品ID": item_id,
            "商品图片列表": [],
            "商品主图链接": "",
            "浏览量": "",
        },
        "卖家信息": {},
        "价格参考": {},
        "ai_analysis": {
            "prompt_version": "feed_scan-r1",
            "is_recommended": verdict == "PASS_候选",
            "reason": f"[{verdict}] {note}".strip(),
            "risk_tags": defects,
            "criteria_analysis": "",
            "value_score": "",
            "value_summary": "",
            "analysis_source": "feed_scan",
            "keyword_hit_count": 0,
        },
    }


def inject_records(records: list[dict]) -> int:
    """注入平台 result_items，口径与 _save_result_record_sync 完全一致。"""
    inserted = 0
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA busy_timeout=5000")
    for record in records:
        item = record["商品信息"]
        link = item["商品链接"]
        link_unique_key = link.split("&", 1)[0]
        try:
            price = float(str(item["当前售价"]).replace("¥", "").strip())
        except (TypeError, ValueError):
            price = None
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO result_items (
                result_filename, keyword, task_name, crawl_time, publish_time, price,
                price_display, item_id, title, link, link_unique_key, seller_nickname,
                is_recommended, analysis_source, keyword_hit_count, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                RESULT_FILENAME, record["搜索关键字"], record["任务名称"],
                record["爬取时间"], item.get("发布时间"), price,
                item.get("当前售价"), item.get("商品ID"), item.get("商品标题"),
                link, link_unique_key, item.get("卖家昵称") or None,
                1 if record["ai_analysis"]["is_recommended"] else 0,
                record["ai_analysis"]["analysis_source"],
                record["ai_analysis"]["keyword_hit_count"],
                json.dumps(record, ensure_ascii=False),
            ),
        )
        inserted += cur.rowcount
    conn.commit()
    conn.close()
    return inserted


# ---------- 采集部分（与旧版相同，仅 STATE 路径自适应） ----------
def extract_items(obj, out: dict):
    if isinstance(obj, dict):
        iid = obj.get("itemId") or obj.get("id")
        title = obj.get("title")
        price = obj.get("soldPrice") or obj.get("price") or obj.get("defaultPrice")
        if iid and (title or price) and isinstance(title, str):
            try:
                p = float(str(price).replace("¥", "").strip())
            except (TypeError, ValueError):
                p = None
            out[str(iid)] = {"title": title, "price": p}
        for v in obj.values():
            extract_items(v, out)
    elif isinstance(obj, list):
        for v in obj:
            extract_items(v, out)


async def collect_feed(context) -> dict:
    page = await context.new_page()
    feed = {}

    async def on_resp(resp):
        try:
            if "mtop." in resp.url:
                if "json" in resp.headers.get("content-type", ""):
                    extract_items(await resp.json(), feed)
        except Exception:
            pass

    page.on("response", on_resp)
    await page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(6000)
    for _ in range(10):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(random.randint(3500, 6500))
    await page.close()
    return feed


async def fetch_detail(context, item_id: str) -> dict:
    page = await context.new_page()
    result = {"item_id": item_id}

    async def on_resp(resp):
        try:
            if "mtop.taobao.idle.pc.detail" in resp.url:
                d = (await resp.json()).get("data", {})
                node = d.get("itemDO") or d.get("item") or d
                result.update({
                    "title": node.get("title", ""),
                    "price": node.get("soldPrice", ""),
                    "desc": node.get("desc", ""),
                    "wantCnt": node.get("wantCnt"),
                    "gmtCreate": node.get("gmtCreate"),
                })
        except Exception:
            pass

    page.on("response", on_resp)
    try:
        await page.goto(f"https://www.goofish.com/item?id={item_id}",
                        wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(7000)
        await page.screenshot(path=str(STATE / f"item_{item_id}.png"))
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"[:100]
    finally:
        await page.close()
    return result


async def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    limit = 12
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    price_min, price_max, band_tasks = load_price_band(conn)
    if dry_run:
        seen = load_seen()
        known = known_item_ids(conn)
        conn.close()
        print(f"[dry-run] DB={DB_PATH} 存在={DB_PATH.exists()}")
        print(f"[dry-run] 价格带(任务交集): {price_min}~{price_max} <- {band_tasks}")
        print(f"[dry-run] seen={len(seen)} 平台库已知 item={len(known)}")
        print("[dry-run] 注入目标:", RESULT_FILENAME, "| keyword:", KEYWORD)
        print("[dry-run] OK：不连闲鱼、不写库")
        return

    from playwright.async_api import async_playwright

    STATE.mkdir(exist_ok=True)
    seen = load_seen()
    known = known_item_ids(conn)
    conn.close()
    today = now_str()

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=False)
        context = await browser.new_context(
            storage_state={"cookies": json.loads(
                (REPO_ROOT / "state" / "acc1.json").read_text(encoding="utf-8")
            ).get("cookies", [])})
        feed = await collect_feed(context)
        print(f"[feed] 信息流捕获商品数: {len(feed)}")

        new_ids = [i for i in feed if i not in seen and i not in known]
        for i in feed:
            seen.setdefault(i, today)
        print(f"[feed] 新商品(未见+库外): {len(new_ids)}")

        candidates = [i for i in new_ids if TOPIC_PAT.search(feed[i]["title"] or "")]
        print(f"[feed] 平板相关新商品: {len(candidates)}")

        records = []
        for iid in candidates[:limit]:
            d = await fetch_detail(context, iid)
            verdict, defects, note = evaluate(d, price_min, price_max)
            print(f"  {verdict:<14} ¥{feed[iid]['price']}  {(d.get('title') or feed[iid]['title'])[:36]}"
                  + (f"  [瑕疵: {','.join(defects)}]" if defects else ""))
            if verdict.startswith(("PASS", "CAND")):
                records.append(build_record(d, feed[iid]["title"],
                                            feed[iid]["price"], verdict, defects, note))
            await asyncio.sleep(random.uniform(6, 12))
        await browser.close()

    # seen 全量回写（SKIP 也记，避免重复打扰）
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")

    inserted = inject_records(records)
    print(f"[inject] 注入平台 result_items: {inserted} 条（目标 {RESULT_FILENAME}）")

    if records:
        (STATE / f"findings_{datetime.now().strftime('%Y%m%d_%H%M')}.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(STATE / "findings.log", "a", encoding="utf-8") as f:
        f.write(f"\n===== {today} | feed={len(feed)} new={len(new_ids)} "
                f"cand={len(candidates)} injected={inserted} =====\n")
        for r in records:
            a = r["ai_analysis"]
            f.write(f"[{'PASS' if a['is_recommended'] else 'CAND'}] "
                    f"¥{r['商品信息']['当前售价']} {r['商品信息']['商品标题'][:40]} "
                    f"id={r['商品信息']['商品ID']} 瑕疵={','.join(a['risk_tags']) or '无'}\n")
    print("DONE, injected:", inserted)


if __name__ == "__main__":
    asyncio.run(main())
