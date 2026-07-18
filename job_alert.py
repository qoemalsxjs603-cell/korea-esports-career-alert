from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from playwright.async_api import (
    Browser,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


ROOT = Path(__file__).resolve().parent
SOURCES_PATH = ROOT / "config" / "sources.json"
SETTINGS_PATH = ROOT / "config" / "settings.json"
KST = timezone(timedelta(hours=9))

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "trk", "trackingId", "refId", "origin",
    "originToLandingJobPostings",
}
BLOCKED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".zip", ".hwp",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp4", ".mp3",
}
GENERIC_NAV_TITLES = {
    "home", "홈", "about", "회사소개", "login", "로그인", "sign up", "회원가입",
    "menu", "메뉴", "more", "더보기", "view all", "전체보기", "apply", "지원하기",
    "privacy", "개인정보처리방침", "terms", "이용약관", "list", "목록",
    "prev", "next", "이전", "다음", "careers", "career", "jobs", "job",
    "recruit", "채용", "채용정보", "전체 채용정보", "신입공채", "헤드헌팅",
    "기업정보 게시물", "연봉정보 게시물", "jobkorea", "사람인",
}
CLOSED_PATTERNS = [
    r"마감된\s*채용공고",
    r"채용이\s*마감",
    r"마감되었습니다",
    r"접수기간이\s*종료",
    r"지원기간이\s*종료",
    r"종료된\s*공고",
    r"지난\s*채용정보",
    r"접수\s*마감",
]
ACTIVE_PATTERNS = [
    r"D-\d+",
    r"오늘마감",
    r"상시채용",
    r"채용시",
    r"입사지원",
    r"즉시지원",
    r"홈페이지\s*지원",
    r"진행중",
]


@dataclass
class Item:
    item_id: str
    source_id: str
    category: str
    source_name: str
    title: str
    url: str
    context: str = ""
    matched: str = ""
    posted_at: str = ""
    deadline: str = ""

    def to_state(self, first_seen_at: str, last_seen_at: str) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "title": self.title,
            "url": self.url,
            "matched": self.matched,
            "posted_at": self.posted_at,
            "deadline": self.deadline,
            "first_seen_at": first_seen_at,
            "last_seen_at": last_seen_at,
        }


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def now_iso() -> str:
    return now_kst().isoformat(timespec="seconds")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def normalize(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_multiline(text: str) -> str:
    text = html.unescape(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def folded(text: str) -> str:
    return re.sub(r"[\s\-_–—·]+", "", normalize(text)).casefold()


def clean_title(text: str) -> str:
    title = normalize(text)
    title = re.sub(r"(?i)(^|\s)(NEW|N|새글)(?=\s|$)", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -|·")
    return title


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in TRACKING_PARAMS
    ]
    clean = parsed._replace(
        fragment="",
        query=urlencode(query, doseq=True),
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
    )
    return urlunparse(clean)


def is_blocked_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in BLOCKED_EXTENSIONS)


def sha_token(value: str, length: int = 18) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def slug_token(value: str, max_len: int = 70) -> str:
    token = re.sub(r"[^0-9a-zA-Z가-힣]+", "-", normalize(value)).strip("-").lower()
    return token[:max_len] or sha_token(value, 12)


def normalize_date_string(value: str) -> str:
    if not value:
        return ""
    m = re.search(r"(20\d{2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})", value)
    if not m:
        return ""
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def parse_date(value: str) -> date | None:
    normalized = normalize_date_string(value)
    if not normalized:
        return None
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date()
    except ValueError:
        return None


def is_expired(deadline: str) -> bool:
    parsed = parse_date(deadline)
    return bool(parsed and parsed < now_kst().date())


def extract_dates(text: str) -> tuple[str, str]:
    text = normalize(text)
    posted = ""
    deadline = ""

    posted_patterns = [
        r"(?:등록일|게시일|작성일|공고일|채용\s*시작일)\s*[:：]?\s*(20\d{2}[./-]\d{1,2}[./-]\d{1,2})",
        r"(?:Posted|Published)\s*(?:on)?\s*[:：]?\s*(20\d{2}[./-]\d{1,2}[./-]\d{1,2})",
    ]
    deadline_patterns = [
        r"(?:마감일|접수마감|지원마감|종료일)\s*[:：]?\s*(20\d{2}[./-]\d{1,2}[./-]\d{1,2})",
        r"~\s*(20\d{2}[./-]\d{1,2}[./-]\d{1,2})",
    ]

    for pattern in posted_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            posted = normalize_date_string(match.group(1))
            break

    for pattern in deadline_patterns:
        matches = list(re.finditer(pattern, text, re.I))
        if matches:
            deadline = normalize_date_string(matches[-1].group(1))
            break

    all_dates = re.findall(r"20\d{2}[./-]\d{1,2}[./-]\d{1,2}", text)
    if not posted and all_dates:
        posted = normalize_date_string(all_dates[0])
    if not deadline and len(all_dates) >= 2:
        deadline = normalize_date_string(all_dates[-1])

    return posted, deadline


def keyword_match(text: str, keywords: list[str]) -> str:
    haystack = folded(text)
    for keyword in keywords:
        if folded(keyword) in haystack:
            return keyword
    return ""


def contains_excluded(text: str, source: dict[str, Any]) -> bool:
    haystack = folded(text)
    return any(folded(word) in haystack for word in source.get("exclude_keywords", []))


def location_criteria_match(text: str, source: dict[str, Any]) -> str:
    haystack = folded(text)
    location_hit = ""
    for group in source.get("criteria_any", []):
        location_hit = next((word for word in group if folded(word) in haystack), "")
        if location_hit:
            break
    if not location_hit:
        return ""

    mode_hit = next(
        (word for word in source.get("criteria_work_mode", []) if folded(word) in haystack),
        "",
    )
    return f"{location_hit} + {mode_hit}" if mode_hit else ""


def is_closed_text(text: str) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in CLOSED_PATTERNS)


def has_active_text(text: str) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in ACTIVE_PATTERNS)


def extract_unique_token(url: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    priority_keys = [
        "wr_id", "rec_idx", "brd_id", "nttId", "recruitNo", "recruit_no",
        "jobId", "job_id", "postingId", "positionId", "id",
    ]
    for key in priority_keys:
        value = query.get(key)
        if value and value not in {"0", "1"}:
            return f"{key}:{value}"

    path_patterns = [
        r"/Recruit/GI_Read/(\d+)",
        r"/jobs/view/([^/?#]+)",
        r"/o/([^/?#]+)",
        r"/positions?/([^/?#]+)",
        r"/jobs?/([^/?#]+)",
        r"/recruit/([^/?#]+)",
    ]
    for pattern in path_patterns:
        match = re.search(pattern, parsed.path, re.I)
        if match:
            return f"path:{match.group(1)}"
    return ""


def make_item_id(
    source_id: str,
    url: str,
    title: str,
    posted_at: str = "",
    explicit_token: str = "",
) -> str:
    token = explicit_token or extract_unique_token(url)
    if token:
        return f"{source_id}:{slug_token(token, 100)}"

    clean_url = canonical_url(url)
    parsed = urlparse(clean_url)
    if parsed.path not in {"", "/"} or parsed.query:
        return f"{source_id}:url:{sha_token(clean_url)}"

    base = f"{clean_title(title)}|{posted_at}"
    return f"{source_id}:title:{sha_token(base)}"


async def dismiss_popups(page: Page) -> None:
    labels = ["동의", "모두 동의", "Accept", "Accept all", "확인", "닫기", "Close"]
    for label in labels:
        try:
            button = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if await button.count():
                await button.first.click(timeout=900)
        except Exception:
            pass


async def auto_scroll(page: Page) -> None:
    previous = -1
    stable = 0
    for _ in range(9):
        height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(650)
        if height == previous:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        previous = height
    await page.evaluate("window.scrollTo(0, 0)")


async def read_detail(page: Page, url: str, timeout_ms: int) -> str:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1300)
        return normalize(await page.locator("body").inner_text(timeout=6000))[:20000]
    except Exception:
        return ""


async def parse_t1_body(page: Page, source: dict[str, Any]) -> list[Item]:
    raw_text = await page.locator("body").inner_text()
    text = normalize_multiline(raw_text)
    heading = re.compile(
        r"(?im)^\s*(\[(?:Esports\s+T1|esports\s+T1\s+Academy)\]\s*[^\n]+)\s*$"
    )
    matches = list(heading.finditer(text))
    output: list[Item] = []

    for index, match in enumerate(matches):
        full_title = clean_title(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start():end]

        period = re.search(
            r"접수기간\s*[:：]\s*"
            r"(20\d{2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2})(?:\s+\d{1,2}시)?"
            r"\s*~\s*"
            r"(20\d{2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2})(?:\s+\d{1,2}시)?",
            block,
            re.I,
        )
        posted_at = normalize_date_string(period.group(1)) if period else ""
        deadline = normalize_date_string(period.group(2)) if period else ""
        if is_expired(deadline):
            continue

        token = f"{slug_token(full_title)}:{posted_at or 'no-date'}"
        output.append(
            Item(
                item_id=f"{source['id']}:{token}",
                source_id=source["id"],
                category=source["category"],
                source_name=source["name"],
                title=full_title,
                url=f"{source['url']}#{slug_token(full_title)}",
                context=normalize(block)[:1000],
                matched="새 공고",
                posted_at=posted_at,
                deadline=deadline,
            )
        )
    return output[: int(source.get("max_items", 100))]


async def parse_ccon_board(page: Page, source: dict[str, Any]) -> list[Item]:
    raw = await page.evaluate(
        """
        () => {
          const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
          const rows = [];
          const seen = new Set();
          for (const a of document.querySelectorAll('a[href*="bo_table=rnt"][href*="wr_id="]')) {
            const href = a.href;
            const id = new URL(href).searchParams.get('wr_id');
            if (!id || seen.has(id)) continue;
            seen.add(id);
            const box = a.closest('li, tr, article, .bo_li, .list-item, .board-list') || a.parentElement;
            const context = clean(box ? box.innerText : a.innerText);
            const title = clean(a.innerText || a.textContent);
            const statusMatch = context.match(/(진행중|종료|결과|공지)/);
            const dateMatch = context.match(/20\\d{2}[.\\/-]\\d{1,2}[.\\/-]\\d{1,2}/);
            rows.push({
              id,
              href,
              title,
              context,
              status: statusMatch ? statusMatch[1] : '',
              posted_at: dateMatch ? dateMatch[0] : ''
            });
          }
          return rows;
        }
        """
    )

    allowed_status = set(source.get("only_status", []))
    output: list[Item] = []
    for row in raw:
        title = clean_title(row.get("title", ""))
        context = normalize(row.get("context", ""))
        status = row.get("status", "")

        if allowed_status and status not in allowed_status:
            continue
        if contains_excluded(f"{title} {context}", source):
            continue
        matched = keyword_match(title, source.get("keywords", []))
        if not matched:
            continue

        posted_at = normalize_date_string(row.get("posted_at", ""))
        output.append(
            Item(
                item_id=f"{source['id']}:wr-id:{row['id']}",
                source_id=source["id"],
                category=source["category"],
                source_name=source["name"],
                title=title,
                url=canonical_url(row["href"]),
                context=context[:900],
                matched=f"{matched} · {status}" if status else matched,
                posted_at=posted_at,
            )
        )
    return output[: int(source.get("max_items", 80))]


async def parse_jobkorea_company(
    page: Page,
    detail_page: Page,
    source: dict[str, Any],
    timeout_ms: int,
) -> list[Item]:
    raw = await page.evaluate(
        """
        () => {
          const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
          const rows = [];
          const seen = new Set();
          for (const a of document.querySelectorAll('a[href*="/Recruit/GI_Read/"]')) {
            const match = a.href.match(/\\/Recruit\\/GI_Read\\/(\\d+)/i);
            if (!match || seen.has(match[1])) continue;
            seen.add(match[1]);
            const box = a.closest('li, tr, article, [class*="list"], [class*="item"], [class*="recruit"]') || a.parentElement;
            const titleNode = box && box.querySelector(
              'h1,h2,h3,h4,strong,b,[class*="title"],[class*="Tit"],[class*="name"]'
            );
            rows.push({
              id: match[1],
              href: a.href,
              title: clean((titleNode && titleNode.innerText) || a.innerText || a.textContent),
              context: clean(box ? box.innerText : a.innerText)
            });
          }
          return rows;
        }
        """
    )

    output: list[Item] = []
    limit = int(source.get("detail_check_limit", 30))
    for row in raw[:limit]:
        title = clean_title(row.get("title", ""))
        context = normalize(row.get("context", ""))
        if not title or title.casefold() in GENERIC_NAV_TITLES:
            continue
        if re.search(r"(JOBKOREA|전체\s*채용정보|신입공채|헤드헌팅|기업정보|연봉정보)", title, re.I):
            continue
        if is_closed_text(f"{title} {context}"):
            continue

        detail = await read_detail(detail_page, row["href"], timeout_ms)
        combined = normalize(f"{title} {context} {detail}")
        if is_closed_text(combined):
            continue

        posted_at, deadline = extract_dates(combined)
        if is_expired(deadline):
            continue

        # 오래된 제목에 '마감 (~YYYY.MM.DD)'이 직접 쓰인 경우 즉시 제외
        title_deadline = re.search(r"마감\s*\(~?\s*(20\d{2}[./-]\d{1,2}[./-]\d{1,2})", title)
        if title_deadline and is_expired(title_deadline.group(1)):
            continue

        # 상세페이지를 못 읽고 카드에도 활성 상태가 전혀 없으면 오탐 방지를 위해 보류한다.
        if not detail and not has_active_text(context):
            continue

        output.append(
            Item(
                item_id=f"{source['id']}:jobkorea:{row['id']}",
                source_id=source["id"],
                category=source["category"],
                source_name=source["name"],
                title=title,
                url=canonical_url(row["href"]),
                context=combined[:1000],
                matched="새 공고 · 진행 중 확인",
                posted_at=posted_at,
                deadline=deadline,
            )
        )

    return output[: int(source.get("max_items", 100))]


async def parse_saramin_company(
    page: Page,
    detail_page: Page,
    source: dict[str, Any],
    timeout_ms: int,
) -> list[Item]:
    raw = await page.evaluate(
        """
        () => {
          const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
          const rows = [];
          const seen = new Set();
          for (const a of document.querySelectorAll('a[href*="rec_idx="], a[href*="/jobs/relay/view"]')) {
            const u = new URL(a.href);
            const id = u.searchParams.get('rec_idx') || (a.href.match(/rec_idx[=/](\\d+)/) || [])[1];
            if (!id || seen.has(id)) continue;
            seen.add(id);
            const box = a.closest('li, tr, article, [class*="item"], [class*="recruit"], [class*="job"]') || a.parentElement;
            const titleNode = box && box.querySelector(
              'h1,h2,h3,h4,strong,b,[class*="title"],[class*="job_tit"],[class*="name"]'
            );
            rows.push({
              id,
              href: a.href,
              title: clean((titleNode && titleNode.innerText) || a.innerText || a.textContent),
              context: clean(box ? box.innerText : a.innerText)
            });
          }
          return rows;
        }
        """
    )

    output: list[Item] = []
    for row in raw[: int(source.get("max_items", 100))]:
        title = clean_title(row.get("title", ""))
        context = normalize(row.get("context", ""))
        if not title or title.casefold() in GENERIC_NAV_TITLES:
            continue
        if is_closed_text(f"{title} {context}"):
            continue

        detail = await read_detail(detail_page, row["href"], timeout_ms)
        combined = normalize(f"{title} {context} {detail}")
        if is_closed_text(combined):
            continue
        posted_at, deadline = extract_dates(combined)
        if is_expired(deadline):
            continue

        output.append(
            Item(
                item_id=f"{source['id']}:saramin:{row['id']}",
                source_id=source["id"],
                category=source["category"],
                source_name=source["name"],
                title=title,
                url=canonical_url(row["href"]),
                context=combined[:1000],
                matched="새 공고",
                posted_at=posted_at,
                deadline=deadline,
            )
        )
    return output



async def parse_greetinghr(
    page: Page,
    detail_page: Page,
    source: dict[str, Any],
    timeout_ms: int,
) -> list[Item]:
    raw = await page.evaluate(
        """
        () => {
          const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
          const rows = [];
          const seen = new Set();

          for (const a of document.querySelectorAll('a[href]')) {
            const href = a.href || '';
            const match = href.match(/\\/(?:ko\\/)?o\\/(\\d+)(?:[/?#]|$)/i);
            if (!match || seen.has(match[1])) continue;
            seen.add(match[1]);
            rows.push({
              id: match[1],
              href,
              anchor_text: clean(a.innerText || a.textContent || '')
            });
          }
          return rows;
        }
        """
    )

    output: list[Item] = []
    for row in raw[: int(source.get("max_items", 100))]:
        detail = await read_detail(detail_page, row["href"], timeout_ms)
        if not detail or is_closed_text(detail):
            continue

        title = ""
        try:
            title = clean_title(
                await detail_page.locator("h1").first.inner_text(timeout=2500)
            )
        except Exception:
            pass

        if not title or title.casefold() in GENERIC_NAV_TITLES:
            try:
                meta_title = await detail_page.locator(
                    'meta[property="og:title"]'
                ).get_attribute("content")
                title = clean_title(meta_title or "")
            except Exception:
                pass

        if not title or title.casefold() in GENERIC_NAV_TITLES:
            title = clean_title(row.get("anchor_text", ""))

        if not title or title.casefold() in GENERIC_NAV_TITLES:
            continue
        if title in {"Gen.G", "How We Work", "Work With Us", "FAQ"}:
            continue

        posted_at, deadline = extract_dates(detail)
        if is_expired(deadline):
            continue

        output.append(
            Item(
                item_id=f"{source['id']}:greeting:{row['id']}",
                source_id=source["id"],
                category=source["category"],
                source_name=source["name"],
                title=title,
                url=canonical_url(row["href"]),
                context=detail[:1000],
                matched="새 공고",
                posted_at=posted_at,
                deadline=deadline,
            )
        )

    return output


async def extract_generic_candidates(page: Page) -> list[dict[str, str]]:
    return await page.evaluate(
        """
        () => {
          const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
          const out = [];
          const seen = new Set();

          for (const a of document.querySelectorAll('a[href]')) {
            const href = a.href || '';
            const box = a.closest(
              'article, li, tr, [class*="job"], [class*="Job"], ' +
              '[class*="recruit"], [class*="Recruit"], [class*="position"], ' +
              '[class*="Position"], [class*="posting"], [class*="Posting"], ' +
              '[class*="card"], [class*="Card"], [class*="announce"]'
            ) || a.parentElement;
            const titleNode = box && box.querySelector(
              'h1,h2,h3,h4,h5,strong,b,[class*="title"],[class*="Title"],[class*="name"]'
            );
            const title = clean(
              (titleNode && titleNode.innerText) ||
              a.innerText ||
              a.textContent ||
              a.getAttribute('aria-label') ||
              a.title
            );
            const context = clean(box ? box.innerText : title);
            if (!title || !href) continue;
            const key = href + '|' + title;
            if (seen.has(key)) continue;
            seen.add(key);
            out.push({href, title, context});
          }
          return out;
        }
        """
    )


async def parse_generic(
    page: Page,
    detail_page: Page,
    source: dict[str, Any],
    timeout_ms: int,
) -> list[Item]:
    raw = await extract_generic_candidates(page)
    include_pattern = source.get("link_include_regex", "")
    include_re = re.compile(include_pattern, re.I) if include_pattern else None
    base_domain = urlparse(source["url"]).netloc.lower()
    accept_external = bool(source.get("accept_external_links"))
    output: list[Item] = []
    seen_ids: set[str] = set()
    detail_budget = int(source.get("detail_check_limit", 20))

    for row in raw:
        title = clean_title(row.get("title", ""))
        context = normalize(row.get("context", ""))
        url = canonical_url(urljoin(source["url"], row.get("href", "")))

        if not title or title.casefold() in GENERIC_NAV_TITLES:
            continue
        if len(title) < 3 or len(title) > 350:
            continue
        if not url.startswith(("https://", "http://")) or is_blocked_url(url):
            continue
        if include_re and not include_re.search(url):
            continue
        if not accept_external and urlparse(url).netloc.lower() != base_domain:
            continue
        if contains_excluded(f"{title} {context}", source):
            continue
        if is_closed_text(f"{title} {context}"):
            continue

        combined = f"{title} {context}"
        matched = ""
        mode = source.get("mode", "new")

        if mode == "keyword":
            matched = keyword_match(combined, source.get("keywords", []))
            if not matched and source.get("detail_check") and detail_budget > 0:
                detail_budget -= 1
                detail = await read_detail(detail_page, url, timeout_ms)
                combined = f"{combined} {detail}"
                matched = keyword_match(combined, source.get("keywords", []))
            if not matched:
                continue
        elif mode == "criteria":
            matched = location_criteria_match(combined, source)
            if not matched and source.get("detail_check") and detail_budget > 0:
                detail_budget -= 1
                detail = await read_detail(detail_page, url, timeout_ms)
                combined = f"{combined} {detail}"
                matched = location_criteria_match(combined, source)
            if not matched:
                continue
        else:
            matched = "새 공고"

        posted_at, deadline = extract_dates(combined)
        if is_expired(deadline):
            continue

        item_id = make_item_id(source["id"], url, title, posted_at)
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        output.append(
            Item(
                item_id=item_id,
                source_id=source["id"],
                category=source["category"],
                source_name=source["name"],
                title=title,
                url=url,
                context=normalize(combined)[:1000],
                matched=matched,
                posted_at=posted_at,
                deadline=deadline,
            )
        )

    return output[: int(source.get("max_items", 200))]


async def crawl_source(
    browser: Browser,
    source: dict[str, Any],
    settings: dict[str, Any],
) -> tuple[list[Item], str]:
    context = await browser.new_context(
        locale="ko-KR",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 1100},
    )
    page = await context.new_page()
    detail_page = await context.new_page()
    timeout_ms = int(settings.get("browser_timeout_ms", 45000))
    page.set_default_timeout(timeout_ms)
    detail_page.set_default_timeout(timeout_ms)

    try:
        wait_until = source.get("wait_until", "domcontentloaded")
        navigation_timeout_ms = int(source.get("navigation_timeout_ms", timeout_ms))
        try:
            await page.goto(
                source["url"],
                wait_until=wait_until,
                timeout=navigation_timeout_ms,
            )
        except PlaywrightTimeoutError:
            if not source.get("allow_partial_load") or page.url == "about:blank":
                raise

        await page.wait_for_timeout(int(source.get("wait_ms", 4000)))
        await dismiss_popups(page)
        await auto_scroll(page)

        body_preview = ""
        try:
            body_preview = normalize(
                await page.locator("body").inner_text(timeout=5000)
            )[:3000]
        except Exception:
            pass

        if re.search(
            r"(Access Denied|접근이 제한|비정상적인 접근|CAPTCHA|로봇이 아닙니다)",
            body_preview,
            re.I,
        ):
            raise RuntimeError("사이트의 자동접속 차단 화면이 표시됨")

        parser = source.get("parser", "generic")
        if parser == "t1_body":
            items = await parse_t1_body(page, source)
        elif parser == "ccon_board":
            items = await parse_ccon_board(page, source)
        elif parser == "jobkorea_company":
            items = await parse_jobkorea_company(page, detail_page, source, timeout_ms)
        elif parser == "saramin_company":
            items = await parse_saramin_company(page, detail_page, source, timeout_ms)
        elif parser == "greetinghr":
            items = await parse_greetinghr(page, detail_page, source, timeout_ms)
        else:
            items = await parse_generic(page, detail_page, source, timeout_ms)

        unique: dict[str, Item] = {}
        for item in items:
            unique[item.item_id] = item
        return list(unique.values()), ""

    except PlaywrightTimeoutError as exc:
        return [], f"시간 초과: {exc}"
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    finally:
        await context.close()


def find_credentials(settings: dict[str, Any]) -> tuple[str, str, str]:
    user = os.getenv("SMTP_USER") or os.getenv("EMAIL_USER") or os.getenv("GMAIL_USER") or ""
    password = (
        os.getenv("SMTP_PASSWORD")
        or os.getenv("EMAIL_PASSWORD")
        or os.getenv("GMAIL_APP_PASSWORD")
        or ""
    )
    recipient = os.getenv("EMAIL_TO") or os.getenv("MAIL_TO") or settings.get("recipient", "")
    return user, password, recipient


def format_kst(value: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def send_email(
    settings: dict[str, Any],
    items_with_first_seen: list[tuple[Item, str]],
    errors: list[str],
    test: bool = False,
) -> None:
    smtp_user, smtp_password, recipient = find_credentials(settings)
    if not smtp_user or not smtp_password or not recipient:
        raise RuntimeError("SMTP_USER, SMTP_PASSWORD, EMAIL_TO 설정을 확인하세요.")

    now_text = now_kst().strftime("%Y-%m-%d %H:%M")
    prefix = settings.get("subject_prefix", "[공고 수집기]")
    subject = f"{prefix} {'테스트 성공' if test else f'새 공고 {len(items_with_first_seen)}건'} · {now_text}"

    grouped: dict[str, list[tuple[Item, str]]] = {}
    for item, first_seen in items_with_first_seen:
        grouped.setdefault(item.category, []).append((item, first_seen))

    blocks: list[str] = []
    for category in ["게임사", "공공기관", "이스포츠 구단", "기타"]:
        rows = grouped.get(category, [])
        if not rows:
            continue
        cards: list[str] = []
        for item, first_seen in rows:
            meta = []
            if item.posted_at:
                meta.append(f"게시일: {html.escape(item.posted_at)}")
            if item.deadline:
                meta.append(f"마감일: {html.escape(item.deadline)}")
            meta.append(f"최초 발견: {html.escape(format_kst(first_seen))}")
            if item.matched:
                meta.append(f"감지 기준: {html.escape(item.matched)}")

            cards.append(
                f"""
                <div style="border:1px solid #e4e7ec;border-radius:10px;padding:14px 16px;margin:10px 0;background:#fff">
                  <div style="font-size:12px;color:#667085">{html.escape(item.source_name)}</div>
                  <div style="font-size:16px;font-weight:700;margin:5px 0 9px">{html.escape(item.title)}</div>
                  <a href="{html.escape(item.url)}" style="color:#175cd3;text-decoration:none">공고 열기</a>
                  <div style="font-size:12px;color:#667085;margin-top:8px;line-height:1.7">
                    {"<br>".join(meta)}
                  </div>
                </div>
                """
            )
        blocks.append(
            f"<h2 style='font-size:18px;margin:25px 0 8px'>{html.escape(category)}</h2>"
            + "".join(cards)
        )

    if test:
        blocks.append(
            "<div style='padding:16px;background:#f0fdf4;border-radius:10px'>"
            "v9 메일 연결이 정상입니다. 실제 알림은 새 공고가 감지될 때만 발송됩니다."
            "</div>"
        )

    error_html = ""
    if errors and settings.get("send_error_email"):
        error_html = (
            "<h3>수집 오류</h3><pre style='white-space:pre-wrap'>"
            + html.escape("\n".join(errors))
            + "</pre>"
        )

    body = f"""
    <html><body style="margin:0;background:#f7f8fa;font-family:Arial,'Noto Sans KR',sans-serif;color:#101828">
      <div style="max-width:720px;margin:0 auto;padding:28px 18px">
        <h1 style="font-size:23px;margin:0 0 5px">맞춤형 채용 공고 알림 v9</h1>
        <div style="color:#667085;font-size:13px">{html.escape(now_text)}</div>
        {''.join(blocks)}
        {error_html}
        <div style="font-size:12px;color:#98a2b3;margin-top:30px">
          공고별 고유번호를 영구 저장해 제목의 'N/새글' 표시가 사라져도 중복 발송하지 않습니다.
        </div>
      </div>
    </body></html>
    """

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = smtp_user
    message["To"] = recipient
    message.attach(MIMEText("새 채용 공고가 감지되었습니다.", "plain", "utf-8"))
    message.attach(MIMEText(body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, [recipient], message.as_string())


def empty_state() -> dict[str, Any]:
    return {"version": 3, "sources": {}, "last_run": None}


def migrate_or_reset_state(raw: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(raw, dict) or raw.get("version") != 3:
        return empty_state(), True
    raw.setdefault("sources", {})
    return raw, False


async def run(args: argparse.Namespace) -> int:
    sources: list[dict[str, Any]] = load_json(SOURCES_PATH)
    settings: dict[str, Any] = load_json(SETTINGS_PATH)
    state_path = ROOT / settings.get("state_file", "data/state.json")
    report_path = ROOT / settings.get("report_file", "data/last_run_report.json")

    if args.test_email:
        send_email(settings, [], [], test=True)
        print("[메일] v9 테스트 메일 발송 완료")
        return 0

    raw_state = load_json(state_path) if state_path.exists() else empty_state()
    state, reset_due_to_version = migrate_or_reset_state(raw_state)
    if reset_due_to_version:
        print("[상태] 이전 버전 상태를 감지해 v9 기준값으로 초기화합니다.")

    if args.reset_baseline:
        state = empty_state()

    if args.notify_t1_current:
        state.setdefault("sources", {}).pop("t1", None)

    all_new: list[tuple[Item, str]] = []
    errors: list[str] = []
    report_sources: list[dict[str, Any]] = []
    timestamp = now_iso()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])

        for source in sources:
            source_id = source["id"]
            print(f"[수집] {source['category']} / {source['name']} / {source.get('parser', 'generic')}", flush=True)
            items, error = await crawl_source(browser, source, settings)

            previous_state = state.get("sources", {}).get(source_id)
            previous_active = set((previous_state or {}).get("active_ids", []))
            previous_seen = dict((previous_state or {}).get("seen_ever", {}))

            if error:
                errors.append(f"{source['name']}: {error}")
                report_sources.append({
                    "source_id": source_id,
                    "name": source["name"],
                    "status": "error",
                    "error": error,
                    "current_count": 0,
                    "new_count": 0,
                })
                print(f"  [오류] {error}", flush=True)
                continue

            current_ids = {item.item_id for item in items}
            first_source_run = previous_state is None
            force_baseline = args.reset_baseline
            notify_first = (
                (
                    source_id in settings.get("notify_existing_on_first_run_sources", [])
                    or (args.notify_t1_current and source_id == "t1")
                )
                and not force_baseline
            )

            new_items: list[tuple[Item, str]] = []
            seen_ever = previous_seen

            for item in items:
                existing = seen_ever.get(item.item_id)
                if existing:
                    first_seen = existing.get("first_seen_at", timestamp)
                else:
                    first_seen = timestamp
                    if not first_source_run or notify_first:
                        new_items.append((item, first_seen))

                seen_ever[item.item_id] = item.to_state(
                    first_seen_at=first_seen,
                    last_seen_at=timestamp,
                )

            suspicious = False
            suspicious_reason = ""
            if previous_active and not current_ids:
                suspicious = True
                suspicious_reason = "이전 공고가 있었지만 이번 수집 결과가 0건"
            elif previous_active:
                ratio = len(current_ids) / max(len(previous_active), 1)
                threshold = float(settings.get("suspicious_drop_ratio", 0.2))
                if ratio < threshold:
                    suspicious = True
                    suspicious_reason = f"공고 수 급감({len(previous_active)}→{len(current_ids)})"

            # 과거에 본 ID는 절대 삭제하지 않는다.
            # 의심스러운 수집 결과일 때 active_ids도 이전 값을 보존해 다음 실행의 대량 오탐을 막는다.
            active_ids = sorted(previous_active | current_ids) if suspicious else sorted(current_ids)

            state.setdefault("sources", {})[source_id] = {
                "name": source["name"],
                "category": source["category"],
                "url": source["url"],
                "parser": source.get("parser", "generic"),
                "seen_ever": seen_ever,
                "active_ids": active_ids,
                "last_success_at": timestamp,
                "last_count": len(items),
                "last_warning": suspicious_reason,
            }

            all_new.extend(new_items)
            report_sources.append({
                "source_id": source_id,
                "name": source["name"],
                "status": "warning" if suspicious else "success",
                "warning": suspicious_reason,
                "current_count": len(items),
                "new_count": len(new_items),
                "items": [
                    {
                        "item_id": item.item_id,
                        "title": item.title,
                        "posted_at": item.posted_at,
                        "deadline": item.deadline,
                    }
                    for item in items
                ],
            })

            if first_source_run and not notify_first:
                print(f"  [기준값] 현재 {len(items)}건 저장 · 메일 없음", flush=True)
            elif first_source_run and notify_first:
                print(f"  [초기 알림 대상] 현재 활성 공고 {len(new_items)}건", flush=True)
            else:
                print(f"  [완료] 현재 {len(items)}건 / 신규 {len(new_items)}건", flush=True)
            if suspicious:
                print(f"  [보호 작동] {suspicious_reason} · 기존 상태 유지", flush=True)

        await browser.close()

    state["last_run"] = timestamp
    state["last_errors"] = errors

    report = {
        "version": 1,
        "run_at": timestamp,
        "new_count": len(all_new),
        "error_count": len(errors),
        "sources": report_sources,
    }
    save_json(report_path, report)

    if args.diagnostic:
        # 진단 실행은 실제 기준값을 바꾸지 않는다.
        # 따라서 진단 후 normal을 실행해도 최초 알림 정책(T1)이 그대로 적용된다.
        print("[진단] 메일·기준값 변경 없이 수집 보고서만 저장했습니다.")
    else:
        save_json(state_path, state)
        if all_new:
            send_email(settings, all_new, errors)
            print(f"[메일] 신규 공고 {len(all_new)}건 발송 완료")
        else:
            print("[메일] 신규 공고 없음 · 발송하지 않음")

    for error in errors:
        print(f"[주의] {error}", file=sys.stderr)
    return 0


def self_test() -> None:
    assert clean_title("공고 N 새글") == "공고"
    assert normalize_date_string("2026.7.3") == "2026-07-03"
    assert normalize_date_string("2026. 07. 13") == "2026-07-13"
    assert extract_unique_token("https://x.test/bbs/board.php?bo_table=rnt&wr_id=373") == "wr_id:373"
    assert extract_unique_token("https://www.jobkorea.co.kr/Recruit/GI_Read/46957648") == "path:46957648"
    one = make_item_id("ccon", "https://x.test/?wr_id=373", "공고 N 새글")
    two = make_item_id("ccon", "https://x.test/?wr_id=373", "공고")
    assert one == two
    assert keyword_match("IP 이스포츠 사업", ["IP이스포츠"]) == "IP이스포츠"
    print("[자체 점검] 핵심 ID·제목·날짜 규칙 정상")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="맞춤형 e스포츠 채용 공고 수집기 v9")
    parser.add_argument("--test-email", action="store_true")
    parser.add_argument("--reset-baseline", action="store_true")
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--notify-t1-current", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    parsed = parse_args()
    if parsed.self_test:
        self_test()
        raise SystemExit(0)
    raise SystemExit(asyncio.run(run(parsed)))
