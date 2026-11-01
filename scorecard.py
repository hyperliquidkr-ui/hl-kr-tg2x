"""매일 밤 '오늘의 성적표' 초안을 만들어 관리자 DM으로 보낸다.

hypurrscan 대시보드를 브라우저로 열어 '24h Fees' 카드의 수치를 읽고 그 카드만 캡처한다.
초안 형식:
    $0.62M/24h

    오늘의 성적표

    https://hypurrscan.io/dashboard
미리보기에는 트렌드 리포트와 같은 [공지방에 게시]/[건너뛰기] 버튼이 붙는다 (처리는 bot.py).
"""

import json
import os
import re
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

import trends

DASHBOARD = "https://hypurrscan.io/dashboard"
PAGE_URL = os.environ.get("SCORECARD_PAGE_URL") or DASHBOARD  # 테스트할 때만 바꾼다
OUT = Path(os.environ.get("SCORECARD_OUT") or "scorecard.png")
DEBUG_DIR = os.environ.get("SCORECARD_DEBUG_DIR")  # 지정하면 전체 화면·카드 글자도 저장 (캡처 위치 점검용)


def capture():
    """(24h Fees 달러 금액, 카드 캡처 파일 경로)를 돌려준다."""
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH"))  # 기본: playwright가 설치한 브라우저
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=2,
                                color_scheme="dark", locale="ko-KR", timezone_id="Asia/Seoul")
        page.goto(PAGE_URL, wait_until="networkidle", timeout=90_000)
        label = page.get_by_text("24h Fees", exact=True).first
        label.wait_for(timeout=60_000)
        page.wait_for_timeout(4000)  # 차트 애니메이션이 끝날 때까지

        # '24h Fees' 글자에서 위로 올라가며, 금액과 차트를 함께 담은 카드 영역을 찾는다
        card = label
        for _ in range(8):
            parent = card.locator("xpath=..")
            box = parent.bounding_box()
            card = parent
            if box and box["width"] >= 350 and box["height"] >= 180 and re.search(r"\d[\d,]{3,}\s*\$", parent.inner_text()):
                break
        text = card.inner_text()
        if DEBUG_DIR:
            Path(DEBUG_DIR).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=f"{DEBUG_DIR}/full.png", full_page=True)
            Path(f"{DEBUG_DIR}/card.txt").write_text(f"{card.bounding_box()}\n\n{text}", encoding="utf-8")
        card.screenshot(path=str(OUT))
        browser.close()

    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*\$", text) or re.search(r"\$\s*(\d[\d,]*(?:\.\d+)?)", text)
    if not m:
        raise RuntimeError(f"24h Fees 금액을 찾지 못했습니다: {text[:200]!r}")
    return float(m.group(1).replace(",", "")), OUT


def send_preview(amount, image):
    draft = f"${amount / 1e6:.2f}M/24h\n\n오늘의 성적표\n\n{DASHBOARD}"
    text, entities = trends.compose(draft, trends.load_footer())
    if trends.DRY_RUN:
        print(text)
        return
    with open(image, "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{trends.TG_TOKEN}/sendPhoto", data={
            "chat_id": trends.TG_ADMIN_ID, "caption": text, "caption_entities": json.dumps(entities),
        }, files={"photo": ("scorecard.png", f, "image/png")}, timeout=60)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto 실패: {data.get('description')}")
    mid = data["result"]["message_id"]
    trends.tg("editMessageReplyMarkup", chat_id=trends.TG_ADMIN_ID, message_id=mid,
              reply_markup=trends.publish_buttons([mid]))


def main():
    try:
        amount, image = capture()
        print(f"24h Fees: ${amount:,.0f}")
        send_preview(amount, image)
    except Exception as e:
        trends.tg_send(f"⚠️ 오늘의 성적표 만들기 실패\n\n{str(e)[:800]}")
        raise


if __name__ == "__main__":
    sys.exit(main())
