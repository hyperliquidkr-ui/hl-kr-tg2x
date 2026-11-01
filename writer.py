"""관리자가 봇 DM에 X 링크·웹사이트 링크를 보내면 공지방 스타일(STYLE.md) 초안을 써 준다.

- X 링크: X API로 글 전문·이미지·인용 원문을 가져와 읽는다.
- 웹사이트 링크: Claude의 web_fetch 도구로 페이지를 읽는다. 미리보기 이미지는 페이지 대표 이미지(og:image).
- 받은 초안에 답장으로 "더 짧게" 같은 요청을 보내면 그 초안을 고쳐 다시 보낸다.
초안 미리보기에는 트렌드 리포트와 같은 [공지방에 게시]/[건너뛰기] 버튼이 붙는다 (처리는 bot.py).
"""

import base64
import re

import anthropic
import requests

import trends

X_STATUS_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/\w+/status/(\d+)")
URL_RE = re.compile(r"https?://[^\s)>\]]+")

WRITE_SYSTEM = f"""당신은 Hyperliquid 한국 커뮤니티 텔레그램 공지 채널 'Hyperliquid_한국지부 공지방'의 에디터입니다.
관리자가 DM으로 보낸 자료(X 글, 웹페이지 링크, 메모)로 공지 초안을 하나 씁니다.
- 아래 스타일 가이드를 그대로 따릅니다: 제목 → 본문 → 레퍼런스 링크. 푸터는 쓰지 않습니다 (자동으로 붙음).
- 웹페이지 링크는 web_fetch 도구로 직접 읽고, X 글은 함께 주어진 본문·이미지·인용 원문을 읽습니다.
- 핵심 내용을 빠짐없이 축약해서 담고, 레퍼런스 링크에는 관리자가 보낸 원문 링크를 씁니다.
- 관리자 메시지에 요청 사항(길이, 강조점, 말투 등)이 있으면 따릅니다.
- '이전 초안'이 주어지면 관리자 메시지는 그 초안에 대한 수정 요청이니, 요청대로 고친 전체 초안을 씁니다.
- 원문에 없는 사실은 추가하지 않고, 확인되지 않은 주장은 그렇다고 밝힙니다.
- 최종 결과는 초안 본문만 <draft>와 </draft> 사이에 씁니다.

<style_guide>
{trends.STYLE}
</style_guide>"""


def tg_file_b64(file_id):
    path = trends.tg("getFile", file_id=file_id)["file_path"]
    r = requests.get(f"https://api.telegram.org/file/bot{trends.TG_TOKEN}/{path}", timeout=60)
    r.raise_for_status()
    return base64.standard_b64encode(r.content).decode()


def og_image(url):
    """웹페이지 대표 이미지 주소. 못 찾으면 None."""
    try:
        html = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).text[:300_000]
    except requests.RequestException:
        return None
    for pattern in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image'):
        m = re.search(pattern, html, re.I)
        if m and m.group(1).startswith("http"):
            return m.group(1)
    return None


def claude_write(content, use_web):
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": content}]
    params = dict(
        model="claude-opus-5-5",
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # 안전 필터로 거절되면 다른 모델로 자동 재시도
        output_config={"effort": "medium"},
        system=WRITE_SYSTEM,
    )
    if use_web:
        params["tools"] = [{"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5}]
    for _ in range(5):  # 웹페이지를 읽느라 응답이 중간에 멈추면(pause_turn) 이어서 받는다
        response = client.beta.messages.create(messages=messages, **params)
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude가 요청을 거절했습니다")
    text = "".join(b.text for b in response.content if b.type == "text")
    m = re.search(r"<draft>(.*?)</draft>", text, re.S)
    draft = (m.group(1) if m else text).strip()
    if not draft:
        raise RuntimeError("초안이 비어 있습니다")
    return draft


def own_media(msg):
    """메시지에 직접 붙은 사진·영상 (텔레그램 file_id라 그대로 다시 보낼 수 있다)."""
    if msg.get("photo"):
        return {"photos": [msg["photo"][-1]["file_id"]], "video_urls": []}
    if msg.get("video"):
        return {"photos": [], "video_urls": [msg["video"]["file_id"]]}
    return None


def handle(msg, text, prev_draft):
    """관리자 DM 메시지 하나를 처리한다. text: 숨은 링크까지 펼친 메시지 글, prev_draft: 답장 대상 초안."""
    urls = list(dict.fromkeys(URL_RE.findall(text)))
    x_ids = [m.group(1) for u in urls if (m := X_STATUS_RE.match(u))][:5]
    web = [u for u in urls if not X_STATUS_RE.match(u)][:3]
    reply = msg.get("reply_to_message") or {}
    if not (x_ids or web or prev_draft):
        trends.tg("sendMessage", chat_id=trends.TG_ADMIN_ID,
                  text="공지로 쓸 X 링크나 웹사이트 링크를 보내 주세요.\n"
                       "받은 초안을 고치려면 그 초안에 답장으로 수정 요청을 보내 주세요.")
        return

    status = trends.tg("sendMessage", chat_id=trends.TG_ADMIN_ID, text="✍️ 공지 초안을 쓰는 중입니다...")
    try:
        tweets = trends.parse_tweets(trends.x_get("tweets", {"ids": ",".join(x_ids), **trends.TWEET_PARAMS})) \
            if x_ids else []
        quoted = trends.fetch_quoted(tweets)
        content = [{"type": "text", "text": f"관리자 메시지:\n{text or '(없음)'}"}]
        if prev_draft:
            content.append({"type": "text", "text": f"이전 초안:\n{prev_draft}"})
        for t in tweets:
            q = quoted.get(t["quoted_id"])
            block = f"X 글 — @{t['author']} {t['url']}\n{t['text']}"
            if q:
                block += f"\n\n(이 글이 인용한 원문 — @{q['author']} {q['url']})\n{q['text']}"
            content.append({"type": "text", "text": block})
            for url in (t["images"] + (q["images"] if q else []))[:3]:
                content.append({"type": "image", "source": {"type": "url", "url": url}})
        for u in web:
            content.append({"type": "text", "text": f"웹페이지 (web_fetch로 읽어 주세요): {u}"})
        if msg.get("photo"):
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": tg_file_b64(msg["photo"][-1]["file_id"])}})
        draft = claude_write(content, use_web=bool(web))

        # 미리보기에 붙일 미디어: 직접 붙인 것 → 고치는 초안의 것 → X 원문 → 웹페이지 대표 이미지
        media = own_media(msg) or own_media(reply)
        if not media:
            with_media = next((t for t in tweets if t["photos"] or t["video_urls"]), None)
            if with_media:
                media = {"photos": with_media["photos"], "video_urls": with_media["video_urls"]}
        if not media and web:
            img = og_image(web[0])
            media = {"photos": [img] if img else [], "video_urls": []}
        trends.send_draft(draft, media or {"photos": [], "video_urls": []}, trends.load_footer())
    finally:
        try:
            trends.tg("deleteMessage", chat_id=trends.TG_ADMIN_ID, message_id=status["message_id"])
        except RuntimeError:
            pass
