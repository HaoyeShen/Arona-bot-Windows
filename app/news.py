"""Daily Kivotos news digest.

The collector deliberately uses a small, replaceable set of public search
endpoints and always falls back to a plain template if the model is offline.
It never runs when the feature is disabled.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from .config_store import ConfigStore, PersonaStore
from .model_client import make_client
from .paths import DATA

log = logging.getLogger(__name__)

NEWS_STATE_FILE = DATA / "news_state.json"
DEFAULT_TIME = "06:50"

# A search result is only accepted if it is visibly about Blue Archive.
RELEVANT_TERMS = (
    "蔚蓝档案", "碧蓝档案", "blue archive", "bluearchive",
    "ブルーアーカイブ", "ブルアカ", "블루 아카이브",
)
NEWS_TERMS = (
    "更新", "卡池", "招募", "维护", "活动", "公告", "新角色", "版本",
    "前瞻", "攻略", "实况", "抽卡", "演习", "总力", "新闻", "爆料",
    "情报", "上线", "复刻", "模式", "介绍", "资讯", "pickup", "banner",
    "update", "event", "character", "news", "patch", "announcement",
)
UNCERTAIN_TERMS = (
    "爆料", "泄露", "泄漏", "传闻", "猜测", "疑似", "据传", "或将",
    "可能", "未确认", "未证实", "rumor", "leak", "unconfirmed", "allegedly",
)
SOCIAL_DOMAINS = (
    "x.com", "twitter.com", "weibo.com", "tieba.baidu.com", "reddit.com",
    "bilibili.com", "youtube.com",
)


@dataclass
class NewsItem:
    region: str
    title: str
    url: str
    source: str
    snippet: str = ""
    uncertain: bool = False


class _BingParser(HTMLParser):
    """Small, dependency-free parser for Bing's organic result list."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._in_algo = 0
        self._in_h2 = 0
        self._in_link = 0
        self._in_cite = 0
        self._current: dict | None = None
        self._capture: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "li" and "b_algo" in classes:
            self._finish()
            self._in_algo = 1
            self._current = {"title": "", "url": "", "source": "", "snippet": ""}
            return
        if not self._in_algo or not self._current:
            return
        if tag == "h2":
            self._in_h2 += 1
        elif tag == "a" and self._in_h2 and not self._current["url"]:
            self._in_link = 1
            self._current["url"] = attributes.get("href") or ""
            self._capture = []
        elif tag == "p" and not self._current["snippet"]:
            self._capture = []
        elif tag == "cite":
            self._in_cite = 1
            self._capture = []

    def handle_endtag(self, tag: str) -> None:
        if not self._in_algo or not self._current:
            return
        if tag == "h2":
            self._in_h2 = max(0, self._in_h2 - 1)
        elif tag == "a" and self._in_link:
            self._current["title"] = _clean("".join(self._capture))
            self._in_link = 0
            self._capture = []
        elif tag == "p" and self._capture:
            self._current["snippet"] = _clean("".join(self._capture))
            self._capture = []
        elif tag == "cite" and self._in_cite:
            self._current["source"] = _clean("".join(self._capture))
            self._in_cite = 0
            self._capture = []
        elif tag == "li":
            self._finish()

    def handle_data(self, data: str) -> None:
        if self._in_algo and self._current and (self._in_link or self._in_cite or self._capture):
            self._capture.append(data)
        elif self._in_algo and self._current and self._in_h2 and not self._current["title"]:
            # Some Bing layouts wrap the title in spans instead of a direct link.
            self._capture.append(data)

    def _finish(self) -> None:
        if self._current:
            if self._current.get("title") and self._current.get("url"):
                self.items.append(self._current)
        self._current = None
        self._in_algo = 0
        self._in_h2 = 0
        self._in_link = 0
        self._in_cite = 0
        self._capture = []

    def close(self) -> None:
        super().close()
        self._finish()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text or "")).strip()


def _domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def _is_uncertain(text: str, source: str) -> bool:
    lowered = (text or "").lower()
    if any(term in lowered for term in UNCERTAIN_TERMS):
        return True
    return any(domain in source.lower() for domain in SOCIAL_DOMAINS)


def _build_opener():
    """Prefer direct, then a reachable system proxy.

    A stale Windows proxy is very common (Clash/V2Ray was closed while the
    environment variable remained).  Search must keep working in that state.
    """
    for force_direct in (True, False):
        try:
            if force_direct:
                return urllib.request.build_opener(urllib.request.ProxyHandler({}))
            configured = urllib.request.getproxies()
            usable = {}
            for scheme, value in configured.items():
                if scheme == "no" or not value:
                    continue
                parsed = urllib.parse.urlparse(str(value))
                host = parsed.hostname or ""
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                if not host:
                    continue
                with socket.create_connection((host, port), timeout=0.35):
                    usable[scheme] = str(value)
            if usable:
                return urllib.request.build_opener(urllib.request.ProxyHandler(usable))
        except Exception:
            continue
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_text(url: str, timeout: float = 10.0) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6,ja;q=0.5",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with _build_opener().open(request, timeout=timeout) as response:
        raw = response.read(2_000_000)
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", response.headers.get("Content-Type", ""), re.I)
    if match:
        charset = match.group(1)
    return raw.decode(charset, "replace")


def _http_json(url: str, timeout: float = 10.0) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0 Safari/537.36",
            "Referer": "https://search.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
            "Cookie": "buvid3=" + str(uuid.uuid4()).upper() + "infoc; b_nut=1790000000",
        },
    )
    with _build_opener().open(request, timeout=timeout) as response:
        raw = response.read(4_000_000)
    return json.loads(raw.decode("utf-8", "replace"))


def _strip_html(text: str) -> str:
    return _clean(re.sub(r"<[^>]+>", "", text or ""))


OFFICIAL_AUTHORS = {
    "蔚蓝档案", "蔚蓝档案小助理", "Blue Archive", "ブルーアーカイブ",
    "BlueArchive", "蔚蓝档案官方",
}


class NewsService:
    """Fetch, deduplicate, summarize, and send one daily digest."""

    REGIONS = (
        ("国服", "蔚蓝档案 国服", "蔚蓝档案"),
        ("日服", "蔚蓝档案 日服", "蔚蓝档案 日服 公告"),
        ("国际服", "蔚蓝档案 国际服", "Blue Archive 国际服"),
    )

    def __init__(self, config: ConfigStore, personas: PersonaStore,
                 state_path: Path | None = None,
                 usage_callback: Callable | None = None) -> None:
        self.config = config
        self.personas = personas
        self.state_path = state_path or NEWS_STATE_FILE
        self.usage_callback = usage_callback
        self._lock = threading.Lock()

    # --------------------------------------------------------------- settings
    @property
    def settings(self) -> dict:
        return self.config.data.setdefault("news", {})

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False))

    @property
    def target_chat(self) -> str:
        return str(self.settings.get("target_chat", "")).strip()

    @property
    def send_time(self) -> str:
        return str(self.settings.get("send_time", DEFAULT_TIME)).strip() or DEFAULT_TIME

    def _target_is_group(self) -> bool:
        target = self.config.target_by_name(self.target_chat)
        return bool(target and target.get("type") == "group" and target.get("enabled", True))

    def _minutes(self) -> tuple[int, int]:
        try:
            hour, minute = self.send_time.split(":", 1)
            return max(0, min(23, int(hour))), max(0, min(59, int(minute)))
        except (TypeError, ValueError):
            return 6, 50

    # ------------------------------------------------------------------ state
    def _load_state(self) -> dict:
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return {}

    def _save_state(self, patch: dict) -> None:
        state = self._load_state()
        state.update(patch)
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("写入新闻状态失败：%s", exc)

    def last_sent_date(self) -> str:
        return str(self._load_state().get("last_sent_date", ""))

    def should_run(self, now: datetime | None = None) -> tuple[bool, str]:
        if not self.enabled:
            return False, "功能关闭"
        if not self.target_chat:
            return False, "未设置发送群聊"
        if not self._target_is_group():
            return False, "目标群聊不存在或不是群聊"
        current = now or datetime.now()
        today = current.date().isoformat()
        state = self._load_state()
        if state.get("last_sent_date") == today:
            return False, "今天已经发送过"
        hour, minute = self._minutes()
        due = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if current < due:
            return False, "还没到发送时间"
        # Do not send yesterday's news at dinner time after a long downtime.
        if current.hour >= 12:
            return False, "已过早间补发窗口"
        attempt = float(state.get("last_attempt_ts", 0) or 0)
        if attempt and time.time() - attempt < 30 * 60:
            return False, "上次尝试失败，稍后重试"
        return True, "可以发送"

    # ------------------------------------------------------------------ fetch
    def _search(self, query: str, region: str, mkt: str, timeout: float) -> list[NewsItem]:
        url = ("https://www.bing.com/search?q=" + urllib.parse.quote_plus(query) +
               f"&mkt={urllib.parse.quote(mkt)}&setlang={mkt.split('-')[0]}&count=8")
        try:
            html = _http_text(url, timeout=timeout)
        except Exception as exc:
            log.warning("新闻搜索失败 · %s · %s", region, exc)
            return []
        parser = _BingParser()
        try:
            parser.feed(html)
            parser.close()
        except Exception as exc:
            log.warning("新闻页面解析失败 · %s · %s", region, exc)
            return []
        found: list[NewsItem] = []
        for row in parser.items:
            title = _clean(row.get("title", ""))
            snippet = _clean(row.get("snippet", ""))
            combined = (title + " " + snippet).lower()
            if not title or not any(term in combined for term in RELEVANT_TERMS):
                continue
            if not any(term in combined for term in (
                "更新", "卡池", "招募", "维护", "活动", "公告", "新角色",
                "版本", "news", "update", "banner", "event", "character",
            )):
                continue
            source = _clean(row.get("source", "")) or _domain(row.get("url", ""))
            if not source:
                source = "来源未确认"
            uncertain = _is_uncertain(title + " " + snippet, source)
            found.append(NewsItem(
                region=region,
                title=title[:110],
                url=row.get("url", ""),
                source=source[:48],
                snippet=snippet[:160],
                uncertain=uncertain,
            ))
        return found

    def _official_cn(self, timeout: float, per_region: int) -> list[NewsItem]:
        """Read the official CN "快讯" list.

        The homepage loads it through /api/news/list.  We read the linked
        articles' text (not videos) and keep the official CN server scope.
        """
        url = ("https://bluearchive-cn.com/api/news/list?"
               "pageIndex=1&pageNum=12&type=")
        try:
            payload = _http_json(url, timeout=timeout)
        except Exception as exc:
            log.warning("国服官网快讯读取失败：%s", exc)
            return []
        if int(payload.get("code", -1)) != 0:
            return []
        rows = (payload.get("data") or {}).get("rows") or []
        now = time.time()
        items: list[NewsItem] = []
        for row in rows:
            title = _clean(str(row.get("title") or ""))
            if not title:
                continue
            published = float(row.get("publishTime") or 0) / 1000.0
            if published and published > 0 and now - published > 21 * 86400:
                continue
            content = _strip_html(str(row.get("content") or ""))
            content = re.sub(r"\s+", " ", content).strip()
            item_id = row.get("id")
            items.append(NewsItem(
                region="国服",
                title=title[:110],
                url=(f"https://bluearchive-cn.com/news/{item_id}" if item_id
                     else "https://bluearchive-cn.com/"),
                source="国服官网",
                snippet=content[:500],
                uncertain=False,
            ))
            if len(items) >= per_region:
                break
        return items

    def _bilibili(self, query: str, region: str, timeout: float,
                  per_region: int) -> list[NewsItem]:
        """Search Bilibili articles.  It is a useful public source for the
        Chinese community and also carries translated JP/global announcements.
        """
        url = ("https://api.bilibili.com/x/web-interface/search/type?"
               "search_type=article&keyword=" + urllib.parse.quote_plus(query) +
               "&order=pubdate&page=1")
        try:
            payload = _http_json(url, timeout=timeout)
        except Exception as exc:
            log.warning("新闻搜索失败 · %s · %s", region, exc)
            return []
        if int(payload.get("code", -1)) != 0:
            log.warning("新闻搜索返回错误 · %s · %s", region, payload.get("message", ""))
            return []
        rows = (payload.get("data") or {}).get("result") or []
        now = time.time()
        found: list[NewsItem] = []
        for row in rows:
            title = _strip_html(str(row.get("title") or ""))
            snippet = _strip_html(str(row.get("desc") or ""))
            combined = (title + " " + snippet).lower()
            if not title or not any(term in combined for term in RELEVANT_TERMS):
                continue
            if not any(term in combined for term in NEWS_TERMS):
                continue
            if region == "国服" and any(tag in title for tag in
                                        ("日服", "国际服", "Global", "繁中")):
                continue
            if region == "日服" and not any(tag in title for tag in
                                           ("日服", "ブルー", "日版")):
                continue
            if region == "国际服" and not any(tag in title for tag in
                                              ("国际服", "Global", "繁中", "EN服")):
                continue
            published = float(row.get("pub_time") or row.get("pubdate") or 0)
            if published and published > 0 and now - published > 14 * 86400:
                continue
            author = _clean(str(row.get("author") or "")) or "Bilibili 用户"
            source = author
            official = author in OFFICIAL_AUTHORS
            uncertain = (not official) or _is_uncertain(title + " " + snippet, source)
            article_id = row.get("id")
            url_item = (f"https://www.bilibili.com/read/cv{article_id}"
                        if article_id else "https://www.bilibili.com/")
            found.append(NewsItem(
                region=region,
                title=title[:110],
                url=url_item,
                source=source[:48],
                snippet=snippet[:160],
                uncertain=uncertain,
            ))
            if len(found) >= per_region:
                break
        return found

    def fetch(self) -> dict[str, list[NewsItem]]:
        settings = self.settings
        timeout = max(5.0, min(20.0, float(settings.get("timeout", 10) or 10)))
        per_region = max(1, min(3, int(settings.get("max_items", 2) or 2)))
        result: dict[str, list[NewsItem]] = {region[0]: [] for region in self.REGIONS}
        seen: set[str] = set()
        for region, first_query, second_query in self.REGIONS:
            if region == "国服":
                for item in self._official_cn(timeout, per_region):
                    key = re.sub(r"[^\w\u4e00-\u9fff]", "", item.title.lower())
                    if key and key not in seen:
                        seen.add(key)
                        result[region].append(item)
                if result[region]:
                    continue
            for query in (first_query, second_query):
                for item in self._bilibili(query, region, timeout, per_region):
                    key = re.sub(r"[^\w\u4e00-\u9fff]", "", item.title.lower())
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    result[region].append(item)
                    if len(result[region]) >= per_region:
                        break
                if len(result[region]) >= per_region:
                    break
            # Fallback to Bing if Bilibili has no usable result; Bing may be
            # empty for some locales but this keeps the collector replaceable.
            if not result[region]:
                for item in self._search(first_query, region,
                                         {"国服": "zh-CN", "日服": "ja-JP",
                                          "国际服": "en-US"}.get(region, "zh-CN"),
                                         timeout):
                    key = re.sub(r"[^\w\u4e00-\u9fff]", "", item.title.lower())
                    if key and key not in seen:
                        seen.add(key)
                        result[region].append(item)
                        if len(result[region]) >= per_region:
                            break
        return result

    # ---------------------------------------------------------------- content
    def _persona(self) -> dict:
        return self.personas.get(self.config.data.get("active_persona_id"))

    def _fallback_message(self, grouped: dict[str, list[NewsItem]]) -> str:
        persona = self._persona()
        name = str(persona.get("name") or "阿洛娜")
        today = datetime.now().strftime("%m月%d日")
        if "普拉娜" in name:
            intro = "老师，早上好。今日基沃托斯新闻整理如下。"
            outro = "未确认消息已经标注，普拉娜会继续留意。"
        else:
            intro = f"老师早上好，今天的基沃托斯新闻{name}整理好啦。"
            outro = f"有新的确切消息，{name}再补充。"
        lines = [f"【基沃托斯新闻 · {today}】", intro]
        for region in ("国服", "日服", "国际服"):
            lines.append("")
            lines.append(f"■ {region}")
            items = grouped.get(region, [])
            if not items:
                lines.append("· 今天暂时没有可靠的新消息。")
                continue
            for item in items:
                suffix = item.source
                if item.uncertain:
                    suffix += "，未确认"
                lines.append(f"· {item.title}（{suffix}）")
        lines.extend(["", outro])
        return "\n".join(lines).strip()

    def _model_message(self, grouped: dict[str, list[NewsItem]]):
        persona = self._persona()
        name = str(persona.get("name") or "阿洛娜")
        facts = []
        for region in ("国服", "日服", "国际服"):
            facts.append(f"【{region}】")
            if not grouped.get(region):
                facts.append("- 今天暂无可靠消息")
            for item in grouped[region]:
                facts.append(
                    f"- {item.title}｜来源：{item.source}"
                    + ("｜未确认" if item.uncertain else "")
                )
        system = (
            f"你是《蔚蓝档案》的{name}，正在给老师发每日「基沃托斯新闻」。\n"
            f"你的语气：{persona.get('tone', '温柔、简洁、认真')}\n"
            "只使用下面给出的新闻事实，绝对不能补充、想象或编造事实。\n"
            "分为【国服】【日服】【国际服】三个板块，每段 1–2 条，每条简短。\n"
            "凡是素材标有「未确认」或来源模糊的，必须在句末写「（未确认）」，"
            "不能把猜测写成确定消息。\n"
            "保留来源名称；总长度不超过 800 字；不要输出 Markdown 表格或分析过程。\n"
            "开头可以自然地向老师问早，结尾用一句符合你性格的收束。"
        )
        prompt = "今日素材：\n" + "\n".join(facts)
        profile = self.config.active_profile()
        client = make_client(profile)
        result = client.chat([{"role": "user", "content": prompt}], system)
        text = str(result.text or "").strip()
        if len(text) < 80:
            return None, result
        if not all(mark in text for mark in ("国服", "日服", "国际服")):
            return None, result
        return text[:1000].strip(), result

    def build_message(self, grouped: dict[str, list[NewsItem]]) -> tuple[str, object | None]:
        if not any(grouped.values()):
            return "", None
        if bool(self.settings.get("use_model_summary", True)):
            try:
                text, result = self._model_message(grouped)
                if text:
                    return text, result
                if result is not None and self.usage_callback:
                    self.usage_callback(result, "news")
            except Exception as exc:
                log.warning("新闻模型总结失败，改用模板：%s", exc)
        return self._fallback_message(grouped), None

    # ------------------------------------------------------------------- send
    def run(self, adapter, now: datetime | None = None, force: bool = False) -> dict:
        with self._lock:
            current = now or datetime.now()
            if not force:
                ok, reason = self.should_run(current)
                if not ok:
                    return {"sent": False, "reason": reason}
            if not self.enabled:
                return {"sent": False, "reason": "功能关闭"}
            target = self.target_chat
            if not target or not self._target_is_group():
                return {"sent": False, "reason": "目标群聊不可用"}
            if not force:
                self._save_state({
                    "last_attempt_ts": time.time(),
                    "last_attempt_date": current.date().isoformat(),
                })
            grouped = self.fetch()
            if not any(grouped.values()):
                self._save_state({"last_error": "没有检索到可靠新闻"})
                return {"sent": False, "reason": "没有检索到可靠新闻"}
            message, usage_result = self.build_message(grouped)
            if not message:
                return {"sent": False, "reason": "新闻总结为空"}
            if usage_result is not None and self.usage_callback:
                self.usage_callback(usage_result, "news")
            try:
                result = adapter.send_text(target, message)
            except Exception as exc:
                self._save_state({"last_error": str(exc)})
                log.warning("基沃托斯新闻发送失败：%s", exc)
                return {"sent": False, "reason": str(exc), "message": message}
            if not getattr(result, "ok", False):
                error = getattr(result, "error", "") or "发送失败"
                self._save_state({"last_error": error})
                log.warning("基沃托斯新闻发送失败 · %s · %s", target, error)
                return {"sent": False, "reason": error, "message": message}
            patch = {
                "last_sent_at": current.isoformat(timespec="seconds"),
                "last_target": target,
                "last_error": "",
                "last_preview": message[:300],
            }
            # A manual test send must not consume the real 06:50 slot.
            if not force:
                patch["last_sent_date"] = current.date().isoformat()
            self._save_state(patch)
            log.info("基沃托斯新闻已发送 · %s · %d 字", target, len(message))
            return {"sent": True, "target": target, "message": message,
                    "usage": usage_result}

    def preview(self) -> dict:
        """Fetch and render without sending (used by the settings page)."""
        grouped = self.fetch()
        if not any(grouped.values()):
            return {"message": "", "items": grouped}
        message, usage_result = self.build_message(grouped)
        return {"message": message, "items": grouped, "usage": usage_result}
