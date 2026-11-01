"""X에서 Hyperliquid 화제글을 모아 관리자 텔레그램 DM으로 트렌드 리포트를 보낸다.

GitHub Actions에서 6시간마다 실행된다.
1. X 최근 검색으로 지난 6시간의 Hyperliquid 관련 글을 가져온다 (가져온 글 수만큼 X 요금 발생).
2. 반응(좋아요·리포스트·조회수)으로 순위를 매겨 상위 후보를 고른다.
3. Claude가 그중 Top 5를 골라 한국어로 요약하고, 소식마다 공지방 스타일(STYLE.md)의 초안을 쓴다.
4. 관리자 DM으로 보낸다: 요약 목록 1개 + 소식마다 원문 사진·영상을 붙인 초안 미리보기.
   미리보기의 [공지방에 게시]를 누르면 bot.py가 그 미리보기를 그대로 공지방에 복사한다.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests
from requests_oauthlib import OAuth1

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_ADMIN_ID = os.environ["TELEGRAM_ADMIN_ID"].strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
# 한 번에 가져올 글 수 (10~100). 글 1개마다 X 읽기 요금이 붙는다.
FETCH_COUNT = int(os.environ.get("TRENDS_FETCH_COUNT") or 30)
WINDOW_HOURS = int(os.environ.get("TRENDS_WINDOW_HOURS") or 6)
QUERY = os.environ.get("TRENDS_QUERY") or (
    "(Hyperliquid OR HyperEVM OR HyperCore OR $HYPE OR \"HIP-3\" OR HIP3) -is:retweet -is:reply")
TOP_N = 5
CANDIDATES = 15  # Claude에게 넘길 후보 수 (스팸 걸러내고 Top 5 고르기용)

STYLE = (Path(__file__).parent / "STYLE.md").read_text(encoding="utf-8")
# 푸터는 관리자 DM에 고정된 메시지를 그대로 쓴다. 공지방 푸터의 로고는 커스텀 이모지라
# 글자로는 재현이 안 되고, 고정 메시지의 서식 정보(entities)를 복사해야 그대로 나온다.
# 고정 메시지가 없을 때만 아래 기본 푸터를 쓴다.
DEFAULT_FOOTER = "🌧Hyperliquid_KR\n공지방 | 채팅방 | X"
DEFAULT_FOOTER_LINKS = [("공지방", "https://t.me/Hyperliquid_KR_announce"),
                        ("채팅방", "https://t.me/Hyperliquid_KR"),
                        (" X", "https://x.com/Hyperliquid_KR")]

KST = timezone(timedelta(hours=9))


def tg(method, **params):
    if DRY_RUN:
        print(f"[DRY_RUN] {method} {list(params)}")
        return {"message_id": 0} if method != "sendMediaGroup" else [{"message_id": 0}]
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=params, timeout=120)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} 실패: {data.get('description')}")
    return data["result"]


def tg_send(text, html_mode=False):
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
            print(c + "\n" + "-" * 40)
            continue
        body = {"chat_id": TG_ADMIN_ID, "text": c, "disable_web_page_preview": True}
        if html_mode:
            body["parse_mode"] = "HTML"
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=body, timeout=30)
        if not r.json().get("ok"):
            raise RuntimeError(f"Telegram 전송 실패: {r.text}")


# ---------------------------------------------------------------- X 검색

def x_auth():
    return OAuth1(os.environ["X_API_KEY"], os.environ["X_API_SECRET"],
                  os.environ["X_ACCESS_TOKEN"], os.environ["X_ACCESS_TOKEN_SECRET"])


def x_get(path, params):
    r = requests.get(f"https://api.x.com/2/{path}", auth=x_auth(), params=params, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"X 조회 실패 ({r.status_code}): {r.text[:500]}")
    return r.json()


TWEET_PARAMS = {
    # note_tweet: Premium 계정의 긴 글 전문 (없으면 text는 앞부분만 잘려 옴)
    "tweet.fields": "public_metrics,created_at,lang,author_id,attachments,note_tweet,referenced_tweets",
    "expansions": "author_id,attachments.media_keys",
    "user.fields": "username,name,verified,public_metrics",
    "media.fields": "type,url,variants,preview_image_url",
}


def parse_tweets(data):
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    media = {m["media_key"]: m for m in data.get("includes", {}).get("media", [])}
    posts = []
    for t in data.get("data", []):
        u = users.get(t["author_id"], {})
        m = t.get("public_metrics", {})
        items = [media[k] for k in t.get("attachments", {}).get("media_keys", []) if k in media]
        photos = [m["url"] for m in items if m.get("type") == "photo" and m.get("url")]
        videos = [m for m in items if m.get("type") in ("video", "animated_gif")]
        # 영상은 화질별 mp4 주소 중 높은 화질부터 시도한다 (텔레그램은 20MB 넘는 주소는 못 받음)
        video_urls = sorted((v for m in videos[:1] for v in m.get("variants", [])
                             if v.get("content_type") == "video/mp4"),
                            key=lambda v: v.get("bit_rate", 0), reverse=True)
        quoted = [r["id"] for r in t.get("referenced_tweets", []) if r["type"] == "quoted"]
        posts.append({
            "id": t["id"],
            "text": (t.get("note_tweet") or {}).get("text") or t["text"],
            "author": u.get("username", "?"),
            "followers": u.get("public_metrics", {}).get("followers_count", 0),
            "likes": m.get("like_count", 0),
            "reposts": m.get("retweet_count", 0) + m.get("quote_count", 0),
            "replies": m.get("reply_count", 0),
            "views": m.get("impression_count", 0),
            "media": "동영상" if videos else "사진" if photos else "",
            "photos": photos[:4],
            # Claude가 읽을 이미지: 사진, 없으면 영상 썸네일
            "images": photos[:4] or [m["preview_image_url"] for m in videos if m.get("preview_image_url")][:1],
            "video_urls": [v["url"] for v in video_urls],
            "quoted_id": quoted[0] if quoted else None,
            "url": f"https://x.com/{u.get('username', 'i')}/status/{t['id']}",
        })
    return posts


def fetch_posts():
    start = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    return parse_tweets(x_get("tweets/search/recent", {
        "query": QUERY,
        "max_results": max(10, min(100, FETCH_COUNT)),
        "sort_order": "relevancy",
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        **TWEET_PARAMS,
    }))


def fetch_quoted(posts):
    """고른 글이 인용한 원문을 가져온다 (최대 TOP_N개라 읽기 요금도 그만큼만)."""
    ids = [p["quoted_id"] for p in posts if p["quoted_id"]]
    if not ids:
        return {}
    try:
        return {q["id"]: q for q in parse_tweets(x_get("tweets", {"ids": ",".join(ids), **TWEET_PARAMS}))}
    except RuntimeError as e:
        print(f"  인용 원문 가져오기 실패 (원문 없이 진행): {e}")
        return {}


def score(p):
    return p["likes"] + 3 * p["reposts"] + p["replies"] + p["views"] / 1000


# ---------------------------------------------------------------- Claude: 고르기 → 초안 쓰기

SELECT_SYSTEM = f"""당신은 Hyperliquid 한국 커뮤니티 텔레그램 공지 채널 'Hyperliquid_한국지부 공지방'의 에디터를 돕습니다.
X에서 모은 Hyperliquid 관련 글 후보 중에서
스팸, 에어드랍 사기성 홍보, 단순 가격 자랑, 의미 없는 글을 제외하고
한국 커뮤니티가 알아야 할 소식(제품·업데이트, 상장, 생태계 프로젝트, 주요 지표, 업계 이슈) 위주로 최대 {TOP_N}개를 고릅니다.
같은 소식을 다룬 글이 여러 개면 가장 원본에 가까운 글 하나만 고릅니다.
각 글을 한국어 1~2문장으로 요약합니다 (summary_ko, 에디터가 훑어보기용).
쓸 만한 소식이 없으면 items를 비웁니다."""

DRAFT_SYSTEM = f"""당신은 Hyperliquid 한국 커뮤니티 텔레그램 공지 채널 'Hyperliquid_한국지부 공지방'의 에디터입니다.
각 X 글(본문, 첨부 이미지, 인용한 원문 포함)을 읽고 아래 스타일 가이드를 그대로 따른 공지 초안을 씁니다.
- 제목 → 본문 → 레퍼런스 링크(해당 글의 URL) 순서이며, 푸터는 쓰지 않습니다 (자동으로 붙음).
- 이미지 속 글자·표·차트 내용과 인용 원문까지 읽되, 스타일 가이드의 분량에 맞게 핵심만 짧게 축약합니다.
- 원문에 없는 사실은 추가하지 말고, 확인되지 않은 주장은 그렇다고 밝힙니다.

<style_guide>
{STYLE}
</style_guide>"""


def schema(fields):
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {"index": {"type": "integer", "description": "글 번호"},
                           **{f: {"type": "string"} for f in fields}},
            "required": ["index", *fields],
            "additionalProperties": False,
        }}},
        "required": ["items"],
        "additionalProperties": False,
    }


def ask_claude(system, content, fields):
    response = anthropic.Anthropic().beta.messages.create(
        model="claude-opus-5-5",
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # 안전 필터로 거절되면 다른 모델로 자동 재시도
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema(fields)}},
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude가 요청을 거절했습니다")
    return json.loads(next(b.text for b in response.content if b.type == "text"))["items"]


def select(candidates):
    """1단계: 글 텍스트만 보고 Top N을 고른다 (이미지를 안 넣어 저렴)."""
    listing = "\n\n".join(
        f"[{i}] @{p['author']} (팔로워 {p['followers']:,}) "
        f"❤️{p['likes']} 🔁{p['reposts']} 💬{p['replies']} 👁{p['views']}"
        f"{' [' + p['media'] + ' 첨부]' if p['media'] else ''}\n{p['url']}\n{p['text']}"
        for i, p in enumerate(candidates)
    )
    items = ask_claude(SELECT_SYSTEM, f"지난 {WINDOW_HOURS}시간 X 후보 글:\n\n{listing}", ["summary_ko"])
    return [(it["summary_ko"], candidates[it["index"]]) for it in items[:TOP_N]
            if 0 <= it["index"] < len(candidates)]


def write_drafts(posts):
    """2단계: 고른 글만 이미지·인용 원문까지 보여주고 초안을 쓴다."""
    quoted = fetch_quoted(posts)
    content = []
    for i, p in enumerate(posts):
        q = quoted.get(p["quoted_id"])
        block = f"[{i}] @{p['author']} {p['url']}\n{p['text']}"
        if q:
            block += f"\n\n(이 글이 인용한 원문 — @{q['author']} {q['url']})\n{q['text']}"
        content.append({"type": "text", "text": block})
        for url in (p["images"] + (q["images"] if q else []))[:3]:
            content.append({"type": "image", "source": {"type": "url", "url": url}})
    content.append({"type": "text", "text": "위 글마다 공지 초안을 써 주세요."})
    items = ask_claude(DRAFT_SYSTEM, content, ["draft"])
    return {it["index"]: it["draft"] for it in items if 0 <= it["index"] < len(posts)}


# ---------------------------------------------------------------- 초안 미리보기

def publish_buttons(ids):
    data = ",".join(map(str, ids))
    return {"inline_keyboard": [[
        {"text": "📤 공지방에 게시", "callback_data": f"pub:{data}"},
        {"text": "❌ 건너뛰기", "callback_data": f"skip:{data}"},
    ]]}


def u16(text):
    """텔레그램 서식 위치는 UTF-16 단위로 센다."""
    return len(text.encode("utf-16-le")) // 2


def load_footer():
    """관리자 DM에 고정된 푸터 메시지의 (글자, 서식)을 가져온다."""
    try:
        pinned = tg("getChat", chat_id=TG_ADMIN_ID).get("pinned_message") or {}
    except RuntimeError as e:
        print(f"  고정 메시지 읽기 실패: {e}")
        pinned = {}
    text = pinned.get("text") or pinned.get("caption") or ""
    if "Hyperliquid_KR" in text:
        return text.strip(), pinned.get("entities") or pinned.get("caption_entities") or []
    print("  ! DM에 고정된 푸터가 없어 기본 푸터를 씁니다")
    ents = []
    for word, url in DEFAULT_FOOTER_LINKS:
        pos = DEFAULT_FOOTER.rindex(word.strip())
        ents.append({"type": "text_link", "offset": u16(DEFAULT_FOOTER[:pos]),
                     "length": u16(word.strip()), "url": url})
    return DEFAULT_FOOTER, ents


def compose(draft, footer):
    """초안 + 푸터를 하나의 (글자, 서식)으로 합친다. 푸터 서식은 위치만 뒤로 민다."""
    head = draft.strip() + "\n\n"
    ftext, fents = footer
    shift = u16(head)
    return head + ftext, [dict(e, offset=e["offset"] + shift) for e in fents]


def send_media(p, caption=None):
    """원문 사진·영상을 보낸다. 보낸 메시지 번호 목록을 돌려주고, 못 보내면 빈 목록."""
    cap = {"caption": caption[0], "caption_entities": caption[1]} if caption else {}
    base = {"chat_id": TG_ADMIN_ID}
    if p["video_urls"]:
        for url in p["video_urls"]:
            try:
                return [tg("sendVideo", **base, video=url, **cap)["message_id"]]
            except RuntimeError as e:
                print(f"  영상 전송 실패, 다음 화질 시도: {e}")
        return []
    try:
        if len(p["photos"]) == 1:
            return [tg("sendPhoto", **base, photo=p["photos"][0], **cap)["message_id"]]
        if p["photos"]:
            group = [{"type": "photo", "media": u} for u in p["photos"]]
            group[0].update(cap)
            return [m["message_id"] for m in tg("sendMediaGroup", **base, media=group)]
    except RuntimeError as e:
        print(f"  사진 전송 실패: {e}")
    return []


def send_draft(draft, p, footer):
    """초안 미리보기를 보낸다. 공지방에 올라갈 모습 그대로이고, 끝에 게시 버튼이 붙는다."""
    body = compose(draft, footer)
    has_media = bool(p["photos"] or p["video_urls"])
    # 텔레그램 사진·영상 설명은 1024자까지라, 짧으면 미디어+설명 한 개로 보낸다
    if has_media and u16(body[0]) <= 1024:
        if len(p["photos"]) > 1 and not p["video_urls"]:  # 앨범에는 버튼을 못 붙여 따로 보낸다
            ids = send_media(p, caption=body)
            if ids:
                tg("sendMessage", chat_id=TG_ADMIN_ID, text="⬆️ 위 초안을 공지방에 올릴까요?",
                   reply_markup=publish_buttons(ids))
                return
        else:
            sent = send_media(p, caption=body)
            if sent:
                tg("editMessageReplyMarkup", chat_id=TG_ADMIN_ID, message_id=sent[0],
                   reply_markup=publish_buttons(sent))
                return
    # 설명이 길거나 미디어가 없으면: 미디어(있으면) → 본문 순서로 보낸다
    ids = send_media(p) if has_media else []
    if has_media and not ids:
        tg_send("⚠️ 원문 사진·영상을 가져오지 못했습니다. 아래 초안을 게시한 뒤 직접 첨부해 주세요.")
    msg = tg("sendMessage", chat_id=TG_ADMIN_ID, text=body[0], entities=body[1],
             link_preview_options={"is_disabled": True})
    ids.append(msg["message_id"])
    tg("editMessageReplyMarkup", chat_id=TG_ADMIN_ID, message_id=msg["message_id"],
       reply_markup=publish_buttons(ids))


# ---------------------------------------------------------------- 메인

def main():
    now = datetime.now(KST).strftime("%m/%d %H시")
    try:
        posts = fetch_posts()
        print(f"X에서 {len(posts)}개 가져옴")
        if not posts:
            return  # 새 화제글이 없으면 아무것도 보내지 않는다
        candidates = sorted(posts, key=score, reverse=True)[:CANDIDATES]
        selected = select(candidates)
        drafts = write_drafts([p for _, p in selected]) if selected else {}
    except Exception as e:
        tg_send(f"⚠️ Hyperliquid 트렌드 리포트 실패 ({now})\n\n{str(e)[:800]}")
        raise

    # 요약 목록 없이 소식별 초안 미리보기만 보낸다
    picked = [(drafts[i], p) for i, (summary, p) in enumerate(selected) if drafts.get(i)]
    for summary, p in selected:
        print(f"  - {summary} ({p['url']})")
    footer = load_footer() if picked else None
    for draft, p in picked:
        try:
            send_draft(draft, p, footer)
        except Exception as e:
            print(f"  ✗ 초안 전송 실패: {e}")
            tg_send(f"⚠️ 초안 전송 실패: {str(e)[:300]}\n\n{draft}")


if __name__ == "__main__":
    main()
