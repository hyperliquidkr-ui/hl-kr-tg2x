# Hyperliquid KR 텔레그램 → X 자동 미러

텔레그램 공지방에 올라온 글을 GitHub Actions가 **5분마다** 확인해서 KR X(트위터) 계정에 그대로 올립니다.

- 글 / 사진(앨범 최대 4장) / 숨은 링크(“여기를 클릭”)를 지원합니다
- 길면 자동으로 스레드로 나눕니다 (X 기준: 한글 1자 = 2, 링크 1개 = 23 → 일반 계정은 한글 약 140자)
- 동영상·파일·투표는 빼고 글만 올립니다
- X 게시에 실패하면 그 글부터 다음 실행 때 다시 시도합니다 (텔레그램은 24시간 보관)

---

## 1. 텔레그램 봇 만들기

1. 텔레그램에서 **@BotFather** → `/newbot` → 이름과 아이디를 입력하면 **봇 토큰**(`123456:ABC...`)이 나옵니다.
2. 공지방에 봇을 추가합니다.
   - **채널**이면: 채널 설정 → 관리자 → 봇 추가 (권한은 기본값이면 충분)
   - **그룹**이면: 봇을 관리자로 추가 (관리자가 아니면 일반 메시지를 못 받습니다)
3. 공지방에 테스트 글을 하나 올린 뒤, 브라우저에서 아래 주소를 엽니다 (`<토큰>`을 바꿔서):
   `https://api.telegram.org/bot<토큰>/getUpdates`
   결과의 `"chat":{"id":-100xxxxxxxxxx` 숫자가 **채팅 ID**입니다. (`-100`까지 포함)

## 2. X API 키 받기

1. KR 계정으로 로그인해서 <https://developer.x.com> 가입 → 프로젝트/앱을 만듭니다.
2. 앱 설정 → **User authentication settings** → App permissions를 **Read and write**로 바꿉니다.
   (콜백 URL은 아무 주소나 넣어도 됩니다. 예: `https://example.com`)
3. **Keys and tokens** 탭에서 아래 4개를 발급합니다.
   - API Key, API Key Secret
   - Access Token, Access Token Secret (**권한을 Read and write로 바꾼 뒤에** 발급/재발급해야 함)
4. 무료 등급은 월 게시 수 제한이 있으니 개발자 포털에서 한도를 확인해 주세요.

## 3. GitHub에 올리기

1. GitHub에서 새 저장소를 만듭니다. **Public을 추천**합니다.
   - Public 저장소는 Actions가 무료로 무제한입니다. 키는 Secrets에 들어가므로 공개되지 않습니다.
   - Private는 무료 한도가 월 2,000분이라 5분마다 돌리면 한도를 넘습니다.
2. 이 폴더의 파일을 그대로 올립니다 (`bot.py`, `requirements.txt`, `.github/workflows/mirror.yml`).
3. 저장소 → **Settings → Secrets and variables → Actions → New repository secret** 에 6개를 넣습니다.

   | 이름 | 값 |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | 1단계 봇 토큰 |
   | `TELEGRAM_CHAT_ID` | 1단계 채팅 ID (예: `-1001234567890`) |
   | `X_API_KEY` | API Key |
   | `X_API_SECRET` | API Key Secret |
   | `X_ACCESS_TOKEN` | Access Token |
   | `X_ACCESS_TOKEN_SECRET` | Access Token Secret |

4. (선택) KR 계정이 **X Premium**이면 같은 화면의 **Variables** 탭에 `X_MAX_WEIGHT` = `25000`을 넣습니다. 그러면 긴 글을 스레드로 나누지 않고 한 트윗으로 올립니다.

## 4. 테스트 → 실제 운영

1. 저장소 → **Actions** 탭 → “Telegram → X mirror” → **Run workflow** (테스트 모드 체크된 상태로 실행).
   로그에 올라갈 트윗 내용이 나오는지 확인합니다. 이때는 X에 올리지 않습니다.
2. 문제가 없으면 그대로 두세요. 5분마다 자동으로 실제 게시가 시작됩니다.

> ⚠️ 1단계의 테스트 글도 처음 실행 때 X에 올라갑니다. 원치 않으면 첫 실제 실행 전에 텔레그램에서 지우거나,
> 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates?offset=-1` 을 한 번 열어 이전 글을 비워 주세요.
> (마지막 1개는 남으니 그 뒤에 한 번 더 열면 됩니다.)

## 알아두면 좋은 점

- GitHub 예약 실행은 바쁠 때 5~15분 정도 늦어질 수 있습니다.
- Public 저장소에 **60일 동안 커밋이 없으면** GitHub가 예약 실행을 멈추고 메일을 보냅니다. 메일의 버튼이나 Actions 탭에서 다시 켜면 됩니다.
- 게시에 실패하면 GitHub가 실패 알림 메일을 보내고, 로그에 X의 에러 메시지가 남습니다.
- 텔레그램에서 **수정한 글**은 다시 올리지 않습니다.
- 이 봇 토큰은 이 용도로만 쓰세요. 같은 토큰을 다른 프로그램(webhook)에서 쓰면 동작하지 않습니다.
