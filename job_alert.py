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

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


ROOT = Path(__file__).resolve().parent
KST = timezone(timedelta(hours=9))
SOURCES_PATH = ROOT / "config" / "sources.json"
SETTINGS_PATH = ROOT / "config" / "settings.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "trk", "trackingId", "refId", "origin",
    "originToLandingJobPostings",
}
NAV_TITLES = {
    "home", "홈", "about", "회사소개", "login", "로그인", "menu", "메뉴",
    "more", "더보기", "view all", "전체보기", "apply", "지원하기",
    "privacy", "개인정보처리방침", "terms", "이용약관", "list", "목록",
    "prev", "next", "이전", "다음", "careers", "career", "jobs", "job",
    "recruit", "채용", "채용정보", "전체 채용정보", "신입공채", "헤드헌팅",
    "기업정보 게시물", "연봉정보 게시물", "jobkorea", "사람인",
    "Gen.G", "How We Work", "Work With Us", "FAQ",
}
CLOSED_PATTERNS = [
    r"마감된\s*채용공고", r"채용이\s*마감", r"마감되었습니다",
    r"접수기간이\s*종료", r"지원기간이\s*종료", r"종료된\s*공고",
    r"접수\s*마감",
]
BLOCK_PATTERNS = [
    r"Access Denied", r"접근이 제한", r"비정상적인 접근",
    r"CAPTCHA", r"로봇이 아닙니다", r"Too Many Requests",
]


@dataclass
class Item:
    item_id: str
    source_id: str
    category: str
    source_name: str
    title: str
    url: str
    posted_at: str = ""
    deadline: str = ""
    matched: str = ""


@dataclass
class ParseResult:
    verified: bool
    items: list[Item]
    method: str
    message: str = ""


def now_kst() -> datetime:
    return datetime.now(timezone.utc).astimezone(KST)


def now_iso() -> str:
    return now_kst().isoformat(timespec="seconds")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def normalize(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_title(text: str) -> str:
    value = normalize(text)
    value = re.sub(r"(?i)(^|\s)(NEW|N|새글)(?=\s|$)", " ", value)
    return re.sub(r"\s+", " ", value).strip(" -|·")


def folded(text: str) -> str:
    return re.sub(r"[\s\-_–—·]+", "", normalize(text)).casefold()


def canonical_url(url: str) -> str:
    parsed = urlparse(url or "")
    query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k not in TRACKING_PARAMS
    ]
    return urlunparse(parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        query=urlencode(query, doseq=True),
        fragment="",
    ))


def normalize_date(value: str) -> str:
    match = re.search(
        r"(20\d{2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})",
        value or "",
    )
    if not match:
        return ""
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def parsed_date(value: str) -> date | None:
    try:
        return datetime.strptime(normalize_date(value), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def is_expired(deadline: str) -> bool:
    value = parsed_date(deadline)
    return bool(value and value < now_kst().date())


def is_closed(text: str) -> bool:
    return any(re.search(pattern, text or "", re.I) for pattern in CLOSED_PATTERNS)


def is_blocked(text: str) -> bool:
    return any(re.search(pattern, text or "", re.I) for pattern in BLOCK_PATTERNS)


def keyword_hit(text: str, keywords: list[str]) -> str:
    haystack = folded(text)
    for word in keywords:
        if folded(word) in haystack:
            return word
    return ""


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def slug(text: str) -> str:
    value = re.sub(r"[^0-9A-Za-z가-힣]+", "-", normalize(text)).strip("-").lower()
    return value[:100] or short_hash(text)


def item_id_from_url(source_id: str, url: str, title: str = "") -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key in ("wr_id", "rec_idx", "nttId", "brd_id", "jobId", "id"):
        if query.get(key):
            return f"{source_id}:{key.lower()}:{query[key]}"

    patterns = [
        r"/Recruit/GI_Read/(\d+)",
        r"/(?:ko/)?o/(\d+)",
        r"/jobs/view/([^/?#]+)",
        r"/positions?/([^/?#]+)",
        r"/jobs?/([^/?#]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, parsed.path, re.I)
        if match:
            return f"{source_id}:path:{match.group(1)}"

    return f"{source_id}:url:{short_hash(canonical_url(url) + '|' + clean_title(title))}"


def extract_dates(text: str) -> tuple[str, str]:
    text = normalize(text)
    dates = re.findall(
        r"20\d{2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2}",
        text,
    )
    posted = ""
    deadline = ""

    posted_match = re.search(
        r"(?:등록일|게시일|작성일|공고일|접수기간)\s*[:：]?\s*"
        r"(20\d{2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2})",
        text,
        re.I,
    )
    if posted_match:
        posted = normalize_date(posted_match.group(1))

    deadline_match = re.search(
        r"(?:마감일|접수마감|지원마감|종료일|~)\s*[:：]?\s*"
        r"(20\d{2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2})",
        text,
        re.I,
    )
    if deadline_match:
        deadline = normalize_date(deadline_match.group(1))

    if not posted and dates:
        posted = normalize_date(dates[0])
    if not deadline and len(dates) >= 2:
        deadline = normalize_date(dates[-1])
    return posted, deadline


def healthy_html(html_text: str, expected_markers: list[str]) -> bool:
    text = normalize(BeautifulSoup(html_text, "lxml").get_text(" ", strip=True))
    if len(text) < 100 or is_blocked(text):
        return False
    if expected_markers and not any(folded(marker) in folded(text) for marker in expected_markers):
        return False
    return True


def request_html(url: str, timeout_seconds: int) -> tuple[str, str]:
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=timeout_seconds,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.text, response.url


async def browser_html(browser, url: str, timeout_ms: int) -> tuple[str, str]:
    context = await browser.new_context(
        locale="ko-KR",
        user_agent=HEADERS["User-Agent"],
        viewport={"width": 1440, "height": 1100},
    )
    page = await context.new_page()

    async def route_handler(route):
        if route.request.resource_type in {"image", "font", "media"}:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", route_handler)
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            if page.url == "about:blank":
                raise
        await page.wait_for_selector("body", state="attached", timeout=20000)
        await page.wait_for_timeout(4500)
        for _ in range(5):
            height = await page.evaluate("() => document.body ? document.body.scrollHeight : 0")
            if not height:
                break
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(500)
        return await page.content(), page.url
    finally:
        await context.close()


async def fetch_source(browser, source: dict[str, Any], settings: dict[str, Any]) -> tuple[str, str, str]:
    request_error = ""
    browser_error = ""

    if not source.get("dynamic"):
        try:
            html_text, final_url = await asyncio.to_thread(
                request_html,
                source["url"],
                int(settings.get("request_timeout_seconds", 30)),
            )
            if len(html_text) >= 200 and not is_blocked(html_text):
                return html_text, final_url, "requests"
        except Exception as exc:
            request_error = f"{type(exc).__name__}: {exc}"

    try:
        html_text, final_url = await browser_html(
            browser,
            source["url"],
            int(settings.get("browser_timeout_ms", 50000)),
        )
        if is_blocked(BeautifulSoup(html_text, "lxml").get_text(" ", strip=True)):
            raise RuntimeError("자동접속 차단 화면")
        return html_text, final_url, "browser"
    except Exception as exc:
        browser_error = f"{type(exc).__name__}: {exc}"

    if source.get("dynamic"):
        try:
            html_text, final_url = await asyncio.to_thread(
                request_html,
                source["url"],
                int(settings.get("request_timeout_seconds", 30)),
            )
            if len(html_text) >= 200 and not is_blocked(html_text):
                return html_text, final_url, "requests-fallback"
        except Exception as exc:
            request_error = f"{type(exc).__name__}: {exc}"

    raise RuntimeError(
        f"요청 실패 [{request_error or '미시도'}] / 브라우저 실패 [{browser_error or '미시도'}]"
    )



def nearest_context(anchor, markers: list[str] | None = None, max_levels: int = 8) -> str:
    """링크에서 위로 올라가며 가장 작은 공고 카드 문맥을 찾는다."""
    markers = markers or []
    node = anchor
    fallback = clean_title(anchor.get_text(" ", strip=True))

    for _ in range(max_levels):
        node = getattr(node, "parent", None)
        if node is None:
            break

        candidate = normalize(node.get_text(" ", strip=True))
        if not candidate:
            continue
        if len(candidate) <= 1600:
            fallback = candidate
        if markers and any(marker in candidate for marker in markers):
            return candidate

    return fallback


def visible_title_from_anchor(anchor) -> str:
    title = clean_title(anchor.get_text(" ", strip=True))
    if title and title.casefold() not in NAV_TITLES:
        return title

    container = anchor.find_parent(["li", "tr", "article", "section", "div"])
    if container:
        heading = container.find(["h1", "h2", "h3", "h4", "strong", "b"])
        if heading:
            return clean_title(heading.get_text(" ", strip=True))
    return ""


def parse_krafton(source: dict[str, Any], html_text: str, base_url: str) -> ParseResult:
    """
    크래프톤은 공고 카드의 '제목 자체'에 Esports가 있을 때만 감지한다.
    상위 컨테이너에 있는 Esports 문구 때문에 TOP/Family Site가 잡히는 것을 차단한다.
    """
    soup = BeautifulSoup(html_text, "lxml")
    body_text = normalize(soup.get_text(" ", strip=True))
    if len(body_text) < 300 or "KRAFTON" not in body_text.upper():
        return ParseResult(False, [], "krafton-strict", "크래프톤 채용 페이지 본문 검증 실패")

    candidates = []
    seen_urls = set()

    for anchor in soup.find_all("a", href=True):
        url = canonical_url(urljoin(base_url, anchor.get("href", "")))
        parsed = urlparse(url)
        path = parsed.path.rstrip("/").lower()

        if "/careers/jobs" not in path:
            continue
        if path in {"/careers/jobs", "/en/careers/jobs", "/ko/careers/jobs"}:
            continue

        title = visible_title_from_anchor(anchor)
        if not title or title.casefold() in NAV_TITLES:
            continue
        if title.casefold() in {"top", "family site", "bookmarks"}:
            continue
        if len(title) < 5 or len(title) > 300:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        candidates.append((title, url))

    # 실제 공고 링크 구조가 검색 페이지 개편으로 달라졌을 때의 보조 규칙
    if not candidates:
        for anchor in soup.find_all("a", href=True):
            title = visible_title_from_anchor(anchor)
            if not title or title.casefold() in NAV_TITLES:
                continue
            if title.casefold() in {"top", "family site", "bookmarks"}:
                continue
            context = nearest_context(anchor)
            if not re.search(r"(Regular|Contractor|Intern|정규|계약|인턴)", context, re.I):
                continue
            url = canonical_url(urljoin(base_url, anchor.get("href", "")))
            if url in seen_urls:
                continue
            seen_urls.add(url)
            candidates.append((title, url))

    if not candidates:
        return ParseResult(False, [], "krafton-strict", "실제 채용 카드 구조를 확인하지 못함")

    items = []
    for title, url in candidates:
        matched = keyword_hit(title, source.get("keywords", []))
        if not matched:
            continue
        items.append(Item(
            item_id=item_id_from_url(source["id"], url, title),
            source_id=source["id"],
            category=source["category"],
            source_name=source["name"],
            title=title,
            url=url,
            matched=matched,
        ))

    return ParseResult(
        True,
        items,
        "krafton-strict",
        f"실제 채용 카드 {len(candidates)}건 중 키워드 일치 {len(items)}건",
    )


def parse_kespa(source: dict[str, Any], html_text: str, base_url: str) -> ParseResult:
    """KeSPA 채용 분류에서 실제 notice_view 게시물 번호만 사용한다."""
    soup = BeautifulSoup(html_text, "lxml")
    anchors = soup.find_all("a", href=re.compile(r"/news/notice_view\?[^#]*brd_id=", re.I))

    if not anchors:
        return ParseResult(False, [], "kespa-strict", "notice_view 채용 게시물 링크를 찾지 못함")

    items = []
    seen = set()

    for anchor in anchors:
        url = canonical_url(urljoin(base_url, anchor.get("href", "")))
        query = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
        post_id = query.get("brd_id", "")
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)

        title = visible_title_from_anchor(anchor)
        if not title:
            continue

        # 실제 모집 원공고만 남기고 결과/전형 공지는 제외
        if "채용" not in title or "공고" not in title:
            continue
        if any(word in title for word in ["최종 결과", "최종결과", "합격", "서류전형", "면접전형", "결과공지"]):
            continue

        context = nearest_context(anchor)
        posted, deadline = extract_dates(context)

        items.append(Item(
            item_id=f"kespa:brd-id:{post_id}",
            source_id=source["id"],
            category=source["category"],
            source_name=source["name"],
            title=title,
            url=url,
            posted_at=posted,
            deadline=deadline,
            matched="채용 공고",
        ))

    return ParseResult(True, items, "kespa-strict", f"채용 원공고 {len(items)}건")


def parse_hanwha(source: dict[str, Any], html_text: str, base_url: str) -> ParseResult:
    """
    무거운 한화인 포털 대신 같은 한화인 공식 도메인의 구형 정적 채용 목록을 읽는다.
    rtSeq가 있는 실제 채용공고만 사용한다.
    """
    soup = BeautifulSoup(html_text, "lxml")
    body_text = normalize(soup.get_text(" ", strip=True))
    anchors = soup.find_all(
        "a",
        href=re.compile(r"(?:view|view_editor)\.do\?[^#]*rtSeq=\d+", re.I),
    )

    if not anchors:
        return ParseResult(False, [], "hanwha-static", "한화인 rtSeq 채용공고 링크를 찾지 못함")

    items = []
    seen = set()

    for anchor in anchors:
        url = canonical_url(urljoin(base_url, anchor.get("href", "")))
        match = re.search(r"(?:\?|&)rtSeq=(\d+)", url, re.I)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))

        title = visible_title_from_anchor(anchor)
        if not title:
            continue
        # 한화 키워드는 공고 제목 자체에서만 판정한다.
        # 상위 목록 전체에 e-스포츠 공고가 하나 있을 때 다른 공고까지 잡히는 것을 방지한다.
        container = anchor.find_parent(["li", "tr", "article"])
        context = normalize(
            container.get_text(" ", strip=True) if container else title
        )

        matched = keyword_hit(title, source.get("keywords", []))
        if not matched:
            continue

        posted, deadline = extract_dates(context)
        if is_expired(deadline):
            continue

        items.append(Item(
            item_id=f"hanwha:rtseq:{match.group(1)}",
            source_id=source["id"],
            category=source["category"],
            source_name=source["name"],
            title=title,
            url=url,
            posted_at=posted,
            deadline=deadline,
            matched=matched,
        ))

    return ParseResult(
        True,
        items,
        "hanwha-static",
        f"한화인 실제 공고 {len(seen)}건 중 e-스포츠 일치 {len(items)}건",
    )


def decode_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value.replace(r"\/", "/").replace(r"\"", '"')


def parse_tencent(source: dict[str, Any], html_text: str, base_url: str) -> ParseResult:
    """
    Tencent 검색 페이지의 실제 링크와 내장 JSON(postId/postName)을 함께 읽는다.
    구조를 확인하지 못하면 0건으로 덮어쓰지 않고 unverified로 남긴다.
    """
    soup = BeautifulSoup(html_text, "lxml")
    body_text = normalize(soup.get_text(" ", strip=True))

    items_by_id: dict[str, Item] = {}

    # 1) 화면에 렌더링된 상세 링크
    link_pattern = re.compile(
        r"(?:jobdesc|jobopportunity|postId=|weekdayJdUid=|position)",
        re.I,
    )
    for anchor in soup.find_all("a", href=link_pattern):
        url = canonical_url(urljoin(base_url, anchor.get("href", "")))
        title = visible_title_from_anchor(anchor)
        if not title or title.casefold() in NAV_TITLES:
            continue
        if len(title) < 5 or len(title) > 300:
            continue

        context = nearest_context(anchor)
        posted, deadline = extract_dates(context)
        item_id = item_id_from_url(source["id"], url, title)

        items_by_id[item_id] = Item(
            item_id=item_id,
            source_id=source["id"],
            category=source["category"],
            source_name=source["name"],
            title=title,
            url=url,
            posted_at=posted,
            deadline=deadline,
            matched="새 공고",
        )

    # 2) Next/Vue 내장 JSON에서 postId + postName/positionName/jobTitle 추출
    raw_html = html_text
    id_matches = list(re.finditer(r'"(?:postId|weekdayJdUid|positionId)"\s*:\s*"?(\d+)"?', raw_html))
    for id_match in id_matches:
        post_id = id_match.group(1)
        window = raw_html[max(0, id_match.start() - 1400): min(len(raw_html), id_match.end() + 2200)]

        title_match = re.search(
            r'"(?:postName|positionName|jobTitle|title)"\s*:\s*"((?:\\.|[^"\\]){5,300})"',
            window,
            re.I,
        )
        if not title_match:
            continue

        title = clean_title(decode_json_string(title_match.group(1)))
        if not title or title.casefold() in NAV_TITLES:
            continue
        if re.search(r"^(Search|Jobs|Filter|Tencent Careers)$", title, re.I):
            continue

        date_match = re.search(
            r'"(?:publishDate|postDate|createTime|date)"\s*:\s*"([^"]+)"',
            window,
            re.I,
        )
        posted = normalize_date(date_match.group(1)) if date_match else ""
        detail_url = f"https://careers.tencent.com/en-us/search.html?weekdayJdUid={post_id}"
        item_id = f"tencent:post:{post_id}"

        items_by_id[item_id] = Item(
            item_id=item_id,
            source_id=source["id"],
            category=source["category"],
            source_name=source["name"],
            title=title,
            url=detail_url,
            posted_at=posted,
            matched="새 공고",
        )

    items = list(items_by_id.values())

    if items:
        return ParseResult(True, items, "tencent-hybrid", f"Tencent 실제 공고 {len(items)}건")

    if re.search(r"(Showing\s+0\s+results|No results found|No jobs|No position)", body_text, re.I):
        return ParseResult(True, [], "tencent-hybrid", "Tencent 검색 결과 0건")

    if re.search(r"Showing\s+\d+\s*-\s*\d+\s+of\s+\d+\s+jobs", body_text, re.I):
        return ParseResult(False, [], "tencent-hybrid", "검색 결과는 있으나 공고 ID/제목 추출 실패")

    return ParseResult(False, [], "tencent-hybrid", "Tencent 검색 페이지 구조를 검증하지 못함")



def parse_bing_rss(source: dict[str, Any], xml_text: str) -> ParseResult:
    """
    JavaScript/접속 제한이 심한 공식 사이트를 위한 보조 감시.
    Bing RSS에서 공식 도메인 결과만 허용하며, 타 도메인은 전부 폐기한다.
    """
    soup = BeautifulSoup(xml_text, "xml")
    channel = soup.find("channel")
    if channel is None:
        return ParseResult(False, [], "official-domain-rss", "RSS 채널 형식을 확인하지 못함")

    allowed_domain = source.get("allowed_domain", "").lower()
    required_words = source.get("rss_required_words", [])
    excluded_words = source.get("rss_excluded_words", [])

    items = []
    seen = set()

    for entry in channel.find_all("item"):
        title = clean_title(entry.title.get_text(" ", strip=True) if entry.title else "")
        url = canonical_url(entry.link.get_text(" ", strip=True) if entry.link else "")
        description = normalize(
            entry.description.get_text(" ", strip=True) if entry.description else ""
        )
        combined = f"{title} {description}"

        if not title or not url:
            continue
        if allowed_domain and urlparse(url).netloc.lower().replace("www.", "") != allowed_domain.replace("www.", ""):
            continue
        if required_words and not all(folded(word) in folded(combined) for word in required_words):
            continue
        if any(folded(word) in folded(combined) for word in excluded_words):
            continue

        item_id = item_id_from_url(source["id"], url, title)
        if item_id in seen:
            continue
        seen.add(item_id)

        items.append(Item(
            item_id=item_id,
            source_id=source["id"],
            category=source["category"],
            source_name=source["name"],
            title=title,
            url=url,
            matched="공식 도메인 검색색인",
        ))

    return ParseResult(
        True,
        items,
        "official-domain-rss",
        f"공식 도메인 검색색인 {len(items)}건 · 직접 사이트보다 반영이 늦을 수 있음",
    )


def request_bing_rss(source: dict[str, Any], timeout_seconds: int) -> ParseResult:
    response = requests.get(
        "https://www.bing.com/search",
        params={"q": source["rss_query"], "format": "rss"},
        headers=HEADERS,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return parse_bing_rss(source, response.text)


def parse_tencent_api_payload(source: dict[str, Any], payload: Any) -> ParseResult:
    if not isinstance(payload, dict):
        return ParseResult(False, [], "tencent-official-api", "API 응답이 JSON 객체가 아님")

    data = payload.get("Data")
    if not isinstance(data, dict):
        return ParseResult(False, [], "tencent-official-api", "API Data 필드를 확인하지 못함")

    posts = data.get("Posts")
    if not isinstance(posts, list):
        return ParseResult(False, [], "tencent-official-api", "API Posts 목록을 확인하지 못함")

    items = []
    seen = set()
    target_locations = [folded(value) for value in source.get(
        "target_locations",
        ["Korea", "Republic of Korea", "South Korea", "Seoul", "Incheon", "韩国", "首尔"],
    )]

    for post in posts:
        if not isinstance(post, dict):
            continue

        post_id = str(
            post.get("RecruitPostId")
            or post.get("PostId")
            or post.get("postId")
            or ""
        ).strip()
        title = clean_title(
            str(
                post.get("RecruitPostName")
                or post.get("PostName")
                or post.get("postName")
                or post.get("PositionName")
                or ""
            )
        )
        country = normalize(str(post.get("CountryName") or post.get("countryName") or ""))
        location = normalize(str(post.get("LocationName") or post.get("locationName") or ""))
        combined_location = folded(f"{country} {location}")

        if not post_id or not title:
            continue
        if target_locations and not any(value in combined_location for value in target_locations):
            continue
        if post_id in seen:
            continue
        seen.add(post_id)

        raw_url = str(post.get("PostURL") or post.get("postURL") or "").strip()
        url = canonical_url(
            raw_url
            if raw_url.startswith("http")
            else f"https://careers.tencent.com/en-us/jobdesc.html?postId={post_id}"
        )
        posted = normalize_date(
            str(
                post.get("LastUpdateTime")
                or post.get("lastUpdateTime")
                or post.get("PublishDate")
                or ""
            )
        )

        items.append(Item(
            item_id=f"tencent:post:{post_id}",
            source_id=source["id"],
            category=source["category"],
            source_name=source["name"],
            title=title,
            url=url,
            posted_at=posted,
            matched=f"{country} {location}".strip() or "Korea",
        ))

    return ParseResult(
        True,
        items,
        "tencent-official-api",
        f"Tencent 공식 API 한국 근무지 공고 {len(items)}건",
    )


def request_tencent_api(source: dict[str, Any], timeout_seconds: int) -> ParseResult:
    endpoint = "https://careers.tencent.com/tencentcareer/api/post/Query"

    common = {
        "timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        "cityId": "",
        "bgIds": "",
        "productId": "",
        "categoryId": "",
        "parentCategoryId": "",
        "attrId": "",
        "keyword": "",
        "pageIndex": "1",
        "pageSize": "100",
        "language": "en-us",
        "area": "us",
    }

    attempts = [
        {**common, "countryId": str(source.get("tencent_country_id", "701"))},
        {**common, "countryId": "", "keyword": "Korea"},
        {**common, "countryId": ""},
    ]

    messages = []
    for params in attempts:
        try:
            response = requests.get(
                endpoint,
                params=params,
                headers=HEADERS,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            result = parse_tencent_api_payload(source, response.json())
            if result.verified and result.items:
                return result

            if result.verified and params.get("countryId"):
                # 국가 필터가 유효하게 0건을 반환했다면 검증된 0건으로 인정
                return result
            messages.append(result.message)
        except Exception as exc:
            messages.append(f"{type(exc).__name__}: {exc}")

    return ParseResult(
        False,
        [],
        "tencent-official-api",
        " / ".join(messages) or "Tencent 공식 API 요청 실패",
    )


def source_has_baseline(previous: dict[str, Any]) -> bool:
    return bool(previous.get("baseline_ready"))


def parse_t1(source: dict[str, Any], html_text: str) -> ParseResult:
    soup = BeautifulSoup(html_text, "lxml")
    raw = soup.get_text("\n", strip=True)
    raw = re.sub(r"\r\n?", "\n", raw)
    heading = re.compile(
        r"(?im)^\s*(\[(?:Esports\s+T1|esports\s+T1\s+Academy)\]\s*[^\n]+)\s*$"
    )
    matches = list(heading.finditer(raw))
    if not matches:
        return ParseResult(False, [], "t1-static", "T1 공고 제목 패턴을 찾지 못함")

    items = []
    for index, match in enumerate(matches):
        title = clean_title(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        block = raw[match.start():end]
        period = re.search(
            r"접수\s*기간\s*[:：]?\s*"
            r"(20\d{2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2})"
            r"(?:\s+\d{1,2}시)?.{0,50}?(?:~|–|—)"
            r".{0,50}?"
            r"(20\d{2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{1,2})",
            block,
            re.I | re.S,
        )
        posted = normalize_date(period.group(1)) if period else ""
        deadline = normalize_date(period.group(2)) if period else ""
        if deadline and is_expired(deadline):
            continue
        item_id = f"t1:{slug(title)}:{posted or short_hash(block)}"
        items.append(Item(
            item_id=item_id,
            source_id=source["id"],
            category=source["category"],
            source_name=source["name"],
            title=title,
            url=source["url"],
            posted_at=posted,
            deadline=deadline,
            matched="새 공고",
        ))

    return ParseResult(True, items, "t1-static", f"활성 공고 {len(items)}건")


def greeting_title(anchor_text: str) -> str:
    text = normalize(anchor_text)
    markers = [
        " Gen.G eSports Global Academy, Ltd.",
        " Gen.G Global Academy",
        " KSV Services, Ltd.",
        " KSV eSports Korea Co., Ltd.",
    ]
    for marker in markers:
        if marker in text:
            return clean_title(text.split(marker, 1)[0])
    return clean_title(text)


def parse_greeting(source: dict[str, Any], html_text: str, base_url: str) -> ParseResult:
    soup = BeautifulSoup(html_text, "lxml")
    anchors = soup.find_all("a", href=re.compile(r"/(?:ko/)?o/\d+"))
    if not anchors:
        return ParseResult(False, [], "greeting-static", "실제 /o/ 공고 링크를 찾지 못함")

    items = []
    seen = set()
    for anchor in anchors:
        url = canonical_url(urljoin(base_url, anchor.get("href", "")))
        match = re.search(r"/(?:ko/)?o/(\d+)", url)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        title = greeting_title(anchor.get_text(" ", strip=True))
        if not title or title in NAV_TITLES:
            continue
        if "인재풀" in title or "Talent Pool" in title:
            continue
        items.append(Item(
            item_id=f"geng:greeting:{match.group(1)}",
            source_id=source["id"],
            category=source["category"],
            source_name=source["name"],
            title=title,
            url=url,
            matched="새 공고",
        ))
    return ParseResult(True, items, "greeting-static", f"실제 공고 {len(items)}건")


def parse_saramin(source: dict[str, Any], html_text: str, base_url: str) -> ParseResult:
    soup = BeautifulSoup(html_text, "lxml")
    body_text = normalize(soup.get_text(" ", strip=True))

    anchors = soup.find_all(
        "a",
        href=re.compile(r"rec_idx=|/jobs/relay/view", re.I),
    )
    items = []
    seen = set()
    for anchor in anchors:
        url = canonical_url(urljoin(base_url, anchor.get("href", "")))
        match = re.search(r"rec_idx=(\d+)", url)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        title = clean_title(anchor.get_text(" ", strip=True))
        if not title or title.casefold() in NAV_TITLES:
            continue
        context = normalize(anchor.parent.get_text(" ", strip=True) if anchor.parent else title)
        if is_closed(context):
            continue
        posted, deadline = extract_dates(context)
        if is_expired(deadline):
            continue
        items.append(Item(
            item_id=f"nongshim_esports:saramin:{match.group(1)}",
            source_id=source["id"],
            category=source["category"],
            source_name=source["name"],
            title=title,
            url=url,
            posted_at=posted,
            deadline=deadline,
            matched="새 공고",
        ))

    if items:
        return ParseResult(True, items, "saramin-static", f"진행 공고 {len(items)}건")
    if "현재 채용중인 공고가 없습니다" in body_text:
        return ParseResult(True, [], "saramin-static", "현재 채용 공고 없음")
    return ParseResult(False, [], "saramin-static", "공고 링크도 명시적 빈 상태도 확인하지 못함")


def parse_ccon(source: dict[str, Any], html_text: str, base_url: str) -> ParseResult:
    soup = BeautifulSoup(html_text, "lxml")
    anchors = soup.find_all("a", href=re.compile(r"bo_table=rnt.*wr_id=\d+"))
    if not anchors:
        return ParseResult(False, [], "ccon-static", "wr_id 게시물 링크를 찾지 못함")

    items = []
    seen = set()
    for anchor in anchors:
        url = canonical_url(urljoin(base_url, anchor.get("href", "")))
        match = re.search(r"(?:\?|&)wr_id=(\d+)", url)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        title = clean_title(anchor.get_text(" ", strip=True))
        context = nearest_context(anchor, markers=["진행중", "종료", "결과"])
        if "진행중" not in context:
            continue
        if not keyword_hit(title, source.get("keywords", [])):
            continue
        if any(word in title for word in ["합격", "결과", "면접전형", "서류전형", "임용등록"]):
            continue
        posted, _ = extract_dates(context)
        items.append(Item(
            item_id=f"ccon:wr-id:{match.group(1)}",
            source_id=source["id"],
            category=source["category"],
            source_name=source["name"],
            title=title,
            url=url,
            posted_at=posted,
            matched="직원채용 · 진행중",
        ))
    return ParseResult(True, items, "ccon-static", f"진행 원공고 {len(items)}건")


def parse_jobkorea(source: dict[str, Any], html_text: str, base_url: str) -> ParseResult:
    soup = BeautifulSoup(html_text, "lxml")
    body_text = normalize(soup.get_text(" ", strip=True))
    anchors = soup.find_all("a", href=re.compile(r"/Recruit/GI_Read/\d+", re.I))
    items = []
    seen = set()
    for anchor in anchors:
        url = canonical_url(urljoin(base_url, anchor.get("href", "")))
        match = re.search(r"/Recruit/GI_Read/(\d+)", url, re.I)
        if not match or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        title = clean_title(anchor.get_text(" ", strip=True))
        if not title or title.casefold() in NAV_TITLES:
            continue
        if re.search(r"JOBKOREA|전체\s*채용정보|신입공채|헤드헌팅|기업정보|연봉정보", title, re.I):
            continue
        container = anchor.find_parent(["li", "tr", "article", "div"]) or anchor.parent
        context = normalize(container.get_text(" ", strip=True) if container else title)
        if is_closed(context):
            continue
        posted, deadline = extract_dates(context)
        if is_expired(deadline):
            continue
        items.append(Item(
            item_id=f"dplus_kia:jobkorea:{match.group(1)}",
            source_id=source["id"],
            category=source["category"],
            source_name=source["name"],
            title=title,
            url=url,
            posted_at=posted,
            deadline=deadline,
            matched="새 공고",
        ))

    if items:
        return ParseResult(True, items, "jobkorea-strict", f"실제 공고 {len(items)}건")
    empty_markers = [
        "진행중인 채용공고가 없습니다",
        "현재 진행중인 채용공고가 없습니다",
        "진행 중인 공고가 없습니다",
    ]
    if any(marker in body_text for marker in empty_markers):
        return ParseResult(True, [], "jobkorea-strict", "현재 채용 공고 없음")

    company_markers = ["에이디이스포츠", "디플러스 기아", "Dplus KIA", "DPLUS KIA"]
    healthy_company_page = (
        len(body_text) >= 500
        and "JOBKOREA" in body_text.upper()
        and any(marker.casefold() in body_text.casefold() for marker in company_markers)
    )
    explicit_zero = bool(
        re.search(r"진행\s*중?\s*0\s*건", body_text, re.I)
        or re.search(r"채용\s*게시물\s*0", body_text, re.I)
    )

    if healthy_company_page and explicit_zero:
        return ParseResult(True, [], "jobkorea-strict", "잡코리아 공식 회사 페이지 · 진행중 0건 확인")
    if healthy_company_page and anchors and not items and all(
        "마감" in normalize(
            (anchor.find_parent(["li", "tr", "article", "div"]) or anchor.parent).get_text(
                " ", strip=True
            )
        )
        for anchor in anchors
    ):
        return ParseResult(True, [], "jobkorea-strict", "잡코리아 공식 회사 페이지 · 모든 공고 마감")
    if healthy_company_page and not anchors:
        return ParseResult(True, [], "jobkorea-strict", "잡코리아 공식 회사 페이지 · 실제 진행 공고 0건")

    return ParseResult(False, [], "jobkorea-strict", "회사 페이지 또는 실제 공고 구조를 검증하지 못함")


def parse_generic(source: dict[str, Any], html_text: str, base_url: str) -> ParseResult:
    soup = BeautifulSoup(html_text, "lxml")
    body_text = normalize(soup.get_text(" ", strip=True))
    if not healthy_html(html_text, source.get("expected_markers", [])):
        return ParseResult(False, [], "generic-strict", "페이지 건강성 검증 실패")

    include_regex = re.compile(source.get("include_url_regex", r"$^"), re.I)
    items = []
    seen = set()
    eligible_links = 0

    for anchor in soup.find_all("a", href=True):
        url = canonical_url(urljoin(base_url, anchor.get("href", "")))
        if not include_regex.search(url):
            continue
        eligible_links += 1

        title = clean_title(anchor.get_text(" ", strip=True))
        if not title or title.casefold() in NAV_TITLES or len(title) > 350:
            continue

        container = anchor.find_parent(["li", "tr", "article", "section", "div"]) or anchor.parent
        context = normalize(container.get_text(" ", strip=True) if container else title)
        combined = f"{title} {context}"
        if is_closed(combined):
            continue

        matched = ""
        mode = source.get("mode")
        if mode == "keyword":
            matched = keyword_hit(combined, source.get("keywords", []))
            if not matched:
                continue
        elif mode == "criteria":
            location = next((x for x in source.get("criteria_locations", []) if folded(x) in folded(combined)), "")
            work_mode = next((x for x in source.get("criteria_work_modes", []) if folded(x) in folded(combined)), "")
            if not location or not work_mode:
                continue
            matched = f"{location} + {work_mode}"
        else:
            matched = "새 공고"

        posted, deadline = extract_dates(combined)
        if is_expired(deadline):
            continue

        item_id = item_id_from_url(source["id"], url, title)
        if item_id in seen:
            continue
        seen.add(item_id)
        items.append(Item(
            item_id=item_id,
            source_id=source["id"],
            category=source["category"],
            source_name=source["name"],
            title=title,
            url=url,
            posted_at=posted,
            deadline=deadline,
            matched=matched,
        ))

    if items:
        return ParseResult(True, items, "generic-strict", f"조건 일치 {len(items)}건")

    if any(marker in body_text for marker in source.get("empty_markers", [])):
        return ParseResult(True, [], "generic-strict", "명시적 빈 상태")

    if source.get("mode") == "keyword" and source.get("verified_zero_on_healthy_page"):
        return ParseResult(True, [], "generic-strict", "페이지는 정상이나 지정 키워드 없음")

    if source.get("mode") == "criteria" and eligible_links > 0:
        return ParseResult(True, [], "generic-strict", "공고 링크는 있으나 지역·근무 조건 불일치")

    return ParseResult(False, [], "generic-strict", "공고 구조를 확정하지 못해 상태 보존")


def parse_source(source: dict[str, Any], html_text: str, final_url: str) -> ParseResult:
    parser = source.get("parser", "generic")
    if parser == "krafton":
        return parse_krafton(source, html_text, final_url)
    if parser == "kespa":
        return parse_kespa(source, html_text, final_url)
    if parser == "hanwha":
        return parse_hanwha(source, html_text, final_url)
    if parser == "tencent":
        return parse_tencent(source, html_text, final_url)
    if parser == "t1":
        return parse_t1(source, html_text)
    if parser == "greeting":
        return parse_greeting(source, html_text, final_url)
    if parser == "saramin":
        return parse_saramin(source, html_text, final_url)
    if parser == "ccon":
        return parse_ccon(source, html_text, final_url)
    if parser == "jobkorea":
        return parse_jobkorea(source, html_text, final_url)
    return parse_generic(source, html_text, final_url)


def empty_state() -> dict[str, Any]:
    return {"version": 12, "initialized": False, "sources": {}, "last_run": None}


def load_state(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.exists():
        return empty_state(), True
    raw = load_json(path)
    if not isinstance(raw, dict) or raw.get("version") != 12:
        return empty_state(), True
    raw.setdefault("sources", {})
    raw.setdefault("initialized", False)
    return raw, False


def credentials(settings: dict[str, Any]) -> tuple[str, str, str]:
    user = os.getenv("SMTP_USER") or os.getenv("EMAIL_USER") or os.getenv("GMAIL_USER") or ""
    password = (
        os.getenv("SMTP_PASSWORD")
        or os.getenv("EMAIL_PASSWORD")
        or os.getenv("GMAIL_APP_PASSWORD")
        or ""
    )
    recipient = os.getenv("EMAIL_TO") or os.getenv("MAIL_TO") or settings.get("recipient", "")
    return user, password, recipient


def send_email(settings: dict[str, Any], items: list[tuple[Item, str]], test: bool = False) -> None:
    user, password, recipient = credentials(settings)
    if not user or not password or not recipient:
        raise RuntimeError("SMTP_USER, SMTP_PASSWORD, EMAIL_TO 설정을 확인하세요.")

    now_text = now_kst().strftime("%Y-%m-%d %H:%M")
    prefix = settings.get("subject_prefix", "[공고 수집기]")
    subject = f"{prefix} {'테스트 성공' if test else f'새 공고 {len(items)}건'} · {now_text}"

    grouped: dict[str, list[tuple[Item, str]]] = {}
    for item, first_seen in items:
        grouped.setdefault(item.category, []).append((item, first_seen))

    blocks = []
    for category in ["게임사", "공공기관", "이스포츠 구단", "기타"]:
        rows = grouped.get(category, [])
        if not rows:
            continue
        cards = []
        for item, first_seen in rows:
            meta = [f"최초 발견: {first_seen[:16].replace('T', ' ')}"]
            if item.posted_at:
                meta.insert(0, f"게시일: {item.posted_at}")
            if item.deadline:
                meta.append(f"마감일: {item.deadline}")
            if item.matched:
                meta.append(f"감지 기준: {item.matched}")
            cards.append(f"""
            <div style="border:1px solid #e4e7ec;border-radius:10px;padding:14px 16px;margin:10px 0;background:#fff">
              <div style="font-size:12px;color:#667085">{html.escape(item.source_name)}</div>
              <div style="font-size:16px;font-weight:700;margin:5px 0 9px">{html.escape(item.title)}</div>
              <a href="{html.escape(item.url)}" style="color:#175cd3;text-decoration:none">공고 열기</a>
              <div style="font-size:12px;color:#667085;margin-top:8px;line-height:1.7">
                {"<br>".join(html.escape(x) for x in meta)}
              </div>
            </div>
            """)
        blocks.append(f"<h2 style='font-size:18px'>{html.escape(category)}</h2>{''.join(cards)}")

    if test:
        blocks.append("<div style='padding:16px;background:#f0fdf4;border-radius:10px'>v12 메일 연결이 정상입니다.</div>")

    body = f"""
    <html><body style="background:#f7f8fa;font-family:Arial,'Noto Sans KR',sans-serif;color:#101828">
      <div style="max-width:720px;margin:0 auto;padding:28px 18px">
        <h1 style="font-size:23px">맞춤형 채용 공고 알림 v12</h1>
        <div style="color:#667085;font-size:13px">{html.escape(now_text)}</div>
        {''.join(blocks)}
        <div style="font-size:12px;color:#98a2b3;margin-top:30px">
          검증된 실제 공고만 전송하며, 사이트 구조를 확인하지 못하면 메일을 보내지 않고 기존 상태를 보존합니다.
        </div>
      </div>
    </body></html>
    """

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = user
    message["To"] = recipient
    message.attach(MIMEText("새 채용 공고가 감지되었습니다.", "plain", "utf-8"))
    message.attach(MIMEText(body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(user, password)
        server.sendmail(user, [recipient], message.as_string())


async def execute(args: argparse.Namespace) -> int:
    sources = load_json(SOURCES_PATH)
    settings = load_json(SETTINGS_PATH)
    state_path = ROOT / settings.get("state_file", "data/state.json")
    report_path = ROOT / settings.get("report_file", "data/last_run_report.json")

    if args.test_email:
        send_email(settings, [], test=True)
        print("[메일] 테스트 메일 발송 완료")
        return 0

    state, migrated = load_state(state_path)
    if migrated:
        print("[상태] 이전 버전을 감지했습니다. v12 첫 normal 실행은 자동 기준값 생성입니다.")

    if args.baseline:
        state = empty_state()

    report_rows = []
    verified_results: dict[str, ParseResult] = {}
    candidates: list[tuple[Item, str]] = []
    timestamp = now_iso()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])

        for source in sources:
            print(f"[수집] {source['name']}", flush=True)
            previous = state.get("sources", {}).get(source["id"], {})
            previous_seen = dict(previous.get("seen_ever", {}))
            previous_active = list(previous.get("active_ids", []))

            try:
                parser_name = source.get("parser", "generic")

                if parser_name == "official_domain_rss":
                    result = await asyncio.to_thread(
                        request_bing_rss,
                        source,
                        int(settings.get("request_timeout_seconds", 30)),
                    )
                    fetch_method = "bing-rss"

                elif parser_name == "tencent_api":
                    result = await asyncio.to_thread(
                        request_tencent_api,
                        source,
                        int(settings.get("request_timeout_seconds", 30)),
                    )
                    fetch_method = "official-json-api"

                else:
                    html_text, final_url, fetch_method = await fetch_source(
                        browser, source, settings
                    )
                    result = parse_source(source, html_text, final_url)

                    # 정적 요청은 성공했지만 파서 검증에 실패한 경우 렌더링 HTML로 재시도
                    if not result.verified and fetch_method.startswith("requests"):
                        try:
                            browser_text, browser_url = await browser_html(
                                browser,
                                source["url"],
                                int(settings.get("browser_timeout_ms", 50000)),
                            )
                            browser_result = parse_source(
                                source, browser_text, browser_url
                            )
                            if browser_result.verified:
                                result = browser_result
                                fetch_method = f"{fetch_method}->browser-retry"
                            else:
                                result.message = (
                                    f"{result.message} / 브라우저 재검증: "
                                    f"{browser_result.message}"
                                )
                        except Exception as retry_exc:
                            result.message = (
                                f"{result.message} / 브라우저 재검증 실패: "
                                f"{type(retry_exc).__name__}: {retry_exc}"
                            )
            except Exception as exc:
                result = ParseResult(
                    False, [], "fetch", f"{type(exc).__name__}: {exc}"
                )
                fetch_method = "failed"

            current_ids = [item.item_id for item in result.items]
            new_for_source = []

            if result.verified:
                verified_results[source["id"]] = result
                had_source_baseline = source_has_baseline(previous)
                updated_seen = previous_seen

                for item in result.items:
                    first_seen = updated_seen.get(
                        item.item_id, {}
                    ).get("first_seen_at", timestamp)
                    if item.item_id not in updated_seen:
                        new_for_source.append((item, first_seen))
                    updated_seen[item.item_id] = {
                        "title": item.title,
                        "url": item.url,
                        "posted_at": item.posted_at,
                        "deadline": item.deadline,
                        "first_seen_at": first_seen,
                        "last_seen_at": timestamp,
                    }

                state.setdefault("sources", {})[source["id"]] = {
                    "name": source["name"],
                    "parser": source.get("parser"),
                    "parser_version": source.get("parser_version", 12),
                    "baseline_ready": True,
                    "seen_ever": updated_seen,
                    "active_ids": current_ids,
                    "last_verified_at": timestamp,
                    "last_message": result.message,
                    "last_count": len(result.items),
                }

                # 각 사이트의 첫 검증 성공은 그 사이트만 기준값으로 저장한다.
                # 나중에 접속이 복구된 사이트가 기존 공고를 한꺼번에 메일로 보내는 것을 방지한다.
                if (
                    state.get("initialized")
                    and had_source_baseline
                    and not args.baseline
                ):
                    candidates.extend(new_for_source)

                status = "success"
                baseline_note = "" if had_source_baseline else " · 사이트 첫 기준값"
                print(
                    f"  [검증 완료] {len(result.items)}건 · "
                    f"신규 후보 {len(new_for_source)}건{baseline_note}"
                )
            else:
                # 검증 실패는 상태를 절대 덮어쓰지 않는다.
                status = "unverified"
                print(f"  [상태 보존] {result.message}")

            report_rows.append({
                "source_id": source["id"],
                "name": source["name"],
                "status": status,
                "verified": result.verified,
                "fetch_method": fetch_method,
                "parser_method": result.method,
                "message": result.message,
                "current_count": len(result.items),
                "previous_active_count": len(previous_active),
                "source_baseline_existed": source_has_baseline(previous),
                "new_candidate_count": len(new_for_source) if result.verified else 0,
                "items": [
                    {
                        "item_id": item.item_id,
                        "title": item.title,
                        "posted_at": item.posted_at,
                        "deadline": item.deadline,
                    }
                    for item in result.items
                ],
            })

        await browser.close()

    report = {
        "version": 12,
        "run_at": timestamp,
        "diagnostic": args.diagnostic,
        "initialized_before_run": bool(state.get("initialized")),
        "verified_source_count": sum(1 for row in report_rows if row["verified"]),
        "unverified_source_count": sum(1 for row in report_rows if not row["verified"]),
        "new_candidate_count": len(candidates),
        "sources": report_rows,
    }
    save_json(report_path, report)

    if args.diagnostic:
        print("[진단] 실제 상태와 메일은 변경하지 않았습니다.")
        return 0

    if args.notify_t1_current:
        t1_result = verified_results.get("t1")
        if not t1_result:
            print("[T1] 검증된 현재 공고가 없어 발송하지 않았습니다.")
        elif not t1_result.items:
            print("[T1] 현재 활성 공고가 0건이라 메일을 보내지 않았습니다.")
        else:
            send_email(
                settings,
                [(item, timestamp) for item in t1_result.items],
            )
            print(f"[T1] 현재 활성 공고 {len(t1_result.items)}건 발송 완료")
        state["initialized"] = True
        state["last_run"] = timestamp
        save_json(state_path, state)
        return 0

    first_activation = not state.get("initialized")
    state["initialized"] = True
    state["last_run"] = timestamp
    save_json(state_path, state)

    if first_activation or args.baseline:
        print("[기준값] v12 현재 공고를 기준값으로 저장했습니다. 메일은 보내지 않았습니다.")
    elif candidates:
        send_email(settings, candidates)
        print(f"[메일] 검증된 신규 공고 {len(candidates)}건 발송 완료")
    else:
        print("[메일] 검증된 신규 공고 없음")

    return 0


def self_test() -> None:
    t1_html = """
    <html><body>
    <h2>[Esports T1] Video Content 팀리드</h2>
    <p>접수기간 : 2026-07-13 17시 ~ 2026-09-11 24시</p>
    <h2>[esports T1 Academy] 기획/운영 PM</h2>
    <p>접수기간 : 2026-06-10 11시 ~ 2026-07-10 24시</p>
    </body></html>
    """
    t1_source = {"id": "t1", "category": "이스포츠 구단", "name": "T1", "url": "https://www.t1.gg/new-page-2"}
    t1_result = parse_t1(t1_source, t1_html)
    assert t1_result.verified
    assert len(t1_result.items) == 1
    assert t1_result.items[0].posted_at == "2026-07-13"
    assert t1_result.items[0].deadline == "2026-09-11"

    geng_html = """
    <a href="/ko/o/199541">(경력) 발로란트 콘텐츠 PD / Valorant Content Producer KSV eSports Korea Co., Ltd. Gen.G Esports</a>
    <a href="/ko/o/100">[인재풀 등록하기] GGA LoL Coach Gen.G Global Academy</a>
    """
    geng_source = {"id": "geng", "category": "이스포츠 구단", "name": "Gen.G"}
    geng_result = parse_greeting(geng_source, geng_html, "https://geng.career.greetinghr.com/ko/work-with-us")
    assert geng_result.verified
    assert len(geng_result.items) == 1
    assert "발로란트 콘텐츠 PD" in geng_result.items[0].title

    saramin_html = "<html><body><h2>진행중 공고</h2><p>현재 채용중인 공고가 없습니다.</p></body></html>"
    ns_source = {"id": "nongshim_esports", "category": "이스포츠 구단", "name": "농심이스포츠"}
    ns_result = parse_saramin(ns_source, saramin_html, "https://www.saramin.co.kr/")
    assert ns_result.verified and len(ns_result.items) == 0

    ccon_html = """
    <div><span>진행중</span><a href="/bbs/board.php?bo_table=rnt&wr_id=373">
    (재)충남콘텐츠진흥원 2026년 제2차 계약직 직원채용 공고 N 새글</a>
    <span>2026-07-16</span></div>
    """
    ccon_source = {"id": "ccon", "category": "공공기관", "name": "충남콘텐츠진흥원", "keywords": ["직원채용"]}
    ccon_result = parse_ccon(ccon_source, ccon_html, "https://ccon.kr/")
    assert ccon_result.verified and len(ccon_result.items) == 1
    assert ccon_result.items[0].item_id == "ccon:wr-id:373"


    krafton_html = """
    <html><body>KRAFTON Careers Jobs
      Art Business Service Data Development Management Game Design IT Infra
      Management Supporting QA Regular Contractor Seoul Pangyo Amsterdam Tokyo
      Current openings and employment opportunities across KRAFTON studios.
      This sentence only makes the test page long enough to represent a real page.
      <a href="/careers/jobs/">TOP</a>
      <a href="/careers/jobs/">Family Site</a>
      <a href="/careers/jobs/12345">[Esports] Global Tournament Manager</a>
      <a href="/careers/jobs/77777">Finance Manager</a>
    </body></html>
    """
    krafton_source = {
        "id": "krafton", "category": "게임사", "name": "크래프톤",
        "keywords": ["Esports"],
    }
    krafton_result = parse_krafton(
        krafton_source, krafton_html, "https://www.krafton.com/careers/jobs/"
    )
    assert krafton_result.verified
    assert len(krafton_result.items) == 1
    assert krafton_result.items[0].title == "[Esports] Global Tournament Manager"

    kespa_html = """
    <a href="/news/notice_view?brd_id=ABC123">
      한국이스포츠협회 전략사업국 매니저 채용 공고
    </a>
    <a href="/news/notice_view?brd_id=RESULT1">
      [최종 결과공지] 한국이스포츠협회 채용 공고
    </a>
    """
    kespa_source = {"id": "kespa", "category": "공공기관", "name": "KeSPA"}
    kespa_result = parse_kespa(
        kespa_source, kespa_html, "https://www.e-sports.or.kr/"
    )
    assert kespa_result.verified and len(kespa_result.items) == 1

    hanwha_html = """
    <ul>
      <li><a href="/web/apply/notification/view.do?rtSeq=999">
        한화생명 LIFEPLUS e-스포츠 마케팅 전문인력 채용
      </a><span>2026.07.18 - 2026.08.10</span></li>
      <li><a href="/web/apply/notification/view.do?rtSeq=998">
        한화에어로스페이스 엔지니어 채용
      </a></li>
    </ul>
    """
    hanwha_source = {
        "id": "hanwha", "category": "이스포츠 구단",
        "name": "한화생명e스포츠", "keywords": ["e-스포츠"],
    }
    hanwha_result = parse_hanwha(
        hanwha_source, hanwha_html, "https://www.hanwhain.com/"
    )
    assert hanwha_result.verified and len(hanwha_result.items) == 1

    tencent_html = """
    <html><body>Tencent Careers Showing 1-1 of 1 jobs
    <script>
    {"postId":"107505","postName":"Game Publishing Manager - Korea",
     "publishDate":"2026-07-18"}
    </script></body></html>
    """
    tencent_source = {
        "id": "tencent", "category": "기타", "name": "텐센트 공식 채용"
    }
    tencent_result = parse_tencent(
        tencent_source, tencent_html, "https://careers.tencent.com/en-us/search.html"
    )
    assert tencent_result.verified and len(tencent_result.items) == 1


    rss_xml = """
    <rss><channel>
      <item>
        <title>한국이스포츠협회 사업기획 매니저 채용 공고</title>
        <link>https://www.e-sports.or.kr/news/notice_view?brd_id=ABC</link>
        <description>채용 공고</description>
      </item>
      <item>
        <title>다른 사이트 채용 공고</title>
        <link>https://example.com/job/1</link>
      </item>
    </channel></rss>
    """
    rss_source = {
        "id": "kespa", "category": "공공기관", "name": "KeSPA",
        "allowed_domain": "e-sports.or.kr",
        "rss_required_words": ["채용", "공고"],
        "rss_excluded_words": ["합격", "결과"],
    }
    rss_result = parse_bing_rss(rss_source, rss_xml)
    assert rss_result.verified and len(rss_result.items) == 1

    tencent_payload = {
        "Data": {
            "Posts": [
                {
                    "RecruitPostId": 107505,
                    "RecruitPostName": "Game Publishing Manager",
                    "CountryName": "Korea",
                    "LocationName": "Seoul",
                    "LastUpdateTime": "2026-07-18",
                    "PostURL": "https://careers.tencent.com/en-us/jobdesc.html?postId=107505",
                },
                {
                    "RecruitPostId": 999,
                    "RecruitPostName": "China Position",
                    "CountryName": "China",
                    "LocationName": "Shenzhen",
                },
            ]
        }
    }
    api_source = {
        "id": "tencent", "category": "기타", "name": "텐센트 공식 채용"
    }
    api_result = parse_tencent_api_payload(api_source, tencent_payload)
    assert api_result.verified and len(api_result.items) == 1
    assert api_result.items[0].item_id == "tencent:post:107505"

    assert not source_has_baseline({})
    assert source_has_baseline({"baseline_ready": True})

    print("[자체 점검] 핵심 전용 파서·공식 API·RSS·소스별 기준값 정상")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="공고 수집기 v12")
    parser.add_argument("--test-email", action="store_true")
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--notify-t1-current", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.self_test:
        self_test()
        raise SystemExit(0)
    raise SystemExit(asyncio.run(execute(args)))
