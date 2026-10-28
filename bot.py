"""Telegram 공지 채널 → X(트위터) 미러 봇 (관리자 DM 승인 후 게시).

GitHub Actions에서 5분마다 실행되고, 한 번 실행되면 RUN_SECS 동안 텔레그램을
long polling 하면서 바로바로 반응한다.

1. 채널에 새 공지가 올라오면 관리자 DM으로 원문을 전달하고 [게시]/[건너뛰기] 버튼을 보낸다.
2. 관리자가 [게시]를 누르면 그 공지를 X에 올린다.

처리한 업데이트는 텔레그램 offset으로 확인하므로 별도의 상태 저장이 필요 없다.
버튼에는 채널 메시지 번호만 담고, 게시할 때 원문을 다시 가져와 쓴다.
(텔레그램은 확인되지 않은 업데이트를 24시간 보관한다.)
"""

import os
import re
import sys
import time

import requests
from requests_oauthlib import OAuth1

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = re.sub(r"^(https?://)?t\.me/", "@", os.environ["TELEGRAM_CHAT_ID"].strip())
if TG_CHAT_ID and not TG_CHAT_ID.startswith(("@", "-")) and not TG_CHAT_ID.isdigit():
    TG_CHAT_ID = "@" + TG_CHAT_ID  # '@' 없이 채널 아이디만 넣은 경우
# 승인 요청을 받을 사람의 텔레그램 숫자 ID. 봇에게 /start를 보내면 봇이 알려준다.
TG_ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
# X Premium 계정이면 25000, 아니면 280 (한글은 1글자를 2로 계산하므로 약 140자)
MAX_WEIGHT = int(os.environ.get("X_MAX_WEIGHT") or 280)
# 한 번 실행될 때 텔레그램을 기다리는 시간(초). cron 간격(5분)보다 조금 짧게.
RUN_SECS = 0 if DRY_RUN else int(os.environ.get("RUN_SECS") or 240)
# 앨범(사진 여러 장)이 올라가는 도중이면 일부만 잡힐 수 있어서 잠시 기다렸다 처리한다.
ALBUM_SETTLE_SECS = 20

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
X_API = "https://api.x.com/2"


# ---------------------------------------------------------------- Telegram

def tg(method, **params):
    r = requests.post(f"{TG_API}/{method}", json=params, timeout=params.get("timeout", 0) + 30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} 실패: {data.get('description')}")
    return data["result"]


def tg_quiet(method, **params):
    try:
        return tg(method, **params)
    except Exception as e:
        print(f"  (무시) {e}")


def tg_download(file_id):
    path = tg("getFile", file_id=file_id)["file_path"]
    r = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}", timeout=60)
    r.raise_for_status()
    return r.content


def is_target(chat):
    # 숫자 ID(-100...) 또는 공개 채널 아이디(@이름) 둘 다 허용
    if TG_CHAT_ID.startswith("@"):
        return (chat.get("username") or "").lower() == TG_CHAT_ID[1:].lower()
    return str(chat["id"]) == TG_CHAT_ID


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


def has_content(msgs):
    return any(post_text(m) or m.get("photo") for m in msgs)


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


# ---------------------------------------------------------------- X 게시

def mirror(msgs, auth):
    """공지를 X에 올리고 첫 트윗 주소를 돌려준다. 이미 올라간 내용이면 None."""
    text = "\n".join(t for t in (post_text(m) for m in msgs) if t)
    photos = [m["photo"][-1]["file_id"] for m in msgs if m.get("photo")][:4]
    skipped = [k for m in msgs for k in ("video", "animation", "document", "poll") if k in m]
    if skipped:
        print(f"  ! 지원하지 않는 첨부({', '.join(skipped)})는 빼고 올립니다")
    chunks = split_text(text, MAX_WEIGHT) or [""]

    print(f"  트윗 {len(chunks)}개, 사진 {len(photos)}장")
    for i, c in enumerate(chunks):
        print(f"  --- [{i + 1}] (weight {weight(c)}/{MAX_WEIGHT})\n{c}")
    if DRY_RUN:
        return None

    media_ids = [x_upload_image(tg_download(f), auth) for f in photos]
    first = reply_to = None
    for i, chunk in enumerate(chunks):
        try:
            reply_to = x_tweet(chunk, auth, media_ids if i == 0 else None, reply_to)
            first = first or reply_to
            print(f"  ✓ 게시됨: https://x.com/i/status/{reply_to}")
        except DuplicateTweet:
            print("  - 이미 올라간 내용이라 건너뜁니다")
            if i == 0:
                return None
    return f"https://x.com/i/status/{first}"


# ---------------------------------------------------------------- 승인 흐름

def encode_ids(ids):
    s = ",".join(map(str, ids))
    return s if len(s) <= 60 else f"{ids[0]}-{ids[-1]}"  # callback_data는 64바이트 제한


def decode_ids(s):
    if "-" in s:
        a, b = map(int, s.split("-"))
        return list(range(a, b + 1))
    return [int(x) for x in s.split(",")]


def ask_approval(msgs):
    ids = [m["message_id"] for m in msgs]
    print(f"[새 공지 #{ids[0]}] 관리자에게 승인 요청")
    if DRY_RUN:
        return
    tg("forwardMessages", chat_id=TG_ADMIN_ID, from_chat_id=msgs[0]["chat"]["id"], message_ids=ids)
    data = encode_ids(ids)
    tg("sendMessage", chat_id=TG_ADMIN_ID,
       text="⬆️ 위 공지를 X(트위터)에 올릴까요?",
       reply_markup={"inline_keyboard": [[
           {"text": "✅ X에 게시", "callback_data": f"ok:{data}"},
           {"text": "❌ 건너뛰기", "callback_data": f"no:{data}"},
       ]]})


def fetch_posts(ids):
    """채널 원문을 관리자 DM으로 잠깐 전달해 내용을 읽고 바로 지운다."""
    msgs = []
    for i in ids:
        try:
            m = tg("forwardMessage", chat_id=TG_ADMIN_ID, from_chat_id=TG_CHAT_ID,
                   message_id=i, disable_notification=True)
        except RuntimeError as e:
            if "not found" in str(e).lower():
                continue  # 원본이 지워졌거나 앨범 번호 범위에 없는 메시지
            raise
        msgs.append(m)
        tg_quiet("deleteMessage", chat_id=TG_ADMIN_ID, message_id=m["message_id"])
    return msgs


def handle_callback(cq, auth):
    btn = cq.get("message") or {}
    if str(cq["from"]["id"]) != TG_ADMIN_ID:
        tg_quiet("answerCallbackQuery", callback_query_id=cq["id"], text="권한이 없습니다")
        return True
    action, _, data = (cq.get("data") or "").partition(":")
    ids = decode_ids(data)
    print(f"[승인 응답: {action} #{ids[0]}]")
    if DRY_RUN:
        return True

    def edit(text, keep_buttons=False):
        params = dict(chat_id=TG_ADMIN_ID, message_id=btn.get("message_id"), text=text)
        if keep_buttons:
            params["reply_markup"] = btn.get("reply_markup")
        tg_quiet("editMessageText", **params)

    if action == "no":
        tg_quiet("answerCallbackQuery", callback_query_id=cq["id"], text="건너뛰었습니다")
        edit("❌ 건너뛰었습니다")
        return True

    tg_quiet("answerCallbackQuery", callback_query_id=cq["id"], text="X에 올리는 중...")
    try:
        msgs = fetch_posts(ids)
        if not msgs:
            edit("⚠️ 채널에서 원본 공지를 찾을 수 없어 건너뛰었습니다")
            return True
        url = mirror(msgs, auth)
    except Exception as e:
        # 버튼을 남겨 두어 관리자가 다시 누르면 재시도할 수 있게 한다
        print(f"  ✗ {e}")
        edit(f"⚠️ X 게시 실패 — [게시]를 다시 누르면 재시도합니다\n\n{str(e)[:500]}", keep_buttons=True)
        return False
    edit(f"✅ X에 게시했습니다\n{url}" if url else "✅ 이미 X에 올라간 내용입니다")
    return True


def status_report():
    """설정 점검 결과. 로그를 보지 않아도 무엇이 문제인지 알 수 있게 한다."""
    lines = [f"• 채널 설정값: {TG_CHAT_ID or '(비어 있음)'}"]
    try:
        chat = tg("getChat", chat_id=TG_CHAT_ID)
        lines.append(f"• 채널 찾음: {chat.get('title')} ✅")
        me = tg("getMe")
        member = tg("getChatMember", chat_id=TG_CHAT_ID, user_id=me["id"])
        if member["status"] in ("administrator", "creator"):
            lines.append(f"• 봇(@{me['username']})이 채널 관리자 ✅")
        else:
            lines.append(f"• 봇(@{me['username']})이 채널 관리자가 아닙니다 ❌ → 채널 관리자로 추가해 주세요")
    except Exception as e:
        lines.append(f"• 채널 확인 실패 ❌ ({e})\n  → 봇이 채널 관리자인지, TELEGRAM_CHAT_ID가 맞는지 확인해 주세요")
    lines.append(f"• 관리자 ID: {TG_ADMIN_ID or '(비어 있음) ❌'}")
    x_keys = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    missing = [k for k in x_keys if not os.environ.get(k)]
    lines.append("• X 키 4개 ✅" if not missing else f"• X 키 없음 ❌: {', '.join(missing)}")
    return "\n".join(lines)


def handle_dm(msg):
    text = msg.get("text") or ""
    if msg["chat"]["type"] != "private" or not text.startswith(("/start", "/id", "/status")):
        return
    uid = str(msg["from"]["id"])
    print(f"[DM {text.split()[0]}] user {uid}")
    if DRY_RUN:
        return
    if TG_ADMIN_ID and uid == TG_ADMIN_ID:
        reply = "✅ 관리자로 등록되어 있습니다. 새 공지가 올라오면 여기로 승인 요청이 옵니다.\n\n"
    else:
        reply = (f"회원님의 텔레그램 ID는\n\n{uid}\n\n입니다. 이 숫자를 GitHub Secrets의 "
                 "TELEGRAM_ADMIN_ID에 넣으면 새 공지마다 여기로 승인 요청이 옵니다.\n\n")
    if not TG_ADMIN_ID or uid == TG_ADMIN_ID:
        reply += "[설정 점검]\n" + status_report()
    tg_quiet("sendMessage", chat_id=msg["chat"]["id"], text=reply)


# ---------------------------------------------------------------- 메인 루프

def group_items(updates):
    """업데이트를 처리 단위로 묶는다. 앨범(같은 media_group_id)은 하나의 공지로 묶는다.
    결과: [(마지막 update_id, 종류, 내용)]"""
    items, by_group = [], {}
    for u in updates:
        uid = u["update_id"]
        if "callback_query" in u:
            items.append((uid, "callback", u["callback_query"]))
        elif "message" in u:
            items.append((uid, "dm", u["message"]))
        elif "channel_post" in u and is_target(u["channel_post"]["chat"]):
            msg = u["channel_post"]
            gid = msg.get("media_group_id")
            if gid and gid in by_group:
                idx = by_group[gid]
                items[idx] = (uid, "post", items[idx][2] + [msg])
                continue
            if gid:
                by_group[gid] = len(items)
            items.append((uid, "post", [msg]))
        else:
            if "channel_post" in u:
                c = u["channel_post"]["chat"]
                items.append((uid, "other_channel", c))
                continue
            items.append((uid, "skip", None))
    return items


def process(updates, auth):
    """처리를 마친 마지막 update_id와 실패 여부를 돌려준다."""
    items = group_items(updates)
    # 마지막 공지가 아직 올라가는 중인 앨범일 수 있으면 잠시 미룬다.
    if items and items[-1][1] == "post" and items[-1][2][-1].get("media_group_id"):
        if time.time() - items[-1][2][-1]["date"] < ALBUM_SETTLE_SECS:
            items.pop()

    done_id, failed, answered = None, False, set()
    for uid, kind, payload in items:
        if kind == "post" and has_content(payload):
            if not TG_ADMIN_ID:
                print(f"[새 공지 #{payload[0]['message_id']}] TELEGRAM_ADMIN_ID가 없어 건너뜀")
            else:
                try:
                    ask_approval(payload)
                except Exception as e:
                    print(f"  ✗ 승인 요청 실패: {e}\n"
                          "    관리자가 봇에게 /start를 보냈는지, TELEGRAM_ADMIN_ID가 맞는지 확인하세요.")
                    return done_id, True
        elif kind == "callback":
            key = (payload.get("message") or {}).get("message_id")
            if key in answered:  # 같은 버튼을 연달아 누른 경우
                tg_quiet("answerCallbackQuery", callback_query_id=payload["id"])
            else:
                answered.add(key)
                if not handle_callback(payload, auth):
                    failed = True
        elif kind == "dm":
            handle_dm(payload)
        elif kind == "other_channel":
            note = (f"설정({TG_CHAT_ID})과 다른 채널의 글이라 무시했습니다: "
                    f"@{payload.get('username')} / {payload['id']}")
            print(f"[무시] {note}")
            if TG_ADMIN_ID and not DRY_RUN:
                tg_quiet("sendMessage", chat_id=TG_ADMIN_ID,
                         text=f"⚠️ {note}\n이 채널이 맞다면 TELEGRAM_CHAT_ID를 고쳐 주세요.")
        done_id = uid
    return done_id, failed


def main():
    auth = None if DRY_RUN else x_auth()
    deadline = time.time() + RUN_SECS
    offset, failed, first = None, False, True
    while True:
        wait = max(0, min(50, int(deadline - time.time())))
        if wait == 0 and not first:
            break
        first = False
        params = dict(timeout=wait, limit=100,
                      allowed_updates=["channel_post", "message", "callback_query"])
        if offset:
            params["offset"] = offset  # 이전 업데이트는 처리 완료로 확인된다
        try:
            updates = tg("getUpdates", **params)
        except RuntimeError as e:
            if "webhook" in str(e).lower():
                sys.exit("이 봇에 webhook이 설정되어 있습니다. deleteWebhook을 먼저 호출하세요.")
            raise
        done_id, err = process(updates, auth) if updates else (None, False)
        if done_id is not None:
            offset = done_id + 1
        if err:
            failed = True
            if done_id is None or done_id != updates[-1]["update_id"]:
                break  # 승인 요청을 못 보낸 공지는 다음 실행 때 다시 시도
        if time.time() >= deadline:
            break
        if updates and done_id != updates[-1]["update_id"]:
            time.sleep(3)  # 앨범을 기다리는 중

    if DRY_RUN:
        print("(DRY_RUN: 아무것도 보내지 않았고, 텔레그램에도 처리 완료로 표시하지 않았습니다)")
    elif offset:
        tg("getUpdates", offset=offset, limit=1, timeout=0)  # 마지막 처리분까지 확인
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
