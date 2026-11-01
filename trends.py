"""X에서 Hyperliquid 화제글을 모아 관리자 텔레그램 DM으로 트렌드 리포트를 보낸다.

GitHub Actions에서 8시간마다 실행된다.
1. X 최근 검색으로 지난 8시간의 Hyperliquid 관련 글을 가져온다 (가져온 글 수만큼 X 요금 발생).
2. 반응(좋아요·리포스트·조회수)으로 순위를 매겨 상위 후보를 고른다.
3. Claude가 그중 Top 5를 골라 한국어로 요약하고 텔레그램 공지 초안을 쓴다.
4. 관리자 DM으로 보낸다. 실제 공지는 관리자가 직접 다듬어 채널에 올린다.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import anthropic
import requests
from requests_oauthlib import OAuth1

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_ADMIN_ID = os.environ["TELEGRAM_ADMIN_ID"].strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
# 한 번에 가져올 글 수 (10~100). 글 1개마다 X 읽기 요금이 붙는다.
FETCH_COUNT = int(os.environ.get("TRENDS_FETCH_COUNT") or 50)
WINDOW_HOURS = int(os.environ.get("TRENDS_WINDOW_HOURS") or 8)
QUERY = os.environ.get("TRENDS_QUERY") or "(Hyperliquid OR HyperEVM OR HyperCore) -is:retweet -is:reply"
TOP_N = 5
CANDIDATES = 15  # Claude에게 넘길 후보 수 (스팸 걸러내고 Top 5 고르기용)

KST = timezone(timedelta(hours=9))


def tg_send(text):
    # 텔레그램 메시지는 4096자 제한이 있어 나눠 보낸다
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > 4000:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    chunks.append(cur)
    for c in chunks:
        if DRY_RUN:
            print(c)
            continue
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={
            "chat_id": TG_ADMIN_ID, "text": c, "disable_web_page_preview": True}, timeout=30)
        if not r.json().get("ok"):
            raise RuntimeError(f"Telegram 전송 실패: {r.text}")


# ---------------------------------------------------------------- X 검색

def fetch_posts():
    start = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    r = requests.get(
        "https://api.x.com/2/tweets/search/recent",
        auth=OAuth1(os.environ["X_API_KEY"], os.environ["X_API_SECRET"],
                    os.environ["X_ACCESS_TOKEN"], os.environ["X_ACCESS_TOKEN_SECRET"]),
        params={
            "query": QUERY,
            "max_results": max(10, min(100, FETCH_COUNT)),
            "sort_order": "relevancy",
            "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tweet.fields": "public_metrics,created_at,lang,author_id",
            "expansions": "author_id",
            "user.fields": "username,name,verified,public_metrics",
        },
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"X 검색 실패 ({r.status_code}): {r.text[:500]}")
    data = r.json()
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    posts = []
    for t in data.get("data", []):
        u = users.get(t["author_id"], {})
        m = t.get("public_metrics", {})
        posts.append({
            "id": t["id"],
            "text": t["text"],
            "author": u.get("username", "?"),
            "followers": u.get("public_metrics", {}).get("followers_count", 0),
            "likes": m.get("like_count", 0),
            "reposts": m.get("retweet_count", 0) + m.get("quote_count", 0),
            "replies": m.get("reply_count", 0),
            "views": m.get("impression_count", 0),
            "url": f"https://x.com/{u.get('username', 'i')}/status/{t['id']}",
        })
    return posts


def score(p):
    return p["likes"] + 3 * p["reposts"] + p["replies"] + p["views"] / 1000


# ---------------------------------------------------------------- Claude 요약

SYSTEM = """당신은 Hyperliquid 한국 커뮤니티(텔레그램 공지 채널 'Hyperliquid KR')의 에디터를 돕습니다.
X에서 모은 Hyperliquid 관련 글 후보를 받아:
1. 스팸, 홍보성 에어드랍 사기, 단순 가격 자랑, 의미 없는 글을 제외하고
   한국 커뮤니티가 알아야 할 소식(제품·업데이트, 상장, 생태계 프로젝트, 주요 지표, 업계 이슈) 위주로 최대 5개를 고릅니다.
2. 각 글을 한국어 2~3문장으로 요약합니다. 원문에 없는 사실을 추가하지 말고, 확인되지 않은 주장은 '주장'임을 밝힙니다.
3. 고른 소식을 바탕으로 텔레그램 공지 초안을 하나 씁니다. 친근하지만 공식 채널다운 말투로, 이모지는 절제해서 쓰고,
   투자 권유로 읽힐 표현은 피합니다. 출처가 필요한 곳에는 원문 링크를 넣습니다.
쓸 만한 소식이 없으면 items를 비우고 초안에 그 사실을 짧게 씁니다."""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "후보 번호"},
                    "summary_ko": {"type": "string"},
                },
                "required": ["index", "summary_ko"],
                "additionalProperties": False,
            },
        },
        "telegram_draft": {"type": "string"},
    },
    "required": ["items", "telegram_draft"],
    "additionalProperties": False,
}


def summarize(candidates):
    listing = "\n\n".join(
        f"[{i}] @{p['author']} (팔로워 {p['followers']:,}) "
        f"❤️{p['likes']} 🔁{p['reposts']} 💬{p['replies']} 👁{p['views']}\n{p['url']}\n{p['text']}"
        for i, p in enumerate(candidates)
    )
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model="claude-opus-5-5",
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # 안전 필터로 거절되면 다른 모델로 자동 재시도
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": f"지난 {WINDOW_HOURS}시간 X 후보 글:\n\n{listing}"}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude가 요약을 거절했습니다")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


# ---------------------------------------------------------------- 메인

def fmt_num(n):
    return f"{n / 1000:.1f}K" if n >= 1000 else str(n)


def main():
    now = datetime.now(KST).strftime("%m/%d %H시")
    try:
        posts = fetch_posts()
        print(f"X에서 {len(posts)}개 가져옴")
        if not posts:
            tg_send(f"🔥 Hyperliquid 트렌드 ({now})\n\n지난 {WINDOW_HOURS}시간 동안 새 화제글이 없습니다.")
            return
        candidates = sorted(posts, key=score, reverse=True)[:CANDIDATES]
        result = summarize(candidates)
    except Exception as e:
        tg_send(f"⚠️ Hyperliquid 트렌드 리포트 실패 ({now})\n\n{str(e)[:800]}")
        raise

    lines = [f"🔥 Hyperliquid 트렌드 Top {TOP_N} ({now}, 지난 {WINDOW_HOURS}시간)"]
    for n, item in enumerate(result["items"][:TOP_N], 1):
        if not 0 <= item["index"] < len(candidates):
            continue
        p = candidates[item["index"]]
        lines.append(
            f"{n}. {item['summary_ko']}\n"
            f"   ❤️ {fmt_num(p['likes'])} · 🔁 {fmt_num(p['reposts'])} · 👁 {fmt_num(p['views'])} — @{p['author']}\n"
            f"   {p['url']}"
        )
    if len(lines) == 1:
        lines.append("공지할 만한 소식이 없었습니다.")
    lines.append("📝 텔레그램 공지 초안 (복사해서 다듬어 쓰세요)")
    tg_send("\n\n".join(lines))
    tg_send(result["telegram_draft"])  # 초안은 따로 보내 길게 눌러 복사하기 쉽게


if __name__ == "__main__":
    main()
