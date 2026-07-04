import json
import os
import re
import smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Any, Set
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
SEEN_PATH = ROOT / "seen_jobs.json"
KST = timezone(timedelta(hours=9))


def log(msg: str) -> None:
    print(msg, flush=True)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_all_config() -> Dict[str, Any]:
    settings = load_json(CONFIG_DIR / "settings.json")
    keywords = load_json(CONFIG_DIR / "keywords.json")
    sources = []
    for filename in ["game_companies.json", "esports_teams.json", "public_orgs.json", "universities.json"]:
        data = load_json(CONFIG_DIR / filename)
        for source in data.get("sources", []):
            source["config_file"] = filename
            sources.append(source)
    return {"settings": settings, "keywords": keywords, "sources": sources}


def load_seen() -> Set[str]:
    if not SEEN_PATH.exists():
        return set()
    try:
        with SEEN_PATH.open("r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: Set[str]) -> None:
    with SEEN_PATH.open("w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def contains_any(text: str, keywords: List[str]) -> bool:
    lower = (text or "").lower()
    return any(k.lower() in lower for k in keywords)


def collect_keywords(group_names: List[str], keywords: Dict[str, List[str]]) -> List[str]:
    result = []
    for group in group_names:
        result.extend(keywords.get(group, []))
    return list(dict.fromkeys(result))


def fetch_page(url: str, timeout: int) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8,zh-CN;q=0.7,zh;q=0.6",
    }
    log(f"    접속: {url}")
    response = requests.get(url, headers=headers, timeout=timeout)
    log(f"    응답: {response.status_code}")
    response.raise_for_status()
    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding
    return response.text


def extract_items(source: Dict[str, Any], url: str, html: str, max_items: int) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    items = []

    title = normalize(soup.title.get_text(" ", strip=True) if soup.title else source["name"])
    page_text = normalize(soup.get_text(" ", strip=True))

    items.append({
        "id": f'{source["name"]}|{url}|PAGE|{title}',
        "source": source["name"],
        "profile": source["profile"],
        "config_file": source.get("config_file", ""),
        "title": title,
        "url": url,
        "text": page_text[:12000],
    })

    for a in soup.find_all("a"):
        link_title = normalize(a.get_text(" ", strip=True))
        href = a.get("href") or ""
        if not link_title or len(link_title) < 2:
            continue
        full_url = urljoin(url, href)
        surrounding = normalize(a.parent.get_text(" ", strip=True) if a.parent else link_title)

        items.append({
            "id": f'{source["name"]}|{full_url}|{link_title[:160]}',
            "source": source["name"],
            "profile": source["profile"],
            "config_file": source.get("config_file", ""),
            "title": link_title[:240],
            "url": full_url,
            "text": surrounding[:3000],
        })

        if len(items) >= max_items:
            break

    return items


def is_match(item: Dict[str, str], settings: Dict[str, Any], keywords: Dict[str, List[str]]) -> bool:
    profile_name = item.get("profile", "esports")
    profile = settings["matching_profiles"].get(profile_name, settings["matching_profiles"]["esports"])
    text = f'{item.get("source","")} {item.get("title","")} {item.get("text","")}'

    excludes = collect_keywords(profile.get("exclude_groups", []), keywords)
    if contains_any(text, excludes):
        return False

    includes = collect_keywords(profile.get("include_groups", []), keywords)
    if contains_any(text, includes):
        return True

    fallbacks = collect_keywords(profile.get("fallback_groups", []), keywords)
    if fallbacks and contains_any(text, fallbacks):
        return True

    for combo in profile.get("combo_groups", []):
        if all(contains_any(text, collect_keywords([group], keywords)) for group in combo):
            return True

    return False


def send_email(subject: str, body: str, to_email: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER") or os.getenv("EMAIL_USER")
    smtp_password = os.getenv("SMTP_PASSWORD") or os.getenv("EMAIL_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM") or smtp_user

    if not smtp_user or not smtp_password:
        log("이메일 정보 없음: 테스트 모드로 출력만 합니다.")
        log(f"제목: {subject}")
        log(body[:4000])
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = to_email

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_from, [to_email], msg.as_string())

    log(f"이메일 발송 완료: {to_email}")


def run() -> int:
    log("=== Korea eSports Career Alert v7 시작 ===")
    config = load_all_config()
    settings = config["settings"]
    keywords = config["keywords"]
    sources = config["sources"]

    timeout = int(settings["runtime"].get("request_timeout_seconds", 25))
    max_items = int(settings["runtime"].get("max_items_per_source", 300))
    initial_notify = bool(settings["runtime"].get("initial_notify_existing", False))
    first_run = not SEEN_PATH.exists()

    seen = load_seen()
    matches = []
    stats = {"sources": 0, "urls": 0, "errors": 0, "candidates": 0}

    log(f"수집 소스: {len(sources)}개")
    log(f"첫 실행 여부: {first_run}")
    log(f"첫 실행 기존 공고 메일 발송: {initial_notify}")

    for source in sources:
        stats["sources"] += 1
        log(f'\n[{source.get("config_file","")}] {source["name"]} / profile={source["profile"]}')
        for url in source.get("urls", []):
            stats["urls"] += 1
            try:
                html = fetch_page(url, timeout)
                items = extract_items(source, url, html, max_items)
                stats["candidates"] += len(items)
                log(f"    후보 추출: {len(items)}개")

                new_count = 0
                for item in items:
                    if item["id"] in seen:
                        continue

                    if is_match(item, settings, keywords):
                        seen.add(item["id"])
                        new_count += 1
                        if (not first_run) or initial_notify:
                            matches.append(item)
                            log(f'    신규 매칭: {item["title"][:90]}')
                        else:
                            log(f'    첫 실행 저장만: {item["title"][:90]}')

                log(f"    이번 URL 신규 매칭 수: {new_count}")

            except Exception as e:
                stats["errors"] += 1
                log(f"    오류 - 건너뜀: {type(e).__name__}: {e}")

    save_seen(seen)

    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    if first_run and not initial_notify:
        log("\n첫 실행이므로 기존 매칭 공고는 저장만 했습니다.")
        log("다음 실행부터 새로 감지되는 공고만 메일 알림 대상입니다.")
        log("=== Korea eSports Career Alert v7 종료 ===")
        return 0

    if not matches:
        log("\n신규 매칭 공고 없음")
        log(f"요약: 소스 {stats['sources']}개 / URL {stats['urls']}개 / 후보 {stats['candidates']}개 / 오류 {stats['errors']}개")
        log("=== Korea eSports Career Alert v7 종료 ===")
        return 0

    lines = [
        "한국 e스포츠/게임사/공공기관/교수초빙/Tencent·NetEase·Garena 한국 관련 신규 공고가 발견되었습니다.",
        f"발견 시각: {now}",
        f"신규 매칭: {len(matches)}건",
        f"검사 요약: 소스 {stats['sources']}개 / URL {stats['urls']}개 / 후보 {stats['candidates']}개 / 오류 {stats['errors']}개",
        "",
    ]

    for idx, item in enumerate(matches, 1):
        lines.extend([
            f"{idx}. {item['title']}",
            f"출처: {item['source']}",
            f"분류: {item.get('config_file','')} / {item.get('profile','')}",
            f"링크: {item['url']}",
            "-" * 80,
        ])

    send_email(f"[e스포츠 공고 알림] 신규 공고 {len(matches)}건", "\n".join(lines), settings["email"]["to"])

    log("=== Korea eSports Career Alert v7 종료 ===")
    return 0
