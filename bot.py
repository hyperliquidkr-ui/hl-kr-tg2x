"""Telegram 공지방 → X(트위터) 미러 봇.

GitHub Actions에서 몇 분마다 실행된다. 텔레그램 getUpdates로 아직 처리하지 않은
새 글을 받아 X에 올리고, 성공한 글까지만 텔레그램에 '처리 완료'로 확인(offset)한다.
그래서 별도의 상태 파일이 필요 없고, 실패한 글은 다음 실행 때 다시 시도된다.
(텔레그램은 확인되지 않은 업데이트를 24시간 보관한다.)
"""

import os
import re
import sys
import time
import json

import requests
from requests_oauthlib import OAuth1

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
# X Premium 계정이면 25000, 아니면 280 (한글은 1글자를 2로 계산하므로 약 140자)
MAX_WEIGHT = int(os.environ.get("X_MAX_WEIGHT") or 280)
# 앨범(사진 여러 장)이 올라가는 도중에 실행되면 일부만 올라갈 수 있어서,
# 이 시간(초)보다 최근 앨범은 다음 실행으로 미룬다.
ALBUM_SETTLE_SECS = 90

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
X_API = "https://api.x.com/2"


# ---------------------------------------------------------------- Telegram

def tg(method, **params):
    r = requests.get(f"{TG_API}/{method}", params=params, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} 실패: {data.get('description')}")
    return data["result"]


def tg_download(file_id):
    path = tg("getFile", file_id=file_id)["file_path"]
    r = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}", timeout=60)
    r.raise_for_status()
    return r.content


def post_of(update):
    # 채널은 channel_post, 그룹은 message 로 들어온다. 수정된 글은 무시한다.
    return update.get("channel_post") or update.get("message")


def with_hidden_links(text, entities):
    """'여기를 클릭' 같은 숨은 링크(text_link)는 X에서 사라지므로 뒤에 (URL)을 붙인다."""
    links = [e for e in entities or [] if e["type"] == "text_link"]
    if not links:
        return text
    # 텔레그램 offset은 UTF-16 단위다.
    b = text.encode("utf-16-le")
    for e in sorted(links, key=lambda e: e["offset"], reverse=True):
        start, end = e["offset"] * 2, (e["offset"] + e["length"]) * 2
        if b[start:end].decode("utf-16-le").strip() == e["url"]:
            continue
        b = b[:end] + f" ({e['url']})".encode("utf-16-le") + b[end:]
    return b.decode("utf-16-le")


def post_text(msg):
    if "text" in msg:
        return with_hidden_links(msg["text"], msg.get("entities"))
    if "caption" in msg:
        return with_hidden_links(msg["caption"], msg.get("caption_entities"))
    return ""


# ---------------------------------------------------------------- X

def x_auth():
    return OAuth1(
        os.environ["X_API_KEY"],
        os.environ["X_API_SECRET"],
        os.environ["X_ACCESS_TOKEN"],
        os.environ["X_ACCESS_TOKEN_SECRET"],
    )


class DuplicateTweet(Exception):
    pass


def x_upload_image(content, auth):
    r = requests.post(
        f"{X_API}/media/upload",
        auth=auth,
        files={"media": ("image.jpg", content, "image/jpeg")},
        data={"media_category": "tweet_image", "media_type": "image/jpeg"},
        timeout=120,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"X 이미지 업로드 실패 ({r.status_code}): {r.text}")
    return r.json()["data"]["id"]


def x_tweet(text, auth, media_ids=None, reply_to=None):
    body = {"text": text}
    if media_ids:
        body["media"] = {"media_ids": media_ids}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": reply_to}
    r = requests.post(f"{X_API}/tweets", auth=auth, json=body, timeout=30)
    if r.status_code == 403 and "duplicate" in r.text.lower():
        raise DuplicateTweet(r.text)
    if r.status_code >= 300:
        raise RuntimeError(f"X 게시 실패 ({r.status_code}): {r.text}")
    return r.json()["data"]["id"]


# ---------------------------------------------------------------- 글자 수 계산 / 나누기

# X는 링크를 길이와 상관없이 23으로 센다. 도메인만 쓴 링크도 잡도록 넓게 매칭한다
# (넉넉하게 세는 쪽은 안전하다).
URL_RE = re.compile(r"(?:https?://|www\.)\S+|\b(?:[\w-]+\.)+[a-zA-Z]{2,}(?:/\S*)?")


def _char_weight(ch):
    c = ord(ch)
    if c <= 4351 or 8192 <= c <= 8205 or 8208 <= c <= 8223 or 8242 <= c <= 8247:
        return 1
    return 2  # 한글, 한자, 이모지 등


def weight(s):
    total, pos = 0, 0
    for m in URL_RE.finditer(s):
        total += sum(map(_char_weight, s[pos:m.start()])) + 23
        pos = m.end()
    return total + sum(map(_char_weight, s[pos:]))


def split_text(text, limit, seps=("\n\n", "\n", " ")):
    """문단 → 줄 → 단어 → 글자 순으로 가능한 큰 단위로 나눈다."""
    text = text.strip()
    if not text:
        return []
    if weight(text) <= limit:
        return [text]
    if not seps:
        out, cur = [], ""
        for ch in text:
            if weight(cur + ch) > limit:
                out.append(cur)
                cur = ch
            else:
                cur += ch
        return out + [cur]
    sep, rest = seps[0], seps[1:]
    out, cur = [], ""
    for part in text.split(sep):
        cand = part if not cur else cur + sep + part
        if weight(cand) <= limit:
            cur = cand
            continue
        if weight(part) <= limit:
            out.append(cur)
            cur = part
        else:
            # 너무 긴 덩어리는 앞 내용과 이어서 더 작은 단위로 나눠 앞 트윗을 꽉 채운다
            sub = split_text(cand, limit, rest)
            out.extend(sub[:-1])
            cur = sub[-1] if sub else ""
    if cur:
        out.append(cur)
    return [c.strip() for c in out if c.strip()]


# ---------------------------------------------------------------- 메인

def group_posts(updates):
    """앨범(media_group_id가 같은 여러 메시지)을 하나의 글로 묶는다."""
    posts = []  # [(마지막 update_id, [msg, ...])]
    by_group = {}
    for u in updates:
        msg = post_of(u)
        if not msg or str(msg["chat"]["id"]) != TG_CHAT_ID:
            posts.append((u["update_id"], []))  # 대상이 아닌 업데이트: 확인만 한다
            continue
        gid = msg.get("media_group_id")
        if gid and gid in by_group:
            idx = by_group[gid]
            posts[idx] = (u["update_id"], posts[idx][1] + [msg])
            continue
        if gid:
            by_group[gid] = len(posts)
        posts.append((u["update_id"], [msg]))
    return posts


def mirror(msgs, auth):
    text = "\n".join(t for t in (post_text(m) for m in msgs) if t)
    photos = [m["photo"][-1]["file_id"] for m in msgs if m.get("photo")][:4]
    skipped = [k for m in msgs for k in ("video", "animation", "document", "poll") if k in m]
    if skipped:
        print(f"  ! 지원하지 않는 첨부({', '.join(skipped)})는 빼고 올립니다")
    chunks = split_text(text, MAX_WEIGHT)
    if not chunks and not photos:
        print("  - 올릴 내용이 없어 건너뜁니다")
        return

    print(f"  트윗 {max(len(chunks), 1)}개, 사진 {len(photos)}장")
    for i, c in enumerate(chunks or [""]):
        print(f"  --- [{i + 1}] (weight {weight(c)}/{MAX_WEIGHT})\n{c}")
    if DRY_RUN:
        return

    media_ids = [x_upload_image(tg_download(f), auth) for f in photos]
    reply_to = None
    for i, chunk in enumerate(chunks or [""]):
        try:
            reply_to = x_tweet(chunk, auth, media_ids if i == 0 else None, reply_to)
            print(f"  ✓ 게시됨: https://x.com/i/status/{reply_to}")
        except DuplicateTweet:
            print("  - 이미 올라간 내용이라 건너뜁니다")
            if i == 0:
                return


def main():
    try:
        updates = tg("getUpdates", timeout=0, limit=100,
                     allowed_updates=json.dumps(["channel_post", "message"]))
    except RuntimeError as e:
        if "webhook" in str(e).lower():
            sys.exit("이 봇에 webhook이 설정되어 있습니다. deleteWebhook을 먼저 호출하세요.")
        raise
    if not updates:
        print("새 글 없음")
        return

    posts = group_posts(updates)
    # 마지막 글이 아직 올라가는 중인 앨범이면 다음 실행으로 미룬다.
    if posts and posts[-1][1] and posts[-1][1][-1].get("media_group_id"):
        if time.time() - posts[-1][1][-1]["date"] < ALBUM_SETTLE_SECS:
            print("방금 올라온 앨범은 다음 실행에서 처리합니다")
            posts.pop()

    auth = None if DRY_RUN else x_auth()
    done_id, failed = None, False
    for update_id, msgs in posts:
        if msgs:
            print(f"[텔레그램 글 #{msgs[0]['message_id']}]")
            try:
                mirror(msgs, auth)
            except Exception as e:  # 실패한 글부터는 확인하지 않고 다음 실행에서 재시도
                print(f"  ✗ {e}")
                failed = True
                break
        done_id = update_id

    if done_id is not None and not DRY_RUN:
        tg("getUpdates", offset=done_id + 1, limit=1, timeout=0)  # 여기까지 처리 완료로 확인
    if DRY_RUN:
        print("(DRY_RUN: X에 올리지 않았고, 텔레그램에도 처리 완료로 표시하지 않았습니다)")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
