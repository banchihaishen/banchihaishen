from astrbot.api.all import *
from astrbot.api.star import StarTools
from astrbot.api.event import filter
from datetime import datetime, timedelta
from playwright.async_api import async_playwright
import random, os, json, aiohttp, asyncio, time, base64, hashlib

# ══════════════════════════════════════════════════════
#  目录
# ══════════════════════════════════════════════════════
PLUGIN_DIR           = StarTools.get_data_dir("astrbot_plugin_animewifex")
CONFIG_DIR           = os.path.join(PLUGIN_DIR, "config")
IMG_DIR              = os.path.join(PLUGIN_DIR, "img", "wife")
CARD_DIR             = os.path.join(PLUGIN_DIR, "cards")
for d in (CONFIG_DIR, IMG_DIR, CARD_DIR):
    os.makedirs(d, exist_ok=True)

RECORDS_FILE         = os.path.join(CONFIG_DIR, "records.json")
WIFE_LIST_CACHE_FILE = os.path.join(CONFIG_DIR, "wife_list_cache.txt")

# ══════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════
def get_today() -> str:
    return (datetime.utcnow() + timedelta(hours=8)).date().isoformat()

def load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def save_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_group_config(gid: str) -> dict:
    return load_json(os.path.join(CONFIG_DIR, f"{gid}.json"))

def save_group_config(gid: str, cfg: dict) -> None:
    save_json(os.path.join(CONFIG_DIR, f"{gid}.json"), cfg)

def parse_wife_name(img: str) -> tuple[str, str]:
    """(角色名, 作品名)；作品名可为空"""
    stem = os.path.splitext(img)[0].split("/")[-1]
    if "!" in stem:
        src, chara = stem.split("!", 1)
        return chara, src
    return stem, ""

# ══════════════════════════════════════════════════════
#  全局状态
# ══════════════════════════════════════════════════════
_records: dict = load_json(RECORDS_FILE)
_cfg_locks: dict[str, asyncio.Lock] = {}
_pw_lock = asyncio.Lock()
_pw_ctx: dict = {}          # {"pw": ..., "browser": ...}

def _get_lock(gid: str) -> asyncio.Lock:
    if gid not in _cfg_locks:
        _cfg_locks[gid] = asyncio.Lock()
    return _cfg_locks[gid]

def _save_records():
    save_json(RECORDS_FILE, _records)

# ══════════════════════════════════════════════════════
#  卡片 HTML
# ══════════════════════════════════════════════════════
_CARD_CSS = """
* { margin:0; padding:0; box-sizing:border-box }
body {
    background: #fff;
    width: 460px;
    font-family: "Hiragino Sans GB","Source Han Sans CN","Noto Sans CJK SC",
                 "WenQuanYi Micro Hei", sans-serif;
}
.card { width:460px; background:#fff; padding:20px 20px 16px }

/* ── 顶部：头像 + 昵称 + 标签 ── */
.top { display:flex; align-items:center; gap:10px; margin-bottom:14px }
.avatar {
    width:40px; height:40px; border-radius:50%; overflow:hidden;
    flex-shrink:0; border:2px solid #eee; background:#f0f0f0
}
.avatar img { width:100%; height:100%; object-fit:cover; display:block }
.avatar-placeholder { display:flex; align-items:center; justify-content:center;
                      font-size:18px; color:#ccc }
.meta { flex:1 }
.nick { font-size:13px; font-weight:700; color:#222 }
.sub  { font-size:11px; color:#aaa; margin-top:1px }
.badge { font-size:10px; font-weight:700; color:#bbb; letter-spacing:.08em }

/* ── 老婆图片：全宽自适应高度 ── */
.img-wrap {
    width:100%; border-radius:8px; overflow:hidden; background:#f5f5f5;
    line-height:0
}
.img-wrap img { width:100%; height:auto; display:block }

/* ── 角色信息 ── */
.info { margin-top:12px }
.chara  { font-size:22px; font-weight:900; color:#111; line-height:1.2 }
.source {
    display:inline-block; font-size:11px; color:#888;
    border:1px solid #e0e0e0; border-radius:3px;
    padding:1px 7px; margin-top:4px
}

/* ── 底部 ── */
.footer {
    margin-top:12px; padding-top:10px; border-top:1px solid #f0f0f0;
    display:flex; justify-content:space-between; align-items:center
}
.date { font-size:10px; color:#ccc }
.dot  { width:5px; height:5px; border-radius:50%; background:#ddd }
"""

def _build_card_html(
    nick: str,
    chara: str,
    src: str,
    wife_b64: str,
    avatar_b64: str,
    sub: str,
    badge: str,
) -> str:
    src_line = f'<span class="source">《{src}》</span>' if src else ""

    if wife_b64:
        img_block = f'<div class="img-wrap"><img src="{wife_b64}"/></div>'
    else:
        img_block = '<div class="img-wrap" style="height:240px"></div>'

    if avatar_b64:
        av_inner = f'<img src="{avatar_b64}"/>'
    else:
        av_inner = '<div class="avatar-placeholder">👤</div>'

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_CARD_CSS}</style></head><body>
<div class="card">
  <div class="top">
    <div class="avatar">{av_inner}</div>
    <div class="meta">
      <div class="nick">{nick}</div>
      <div class="sub">{sub}</div>
    </div>
    <div class="badge">{badge}</div>
  </div>
  {img_block}
  <div class="info">
    <div class="chara">{chara}</div>
    {src_line}
  </div>
  <div class="footer">
    <span class="date">{get_today()}</span>
    <div class="dot"></div>
  </div>
</div></body></html>"""

# ══════════════════════════════════════════════════════
#  Playwright 高清截图
# ══════════════════════════════════════════════════════
async def _ensure_browser():
    if not _pw_ctx.get("browser") or not _pw_ctx["browser"].is_connected():
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
        _pw_ctx["pw"] = pw
        _pw_ctx["browser"] = browser
    return _pw_ctx["browser"]

async def _html_to_png(html: str, cache_key: str) -> str:
    out = os.path.join(CARD_DIR, f"{cache_key}.png")
    if os.path.exists(out):
        return out
    async with _pw_lock:
        browser = await _ensure_browser()
        # device_scale_factor=2 → 2× DPR，高清输出
        page = await browser.new_page(
            viewport={"width": 460, "height": 2000},
            device_scale_factor=2.0,
        )
        await page.set_content(html, wait_until="domcontentloaded")
        el = await page.query_selector(".card")
        await el.screenshot(path=out)
        await page.close()
    return out

async def _fetch_bytes(url: str, session: aiohttp.ClientSession) -> bytes:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                return await r.read()
    except Exception:
        pass
    return b""

def _to_b64(raw: bytes, ext: str) -> str:
    if not raw:
        return ""
    mime = {"jpg":"image/jpeg","jpeg":"image/jpeg",
            "png":"image/png","gif":"image/gif",
            "webp":"image/webp"}.get(ext.lower(), "image/jpeg")
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"

async def _render_card(
    nick: str,
    img: str,
    base_url: str,
    avatar_url: str = "",
    sub: str = "抽到了今日老婆",
    badge: str = "TODAY",
) -> str:
    chara, src = parse_wife_name(img)
    ext = img.rsplit(".", 1)[-1] if "." in img else "jpg"

    async with aiohttp.ClientSession() as session:
        # 老婆图
        local = os.path.join(IMG_DIR, img)
        if os.path.exists(local):
            wife_raw = open(local, "rb").read()
        else:
            wife_raw = await _fetch_bytes(base_url + img, session)

        # 头像
        av_raw = await _fetch_bytes(avatar_url, session) if avatar_url else b""

    wife_b64   = _to_b64(wife_raw, ext)
    avatar_b64 = _to_b64(av_raw, "jpg") if av_raw else ""

    html = _build_card_html(nick, chara, src, wife_b64, avatar_b64, sub, badge)
    key  = hashlib.md5(f"{nick}{img}{badge}".encode()).hexdigest()[:14]
    return await _html_to_png(html, key)

# ══════════════════════════════════════════════════════
#  插件主体
# ══════════════════════════════════════════════════════
class WifePlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config          = config
        self.need_prefix     = config.get("need_prefix", False)
        self.ntr_max         = config.get("ntr_max", 3)
        self.ntr_possibility = config.get("ntr_possibility", 0.4)
        self.change_max      = config.get("change_max_per_day", 3)
        self.image_base_url  = config.get("image_base_url", "").rstrip("/") + "/"
        self.image_list_url  = config.get("image_list_url", "")
        # QQ 头像 API
        self.qq_avatar_url   = "https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640"

        self._commands = {
            "查老婆": self._cmd_search,
            "牛老婆": self._cmd_ntr,
            "换老婆": self._cmd_change,
            "ck":     self._cmd_change,
        }

    # ── 事件入口 ──────────────────────────────────────
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        if not event.message_obj or not hasattr(event.message_obj, "group_id"):
            return
        if self.need_prefix and not event.is_at_or_wake_command:
            return
        text = event.message_str.strip()
        for cmd, func in self._commands.items():
            if text.startswith(cmd):
                async for res in func(event):
                    yield res
                return

    # ── 获取图片列表 ───────────────────────────────────
    async def _all_wife_imgs(self) -> list[str]:
        """返回全部可用老婆图片名列表（本地优先）"""
        try:
            local = [f for f in os.listdir(IMG_DIR)
                     if f.lower().endswith((".png",".jpg",".jpeg",".webp",".gif"))]
            if local:
                return local
        except Exception:
            pass

        cached, expired = [], True
        if os.path.exists(WIFE_LIST_CACHE_FILE):
            expired = (time.time() - os.path.getmtime(WIFE_LIST_CACHE_FILE)) >= 3600
            try:
                cached = [l.strip() for l in
                          open(WIFE_LIST_CACHE_FILE, encoding="utf-8").read().splitlines()
                          if l.strip()]
            except Exception:
                pass

        if not expired and cached:
            return cached

        try:
            url = self.image_list_url or self.image_base_url
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as s:
                async with s.get(url) as resp:
                    if resp.status == 200:
                        lines = [l.strip() for l in
                                 (await resp.text()).splitlines() if l.strip()]
                        if lines:
                            open(WIFE_LIST_CACHE_FILE, "w",
                                 encoding="utf-8").write("\n".join(lines))
                            return lines
        except Exception:
            pass

        return cached  # 可能为空

    async def _pick_unique_wife(self, gid: str, uid: str) -> str | None:
        """
        为 uid 在 gid 中抽取今日未被任何人持有的老婆。
        已被本群其他人持有的老婆会被排除。
        """
        all_imgs = await self._all_wife_imgs()
        if not all_imgs:
            return None

        today = get_today()
        cfg   = load_group_config(gid)

        # 今日本群已被持有的图片集合（排除该用户自己的旧老婆）
        taken = {
            data[0]
            for u, data in cfg.items()
            if u != uid
            and isinstance(data, list)
            and len(data) >= 2
            and data[1] == today
        }

        pool = [img for img in all_imgs if img not in taken]
        if not pool:
            # 所有图都被持有，退化为全量随机（极端情况）
            pool = all_imgs

        return random.choice(pool)

    # ── 解析 @ ────────────────────────────────────────
    def _parse_at(self, event: AstrMessageEvent) -> str | None:
        if not (event.message_obj and hasattr(event.message_obj, "message")):
            return None
        for comp in event.message_obj.message:
            if isinstance(comp, At):
                return str(comp.qq)
        return None

    def _avatar_url(self, uid: str) -> str:
        return self.qq_avatar_url.format(uid=uid)

    # ── 发卡片（降级到纯文本）────────────────────────
    async def _send_card(
        self, event, uid: str, nick: str, img: str,
        sub="抽到了今日老婆", badge="TODAY"
    ):
        try:
            png = await _render_card(
                nick, img, self.image_base_url,
                avatar_url=self._avatar_url(uid),
                sub=sub, badge=badge,
            )
            yield event.chain_result([Image.fromFileSystem(png)])
        except Exception:
            chara, src = parse_wife_name(img)
            title = f"《{src}》的{chara}" if src else chara
            yield event.plain_result(f"[{badge}] {nick} → {title}")

    # ── 查老婆 ────────────────────────────────────────
    async def _cmd_search(self, event: AstrMessageEvent):
        gid   = str(event.message_obj.group_id)
        tid   = self._parse_at(event) or str(event.get_sender_id())
        today = get_today()
        cfg   = load_group_config(gid)
        data  = cfg.get(tid)

        if not data or not isinstance(data, list) or data[1] != today:
            yield event.plain_result("对方今天还没有老婆")
            return

        img, _, owner = data
        async for res in self._send_card(event, tid, owner, img,
                                         sub="的今日老婆", badge="WIFE"):
            yield res

    # ── 牛老婆 ────────────────────────────────────────
    async def _cmd_ntr(self, event: AstrMessageEvent):
        gid   = str(event.message_obj.group_id)
        uid   = str(event.get_sender_id())
        nick  = event.get_sender_name()
        tid   = self._parse_at(event)
        today = get_today()

        if not tid or tid == uid:
            yield event.plain_result("请 @ 要牛的对象，且不能牛自己")
            return

        grp = _records.setdefault("ntr", {}).setdefault(gid, {})
        rec = grp.get(uid, {"date": today, "count": 0})
        if rec["date"] != today:
            rec = {"date": today, "count": 0}
        if rec["count"] >= self.ntr_max:
            yield event.plain_result(f"今日已牛 {self.ntr_max} 次，明天再来")
            return

        async with _get_lock(gid):
            cfg = load_group_config(gid)
            if tid not in cfg or cfg[tid][1] != today:
                yield event.plain_result("对方今天没有老婆可牛")
                return

            rec["count"] += 1
            grp[uid] = rec
            _save_records()

            if random.random() < self.ntr_possibility:
                img      = cfg[tid][0]
                cfg[uid] = [img, today, nick]
                del cfg[tid]
                save_group_config(gid, cfg)

                async for res in self._send_card(
                    event, uid, nick, img,
                    sub="牛走了别人的老婆 👿", badge="NTR"
                ):
                    yield res
            else:
                # 牛失败：给消息贴 9 号表情
                try:
                    await event.bot.set_msg_emoji_like(
                        message_id=event.message_obj.message_id,
                        emoji_id="9"
                    )
                except Exception:
                    pass

    # ── 换老婆 ────────────────────────────────────────
    async def _cmd_change(self, event: AstrMessageEvent):
        gid   = str(event.message_obj.group_id)
        uid   = str(event.get_sender_id())
        nick  = event.get_sender_name()
        today = get_today()

        grp = _records.setdefault("change", {}).setdefault(gid, {})
        rec = grp.get(uid, {"date": "", "count": 0})
        if rec["date"] == today and rec["count"] >= self.change_max:
            yield event.plain_result(f"今天已换了 {self.change_max} 次，明天再来")
            return

        async with _get_lock(gid):
            img = await self._pick_unique_wife(gid, uid)
            if not img:
                yield event.plain_result("获取新老婆失败，请稍后重试")
                return

            cfg      = load_group_config(gid)
            cfg[uid] = [img, today, nick]
            save_group_config(gid, cfg)

        grp[uid] = {
            "date":  today,
            "count": (rec["count"] + 1 if rec["date"] == today else 1),
        }
        _save_records()

        async for res in self._send_card(event, uid, nick, img,
                                         sub="抽到了今日老婆", badge="TODAY"):
            yield res

    # ── 清理 ──────────────────────────────────────────
    async def terminate(self):
        _cfg_locks.clear()
        try:
            if _pw_ctx.get("browser"):
                await _pw_ctx["browser"].close()
            if _pw_ctx.get("pw"):
                await _pw_ctx["pw"].stop()
        except Exception:
            pass
        _pw_ctx.clear()
