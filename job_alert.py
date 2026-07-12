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
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PlaywrightTimeoutError


ROOT = Path(__file__).resolve().parent
SOURCES_PATH = ROOT / "config" / "sources.json"
SETTINGS_PATH = ROOT / "config" / "settings.json"

NAV_TEXTS = {
    "홈", "home", "회사소개", "about", "로그인", "login", "회원가입", "sign up",
    "메뉴", "menu", "더보기", "more", "전체보기", "view all", "지원하기", "apply",
    "개인정보처리방침", "privacy", "이용약관", "terms", "목록", "list",
    "이전", "다음", "prev", "next", "채용", "careers", "jobs", "recruit"
}
BLOCKED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".zip", ".hwp",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp4", ".mp3"
}
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "trk", "trackingId", "refId", "origin", "originToLandingJobPostings"
}


@dataclass
class Item:
    source_id: str
    category: str
    source_name: str
    title: str
    url: str
    context: str = ""
    matched: str = ""

    @property
    def key(self) -> str:
        base = f"{self.source_id}|{canonical_url(self.url)}|{normalize(self.title)}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def normalize(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def folded(text: str) -> str:
    # 하이픈·공백 차이로 키워드를 놓치지 않되, 단어 자체는 유지한다.
    return re.sub(r"[\s\-_–—·]+", "", normalize(text)).casefold()


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key not in TRACKING_PARAMS:
            query.append((key, value))
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


def keyword_match(text: str, keywords: list[str]) -> str:
    haystack = folded(text)
    for keyword in keywords:
        if folded(keyword) in haystack:
            return keyword
    return ""


def location_criteria_match(text: str, source: dict[str, Any]) -> str:
    f = folded(text)
    location_groups = source.get("criteria_any", [])
    location_hit = ""
    for group in location_groups:
        hit = next((word for word in group if folded(word) in f), "")
        if hit:
            location_hit = hit
            break
    if not location_hit:
        return ""

    modes = source.get("criteria_work_mode", [])
    mode_hit = next((word for word in modes if folded(word) in f), "")
    if not mode_hit:
        return ""
    return f"{location_hit} + {mode_hit}"


async def auto_scroll(page: Page) -> None:
    # 무한 스크롤형 채용 페이지를 위해 제한적으로 스크롤한다.
    previous = 0
    stable = 0
    for _ in range(10):
        height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(700)
        if height == previous:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        previous = height
    await page.evaluate("window.scrollTo(0, 0)")


async def dismiss_popups(page: Page) -> None:
    labels = ["동의", "모두 동의", "Accept", "Accept all", "확인", "닫기", "Close"]
    for label in labels:
        try:
            button = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if await button.count():
                await button.first.click(timeout=1000)
        except Exception:
            pass


async def extract_candidates(page: Page, source: dict[str, Any]) -> list[dict[str, str]]:
    raw = await page.evaluate(
        """
        () => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const selectors = [
            'article', 'li',
            '[class*="job"]', '[class*="Job"]',
            '[class*="recruit"]', '[class*="Recruit"]',
            '[class*="position"]', '[class*="Position"]',
            '[class*="posting"]', '[class*="Posting"]',
            '[class*="career"]', '[class*="Career"]',
            '[class*="announce"]', '[class*="board"] tr'
          ];
          const result = [];
          const seen = new Set();

          const push = (title, href, context, kind) => {
            title = clean(title);
            context = clean(context);
            href = href || '';
            if (!title || !href) return;
            const key = href + '|' + title;
            if (seen.has(key)) return;
            seen.add(key);
            result.push({title, href, context, kind});
          };

          for (const a of document.querySelectorAll('a[href]')) {
            const title = clean(a.innerText || a.textContent || a.getAttribute('aria-label') || a.title);
            const href = a.href;
            const container = a.closest(
              'article, li, tr, [class*="job"], [class*="Job"], [class*="recruit"], [class*="Recruit"], [class*="position"], [class*="Position"], [class*="posting"], [class*="Posting"], [class*="card"], [class*="Card"]'
            );
            const context = clean(container ? container.innerText : title);
            push(title, href, context, 'anchor');
          }

          for (const selector of selectors) {
            for (const el of document.querySelectorAll(selector)) {
              const text = clean(el.innerText);
              if (!text || text.length < 4 || text.length > 2500) continue;
              const a = el.querySelector('a[href]');
              if (!a) continue;
              const titleEl = el.querySelector('h1,h2,h3,h4,h5,strong,b,[class*="title"],[class*="Title"]');
              const title = clean(titleEl ? titleEl.innerText : a.innerText || text.slice(0, 180));
              push(title, a.href, text, 'block');
            }
          }
          return result;
        }
        """
    )

    base_domain = urlparse(source["url"]).netloc.lower()
    hints = [h.casefold() for h in source.get("url_hints", [])]
    accept_external = bool(source.get("accept_external_links", False))
    output: list[dict[str, str]] = []
    seen: set[str] = set()

    for candidate in raw:
        title = normalize(candidate.get("title", ""))
        context = normalize(candidate.get("context", ""))
        url = canonical_url(urljoin(source["url"], candidate.get("href", "")))

        if not url.startswith(("http://", "https://")) or is_blocked_url(url):
            continue
        if len(title) < 3 or len(title) > 350:
            continue
        if title.casefold() in NAV_TEXTS:
            continue
        if re.fullmatch(r"[\W_]+", title):
            continue

        parsed = urlparse(url)
        same_domain = parsed.netloc.lower() == base_domain
        hint_hit = any(h in url.casefold() for h in hints)
        text_job_hit = bool(re.search(
            r"(채용|채용공고|모집|초빙|지원자|recruit|career|job|position|opening|intern|인턴|계약직|정규직|esports|e-?sports)",
            f"{title} {context}", re.I
        ))

        mode = source["mode"]
        if mode == "new":
            if not (hint_hit or text_job_hit or (accept_external and not same_domain)):
                continue
        elif mode in {"keyword", "criteria"}:
            # 키워드/조건형은 본문 매칭이 최종 필터이므로 외부 지원 링크도 허용한다.
            pass

        dedupe = f"{url}|{folded(title)}"
        if dedupe in seen:
            continue
        seen.add(dedupe)
        output.append({"title": title, "url": url, "context": context})

    return output[: int(source.get("max_items", 200))]


async def read_detail(page: Page, url: str, timeout_ms: int) -> str:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1800)
        return normalize(await page.locator("body").inner_text(timeout=5000))[:12000]
    except Exception:
        return ""


async def crawl_source(browser: Browser, source: dict[str, Any], settings: dict[str, Any]) -> tuple[list[Item], str]:
    context = await browser.new_context(
        locale="ko-KR",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 1100},
    )
    page = await context.new_page()
    timeout_ms = int(settings.get("browser_timeout_ms", 45000))
    page.set_default_timeout(timeout_ms)

    try:
        await page.goto(source["url"], wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(int(source.get("wait_ms", 4000)))
        await dismiss_popups(page)
        await auto_scroll(page)
        candidates = await extract_candidates(page, source)

        items: list[Item] = []
        detail_budget = int(settings.get("max_detail_pages_per_source", 30))
        detail_page = await context.new_page()

        for candidate in candidates:
            combined = f'{candidate["title"]} {candidate["context"]}'
            matched = ""

            if source["mode"] == "keyword":
                matched = keyword_match(combined, source.get("keywords", []))
                if not matched and source.get("detail_check") and detail_budget > 0:
                    detail_budget -= 1
                    detail = await read_detail(detail_page, candidate["url"], timeout_ms)
                    matched = keyword_match(detail, source.get("keywords", []))
                    combined = f"{combined} {detail}"
                if not matched:
                    continue

            elif source["mode"] == "criteria":
                matched = location_criteria_match(combined, source)
                if not matched and source.get("detail_check") and detail_budget > 0:
                    detail_budget -= 1
                    detail = await read_detail(detail_page, candidate["url"], timeout_ms)
                    matched = location_criteria_match(f"{combined} {detail}", source)
                    combined = f"{combined} {detail}"
                if not matched:
                    continue

            item = Item(
                source_id=source["id"],
                category=source["category"],
                source_name=source["name"],
                title=candidate["title"],
                url=candidate["url"],
                context=normalize(combined)[:700],
                matched=matched,
            )
            items.append(item)

        # 같은 공고 링크가 카드 구조 차이로 중복 추출되는 것을 제거
        unique: dict[str, Item] = {}
        for item in items:
            key = canonical_url(item.url)
            current = unique.get(key)
            if current is None or len(item.title) > len(current.title):
                unique[key] = item
        return list(unique.values()), ""

    except PlaywrightTimeoutError as exc:
        return [], f"시간 초과: {exc}"
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    finally:
        await context.close()


def find_credentials(settings: dict[str, Any]) -> tuple[str, str, str]:
    user = (
        os.getenv("SMTP_USER")
        or os.getenv("EMAIL_USER")
        or os.getenv("GMAIL_USER")
        or ""
    )
    password = (
        os.getenv("SMTP_PASSWORD")
        or os.getenv("EMAIL_PASSWORD")
        or os.getenv("GMAIL_APP_PASSWORD")
        or ""
    )
    recipient = (
        os.getenv("EMAIL_TO")
        or os.getenv("MAIL_TO")
        or settings.get("recipient", "")
    )
    return user, password, recipient


def send_email(settings: dict[str, Any], items: list[Item], errors: list[str], test: bool = False) -> None:
    smtp_user, smtp_password, recipient = find_credentials(settings)
    if not smtp_user or not smtp_password or not recipient:
        raise RuntimeError(
            "메일 설정이 없습니다. SMTP_USER와 SMTP_PASSWORD GitHub Secrets를 확인하세요."
        )

    now_kst = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    prefix = settings.get("subject_prefix", "[공고 수집기]")
    subject = f"{prefix} {'테스트 성공' if test else f'새 공고 {len(items)}건'} · {now_kst}"

    grouped: dict[str, list[Item]] = {}
    for item in items:
        grouped.setdefault(item.category, []).append(item)

    blocks = []
    category_order = ["게임사", "공공기관", "이스포츠 구단", "기타"]
    for category in category_order:
        category_items = grouped.get(category, [])
        if not category_items:
            continue
        cards = []
        for item in category_items:
            matched = (
                f'<div style="font-size:12px;color:#667085;margin-top:5px">'
                f'감지 기준: {html.escape(item.matched)}</div>'
                if item.matched else ""
            )
            cards.append(
                f"""
                <div style="border:1px solid #e4e7ec;border-radius:10px;padding:14px 16px;margin:10px 0;background:#fff">
                  <div style="font-size:12px;color:#667085">{html.escape(item.source_name)}</div>
                  <div style="font-size:16px;font-weight:700;margin:4px 0 8px">
                    {html.escape(item.title)}
                  </div>
                  <a href="{html.escape(item.url)}" style="color:#175cd3;text-decoration:none">공고 열기</a>
                  {matched}
                </div>
                """
            )
        blocks.append(
            f"<h2 style='font-size:18px;margin:25px 0 8px'>{html.escape(category)}</h2>"
            + "".join(cards)
        )

    if test and not items:
        blocks.append(
            "<div style='padding:16px;background:#f0fdf4;border-radius:10px'>"
            "메일 연결이 정상입니다. 실제 알림은 새 공고가 감지될 때만 발송됩니다."
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
        <h1 style="font-size:23px;margin:0 0 5px">맞춤형 채용 공고 알림</h1>
        <div style="color:#667085;font-size:13px">{html.escape(now_kst)}</div>
        {''.join(blocks)}
        {error_html}
        <div style="font-size:12px;color:#98a2b3;margin-top:30px">
          첫 실행의 기존 공고는 기준값으로만 저장되며 메일로 발송되지 않습니다.
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


async def run(args: argparse.Namespace) -> int:
    sources: list[dict[str, Any]] = load_json(SOURCES_PATH)
    settings: dict[str, Any] = load_json(SETTINGS_PATH)
    state_path = ROOT / settings.get("state_file", "data/state.json")

    if args.reset_baseline:
        state = {"version": 2, "sources": {}, "last_run": None}
    else:
        state = load_json(state_path) if state_path.exists() else {
            "version": 2, "sources": {}, "last_run": None
        }

    all_new: list[Item] = []
    errors: list[str] = []
    crawl_results: dict[str, list[Item]] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        for source in sources:
            print(f"[수집] {source['category']} / {source['name']}", flush=True)
            items, error = await crawl_source(browser, source, settings)
            crawl_results[source["id"]] = items

            if error:
                msg = f"{source['name']}: {error}"
                errors.append(msg)
                print(f"  [오류] {msg}", flush=True)
                # 수집 실패 시 기존 상태를 덮어쓰지 않는다.
                continue

            previous = set(state.get("sources", {}).get(source["id"], {}).get("keys", []))
            current = {item.key for item in items}

            if source["id"] not in state.get("sources", {}):
                print(f"  [기준값 생성] 현재 공고 {len(items)}건 — 메일 발송 안 함", flush=True)
            else:
                new_items = [item for item in items if item.key not in previous]
                all_new.extend(new_items)
                print(f"  [완료] 현재 {len(items)}건 / 신규 {len(new_items)}건", flush=True)

            state.setdefault("sources", {})[source["id"]] = {
                "name": source["name"],
                "category": source["category"],
                "url": source["url"],
                "keys": sorted(current),
                "items": [
                    {
                        "key": item.key,
                        "title": item.title,
                        "url": item.url,
                        "matched": item.matched,
                    }
                    for item in items
                ],
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

        await browser.close()

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["last_errors"] = errors
    save_json(state_path, state)

    if args.test_email:
        sample = []
        for source in sources:
            items = crawl_results.get(source["id"], [])
            if items:
                sample = [items[0]]
                break
        send_email(settings, sample, errors, test=True)
        print("[메일] 테스트 메일 발송 완료")
    elif all_new:
        send_email(settings, all_new, errors)
        print(f"[메일] 신규 공고 {len(all_new)}건 발송 완료")
    else:
        print("[메일] 신규 공고 없음 — 발송하지 않음")

    if errors:
        print("\n".join(f"[주의] {e}" for e in errors), file=sys.stderr)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="맞춤형 e스포츠 채용 공고 수집기")
    parser.add_argument("--test-email", action="store_true", help="메일 연결 테스트")
    parser.add_argument("--reset-baseline", action="store_true", help="기준값 재생성")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
