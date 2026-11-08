# -*- coding: utf-8 -*-
"""无锡科技职业技术学院 · 电费查询插件

独立运行，不依赖任何外部脚本：
  - 查询电表余额 / 用电量 / 购电量（可按「默认开始时间」分段统计）
  - 查询充值记录（明细 / 每人汇总 / 总表），时间精确到秒
  - 充值账号可备注姓名，导出与通知自动带备注
  - 定时日报 + 充值提醒，可自选推送目标，多笔可合并成一条
  - 内置 Web 控制台：选楼栋/房间、按时间范围查询、备注管理、导出、手动推送

日志：全部输出走 astrbot.api.logger；本文件 import logging 仅用于实现只读的内存环形缓冲。
"""
import asyncio
import io
import json
import os
import sys

# `logging` 只用于下面 _RingHandler 继承 logging.Handler、使用 logging.Formatter，
# 把本插件 logger 已经产生的日志抄一份进内存，供控制台「运行日志」查看。
# 不调用 logging.basicConfig()，不修改 root logger，不设置任何全局日志级别，
# 也不注册文件或网络 handler —— 所有日志输出仍然走 astrbot.api.logger。
import logging

import threading
import time
from collections import deque
from datetime import date, datetime, timedelta

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.api.web import request, json_response, error_response, file_response
from astrbot.core.utils.astrbot_path import (
    get_astrbot_config_path,
    get_astrbot_plugin_data_path,
)

PLUGIN_NAME = "astrbot_plugin_wxsc_elec_bill"


class _RingHandler(logging.Handler):
    """把本插件自己的日志收进内存环形缓冲，供控制台「运行日志」查看。

    只收名字带插件名、或正文带 [电费] 前缀的记录，避免混入其它插件。

    只读旁路：不改日志级别、不写文件、不发网络请求，也不触碰 AstrBot 的全局日志
    配置。它只是挂在插件 logger 上的一个采集器，重载插件时会被主动摘掉。
    """

    def __init__(self, buf):
        logging.Handler.__init__(self)
        self.buf = buf
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        try:
            name = str(getattr(record, "name", "") or "")
            msg = self.format(record)
            if PLUGIN_NAME not in name and "[电费]" not in msg:
                return
            self.buf.append({
                "t": time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.localtime(record.created)),
                "lv": str(record.levelname or "INFO").upper(),
                "line": int(getattr(record, "lineno", 0) or 0),
                "msg": msg,
            })
        except Exception:  # noqa: BLE001
            pass

PLUGIN_VERSION = "3.1.2"
DISPLAY_NAME = "无锡科技职业技术学院电费查询"

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import elec_core as ec  # noqa: E402

# 这些键不在面板里展示，但代码仍然读（保留兼容，默认值即可）
XIAOQU_DEFAULT = "1"
DATASOURCE_DEFAULT = "cg1_1"

CONF_KEYS = (
    "loudong_no", "room_no",
    "notify_enable", "notify_times", "notify_targets",
    "recharge_watch", "recharge_interval", "low_threshold",
    "show_left", "show_delta", "show_total", "show_forecast",
    "show_recharge", "show_warn",
    "napcat_url", "napcat_token", "default_start",
    "elec_price",
)

ROOM_KEYS = ("loudong_no", "room_no", "xiaoqu_id", "datasource")


def _chain(text):
    """构造只含文本的消息链（兼容不同小版本 API）。"""
    from astrbot.core.message.message_event_result import MessageChain

    try:
        mc = MessageChain()
        mc.message(text)
        return mc
    except Exception:  # noqa: BLE001
        return MessageChain([Plain(text)])


def _as_bool(v, default=True):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "是", "开"):
        return True
    if s in ("0", "false", "no", "off", "否", "关"):
        return False
    return default


def _times(raw, default="08:00,20:00"):
    """把 "8,20:30,07:00" 之类解析成 ["08:00","20:30","07:00"]。"""
    out = []
    for part in str(raw or default).replace("，", ",").split(","):
        p = part.strip()
        if not p:
            continue
        if ":" not in p:
            if p.isdigit() and 0 <= int(p) <= 23:
                out.append("%02d:00" % int(p))
            continue
        hh, _, mm = p.partition(":")
        if hh.isdigit() and mm.isdigit():
            h, m = int(hh), int(mm)
            if 0 <= h <= 23 and 0 <= m <= 59:
                out.append("%02d:%02d" % (h, m))
    return sorted(set(out)) or _times(default, "08:00,20:00")


def _num(s):
    """取字符串开头的数字串。"""
    s = str(s or "").strip()
    out = ""
    for ch in s:
        if ch.isdigit():
            out += ch
        elif out:
            break
    return out


def _now_ts():
    return int(time.time())


def _split_dt(text):
    """把 '2025-09-13 22:02:33' 拆成 ('2025-09-13', '22:02:33')。"""
    t = str(text or "").strip()
    if len(t) >= 19:
        return t[:10], t[11:19]
    if len(t) == 16:
        return t[:10], t[11:16]
    if len(t) == 10:
        return t, ""
    return t, ""


@register("astrbot_plugin_wxsc_elec_bill", "jiumo",
          "无锡科技职业技术学院电费查询：余额查询、充值台账、备注管理、定时通知", PLUGIN_VERSION)
class ElecBillPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config
        data_root = get_astrbot_plugin_data_path()
        self.data_dir = os.path.join(data_root, PLUGIN_NAME)
        self.export_dir = os.path.join(self.data_dir, "export")
        os.makedirs(self.export_dir, exist_ok=True)
        self.notes_file = os.path.join(self.data_dir, "notes.json")
        self.state_file = os.path.join(self.data_dir, "state.json")
        self.hist_file = os.path.join(self.data_dir, "hist.json")
        self._room_cache = None
        self._room_lock = threading.Lock()
        self._money_cache = {"at": 0.0, "key": "", "data": None}
        self._preview_cache = {"at": 0.0, "key": "", "room": None,
                              "items": None, "info": None}
        self._sent = {}
        self._last_watch = 0.0
        self._task = None
        self._warm_task = None
        self._notes_cache = None
        self._tree_cache = {"ts": 0.0, "rows": []}
        self._log_buf = deque(maxlen=600)
        self._notice_file = os.path.join(self.data_dir, "notices.json")
        self._notice_buf = deque(maxlen=200)
        self._notice_load()
        self._attach_log_ring()
        try:
            self._cfg_path = os.path.join(get_astrbot_config_path(),
                                          PLUGIN_NAME + "_config.json")
        except Exception:  # noqa: BLE001
            self._cfg_path = ""
        self._cfg_mtime = None
        self._cfg_refresh()
        self._register_apis()

    def _register_apis(self):
        table = [
            ("/overview", self.api_overview, ["GET"]),
            ("/settings", self.api_settings, ["POST"]),
            ("/rooms", self.api_rooms, ["GET"]),
            ("/data", self.api_data, ["GET"]),
            ("/notes", self.api_notes, ["POST"]),
            ("/notify", self.api_notify, ["POST"]),
            ("/export", self.api_export, ["POST"]),
            ("/download", self.api_download, ["GET"]),
            ("/targets", self.api_targets, ["GET"]),
            ("/preview", self.api_preview, ["POST"]),
            ("/runlog", self.api_runlog, ["GET"]),
        ]
        for route, fn, methods in table:
            try:
                self.context.register_web_api("/" + PLUGIN_NAME + route, fn, methods,
                                              "wxsc elec bill")
            except Exception as e:  # noqa: BLE001
                logger.error("[电费] 注册接口 %s 失败: %s", route, e)

    # ------------------------------------------------------------ 配置
    def _cfg_refresh(self):
        """配置文件被面板改动后热加载，无需重载插件。"""
        path = getattr(self, "_cfg_path", "")
        if not path:
            return
        try:
            mt = os.path.getmtime(path)
        except Exception:  # noqa: BLE001
            return
        if mt == getattr(self, "_cfg_mtime", None):
            return
        self._cfg_mtime = mt
        try:
            with io.open(path, encoding="utf-8-sig") as f:
                disk = json.load(f)
        except Exception:  # noqa: BLE001
            return
        if not isinstance(disk, dict):
            return
        room_changed = False
        for k, v in disk.items():
            if self.config.get(k) != v:
                self.config[k] = v
                if k in ROOM_KEYS:
                    room_changed = True
        if room_changed:
            self._room_cache = None

    def _c(self, key, default=None):
        try:
            v = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            v = default
        return default if v is None else v

    def _set_conf(self, **kv):
        changed = {}
        for k, v in kv.items():
            if v is None:
                continue
            self.config[k] = v
            changed[k] = v
        try:
            self.config.save_config()
        except Exception as e:  # noqa: BLE001
            logger.error("[电费] 保存配置失败: %s", e)
        if any(k in changed for k in ROOM_KEYS):
            self._room_cache = None
        return changed

    def _settings_dict(self):
        return {k: self._c(k) for k in CONF_KEYS}

    def _xiaoqu(self):
        return str(self._c("xiaoqu_id", XIAOQU_DEFAULT) or XIAOQU_DEFAULT)

    def _datasource(self):
        return str(self._c("datasource", DATASOURCE_DEFAULT) or DATASOURCE_DEFAULT)

    def _default_start(self):
        """默认开始时间（datetime 或 None）。"""
        raw = str(self._c("default_start", "") or "").strip()
        return self._dt(raw)

    def _price(self):
        """固定电价（元/度），算不准就用 0.55。"""
        try:
            v = float(self._c("elec_price", 0.55))
        except Exception:  # noqa: BLE001
            v = 0.55
        return v if v > 0 else 0.55

    def _default_range(self):
        d = self._default_start()
        if not d:
            return {"text": "", "label": "全部", "start": "", "end": ""}
        st = d.strftime("%Y-%m-%d %H:%M:%S")
        return {"text": st, "label": "%s 起" % st, "start": st, "end": ""}

    # ------------------------------------------------------------ 房间
    # 线程模型（v3.0.0）：
    #   _room / _where / _room_json / _balance / _room_tree / _report_text
    #   内部都会同步走 urllib（elec_core.call，超时 15 秒），属于阻塞调用。
    #   事件循环里一律用 await asyncio.to_thread(...) 包一层，
    #   不要在这些 async 函数体内直接调用它们。
    def _room(self):
        """解析当前房间。缓存命中立即返回，未命中会走网络（最长约 15 秒）。

        阻塞方法，只能在线程里调用；协程里请用 await self._room_async()。
        """
        if self._room_cache:
            return self._room_cache
        with self._room_lock:
            if self._room_cache:
                return self._room_cache
            ln = str(self._c("loudong_no", "") or "").strip()
            rn = str(self._c("room_no", "") or "").strip()
            if not (ln and rn):
                raise RuntimeError("还没设置楼栋号和房间号，请到插件设置或 Web 控制台里选择")
            room = ec.resolve_room(self._xiaoqu(), self._datasource(), ln, rn, "", "")
            self._room_cache = room
            return room

    @staticmethod
    def _fetch_recharge(room, tries=2):
        """拉充值记录，超时自动重试一次（平台偶发慢响应）。"""
        last = None
        for i in range(tries):
            try:
                return ec.fetch_recharge(room)
            except Exception as e:  # noqa: BLE001
                last = e
                logger.warning("[电费] 拉取充值记录失败（第 %d 次）：%s", i + 1, e)
                if i + 1 < tries:
                    time.sleep(1.2)
        raise last

    async def _room_async(self):
        """把可能阻塞的房间解析丢到工作线程，事件循环不受影响。"""
        return await asyncio.to_thread(self._room)

    async def _where_async(self):
        return await asyncio.to_thread(self._where)

    def _where(self):
        """房间中文描述。**绝不触发网络**：缓存未命中时用配置直接拼。

        这样任何协程里调用它都不会阻塞事件循环；需要精确名称时先 await
        self._room_async() 把缓存填好。
        """
        room = self._room_cache
        if room:
            try:
                return ec.describe(room)
            except Exception:  # noqa: BLE001
                pass
        ln = str(self._c("loudong_no", "") or "").strip()
        rn = str(self._c("room_no", "") or "").strip()
        if not (ln and rn):
            return "（未设置房间）"
        return "%s号楼 %s室（未校验）" % (ln, rn)

    def _room_json(self):
        room = self._room()
        return {"loudongId": room.get("loudongId"), "roomId": room.get("roomId"),
                "desc": ec.describe(room)}

    def _room_tree(self, force=False):
        """楼栋 → 房间 的可选树，供控制台下拉选择，避免手填不可查的房间。"""
        now = time.time()
        if (not force and self._tree_cache["rows"]
                and now - self._tree_cache["ts"] < 300):
            return self._tree_cache["rows"]
        rows = ec.all_rooms(self._xiaoqu(), self._datasource(), force=force)
        groups = {}
        for r in rows:
            key = (r["loudongId"], r["loudong"] or r["loudongId"])
            groups.setdefault(key, []).append({
                "room": r["room"],
                "roomId": r["roomId"],
                "no": _num(r["room"]),
            })
        out = []
        for (lid, lname), rooms in groups.items():
            rooms.sort(key=lambda x: (len(x["no"]), x["no"]))
            out.append({
                "loudongId": lid,
                "loudong": lname,
                "no": ec.loudong_number(lname),
                "rooms": rooms,
            })
        out.sort(key=lambda g: (g["no"] if g["no"] is not None else 9999,
                                len(g["loudong"]), g["loudong"]))
        self._tree_cache = {"ts": now, "rows": out}
        return out

    # ------------------------------------------------------------ 备注
    def _notes(self):
        if self._notes_cache is None:
            try:
                with open(self.notes_file, encoding="utf-8") as f:
                    self._notes_cache = json.load(f)
            except Exception:  # noqa: BLE001
                self._notes_cache = {}
        return self._notes_cache

    def _save_notes(self):
        with open(self.notes_file, "w", encoding="utf-8") as f:
            json.dump(self._notes(), f, ensure_ascii=False, indent=2)

    def _label(self, buyer):
        b = str(buyer)
        return self._notes().get(b) or ("#%s" % b)

    def set_note(self, buyer, name):
        b = str(buyer).strip()
        if not b:
            return False
        if b.startswith("#"):
            b = b[1:]
        notes = self._notes()
        name = (name or "").strip()
        if name:
            notes[b] = name
        else:
            notes.pop(b, None)
        self._save_notes()
        return True

    # ------------------------------------------------------------ 历史与累计
    def _hist(self):
        try:
            with open(self.hist_file, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _hist_touch(self, info):
        """按天记录用电/购电的首个与最新样本，用于算「昨天用电」与累计。"""
        h = self._hist()
        d = date.today().strftime("%Y-%m-%d")
        used = float(info.get("used") or 0)
        buy = float(info.get("buy") or 0)
        rec = h.get(d) or {}
        rec.setdefault("first_used", used)
        rec.setdefault("first_buy", buy)
        rec["used"] = used
        rec["buy"] = buy
        h[d] = rec
        for k in sorted(h.keys())[:-120]:
            h.pop(k, None)
        try:
            with open(self.hist_file, "w", encoding="utf-8") as f:
                json.dump(h, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
        return h

    def _day_used(self, h, back=1):
        """back=1 昨天，back=2 前天……"""
        key = (date.today() - timedelta(days=back)).strftime("%Y-%m-%d")
        r = h.get(key) or {}
        if "first_used" in r and "used" in r:
            v = r["used"] - r["first_used"]
            if v >= 0:
                return v
        today = h.get(date.today().strftime("%Y-%m-%d")) or {}
        if r.get("used") is not None and today.get("first_used") is not None:
            v = today["first_used"] - r["used"]
            if v >= 0:
                return v
        return None

    def _cum(self, h, info):
        """按「默认开始时间」算区间用电/购电。

        返回 dict（而不是元组），把几种「算不出可信区间值」的情形分开，
        前端才能给出不误导的提示。

        ready     是否算出了区间值
        used/buy  区间用量（ready 为真时有效）
        base      实际用作基线的日期
        approx    基线是近似的：早于默认开始时间只有更晚的样本
        from_open 没有比默认开始更早的样本，按「户表累计」处理：
                  认为默认开始时间点上的读数就是 0（宿舍开学开户时表底为 0），
                  区间值等于平台返回的累计值，日期显示用户设置的默认开始时间
        hist_from 历史数据最早日期
        start     配置的默认开始时间
        reason    未就绪原因：no_start / no_hist / no_base
        """
        d = self._default_start()
        keys = sorted(h.keys())
        hist_from = keys[0] if keys else ""
        out = {"ready": False, "used": None, "buy": None, "base": "",
               "approx": False, "from_open": False, "hist_from": hist_from,
               "start": d.strftime("%Y-%m-%d %H:%M:%S") if d else "",
               "reason": ""}
        if not d:
            out["reason"] = "no_start"
            return out
        if not keys:
            out["reason"] = "no_hist"
            return out

        today = date.today().strftime("%Y-%m-%d")
        dk = d.strftime("%Y-%m-%d")
        if dk > today:
            # 默认开始时间填成了未来：直接说清楚，别算出一个 0 度来
            out["reason"] = "start_future"
            out["base"] = dk
            return out

        base, approx, from_open = "", False, False
        for k in keys:
            if k <= dk:
                base = k
        if not base:
            # 早于默认开始时间一条样本都没有：按「户表累计」处理。
            # 宿舍开学开户时表底为 0，所以把该时点读数视为 0，
            # 区间值 = 平台给的累计值，日期显示默认开始时间而不是安装日。
            base = dk
            from_open = True
        if base > today:
            out["reason"] = "no_base"
            out["base"] = base
            return out

        if from_open:
            try:
                cu = float(info["used"])
                cb = float(info["buy"])
            except Exception:  # noqa: BLE001
                out["reason"] = "no_base"
                return out
        else:
            r = h.get(base) or {}
            if r.get("first_used") is None:
                out["reason"] = "no_base"
                out["base"] = base
                return out
            try:
                cu = float(info["used"]) - float(r["first_used"])
                cb = float(info["buy"]) - float(r.get("first_buy") or 0)
            except Exception:  # noqa: BLE001
                out["reason"] = "no_base"
                return out

        out.update({"ready": True, "used": max(cu, 0.0), "buy": max(cb, 0.0),
                    "base": base, "approx": approx, "from_open": from_open})
        return out

    def _avg_used(self, h):
        """日均用电（度/天）。

        优先用历史读数跨天差值：最后一天的累计读数 − 最早一天的起始读数，除以相隔天数。
        只有一天样本时退回「昨天」的单日用量。
        返回 (avg, span_days, source)；算不出返回 (None, 0, "")。
        """
        first = last = None
        for k in sorted(h.keys()):
            r = h.get(k) or {}
            fu, u = r.get("first_used"), r.get("used")
            if fu is None:
                continue
            if first is None:
                first = (k, float(fu))
            if u is not None:
                last = (k, float(u))
        if first and last and last[0] > first[0]:
            try:
                d0 = date(*[int(x) for x in first[0].split("-")])
                d1 = date(*[int(x) for x in last[0].split("-")])
                span = (d1 - d0).days
                diff = last[1] - first[1]
                if span > 0 and diff > 0:
                    return diff / span, span, "hist"
            except Exception:  # noqa: BLE001
                pass
        y = self._day_used(h, 1)
        if y and y > 0.05:
            return float(y), 1, "yday"
        return None, 0, ""

    def _forecast(self, h, info):
        """当前电量还能用多久。

        ready    是否算得出
        avg      日均用电（度/天）
        span     样本跨度天数
        source   hist = 跨天读数差 / yday = 只用昨天一天
        days     预计可用天数（一位小数）
        hours    预计可用小时（一位小数）
        until    预计用完日期 YYYY-MM-DD
        """
        left = round(float(info.get("left") or 0), 2)
        avg, span, src = self._avg_used(h)
        out = {"ready": False, "avg": None, "span": span, "source": src,
               "days": None, "hours": None, "until": "", "left": left,
               "reason": ""}
        if left <= 0:
            out["reason"] = "empty"
            return out
        if not avg or avg <= 0.05:
            out["reason"] = "no_data"
            return out
        days = left / avg
        out.update({"ready": True, "avg": round(avg, 2),
                    "days": round(days, 1), "hours": round(days * 24.0, 1)})
        try:
            out["until"] = (date.today() + timedelta(days=int(days))).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            out["until"] = ""
        return out

    def _balance(self):
        """当前房间的三组电量：
        all   = 平台返回的全量（从有数据开始到现在）
        range = 从默认开始时间到现在（缺基线时退回全量，并把原因带给前端）
        """
        room = self._room()
        info = ec.query_room(room)
        h = self._hist_touch(info)
        c = self._cum(h, info)
        if c["ready"]:
            rng = {"used": round(c["used"], 2), "buy": round(c["buy"], 2),
                   "ready": True, "approx": c["approx"],
                   "from_open": bool(c.get("from_open")),
                   "base": c["base"], "hist_from": c["hist_from"],
                   "start": c["start"], "reason": ""}
        else:
            rng = {"used": round(float(info["used"]), 2),
                   "buy": round(float(info["buy"]), 2),
                   "ready": False, "approx": False, "from_open": False,
                   "base": c["base"], "hist_from": c["hist_from"],
                   "start": c["start"], "reason": c["reason"]}
        today = date.today().strftime("%Y-%m-%d")
        rec = h.get(today) or {}
        tdelta = None
        try:
            if rec.get("first_used") is not None:
                tdelta = round(float(info["used"]) - float(rec["first_used"]), 2)
                if tdelta < 0:
                    tdelta = None
        except Exception:  # noqa: BLE001
            tdelta = None
        return {
            "all": {"used": round(float(info["used"]), 2),
                    "buy": round(float(info["buy"]), 2),
                    "left": round(float(info["left"]), 2),
                    "today": tdelta},
            "range": rng,
            "forecast": self._forecast(h, info),
            "left": round(float(info["left"]), 2),
        }

    def _money_stat(self, start=""):
        """按固定电价，用「默认开始时间」起的充值记录推算购电量 / 用电量。

        平台只给「当前累计读数」（elecUsed / elecBuy），没有任何历史明细接口，
        所以区间口径改用充值台账反推：
            区间购电 = 区间充值金额 ÷ 电价
            区间用电 = 区间购电 − 当前余额（假定区间起点余额为 0）
        结果缓存 180 秒，避免每刷一次首页就拉 200+ 条充值记录。

        阻塞方法（内部走网络），只能在线程里调用。
        """
        price = self._price()
        ck = "%s|%.4f" % (start, price)
        now = time.time()
        cache = self._money_cache or {}
        if cache.get("data") and cache.get("key") == ck and (now - cache.get("at", 0)) < 180:
            return cache["data"]
        room = self._room()
        recs = self._fetch_recharge(room)
        info = ec.query_room(room)
        sel = [r for r in recs if not start or str(r.get("time") or "") >= str(start)]
        amount = round(sum(float(r.get("amt") or 0) for r in sel), 2)
        buy = round(amount / price, 2) if price > 0 else 0.0
        left = round(float(info.get("left") or 0), 2)
        used = round(buy - left, 2)
        if used < 0:
            used = 0.0
        ratio = round(used / buy * 100, 2) if buy > 0 else 0.0
        days = 0
        try:
            d0 = date(*[int(x) for x in str(start or "")[:10].split("-")])
            days = (date.today() - d0).days
        except Exception:  # noqa: BLE001
            days = 0
        avg = round(used / days, 2) if (days > 0 and used > 0) else None
        out = {"ready": True, "reason": "", "price": price, "days": days, "avg": avg,
               "start": str(start or "")[:10], "count": len(sel),
               "amount": amount, "buy": buy, "used": used,
               "left": left, "ratio": ratio}
        self._money_cache = {"at": now, "key": ck, "data": out}
        return out

    # ------------------------------------------------------------ 推送目标
    def _targets(self):
        raw = str(self._c("notify_targets", "") or "")
        out = []
        for line in raw.replace("，", ",").replace(";", ",").replace("\n", ",").split(","):
            t = line.strip()
            if not t:
                continue
            if t.isdigit():
                t = "aiocqhttp:GroupMessage:%s" % t
            if t not in out:
                out.append(t)
        return out

    # ------------------------------------------------------------ 运行日志
    def _attach_log_ring(self):
        """挂上日志环形缓冲；重载插件时先摘掉旧 handler，避免越挂越多。"""
        try:
            for h in list(getattr(logger, "handlers", []) or []):
                if getattr(h, "_wxsc_ring", False):
                    try:
                        logger.removeHandler(h)
                    except Exception:  # noqa: BLE001
                        pass
            hd = _RingHandler(self._log_buf)
            hd._wxsc_ring = True
            logger.addHandler(hd)
        except Exception as e:  # noqa: BLE001
            logger.warning("[电费] 安装日志缓冲失败: %s", e)

    def _notice_load(self):
        try:
            with io.open(self._notice_file, "r", encoding="utf-8") as f:
                rows = json.load(f)
            if isinstance(rows, list):
                for r in rows[-200:]:
                    if isinstance(r, dict):
                        self._notice_buf.append(r)
        except Exception:  # noqa: BLE001
            pass

    def _notice_add(self, kind, ok, targets, text, fail):
        """记一条发送结果，并落盘，重启后仍能看到发过什么。"""
        try:
            rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "kind": str(kind or "通知"), "ok": int(ok or 0),
                   "fail": len(fail or []), "targets": list(targets or []),
                   "text": str(text or "")[:2000]}
            self._notice_buf.append(rec)
            tmp = self._notice_file + ".tmp"
            with io.open(tmp, "w", encoding="utf-8") as f:
                json.dump(list(self._notice_buf)[-200:], f, ensure_ascii=False)
            os.replace(tmp, self._notice_file)
        except Exception as e:  # noqa: BLE001
            logger.warning("[电费] 保存通知记录失败: %s", e)

    async def api_runlog(self):
        """控制台「运行日志」：本进程日志 + 已发送通知。"""
        try:
            logs = list(self._log_buf)[-400:]
            logs.reverse()
            cnt = {"INFO": 0, "WARN": 0, "ERRO": 0}
            for r in self._log_buf:
                lv = str(r.get("lv") or "").upper()
                if lv in ("WARNING", "WARN"):
                    cnt["WARN"] += 1
                elif lv in ("ERROR", "CRITICAL", "CRIT"):
                    cnt["ERRO"] += 1
                else:
                    cnt["INFO"] += 1
            nt = list(self._notice_buf)[-80:]
            nt.reverse()
            return self._ok({"logs": logs, "counts": cnt, "notices": nt,
                             "noticeTotal": len(self._notice_buf),
                             "version": PLUGIN_VERSION})
        except Exception as e:  # noqa: BLE001
            logger.error("[电费] runlog 失败: %s", e)
            return self._err("读取日志失败：%s" % e)

    async def _send(self, text, targets=None, kind='通知'):
        tg = targets or self._targets()
        if not tg:
            raise RuntimeError("没有配置推送目标，请在 Web 控制台或插件设置里选择会话")
        ok, fail = 0, []
        chain = _chain(text)
        for t in tg:
            try:
                await self.context.send_message(t, chain)
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail.append("%s: %s" % (t, e))
                logger.warning("[电费] 推送到 %s 失败: %s", t, e)
        self._notice_add(kind, ok, tg, text, fail)
        if ok == 0:
            raise RuntimeError("全部推送失败：" + "; ".join(fail)[:300])
        return ok, fail

    # ------------------------------------------------------------ NapCat
    # 这些方法都是同步阻塞的（urllib / 端口探测），只允许在 asyncio.to_thread 里调用。
    def _napcat_base(self):
        """按「配置 → 环境变量 → 本机常见端口」顺序确定 NapCat HTTP 地址。

        只做 HTTP 探测：不启动子进程、不执行 docker、不需要任何提权。
        """
        cfg = str(self._c("napcat_url", "") or "").strip().rstrip("/")
        if cfg:
            return cfg if cfg.startswith("http") else "http://" + cfg
        for env in ("NAPCAT_URL", "NAPCAT_HTTP"):
            val = str(os.environ.get(env) or "").strip().rstrip("/")
            if val:
                return val if val.startswith("http") else "http://" + val
        import urllib.request
        for host in ("127.0.0.1:3000", "127.0.0.1:3001", "host.docker.internal:3000"):
            try:
                req = urllib.request.Request(
                    "http://%s/get_status" % host, data=b"{}", method="POST",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=2) as r:
                    body = json.loads(r.read().decode("utf-8", "replace"))
                if isinstance(body, dict) and ("retcode" in body or "status" in body):
                    return "http://%s" % host
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError("未找到 NapCat 服务（已试 127.0.0.1:3000/3001），"
                           "请在插件设置里填写 napcat_url")

    def _napcat_call(self, action, payload, base=None):
        import urllib.request
        base = (base or self._napcat_base()).rstrip("/")
        token = str(self._c("napcat_token", "") or "")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request("%s/%s" % (base, action),
                                     data=json.dumps(payload).encode(),
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    # ------------------------------------------------------------ 数据行
    def summary_rows(self, items):
        rows = ec.summarize(items)
        out = []
        total = sum(float(r.get("amt", 0) or 0) for r in rows) or 1.0
        for r in rows:
            amt = float(r.get("amt", 0) or 0)
            out.append({
                "buyer": r["buyer"],
                "label": self._label(r["buyer"]),
                "count": r.get("n", 0),
                "amount": round(amt, 2),
                "ratio": round(amt * 100.0 / total, 1),
                "first": r["first"],
                "last": r["last"],
            })
        return out

    def detail_rows(self, items):
        out = []
        for r in sorted(items, key=lambda x: x["time"], reverse=True):
            out.append({
                "time": r["time"],
                "amount": round(r["amt"], 2),
                "buyer": r["buyer"],
                "label": self._label(r["buyer"]),
                "recNo": r["recNo"],
                "outNo": r["outNo"],
            })
        return out

    # ------------------------------------------------------------ 消息模板
    def _report_text(self, opts=None):
        opts = opts or {}
        room = self._room()
        info = ec.query_room(room)
        try:
            items = ec.fetch_recharge(room)
        except Exception:  # noqa: BLE001
            items = []
        s, e, _lab = ec.parse_range("本月")
        month = ec.in_range(items, s, e)
        h = self._hist_touch(info)
        yday = self._day_used(h, 1)
        _cref = self._cum(h, info)
        cu, cb = ((_cref["used"], _cref["buy"]) if _cref["ready"] else (None, None))
        low = float(self._c("low_threshold", 30))
        title = str(opts.get("title") or "").strip() or (
            "⚡ 电费日报 %s" % date.today().strftime("%Y-%m-%d"))
        lines = [title, "🏠 房间：%s" % ec.describe(room)]
        if self._flag(opts, "show_left"):
            lines.append("🔋 剩余电量：%.2f度" % float(info["left"]))
        if self._flag(opts, "show_delta"):
            lines.append("📈 昨天用电：%s" % ("%.2f度" % yday if yday is not None else "--"))
        if self._flag(opts, "show_forecast"):
            fc = self._forecast(h, info)
            if fc.get("ready"):
                dd = float(fc["days"])
                if dd >= 10:
                    txt = "%d 天" % int(round(dd))
                elif dd >= 1:
                    txt = "%.1f 天" % dd
                else:
                    txt = "%.0f 小时" % float(fc["hours"])
                lines.append("📅 预计使用时间：" + txt)
            else:
                lines.append("📅 预计使用时间：--")
        if self._flag(opts, "show_total"):
            if cu is None:
                cu = float(info["used"])
            if cb is None:
                cb = float(info["buy"])
            lines.append("📊 累计用电：%.2f度" % cu)
            lines.append("📊 累计购电：%.2f度" % cb)
        if self._flag(opts, "show_recharge") and month:
            lines.append("💰 本月已充%.2f元（%d 笔）"
                         % (sum(r["amt"] for r in month), len(month)))
        if self._flag(opts, "show_warn") and float(info["left"]) < low:
            lines.append("⚠️ 电量偏低，建议尽快充值")
        meta = {"left": round(float(info["left"]), 2),
                "used": round(float(info["used"]), 2),
                "buy": round(float(info["buy"]), 2),
                "yday": None if yday is None else round(yday, 2),
                "cumUsed": None if cu is None else round(cu, 2),
                "cumBuy": None if cb is None else round(cb, 2),
                "monthAmt": round(sum(r["amt"] for r in month), 2),
                "monthCnt": len(month)}
        return "\n".join(lines), meta

    def build_report(self, info=None, prev_used=None, prev_date=None,
                     month_amt=0.0, month_cnt=0):
        return self._report_text({})[0]

    def build_recharge_msg(self, rec, info=None, mask=None):
        """充值提醒正文（固定格式，与用户约定的一致）。"""
        who = self._who(rec["buyer"], mask)
        order = str(rec.get("outNo") or rec.get("recNo") or "").strip() or "-"
        day, hms = _split_dt(rec.get("time"))
        left = float(info["left"]) if info else None
        lines = ["💰 充值提醒    %s" % day,
                 "💵 充值金额：%.2f 元" % float(rec["amt"]),
                 "🕐 充值时间：%s" % hms,
                 "👤 充值账号：%s" % who,
                 "🧾 充值订单号：%s" % order,
                 "🏠 充值房间：%s" % self._where()]
        if left is not None:
            lines.append("🔋 电量剩余：%.2f 度" % left)
        return "\n".join(lines)

    @staticmethod
    def _merge_msgs(msgs):
        """把多条现成文本简单拼接（保留给旧调用点）。"""
        return "\n\n".join(m for m in msgs if m)

    @staticmethod
    def _dot_date(day):
        """2025-09-13 -> 2025.09.13"""
        return str(day or "").replace("-", ".")[:10]

    def _display_name(self, buyer, mask=None):
        """优先用备注名，没有备注就用账号本身；mask 里的人强制用账号。"""
        b = str(buyer).strip()
        if mask and b in mask:
            return b
        label = str(self._label(b) or "").strip()
        if label and not label.startswith("#") and label != b:
            return label
        return b

    def _who(self, buyer, mask=None):
        """备注 + 账号（没有备注就只用账号）；mask 里的人只显示账号。"""
        acct = str(buyer).strip()
        if mask and acct in mask:
            return acct
        label = str(self._label(buyer) or "").strip()
        if label and not label.startswith("#") and label != acct:
            return "%s %s" % (label, acct)
        return acct

    def _merge_title(self, recs, mask=None):
        buyers = {str(r["buyer"]) for r in recs}
        if len(buyers) == 1:
            return "%s的充值记录" % self._display_name(list(buyers)[0], mask)
        return "充值记录 · %d 人 %d 笔" % (len(buyers), len(recs))

    def build_merge_text(self, recs, start="", end="", mask=None):
        """多笔充值合并成一条消息：头部只写一次，每条只留金额/时间/订单号。"""
        rows = sorted(recs, key=lambda x: str(x.get("time") or ""), reverse=True)
        if not rows:
            return ""
        days = [self._dot_date(r.get("time")) for r in rows if r.get("time")]
        a = self._dot_date(start) if start else (min(days) if days else "")
        b = self._dot_date(end) if end else "至今"
        blocks = []
        for r in rows:
            _, hms = _split_dt(r.get("time"))
            order = str(r.get("outNo") or r.get("recNo") or "").strip() or "-"
            blocks.append("💵 充值金额：%.2f 元\n👤 充值账号：%s\n🕐 充值时间：%s %s\n🧾 充值订单号：%s"
                          % (float(r["amt"]), self._who(r["buyer"], mask),
                             self._dot_date(r.get("time")), hms, order))
        return "💰 %s\n🕐%s~%s\n\n%s" % (
            self._merge_title(rows, mask), a, b, "\n\n".join(blocks))

    def build_latest_text(self, recs, day="", mask=None):
        """最近一次充值：同一天全部人，按人分组，同一人多笔连续排列。"""
        rows = sorted(recs, key=lambda x: str(x.get("time") or ""), reverse=True)
        if not rows:
            return ""
        day = self._dot_date(day) if day else self._dot_date(rows[0].get("time"))
        groups = {}
        for r in rows:
            groups.setdefault(str(r["buyer"]), []).append(r)
        parts = ["💰 最近充值记录\n🕐%s" % day]
        for buyer, group in groups.items():
            lines = ["👤 充值账号：%s" % self._who(buyer, mask)]
            for r in group:
                _, hms = _split_dt(r.get("time"))
                order = str(r.get("outNo") or r.get("recNo") or "").strip() or "-"
                lines.append("💵 充值金额：%.2f 元\n🕐 充值时间：%s %s\n🧾 充值订单号：%s"
                             % (float(r["amt"]), self._dot_date(r.get("time")), hms, order))
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    # ------------------------------------------------------------ 导出
    def _stamp(self):
        return datetime.now().strftime("%Y%m%d-%H%M%S")

    def _columns(self, table):
        if table == "detail":
            return ["序号", "充值时间", "金额(元)", "备注", "账号", "记录号", "订单号"]
        if table == "person":
            return ["序号", "充值时间", "金额(元)", "记录号", "订单号"]
        return ["备注", "账号", "笔数", "金额(元)", "占比(%)", "首次充值", "最近充值"]

    def _rows(self, items, table="summary", buyer=None):
        if table == "detail":
            return [[i, r["time"], round(r["amt"], 2), self._label(r["buyer"]),
                     r["buyer"], r.get("recNo", ""), str(r.get("outNo", "")).strip()]
                    for i, r in enumerate(sorted(items, key=lambda x: x["time"],
                                                 reverse=True), 1)]
        if table == "person":
            sel = ([r for r in items if str(r["buyer"]) == str(buyer)]
                   if buyer else items)
            return [[i, r["time"], round(r["amt"], 2), r.get("recNo", ""),
                     str(r.get("outNo", "")).strip()]
                    for i, r in enumerate(sorted(sel, key=lambda x: x["time"],
                                                 reverse=True), 1)]
        return [[r["label"], r["buyer"], r["count"], r["amount"], r["ratio"],
                 r["first"], r["last"]] for r in self.summary_rows(items)]

    def _title(self, table, buyer=None):
        if table == "detail":
            return "充值明细表"
        if table == "person":
            return "个人充值记录 · %s" % (self._label(buyer) if buyer else "全部账号")
        return "每人总金额表"

    def _suffix(self, table, buyer=None):
        if table == "detail":
            return "充值明细表"
        if table == "person":
            return "个人充值记录-%s" % (buyer or "all")
        return "每人总金额表"

    def export_xlsx(self, room, items, label="全部", table="summary", buyer=None):
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter

        cols = self._columns(table)
        rows = self._rows(items, table, buyer)
        wb = Workbook()
        ws = wb.active
        ws.title = (self._suffix(table, buyer))[:31]
        head_fill = PatternFill("solid", fgColor="1F6FEB")
        head_font = Font(color="FFFFFF", bold=True, size=11)
        thin = Side(style="thin", color="D0D7DE")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        ws.append([self._title(table, buyer)])
        ws.append(["房间：%s ｜ 范围：%s ｜ 共 %d 笔 ｜ 合计 %.2f 元 ｜ 导出 %s"
                   % (ec.describe(room), label, len(items),
                      round(sum(r["amt"] for r in items), 2),
                      datetime.now().strftime("%Y-%m-%d %H:%M:%S"))])
        ws.append(cols)
        for r in rows:
            ws.append(list(r))
        for c in ws[3]:
            c.fill = head_fill
            c.font = head_font
            c.alignment = Alignment(horizontal="center", vertical="center")
        for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=len(cols)):
            for c in row:
                c.border = border
        ws["A1"].font = Font(bold=True, size=13)
        widths = {"summary": [16, 14, 8, 12, 10, 20, 20],
                  "detail": [7, 20, 12, 16, 14, 12, 20],
                  "person": [7, 20, 12, 12, 20]}[table]
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A4"
        path = os.path.join(self.export_dir, "%s-%s-%s.xlsx"
                            % (self._suffix(table, buyer),
                               ec.describe(room).replace(" ", ""), self._stamp()))
        wb.save(path)
        return path

    def export_md(self, room, items, label="全部", table="summary", buyer=None):
        cols = self._columns(table)
        rows = self._rows(items, table, buyer)
        lines = ["# %s · %s" % (self._title(table, buyer), ec.describe(room)), "",
                 "- 范围：%s" % label,
                 "- 笔数：%d 笔" % len(items),
                 "- 合计：%.2f 元" % round(sum(r["amt"] for r in items), 2),
                 "- 导出时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "",
                 "| " + " | ".join(cols) + " |",
                 "| " + " | ".join(["---"] * len(cols)) + " |"]
        for r in rows:
            lines.append("| " + " | ".join(str(x) for x in r) + " |")
        path = os.path.join(self.export_dir, "%s-%s-%s.md"
                            % (self._suffix(table, buyer),
                               ec.describe(room).replace(" ", ""), self._stamp()))
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path

    # ------------------------------------------------------------ 调度
    async def initialize(self):
        self._task = asyncio.create_task(self._loop())
        self._warm_task = asyncio.create_task(self._warmup())
        logger.info("[电费] 后台任务已启动，通知时间 %s，目标 %s",
                    _times(self._c("notify_times", "08:00,20:00")),
                    self._targets() or "（未配置）")

    async def terminate(self):
        for attr in ("_task", "_warm_task"):
            task = getattr(self, attr, None)
            if task:
                task.cancel()
                setattr(self, attr, None)

    async def _warmup(self):
        """启动时在线程里预解析房间，让首次查询直接命中缓存。

        以前房间解析要等用户第一次查询或第一次定时推送时才发生，那一刻会把
        整个事件循环卡住十几秒；现在提前在工作线程里做掉。
        """
        await asyncio.sleep(3)
        try:
            await asyncio.to_thread(self._room)
            logger.info("[电费] 房间已就绪：%s", self._where())
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("[电费] 启动预取房间失败（不影响后续查询）: %s", e)

    async def _loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.error("[电费] 后台任务异常: %s", e)
            await asyncio.sleep(20)

    async def _tick(self):
        self._cfg_refresh()
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        hm = now.strftime("%H:%M")

        if _as_bool(self._c("notify_enable", True)) and self._targets():
            slots = _times(self._c("notify_times", "08:00,20:00"))
            done = self._sent.setdefault(today, [])
            if hm in slots and hm not in done:
                try:
                    await self._send_report()
                    done.append(hm)
                except Exception as e:  # noqa: BLE001
                    logger.error("[电费] %s 定时推送失败: %s", hm, e)

        if _as_bool(self._c("recharge_watch", True)) and self._targets():
            gap = int(self._c("recharge_interval", 300))
            if time.time() - self._last_watch >= gap:
                self._last_watch = time.time()
                try:
                    await self._watch_recharge()
                except Exception as e:  # noqa: BLE001
                    logger.error("[电费] 充值监控异常: %s", e)

    def _load_state(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self, st):
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)

    async def _send_report(self, targets=None):
        msg, meta = await asyncio.to_thread(self._report_text, {})
        sent_to = list(targets) if targets else self._targets()
        await self._send(msg, targets=sent_to, kind="日报")
        logger.info("[电费] 日报已推送 %d 个目标", len(sent_to))
        return msg

    async def _watch_recharge(self, only_buyers=None):
        room = await asyncio.to_thread(self._room)
        items = await asyncio.to_thread(self._fetch_recharge, room)
        recnos = [r["recNo"] for r in items]
        st = self._load_state()
        seen = st.get("seen")
        if seen is None:
            st["seen"] = recnos[-500:]
            st["ts"] = _now_ts()
            self._save_state(st)
            logger.info("[电费] 充值监控基线建立 %d 条", len(recnos))
            return []
        known = set(seen)
        fresh = [r for r in items if r["recNo"] not in known]
        if not fresh:
            return []
        fresh.sort(key=lambda r: r["time"])
        info = await asyncio.to_thread(ec.query_room, room)
        sent = []
        for r in fresh:
            if only_buyers and str(r["buyer"]) not in only_buyers:
                continue
            await self._send(self.build_recharge_msg(r, info), kind="充值提醒")
            sent.append(r)
        st["seen"] = recnos[-500:]
        st["ts"] = _now_ts()
        self._save_state(st)
        logger.info("[电费] 充值提醒推送 %d 笔", len(sent))
        return sent

    # ------------------------------------------------------------ 通用
    @staticmethod
    def _dt(s):
        s = str(s or "").strip()
        if not s:
            return None
        for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, f)
            except ValueError:
                continue
        return None

    def _filter(self, items, start=None, end=None):
        s, e = self._dt(start), self._dt(end)
        out = []
        for r in items:
            try:
                t = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S")
            except Exception:  # noqa: BLE001
                continue
            if s and t < s:
                continue
            if e and t > e:
                continue
            out.append(r)
        return out

    @staticmethod
    def _label_text(start, end):
        if start and end:
            return "%s ~ %s" % (start, end)
        if start:
            return "%s 起" % start
        if end:
            return "截至 %s" % end
        return "全部"

    def _flag(self, opts, key, default=True):
        if isinstance(opts, dict) and key in opts:
            return _as_bool(opts.get(key))
        return _as_bool(self._c(key, default))

    def _args(self, event):
        try:
            parts = (event.message_str or "").split()
        except Exception:  # noqa: BLE001
            parts = []
        return parts[1:] if parts else []

    # ------------------------------------------------------------ 指令
    def _elec_text(self):
        """同步组装「电费」查询结果（内部会走网络），整块丢进工作线程执行。"""
        bal = self._balance()
        low = float(self._c("low_threshold", 30))
        rng = bal["range"]
        lines = ["⚡ 电费查询", "────────────────", "🏠 %s" % self._where(),
                 "🔋 剩余电量：%.2f 度" % bal["left"]]
        if rng["ready"]:
            lines.append("📊 自 %s 起：已用 %.2f / 已购 %.2f 度"
                         % (rng["start"][:10], rng["used"], rng["buy"]))
        else:
            lines.append("📊 累计：已用 %.2f / 已购 %.2f 度"
                         % (bal["all"]["used"], bal["all"]["buy"]))
        if bal["left"] < low:
            lines.append("⚠️ 电量偏低，建议尽快充值")
        return "\n".join(lines)

    @filter.command("电费")
    async def cmd_elec(self, event: AstrMessageEvent):
        """查询电费余额与用电量"""
        try:
            text = await asyncio.to_thread(self._elec_text)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result("查询失败：%s" % e)
            return
        yield event.plain_result(text)

    # ------------------------------------------------------------ Web API
    def _ok(self, payload=None):
        return json_response({"status": "ok", "message": "", "data": payload})

    def _err(self, msg, code=400):
        return error_response(str(msg), status_code=code)

    def _overview_sync(self):
        """同步收集首屏数据（room / balance 内部会走网络），整块丢进工作线程。

        这样整个首屏只占用一个工作线程、一次排队，不会出现
        「先把 self._room() 在外层同步求值」那种把事件循环卡住的写法。
        """
        data = {"settings": self._settings_dict(),
                "notes": self._notes(),
                "targets": self._targets(),
                "version": PLUGIN_VERSION,
                "defaultRange": self._default_range()}
        try:
            data["room"] = self._room_json()
        except Exception as e:  # noqa: BLE001
            data["room"] = None
            data["roomError"] = str(e)
        try:
            data["balance"] = self._balance()
        except Exception as e:  # noqa: BLE001
            data["balance"] = None
            data["balanceError"] = str(e)
        return data

    async def api_overview(self):
        """控制台首屏：设置 + 房间 + 电量（全量/按默认开始时间）+ 默认范围。"""
        try:
            self._cfg_refresh()
            return self._ok(await asyncio.to_thread(self._overview_sync))
        except Exception as e:  # noqa: BLE001
            logger.error("[电费] overview 失败: %s", e)
            return self._err("读取概览失败：%s" % e)

    async def api_money(self):
        """按固定电价 + 充值台账推算区间购电 / 用电（平台无历史明细，只能这样算）。"""
        try:
            self._cfg_refresh()
            start = ""
            d0 = self._default_start()
            if d0:
                start = d0.strftime("%Y-%m-%d %H:%M:%S")
            return self._ok(await asyncio.to_thread(self._money_stat, start))
        except Exception as e:  # noqa: BLE001
            logger.error("[电费] money 失败: %s", e)
            return self._ok({"ready": False, "reason": "error", "message": str(e)})

    async def api_settings(self):
        try:
            body = await request.json(default={}) or {}
            patch = body.get("settings") or body
            allowed = set(CONF_KEYS)
            changed = {}
            for k, v in patch.items():
                if k in allowed:
                    self.config[k] = v
                    changed[k] = v
            if changed:
                try:
                    self.config.save_config()
                except Exception as e:  # noqa: BLE001
                    return self._err("保存失败：%s" % e)
                try:
                    self._cfg_mtime = os.path.getmtime(self._cfg_path)
                except Exception:  # noqa: BLE001
                    self._cfg_mtime = None
                if any(k in changed for k in ROOM_KEYS):
                    self._room_cache = None
            return self._ok({"changed": changed, "settings": self._settings_dict()})
        except Exception as e:  # noqa: BLE001
            return self._err("保存设置失败：%s" % e)

    async def api_rooms(self):
        """楼栋 + 房间候选，供控制台下拉选择。"""
        try:
            self._cfg_refresh()
            force = str(request.query.get("force") or "") in ("1", "true", "yes")
            tree = await asyncio.to_thread(self._room_tree, force)
            return self._ok({"loudongs": tree,
                             "current": {"loudong_no": self._c("loudong_no", ""),
                                         "room_no": self._c("room_no", "")},
                             "count": sum(len(g["rooms"]) for g in tree)})
        except Exception as e:  # noqa: BLE001
            return self._err("拉取楼栋/房间列表失败：%s" % e)

    async def api_data(self):
        try:
            start = request.query.get("start") or ""
            end = request.query.get("end") or ""
            room = await asyncio.to_thread(self._room)
            items = await asyncio.to_thread(self._fetch_recharge, room)
            sel = self._filter(items, start, end)
            detail = [{"time": r["time"], "amt": round(r["amt"], 2),
                       "buyer": r["buyer"], "label": self._label(r["buyer"]),
                       "recNo": r["recNo"], "outNo": r["outNo"]}
                      for r in sorted(sel, key=lambda x: x["time"], reverse=True)]
            return self._ok({"room": ec.describe(room),
                             "label": self._label_text(start, end),
                             "count": len(sel),
                             "total": round(sum(r["amt"] for r in sel), 2),
                             "summary": self.summary_rows(sel),
                             "items": detail,
                             "notes": self._notes()})
        except Exception as e:  # noqa: BLE001
            return self._err("查询失败：%s" % e)

    async def api_notes(self):
        try:
            body = await request.json(default={}) or {}
            if "buyer" in body:
                self.set_note(body.get("buyer"), body.get("name", ""))
            for k, v in (body.get("notes") or {}).items():
                self.set_note(k, v)
            return self._ok({"notes": self._notes()})
        except Exception as e:  # noqa: BLE001
            return self._err("保存备注失败：%s" % e)

    async def api_targets(self):
        """群聊 + 私聊会话，全部动态读取，不写死任何会话。"""
        groups, friends, seen = [], [], set()
        try:
            insts = list(self.context.platform_manager.platform_insts)
        except Exception:  # noqa: BLE001
            insts = []
        for p in insts:
            try:
                pid = p.meta().id
            except Exception:  # noqa: BLE001
                continue
            bot = getattr(p, "bot", None)
            if bot is None:
                continue
            try:
                res = await bot.call_action("get_group_list")
            except Exception:  # noqa: BLE001
                res = None
            for g in (res or []):
                gid = g.get("group_id")
                if gid is None:
                    continue
                umo = "%s:GroupMessage:%s" % (pid, gid)
                if umo in seen:
                    continue
                seen.add(umo)
                groups.append({"umo": umo, "name": g.get("group_name") or str(gid),
                               "count": g.get("member_count"), "type": "group"})
            try:
                res2 = await bot.call_action("get_friend_list")
            except Exception:  # noqa: BLE001
                res2 = None
            for f in (res2 or []):
                uid = f.get("user_id")
                if uid is None:
                    continue
                umo = "%s:FriendMessage:%s" % (pid, uid)
                if umo in seen:
                    continue
                seen.add(umo)
                friends.append({"umo": umo,
                                "name": f.get("remark") or f.get("nickname") or str(uid),
                                "count": None, "type": "friend"})
        if not groups or not friends:
            try:
                body = await asyncio.to_thread(self._napcat_call, "get_group_list", {})
                for g in (body.get("data") or []):
                    gid = g.get("group_id")
                    if gid is None:
                        continue
                    umo = "aiocqhttp:GroupMessage:%s" % gid
                    if umo in seen:
                        continue
                    seen.add(umo)
                    groups.append({"umo": umo, "name": g.get("group_name") or str(gid),
                                   "count": g.get("member_count"), "type": "group"})
            except Exception:  # noqa: BLE001
                pass
            try:
                body = await asyncio.to_thread(self._napcat_call, "get_friend_list", {})
                for f in (body.get("data") or []):
                    uid = f.get("user_id")
                    if uid is None:
                        continue
                    umo = "aiocqhttp:FriendMessage:%s" % uid
                    if umo in seen:
                        continue
                    seen.add(umo)
                    friends.append({"umo": umo,
                                    "name": f.get("remark") or f.get("nickname") or str(uid),
                                    "count": None, "type": "friend"})
            except Exception:  # noqa: BLE001
                pass
        return self._ok({"configured": self._targets(),
                         "groups": groups, "friends": friends})

    async def api_preview(self):
        """预览：只读，不产生任何副作用。"""
        try:
            body = await request.json(default={}) or {}
            kind = str(body.get("kind") or "report").lower()
            start = str(body.get("start") or "")
            end = str(body.get("end") or "")
            if kind == "report":
                msg, meta = await asyncio.to_thread(self._report_text,
                                                    body.get("opts") or {})
                return self._ok({"kind": "report", "text": msg, "meta": meta,
                                 "targets": self._targets()})
            ckey = "%s|%s" % (start, end)
            cache = self._preview_cache or {}
            if (cache.get("items") is not None and cache.get("key") == ckey
                    and time.time() - float(cache.get("at") or 0) < 120):
                room = cache.get("room")
                items = cache.get("items")
                info = cache.get("info")
            else:
                room = await asyncio.to_thread(self._room)
                items = await asyncio.to_thread(self._fetch_recharge, room)
                try:
                    info = await asyncio.to_thread(ec.query_room, room)
                except Exception as e:  # noqa: BLE001
                    # 拿不到实时电量也要能出预览，只是少一行「电量剩余」
                    logger.warning("[电费] 预览时查询电量失败（继续）: %s", e)
                    info = None
                self._preview_cache = {"at": time.time(), "key": ckey,
                                       "room": room, "items": items, "info": info}
            sel_all = self._filter(items, start, end)
            if kind == "recharge":
                mask = {str(x) for x in (body.get("mask") or [])}
                sel = self._pick(sel_all, body)
                # 最新的在前
                sel = sorted(sel, key=lambda x: str(x.get("time") or ""), reverse=True)
                limit = max(1, min(int(body.get("limit") or 10), 50))
                msgs = [self._msg_dict(r, info, mask) for r in sel[:limit]]
                merged = self.build_merge_text(sel, start, end, mask)
                # 「最近充值 / 上次充值」按时间范围内的全量算，不受上面按账号勾选的影响
                day_batches = self._day_batches(sel_all, info, start, end, mask=mask)
                latest = self._latest_batch(sel_all)
                first = day_batches[0] if day_batches else {}
                latest_text = first.get("text", "")
                latest_merged = first.get("merged", "")
                latest_msgs = first.get("msgs", [])
                return self._ok({"kind": "recharge", "count": len(sel),
                                 "total": round(sum(r["amt"] for r in sel), 2),
                                 "messages": msgs, "range": self._label_text(start, end),
                                 "mask": sorted(mask), "people": self._people(sel_all),
                                 "merged": merged,
                                 "latest": latest,
                                 "latestText": latest_text,
                                 "latestMerged": latest_merged,
                                 "latestMsgs": latest_msgs,
                                 "dayBatches": day_batches,
                                 "previewLimit": limit,
                                 "truncated": max(0, len(sel) - limit),
                                 "targets": self._targets()})
            fmt = str(body.get("fmt") or "excel").lower()
            table = str(body.get("table") or "summary").lower()
            buyer = body.get("buyer")
            if table == "person" and buyer:
                sel_all = [r for r in sel_all if str(r["buyer"]) == str(buyer)]
            rows = self._rows(sel_all, table, buyer)
            limit = max(1, min(int(body.get("limit") or 10), 200))
            return self._ok({"kind": "export", "fmt": fmt, "table": table, "buyer": buyer,
                             "title": self._title(table, buyer),
                             "columns": self._columns(table),
                             "rows": rows[:limit], "count": len(sel_all),
                             "total": round(sum(r["amt"] for r in sel_all), 2),
                             "range": self._label_text(start, end),
                             "room": ec.describe(room),
                             "previewLimit": limit,
                             "truncated": max(0, len(rows) - limit),
                             "targets": self._targets()})
        except Exception as e:  # noqa: BLE001
            logger.error("[电费] 预览失败: %s", e)
            return self._err("预览失败：%s" % e)

    def _people(self, sel):
        """本批出现过的充值人，含备注名、笔数与金额，供筛选行 / 备注行渲染。"""
        stat = {}
        for r in sel:
            b = str(r["buyer"])
            it = stat.get(b)
            if it is None:
                it = {"buyer": b, "count": 0, "total": 0.0}
                stat[b] = it
            it["count"] += 1
            try:
                it["total"] += float(r.get("amt") or 0)
            except (TypeError, ValueError):
                pass
        out = []
        for b, it in stat.items():
            out.append({"buyer": b, "count": it["count"],
                        "total": round(it["total"], 2),
                        "label": self._display_name(b),
                        "who": self._who(b)})
        out.sort(key=lambda x: str(x["buyer"]))
        return out

    def _msg_dict(self, r, info, mask=None):
        """一条充值的预览结构（正文 + 展示字段）。"""
        return {"buyer": str(r["buyer"]), "label": self._label(r["buyer"]),
                "amt": round(r["amt"], 2), "time": r["time"],
                "day": self._dot_date(r.get("time")),
                "recNo": r["recNo"], "outNo": r.get("outNo", ""),
                "text": self.build_recharge_msg(r, info, mask)}

    def _pick(self, sel, body):
        """按 recNo 列表或 buyer 列表筛选要推送的记录。"""
        rec_nos = [str(x) for x in (body.get("recNos") or [])]
        buyers = [str(x) for x in (body.get("buyers") or [])]
        if rec_nos:
            keep = set(rec_nos)
            return [r for r in sel if str(r.get("recNo")) in keep]
        if buyers:
            keep = set(buyers)
            return [r for r in sel if str(r["buyer"]) in keep]
        return list(sel)

    @staticmethod
    def _batch_of_day(sel, day):
        """指定充值日期【那一天】的全部人的记录。"""
        empty = {"recNos": [], "count": 0, "total": 0.0, "time": "",
                 "day": "", "buyers": [], "persons": 0}
        if not sel or not day:
            return dict(empty)
        batch = [r for r in sel if str(r.get("time") or "")[:10] == day]
        if not batch:
            return dict(empty)
        buyers = sorted({str(r["buyer"]) for r in batch})
        return {"recNos": [r["recNo"] for r in batch],
                "count": len(batch),
                "persons": len(buyers),
                "total": round(sum(r["amt"] for r in batch), 2),
                "time": day,
                "day": day,
                "buyers": buyers}

    @staticmethod
    def _latest_batch(sel):
        """最近一次充值 = 最新充值日期【那一天】的全部人的记录。"""
        days = sorted({str(r["time"])[:10] for r in sel if r.get("time")})
        if not days:
            return ElecBillPlugin._batch_of_day([], "")
        return ElecBillPlugin._batch_of_day(sel, days[-1])

    def _day_batches(self, sel, info, start="", end="", limit=12, mask=None):
        """按充值日期聚成批次，倒序返回（最新在前），供「最近充值 / 上次充值」切换。"""
        out = []
        days = sorted({str(r["time"])[:10] for r in sel if r.get("time")}, reverse=True)
        for day in days[:max(1, limit)]:
            batch = self._batch_of_day(sel, day)
            rows = [r for r in sel if str(r.get("time") or "")[:10] == day]
            rows = sorted(rows, key=lambda x: str(x.get("time") or ""))
            out.append({"day": day,
                        "count": batch["count"],
                        "persons": batch["persons"],
                        "total": batch["total"],
                        "buyers": batch["buyers"],
                        "recNos": batch["recNos"],
                        "msgs": [self._msg_dict(r, info, mask) for r in rows[:200]],
                        "text": self.build_latest_text(rows, day, mask),
                        "merged": self.build_merge_text(rows, start, end, mask)})
        return out

    async def api_notify(self):
        """推送：客户端只传展示选项与勾选的记录，金额等真实数据一律服务端重算。"""
        try:
            body = await request.json(default={}) or {}
            kind = str(body.get("kind") or "report").lower()
            targets = [t for t in (body.get("targets") or []) if t] or self._targets()
            if not targets:
                return self._err("未选择推送目标")
            if kind == "report":
                msg, _meta = await asyncio.to_thread(self._report_text,
                                                     body.get("opts") or {})
                ok, fail = await self._send(msg, targets=targets, kind="日报")
                return self._ok({"kind": "report", "sent": ok, "count": ok,
                                 "failed": fail, "targets": targets, "text": msg})
            start = str(body.get("start") or "")
            end = str(body.get("end") or "")
            room = await asyncio.to_thread(self._room)
            items = await asyncio.to_thread(self._fetch_recharge, room)
            sel_all = sorted(self._filter(items, start, end), key=lambda x: x["time"])
            mask = {str(x) for x in (body.get("mask") or [])}
            sel = self._pick(sel_all, body)
            if not sel:
                return self._err("该范围内没有可推送的充值记录")
            try:
                info = await asyncio.to_thread(ec.query_room, room)
            except Exception as e:  # noqa: BLE001
                logger.warning("[电费] 推送时查询电量失败（继续）: %s", e)
                info = None
            mode = str(body.get("mode") or "").lower()
            if mode == "latest":
                # day 为空 = 最近一次；指定 day = 「上次充值」等更早的某一批
                day = str(body.get("day") or "").strip()
                latest = self._batch_of_day(sel_all, day) if day else self._latest_batch(sel_all)
                if not latest["count"]:
                    return self._err("该日期没有可推送的充值记录")
                keys = {str(x) for x in latest["recNos"]}
                batch = [r for r in sel_all if str(r.get("recNo")) in keys]
                if _as_bool(body.get("merge"), False):
                    # 勾了「合并一条消息」：发合并模板（每笔只留金额/时间/订单号）
                    text = self.build_merge_text(batch, start, end, mask)
                    merged = True
                else:
                    # 默认：按人分组的「最近充值记录」模板
                    text = self.build_latest_text(batch, latest.get("day") or "", mask)
                    merged = False
                ok, fail = await self._send(text, targets=targets, kind="充值记录")
                return self._ok({"kind": "recharge", "mode": "latest", "merged": merged,
                                 "mask": sorted(mask),
                                 "count": len(batch), "sent": ok, "failed": fail,
                                 "day": latest.get("day") or "",
                                 "targets": targets, "text": text})
            merge = _as_bool(body.get("merge"), False)
            if merge:
                text = self.build_merge_text(sel, start, end, mask)
                ok, fail = await self._send(text, targets=targets, kind="充值记录")
                return self._ok({"kind": "recharge", "merged": True, "count": len(sel),
                                 "sent": ok, "failed": fail, "targets": targets,
                                 "text": text})
            ok, fail = 0, []
            for r in sel[:50]:
                try:
                    await self._send(self.build_recharge_msg(r, info, mask),
                                     targets=targets, kind="充值提醒")
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    fail.append(str(e)[:120])
            return self._ok({"kind": "recharge", "merged": False, "sent": ok,
                             "count": ok, "skipped": max(0, len(sel) - 50),
                             "failed": fail, "targets": targets})
        except Exception as e:  # noqa: BLE001
            logger.error("[电费] 推送失败: %s", e)
            return self._err("推送失败：%s" % e)

    async def api_export(self):
        try:
            body = await request.json(default={}) or {}
            fmt = str(body.get("fmt") or "excel").lower()
            table = str(body.get("table") or "summary").lower()
            buyer = body.get("buyer")
            start = str(body.get("start") or "")
            end = str(body.get("end") or "")
            room = await asyncio.to_thread(self._room)
            items = await asyncio.to_thread(self._fetch_recharge, room)
            sel = self._filter(items, start, end)
            if table == "person" and buyer:
                sel = [r for r in sel if str(r["buyer"]) == str(buyer)]
            if not sel:
                return self._err("该范围内没有数据")
            label = self._label_text(start, end)
            fn = self.export_md if fmt == "md" else self.export_xlsx
            path = await asyncio.to_thread(fn, room, sel, label, table, buyer)
            return self._ok({"name": os.path.basename(path), "path": path,
                             "table": table, "count": len(sel)})
        except Exception as e:  # noqa: BLE001
            logger.error("[电费] 导出失败: %s", e)
            return self._err("导出失败：%s" % e)

    async def api_download(self):
        try:
            name = os.path.basename(request.query.get("name") or "")
            if not name:
                return self._err("缺少文件名")
            path = os.path.join(self.export_dir, name)
            if not os.path.isfile(path):
                return self._err("文件不存在", 404)
            return file_response(path, filename=name)
        except Exception as e:  # noqa: BLE001
            return self._err("下载失败：%s" % e)
