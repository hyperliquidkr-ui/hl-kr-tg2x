# Hyperliquid KR 텔레그램 → X 미러 (DM 승인 후 게시)

텔레그램 공지 채널에 새 글이 올라오면, 봇이 관리자 텔레그램 DM으로 원문과
**[✅ X에 게시] [❌ 건너뛰기]** 버튼을 보냅니다. **게시**를 누른 글만 KR X(트위터) 계정에 올라갑니다.

- GitHub Actions가 5분마다 실행되고, 실행될 때마다 약 4분간 대기하며 바로 반응합니다
  (GitHub 예약 실행이 늦어지면 반응도 몇 분 늦어질 수 있습니다)
- 글 / 사진(앨범 최대 4장) / 숨은 링크(“여기를 클릭”)를 지원합니다
- Premium 계정 기준으로 긴 글도 한 트윗으로 올립니다
- 동영상·파일·투표는 빼고 글만 올립니다
- X 게시에 실패하면 DM에 이유가 표시되고 버튼이 남아 있어서, 다시 누르면 재시도합니다
- 승인 버튼은 `TELEGRAM_ADMIN_ID`로 등록한 사람만 누를 수 있습니다

---

## 1. 텔레그램 봇

1. **@BotFather** → `/newbot` → 이름과 아이디를 입력하면 **봇 토큰**(`123456:ABC...`)이 나옵니다.
2. 공지 채널 → 관리자 → 봇을 관리자로 추가합니다 (권한은 기본값이면 충분).
3. 관리자 본인이 텔레그램에서 봇을 검색해 **시작(/start)** 을 누릅니다.
   봇은 먼저 말을 걸 수 없어서 이 단계가 꼭 필요합니다.
   텔레그램 토큰이 GitHub에 등록되어 있으면, 봇이 몇 분 안에 **관리자 ID(숫자)** 를 답장으로 알려줍니다.

## 2. X API 키

1. KR 계정으로 로그인해서 <https://developer.x.com> 가입 → 앱을 만듭니다.
2. 앱 설정 → **User authentication settings** → App permissions를 **Read and write**로 바꿉니다.
3. **Keys and tokens** 탭에서 API Key / API Key Secret / Access Token / Access Token Secret을 발급합니다.
   (Access Token은 **권한을 Read and write로 바꾼 뒤에** 발급해야 합니다)

## 3. GitHub Secrets

저장소 → **Settings → Secrets and variables → Actions → New repository secret** 에 7개를 넣습니다.

| 이름 | 값 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 봇 토큰 |
| `TELEGRAM_CHAT_ID` | `@Hyperliquid_KR_announce` |
| `TELEGRAM_ADMIN_ID` | 봇이 /start 답장으로 알려준 숫자 |
| `X_API_KEY` | API Key |
| `X_API_SECRET` | API Key Secret |
| `X_ACCESS_TOKEN` | Access Token |
| `X_ACCESS_TOKEN_SECRET` | Access Token Secret |

`TELEGRAM_ADMIN_ID`를 넣기 전에 올라온 공지는 승인 요청 없이 건너뜁니다 (X에도 올리지 않음).

Premium을 해지하면 같은 화면의 **Variables** 탭에 `X_MAX_WEIGHT` = `280`을 넣으세요. 긴 글을 스레드로 나눕니다.

## 4. 테스트

Actions 탭 → “Telegram → X mirror” → **Run workflow** (테스트 모드 체크).
로그만 남기고 아무것도 보내지 않습니다. 이후에는 5분마다 자동으로 실제 운영됩니다.

## 알아두면 좋은 점

- 승인 요청을 **24시간 안에** 눌러야 합니다 (텔레그램 보관 기한).
- Public 저장소에 **60일 동안 커밋이 없으면** GitHub가 예약 실행을 멈추고 메일을 보냅니다. Actions 탭에서 다시 켜면 됩니다.
- 텔레그램에서 **수정한 글**은 다시 승인 요청하지 않습니다. 단, 승인 버튼을 누를 때 최신 내용으로 올라갑니다.
- 이 봇 토큰은 이 용도로만 쓰세요. 다른 프로그램(webhook)에서 같이 쓰면 동작하지 않습니다.
