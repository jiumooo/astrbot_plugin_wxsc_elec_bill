#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""电费查询核心库（无 AstrBot 依赖）

- 接口协议：RSA(512bit) 分段加密 + MD5 签名
- 楼栋/房间解析：把「3号楼 / 101室」映射成 loudongId / roomId
- 充值记录：拉取、按日期范围过滤、按人汇总
- 导出：Markdown / Excel
"""
import base64
import hashlib
import json
import os
import random
import time
import urllib.request
from datetime import date, datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── 平台参数：来自小程序前端，非本插件申请、非平台授权凭据 ──────────────
# APPID / APPSECRET 与下面这对 RSA 密钥，都打包在该缴费平台**微信小程序前端**
# 的公开资源里，任何安装者都能在本地提取，属于该平台前端的既有设计。
# 本插件只是原样复刻它的请求协议：公钥加密业务参数、私钥解密响应，
# 与小程序原本的行为一致。这组参数**只用于该电费接口的信封加解密**，
# 与使用者账号、微信登录态、校园统一身份认证无关；插件不读取、不保存任何登录凭据。
# 平台若轮换或作废这组参数，插件将失效；若平台方提出异议，
# 按 DISCLAIMER.md 第五节立即停止维护并从各分发渠道下架。
# 来源与责任的完整说明见 DISCLAIMER.md「二、接口与密钥来源」。
APPID = "AABBCC01"
APPSECRET = "5F10C0339BA54972A4B7599754BB919D"
PUB = (
    "MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBALaQJYUcpj5hJHy5XLl7GgSho+8J8sc4"
    "USX3EKEtV/k9DiL8JAVepNuP/5ay+x9NhlpbcbcBJkfpywRCLRv9fesCAwEAAQ=="
)
PRIV = (
    "MIIBUwIBADANBgkqhkiG9w0BAQEFAASCAT0wggE5AgEAAkEAtpAlhRymPmEkfLlcuXsaBKGj7wnyxzhRJfcQoS1X"
    "+T0OIvwkBV6k24//lrL7H02GWltxtwEmR+nLBEItG/196wIDAQABAkAaNRctxK8YxVk3jmSBO+rAP3QJHKR9dNgxWwZ"
    "IHPpEDQGOfnFAHSXFsbHpkGCZ0u6gRaOqVCVl6dkOZV4CRmABAiEA5QKBKWg+0tOxi6zhQr22ybSkbW6nxsbfPL6opA"
    "irRUECIQDMFEo5WNBsE2u+DlQOTsWHAlViwnoIl0QNnLeskXXcKwIgNxFOG7XcfXe4Fr0ggZr67C/K5iGYqHzDy2iRRr"
    "hq7sECICEtEzvrYkaPia5l6ehBif7+tB7P+sK2xItp9LoLE5vtAiA05JSSoPPRTnLXJkxKSSzMxcYZvmTnsV0rFayTCO"
    "GdEA=="
)
BASE = "https://pay.wxstc.cn/grdp-ele-third-unite"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MicroMessenger/7.0.20 MiniProgramEnv/Windows"

_pub = serialization.load_der_public_key(base64.b64decode(PUB))
_priv = serialization.load_der_private_key(base64.b64decode(PRIV), password=None)
_BS = _priv.key_size // 8


def _enc_long(text):
    step = _pub.key_size // 8 - 11
    out = b""
    for i in range(0, len(text), step):
        out += _pub.encrypt(text[i:i + step], padding.PKCS1v15())
    return base64.b64encode(out).decode()


def _dec_long(b64):
    ct = base64.b64decode(b64)
    out = b""
    for i in range(0, len(ct), _BS):
        blk = ct[i:i + _BS]
        if len(blk) < _BS:
            break
        out += _priv.decrypt(blk, padding.PKCS1v15())
    return out.decode("utf-8", "replace")


def call(path, payload, timeout=15):
    biz = _enc_long(json.dumps(payload, separators=(",", ":")).encode())
    ts = int(time.time() * 1000)
    rid = format(int(str(random.randint(100, 999)) + str(ts)), "x")
    body = {
        "appid": APPID,
        "biz_data": biz,
        "biz_encryption": "RSA",
        "biz_type": 0,
        "requestid": rid,
        "sign_type": "MD5",
        "timestamp": ts,
        "version": "V3.0",
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True) + APPSECRET
    body["sign"] = hashlib.md5(raw.encode()).hexdigest().upper()

    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body, separators=(",", ":")).encode(),
        method="POST",
        headers={"Content-Type": "application/json;charset=UTF-8", "Accept": "*/*", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read().decode("utf-8", "replace"))
    if isinstance(res.get("data"), str) and res["data"]:
        res["decoded"] = json.loads(_dec_long(res["data"]))
    return res


# ---------------------------------------------------------------- 楼栋/房间解析
_ROOM_CACHE = {"ts": 0.0, "rows": []}
_ROOM_TTL = 3600


def all_rooms(xiaoqu_id="1", datasource="cg1_1", force=False):
    now = time.time()
    if not force and _ROOM_CACHE["rows"] and now - _ROOM_CACHE["ts"] < _ROOM_TTL:
        return _ROOM_CACHE["rows"]
    res = call("/api/school/ele/third/room/list",
               {"xiaoquId": str(xiaoqu_id), "datasource": datasource, "servicecode": "0826"})
    d = res.get("decoded") or []
    rows = [
        {
            "loudongId": str(x.get("loudongId")).strip(),
            "loudong": str(x.get("loudong") or "").strip(),
            "roomId": str(x.get("roomId")).strip(),
            "room": str(x.get("room") or "").strip(),
        }
        for x in d
        if x.get("loudongId") and x.get("roomId")
    ]
    _ROOM_CACHE["rows"] = rows
    _ROOM_CACHE["ts"] = now
    return rows


def _digits(s):
    s = str(s or "").strip()
    out = ""
    for ch in s:
        if ch.isdigit():
            out += ch
        elif out:
            break
    return out


_CN_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def loudong_number(name):
    """从「二号楼」「12号楼」「十一号楼」里取出楼栋号"""
    t = str(name or "").replace(" ", "")
    for suf in ("号楼", "号", "栋", "楼"):
        if t.endswith(suf):
            t = t[: -len(suf)]
    if t.isdigit():
        return int(t)
    if t == "十":
        return 10
    if t.startswith("十") and len(t) == 2:
        return 10 + _CN_DIGIT.get(t[1], 0)
    if len(t) == 2 and t[1] == "十":
        return _CN_DIGIT.get(t[0], 0) * 10
    if len(t) == 1:
        return _CN_DIGIT.get(t)
    return None


def resolve_room(xiaoqu_id="1", datasource="cg1_1", loudong_no="", room_no="",
                 loudong_id="", room_id=""):
    """把「3号楼 / 101室」解析成接口需要的 4 个 ID

    返回 dict: xiaoquId, loudongId, roomId, datasource, loudongName, roomName
    解析失败抛 ValueError，消息里说明原因
    """
    base = {"xiaoquId": str(xiaoqu_id), "datasource": datasource}

    if str(loudong_id).strip() and str(room_id).strip():
        base.update(loudongId=str(loudong_id).strip(), roomId=str(room_id).strip(),
                    loudongName="", roomName="")
        return base

    ln = _digits(loudong_no)
    rn = _digits(room_no)
    if not ln or not rn:
        raise ValueError("楼栋号和房间号都得填数字，例如 楼栋=3、房间=101（表示 3号楼 101室）")

    rows = all_rooms(xiaoqu_id, datasource)
    want_l = "%d号楼" % int(ln)
    cand = [r for r in rows if loudong_number(r["loudong"]) == int(ln)]
    if not cand:
        names = sorted({r["loudong"] for r in rows})
        raise ValueError("找不到「%s」。可选楼栋：%s" % (want_l, "、".join(names)))

    loudong_id = cand[0]["loudongId"]
    loudong_name = cand[0]["loudong"]

    full = ln + rn  # 3 + 101 -> 3101
    hit = [r for r in rows if r["loudongId"] == loudong_id and r["room"] == full]
    if not hit:
        hit = [r for r in rows if r["loudongId"] == loudong_id and r["room"] == rn]
    if not hit:
        sample = sorted(r["room"] for r in cand)[:6]
        raise ValueError("在「%s」里找不到房间「%s」（按 %s室 解析成 %s）。该楼房间示例：%s"
                         % (loudong_name, rn, rn, full, "、".join(sample)))

    base.update(loudongId=loudong_id, roomId=hit[0]["roomId"],
                loudongName=loudong_name, roomName=hit[0]["room"])
    return base


def describe(room):
    if room.get("loudongName"):
        rn = room.get("roomName") or ""
        lname = room["loudongName"]
        short = rn[len(_digits(lname)):] if rn.startswith(_digits(lname)) else rn
        return "%s %s室" % (lname, short)
    return "楼栋%s / 房间%s" % (room.get("loudongId"), room.get("roomId"))


# ---------------------------------------------------------------- 查询
def query_room(room):
    """当前余额/用量"""
    res = call("/api/school/ele/third/room/get", room)
    d = res.get("decoded")
    if not d:
        raise RuntimeError("接口未返回可用数据: %s" % json.dumps(res, ensure_ascii=False)[:200])
    used = float(d["elecUsed"])
    buy = float(d["elecBuy"])
    return {
        "xiaoqu": d.get("xiaoqu"),
        "loudong": str(d.get("loudong") or "").strip(),
        "room": str(d.get("room") or "").strip(),
        "used": used,
        "buy": buy,
        "left": buy - used,
    }


def fetch_recharge(room, size=500):
    p = dict(room)
    p.update(pageNum=1, page=1, limit=size, pageSize=size, servicecode="0826")
    res = call("/api/school/ele/third/recharge/list", p)
    d = res.get("decoded")
    if not isinstance(d, list):
        raise RuntimeError("充值接口未返回列表: %s" % json.dumps(res, ensure_ascii=False)[:200])
    out = []
    for x in d:
        out.append({
            "time": str(x.get("trantime") or "").strip(),
            "amt": round(float(x.get("tranamt") or 0), 2),
            "buyer": str(x.get("buyerId") or "").strip(),
            "recNo": str(x.get("recNo") or "").strip(),
            "outNo": str(x.get("outNo") or "").strip(),
            "room": str(x.get("room") or "").strip(),
            "loudong": str(x.get("loudong") or "").strip(),
        })
    out.sort(key=lambda r: r["time"], reverse=True)
    return out


# ---------------------------------------------------------------- 日期范围
def _d(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def parse_range(text):
    """把用户的自然语言范围解析成 (start, end, label)，日期均含端点

    支持: 空/全部 | 近7天 7d 近30天 | 本月 上月 今年 去年
          2024-09（整月） | 2024（整年） | 2024-09-01~2024-12-31
    """
    t = (text or "").strip().replace("～", "~").replace("—", "~").replace("到", "~")
    today = date.today()

    if not t or t in ("全部", "all", "所有"):
        return "", "", "全部"

    import re
    m = re.match(r"^(?:近|最近|last)?\s*(\d{1,3})\s*(?:天|d|day|days)$", t, re.I)
    if m:
        n = int(m.group(1))
        return (today - timedelta(days=n - 1)).isoformat(), today.isoformat(), "近%d天" % n

    if t in ("本月", "这个月"):
        return today.replace(day=1).isoformat(), today.isoformat(), "本月"
    if t in ("上月", "上个月"):
        first = today.replace(day=1)
        last = first - timedelta(days=1)
        return last.replace(day=1).isoformat(), last.isoformat(), "上月"
    if t == "今年":
        return date(today.year, 1, 1).isoformat(), today.isoformat(), "%d年" % today.year
    if t == "去年":
        return date(today.year - 1, 1, 1).isoformat(), date(today.year - 1, 12, 31).isoformat(), "%d年" % (today.year - 1)

    m = re.match(r"^(\d{4})-(\d{1,2})$", t)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        first = date(y, mo, 1)
        nxt = date(y + (mo // 12), (mo % 12) + 1, 1)
        return first.isoformat(), (nxt - timedelta(days=1)).isoformat(), "%d-%02d" % (y, mo)

    m = re.match(r"^(\d{4})$", t)
    if m:
        y = int(m.group(1))
        return date(y, 1, 1).isoformat(), date(y, 12, 31).isoformat(), "%d年" % y

    if "~" in t:
        a, b = [s.strip() for s in t.split("~", 1)]
        def norm(s, end=False):
            if len(s) == 7:
                y, mo = int(s[:4]), int(s[5:7])
                first = date(y, mo, 1)
                nxt = date(y + (mo // 12), (mo % 12) + 1, 1)
                return (nxt - timedelta(days=1)).isoformat() if end else first.isoformat()
            if len(s) == 4:
                y = int(s)
                return date(y, 12, 31).isoformat() if end else date(y, 1, 1).isoformat()
            return _d(s).isoformat()
        try:
            s1, s2 = norm(a), norm(b, end=True)
        except Exception:
            raise ValueError("日期格式看不懂：%s。示例：2024-09-01~2024-12-31" % t)
        return s1, s2, "%s~%s" % (s1, s2)

    try:
        x = _d(t)
        return x.isoformat(), x.isoformat(), x.isoformat()
    except Exception:
        raise ValueError("范围看不懂：%s。可用：全部 / 近30天 / 本月 / 2024-09 / 2024 / 2024-09-01~2024-12-31" % t)


def in_range(items, start, end):
    if not start and not end:
        return list(items)
    out = []
    for r in items:
        d = r["time"][:10]
        if start and d < start:
            continue
        if end and d > end:
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------- 汇总
def summarize(items):
    agg = {}
    for r in items:
        b = r["buyer"] or "未知"
        a = agg.setdefault(b, {"buyer": b, "n": 0, "amt": 0.0, "first": r["time"], "last": r["time"]})
        a["n"] += 1
        a["amt"] += r["amt"]
        if r["time"] < a["first"]:
            a["first"] = r["time"]
        if r["time"] > a["last"]:
            a["last"] = r["time"]
    rows = list(agg.values())
    for a in rows:
        a["amt"] = round(a["amt"], 2)
    rows.sort(key=lambda a: (-a["amt"], -a["n"]))
    return rows


def total_of(items):
    return round(sum(r["amt"] for r in items), 2)


# ---------------------------------------------------------------- 文本渲染
def fmt_recharge(title, items, top=15):
    if not items:
        return "%s\n────────────────\n没有记录。" % title
    lines = [title, "────────────────",
             "共 %d 笔 · 合计 %.2f 元" % (len(items), total_of(items)), ""]
    for r in items[:top]:
        lines.append("%s  %8.2f 元  #%s" % (r["time"][:16], r["amt"], r["buyer"]))
    if len(items) > top:
        lines.append("… 另有 %d 笔，用 /导出 拿完整表" % (len(items) - top))
    return "\n".join(lines)


def fmt_summary(title, items):
    rows = summarize(items)
    if not rows:
        return "%s\n────────────────\n没有记录。" % title
    lines = [title, "────────────────",
             "共 %d 笔 · %.2f 元 · %d 人" % (len(items), total_of(items), len(rows)), ""]
    for a in rows:
        lines.append("%-10s %3d 笔  %8.2f 元" % ("#" + a["buyer"], a["n"], a["amt"]))
    return "\n".join(lines)


# ---------------------------------------------------------------- 导出
def _safe_name(s):
    bad = '\\/:*?"<>| '
    out = "".join(("_" if ch in bad else ch) for ch in str(s))
    return out.strip() or "export"


def export_md(room, items, out_dir, label="全部"):
    os.makedirs(out_dir, exist_ok=True)
    who = describe(room)
    rows = summarize(items)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(out_dir, _safe_name("充值台账-%s-%s.md" % (who, label)) + "")
    path = os.path.join(out_dir, "充值台账-%s-%s-%s.md" % (_safe_name(who), _safe_name(label), stamp))

    L = []
    L.append("# 电费充值台账 · %s\n" % who)
    L.append("| 项目 | 值 |")
    L.append("| --- | --- |")
    L.append("| 范围 | %s |" % label)
    L.append("| 笔数 | %d |" % len(items))
    L.append("| 合计金额 | %.2f 元 |" % total_of(items))
    L.append("| 涉及人数 | %d |" % len(rows))
    L.append("| 导出时间 | %s |" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if items:
        L.append("| 时间跨度 | %s ~ %s |" % (min(r["time"] for r in items), max(r["time"] for r in items)))
    L.append("")
    L.append("## 一、每人充值汇总\n")
    L.append("| # | 买家ID | 笔数 | 累计金额(元) | 首次充值 | 最近充值 |")
    L.append("| ---: | --- | ---: | ---: | --- | --- |")
    for i, a in enumerate(rows, 1):
        L.append("| %d | `%s` | %d | %.2f | %s | %s |"
                 % (i, a["buyer"], a["n"], a["amt"], a["first"], a["last"]))
    L.append("")
    L.append("## 二、充值明细（按时间倒序）\n")
    L.append("| # | 充值时间 | 金额(元) | 买家ID | 记录号 | 外部订单号 |")
    L.append("| ---: | --- | ---: | --- | --- | --- |")
    for i, r in enumerate(items, 1):
        L.append("| %d | %s | %.2f | `%s` | %s | %s |"
                 % (i, r["time"], r["amt"], r["buyer"], r["recNo"], r["outNo"]))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    return path


def export_xlsx(room, items, out_dir, label="全部"):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
        from openpyxl.utils import get_column_letter
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("环境里没有 openpyxl（%s），无法导出 Excel" % e)

    os.makedirs(out_dir, exist_ok=True)
    who = describe(room)
    rows = summarize(items)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(out_dir, "充值台账-%s-%s-%s.xlsx" % (_safe_name(who), _safe_name(label), stamp))

    head_fill = PatternFill("solid", fgColor="2F6FEB")
    head_font = Font(color="FFFFFF", bold=True)
    thin = Side(style="thin", color="D0D7DE")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()

    ws = wb.active
    ws.title = "按人汇总"
    ws.append(["买家ID", "笔数", "累计金额(元)", "占总额比", "首次充值", "最近充值"])
    for a in rows:
        pct = (a["amt"] / total_of(items) * 100) if total_of(items) else 0
        ws.append([a["buyer"], a["n"], a["amt"], round(pct, 2) / 100, a["first"], a["last"]])
    ws.append([])
    ws.append(["合计", len(items), total_of(items), 1, "", ""])

    ws2 = wb.create_sheet("充值明细")
    ws2.append(["序号", "充值时间", "金额(元)", "买家ID", "记录号", "外部订单号"])
    for i, r in enumerate(items, 1):
        ws2.append([i, r["time"], r["amt"], r["buyer"], r["recNo"], r["outNo"]])

    ws3 = wb.create_sheet("信息")
    ws3.append(["项目", "值"])
    ws3.append(["房间", who])
    ws3.append(["范围", label])
    ws3.append(["笔数", len(items)])
    ws3.append(["合计金额(元)", total_of(items)])
    ws3.append(["涉及人数", len(rows)])
    ws3.append(["导出时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])

    widths = {1: 16, 2: 22, 3: 14, 4: 14, 5: 22, 6: 22}
    for sheet in (ws, ws2, ws3):
        for c in range(1, 7):
            sheet.column_dimensions[get_column_letter(c)].width = widths.get(c, 14)
        for cell in sheet[1]:
            cell.fill = head_fill
            cell.font = head_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for row in sheet.iter_rows(min_row=1, max_row=sheet.max_row):
            for cell in row:
                cell.border = border

    for cell in ws["D"][1:]:
        cell.number_format = "0.00%"
    for c in ("C",):
        for cell in ws[c][1:]:
            cell.number_format = "0.00"
    for cell in ws2["C"][1:]:
        cell.number_format = "0.00"
    ws.freeze_panes = "A2"
    ws2.freeze_panes = "A2"
    wb.save(path)
    return path
